"""Main LSP server for java-functional-lsp.

Provides custom Java diagnostics via tree-sitter analysis.
Proxies to jdtls for full Java language features (completions, hover, go-to-def).
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Coroutine
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from typing import BinaryIO

from lsprotocol import types as lsp
from lsprotocol.converters import get_converter
from pygls.lsp.server import LanguageServer
from pygls.uris import from_fs_path, to_fs_path

from .analyzers import KNOWN_RULES
from .analyzers.base import Analyzer, Severity, get_parser, is_excluded, is_suppressed
from .analyzers.base import Diagnostic as LintDiagnostic
from .analyzers.exception_checker import ExceptionChecker
from .analyzers.functional_checker import FunctionalChecker
from .analyzers.mutation_checker import MutationChecker
from .analyzers.null_checker import NullChecker
from .analyzers.spring_checker import SpringChecker
from .capabilities import (
    REGISTRY,
    CapabilityEntry,
    CapabilityNegotiator,
    ClientCapabilityProbe,
    HandlerWiring,
    StaticCapabilityBuilder,
)
from .fixes import get_fix, get_fix_registry_keys
from .proxy import JdtlsProxy, _module_snapshot_path, _resolve_module_uri

logger = logging.getLogger(__name__)

_SEVERITY_MAP = {
    Severity.ERROR: lsp.DiagnosticSeverity.Error,
    Severity.WARNING: lsp.DiagnosticSeverity.Warning,
    Severity.INFO: lsp.DiagnosticSeverity.Information,
    Severity.HINT: lsp.DiagnosticSeverity.Hint,
}

_ANALYZERS: list[Analyzer] = [
    NullChecker(),
    ExceptionChecker(),
    MutationChecker(),
    SpringChecker(),
    FunctionalChecker(),
]

# JetBrains IDE detection — all products contain one of these substrings in clientInfo.name.
# When detected, jdtls is skipped because IntelliJ provides native Java language support.
_JDTLS_SKIP_CLIENTS = ("IntelliJ", "JetBrains")

#: LSP-aware cattrs converter. Unstructures to the LSP JSON shape
#: (camelCase field names, discriminated unions, None-field pruning) and
#: correspondingly structures from the same shape. Using a vanilla
#: ``cattrs.Converter()`` here emits snake_case field names (``text_document``
#: instead of ``textDocument``), which breaks request forwarding to jdtls —
#: jdtls then sees a null ``TextDocumentIdentifier`` and throws NPEs during
#: go-to-definition, references, etc.
_converter = get_converter()

# Cap on _session_opened_uris: a session navigating many files still bounds memory.
# 4096 comfortably covers even aggressive monorepo navigation.
_MAX_OPENED_URIS = 4096


def _normalize_uri(uri: str) -> str:
    """Canonicalize a file:// URI so equivalent paths compare equal.

    Round-trips through pygls.uris to normalize URL encoding differences
    (``%20`` vs literal space, ``%3A`` vs ``:``, trailing slashes, double
    slashes) between what the client sends and what jdtls echoes back.

    Non-file URIs (``jdt://``, ``untitled://``, etc.) are returned unchanged —
    they're never stored in ``_session_opened_uris`` so they'll fall through the gate
    regardless. Case-insensitive filesystems (macOS HFS+, NTFS) are not
    normalized here: attempting that would require ``Path.resolve()``, which
    would stat the filesystem and follow symlinks — too expensive for a hot
    path that runs on every jdtls publish. On Windows, drive-letter case
    (``C:`` vs ``c:``) is similarly not normalized.
    """
    if not uri.startswith("file:"):
        return uri
    fs = to_fs_path(uri)
    if fs is None:
        return uri
    normalized = from_fs_path(fs)
    return normalized if normalized is not None else uri


# Publish modes while an edited file waits for jdtls (#109). "hold-all" publishes
# nothing until jdtls answers; "custom-first" publishes custom diagnostics at the
# debounce and the full merge once jdtls answers — for agent hosts that attach
# diagnostics right after the edit and would otherwise miss the custom ones.
_HOLD_OFF = "off"
_HOLD_ALL = "hold-all"
_HOLD_CUSTOM_FIRST = "custom-first"
_HOLD_MODES = (_HOLD_OFF, _HOLD_ALL, _HOLD_CUSTOM_FIRST)
_CUSTOM_FIRST_CLIENTS = ("Claude Code",)
_CUSTOM_SOURCE = "java-functional-lsp"


class _JdtlsFreshness:
    """Decides when cached jdtls diagnostics are fresh enough to publish after an edit.

    jdtls validates on one debounced job shared by all files — any didChange resets it,
    and it publishes 0.4-2.4s later — and its publishes carry no document version. So
    freshness is inferred from time since the last change forwarded to jdtls. Pure
    state with an injectable clock; the server owns the tasks.
    """

    FRESH_MIN_AGE = 0.4  # jdtls minimum publish debounce
    DEADLINE_AFTER_CHANGE = 3.0
    CEILING = 10.0
    LATE_CORRECTION_WINDOW = 2.0
    EWMA_ALPHA = 0.3
    SUMMARY_EVERY = 100

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._pending: dict[str, float] = {}
        self._released: dict[str, tuple[float, str]] = {}
        self._last_change: float | None = None
        self._latency_ewma = 0.0
        self._latency_max = 0.0
        self.counts: Counter[str] = Counter()

    def note_forwarded_change(self) -> None:
        self._last_change = self.clock()

    def mark_pending(self, key: str) -> None:
        self._pending.setdefault(key, self.clock())
        self._released.pop(key, None)

    def is_pending(self, key: str) -> bool:
        return key in self._pending

    def deadline(self, key: str) -> float | None:
        since = self._pending.get(key)
        if since is None:
            return None
        last = self._last_change if self._last_change is not None else since
        after_change = max(self.DEADLINE_AFTER_CHANGE, 2 * self._latency_ewma)
        return min(last + after_change, since + self.CEILING)

    def on_publish(self, key: str, signature: str) -> str:
        """Classify a jdtls publish as released, too_early, late_correction or untracked."""
        now = self.clock()
        if key in self._pending:
            age = now - self._last_change if self._last_change is not None else self.FRESH_MIN_AGE
            # Fixed floor, not adaptive: an adaptive floor would reject normal-latency
            # publishes after one slow one and could never come back down.
            if age < self.FRESH_MIN_AGE:
                return self.record("too_early")
            del self._pending[key]
            self._released[key] = (now, signature)
            self._latency_max = max(self._latency_max, age)
            self._latency_ewma = (
                age if not self._latency_ewma else self._latency_ewma + self.EWMA_ALPHA * (age - self._latency_ewma)
            )
            return self.record("released")
        released = self._released.pop(key, None)
        if released is not None:
            at, released_signature = released
            if now - at <= self.LATE_CORRECTION_WINDOW:
                if released_signature != signature:
                    return self.record("late_correction")
                self._released[key] = released
        return "untracked"

    def expire(self, key: str) -> bool:
        """Drop *key* once its deadline has passed. Returns True when it timed out."""
        deadline = self.deadline(key)
        if deadline is None or self.clock() < deadline:
            return False
        del self._pending[key]
        self.record("timeout")
        return True

    def forget(self, key: str) -> None:
        self._pending.pop(key, None)
        self._released.pop(key, None)

    def reset(self) -> int:
        """Clear all tracking; returns how many files were still pending."""
        pending = len(self._pending)
        self._pending.clear()
        self._released.clear()
        self._last_change = None
        return pending

    def record(self, outcome: str) -> str:
        self.counts[outcome] += 1
        if sum(self.counts.values()) % self.SUMMARY_EVERY == 0:
            logger.info("%s", self.summary())
        return outcome

    def summary(self) -> str:
        counts = " ".join(f"{name}={n}" for name, n in sorted(self.counts.items())) or "no-holds"
        return (
            f"jdtls freshness: {counts} latency_ewma_ms={self._latency_ewma * 1000:.0f} "
            f"latency_max_ms={self._latency_max * 1000:.0f}"
        )


def _diagnostics_signature(diagnostics: list[Any]) -> str:
    return hashlib.sha256(json.dumps(diagnostics, sort_keys=True, default=str).encode()).hexdigest()


class JavaFunctionalLspServer(LanguageServer):
    def __init__(self) -> None:
        from . import __version__

        super().__init__("java-functional-lsp", __version__)
        self._parser = get_parser()
        self._config: dict[str, Any] = {}
        self._init_params: dict[str, Any] = {}
        self._proxy = JdtlsProxy(
            on_diagnostics=self._on_jdtls_diagnostics,
            uri_key=_normalize_uri,
            on_stopped=self._on_jdtls_stopped,
        )
        self._user_suppress_patterns: list[re.Pattern[str]] = []
        self._skip_jdtls: bool = False
        self._hold_mode: str = _HOLD_OFF
        self._skip_jdtls_registration: bool = False
        self._init_generation: int = 0
        # Capability entries the negotiator decided to register dynamically
        # (client claimed dynamicRegistration support). Populated in
        # on_initialize and consumed by _register_jdtls_capabilities.
        self._dynamic_features: tuple[CapabilityEntry, ...] = ()
        # URIs the client has opened at least once this session (normalized form).
        # Jdtls indexes the whole workspace and publishes diagnostics for files the
        # client never touched; we only republish for URIs present here so unrelated
        # modules don't flood the client. Cumulative (not cleared on didClose) to
        # survive the async race between didClose and late jdtls publishes.
        # Bounded LRU: insertion-ordered, oldest evicted on overflow so a long
        # session navigating many files can't grow the set unboundedly.
        # Reset on initialize. Values are the URI string exactly as the client
        # sent it, so publishes triggered by jdtls go out under the client's form.
        self._session_opened_uris: OrderedDict[str, str] = OrderedDict()

    def _record_opened(self, uri: str) -> None:
        """Record *uri* as opened this session, evicting the oldest entry at cap.

        Keys are normalized so gate lookups in ``_on_jdtls_diagnostics`` can use a
        fast raw-first check without redundant normalization.
        """
        client_uri = uri
        uri = _normalize_uri(uri)
        if uri in self._session_opened_uris:
            self._session_opened_uris[uri] = client_uri
            self._session_opened_uris.move_to_end(uri)
            return
        self._session_opened_uris[uri] = client_uri
        # Size check uses > so the dict stabilises at exactly _MAX_OPENED_URIS
        # entries after the pop: we insert first (reaching MAX+1 momentarily),
        # then evict. The transient overshoot is invisible to other callers
        # because asyncio is single-threaded and no await intervenes here.
        if len(self._session_opened_uris) > _MAX_OPENED_URIS:
            self._session_opened_uris.popitem(last=False)

    def _on_jdtls_diagnostics(self, uri: str, diagnostics: list[Any]) -> None:
        """Called when jdtls publishes diagnostics — merge with custom and re-publish.

        Also marks the file's module as READY, since receiving diagnostics from
        jdtls is a reliable signal that the module has been indexed (more reliable
        than a first non-None response which may be semantically empty).

        Exception: if any diagnostic carries ``_JDTLS_NON_PROJECT_CODE`` (code 16),
        jdtls has opened the file but hasn't resolved its Maven project yet — only
        syntax errors are available.  We skip the READY transition and wait for the
        follow-up publish (after Maven import completes) that arrives without code 16.

        When a module transitions to READY, fires ``_apply_module_diff`` to
        notify jdtls about any externally changed files (e.g., after git pull).
        """
        if not uri.endswith(".java"):
            return
        module_uri = _resolve_module_uri(uri)
        if module_uri:
            is_non_project = any(
                str(d.get("code", "")) == _JDTLS_NON_PROJECT_CODE for d in diagnostics if isinstance(d, dict)
            )
            if is_non_project:
                logger.info("jdtls: skipping READY for %s (non-project file, Maven import pending)", Path(uri).name)
            else:
                already_ready = self._proxy.modules.is_ready(module_uri)
                self._proxy.modules.mark_ready(module_uri)
                if not already_ready:
                    # First READY transition only — subsequent diagnostics for the same
                    # module are no-ops to avoid spawning O(files) redundant tasks.
                    _fire_and_forget(_apply_module_diff(self._proxy, module_uri))
        # Only republish for files the client has opened at any point this
        # session (cumulative). Jdtls scans the whole workspace; without this
        # guard, diagnostics for 200+ unrelated modules leak out as
        # <new-diagnostics> tags (see #71). Non-file URIs (jdt://, untitled://,
        # etc.) are never recorded by _record_opened so they're dropped here.
        #
        # Two-phase lookup: check the raw URI first (O(1), no allocation) since
        # jdtls typically echoes URIs in the same form as the client — the fast
        # path avoids the to_fs_path/from_fs_path round-trip on every publish.
        # Fall back to the normalized form only on a miss to handle encoding
        # mismatches (e.g., client sends literal space, jdtls echoes %20).
        if uri not in self._session_opened_uris and _normalize_uri(uri) not in self._session_opened_uris:
            return
        key = _normalize_uri(uri)
        outcome = _freshness.on_publish(key, _diagnostics_signature(diagnostics))
        if outcome == "too_early":
            logger.debug("jdtls publish for %s arrived too soon after the last edit; still holding", Path(uri).name)
            return
        if outcome == "released":
            # The hold task (waiting, or still in its debounce sleep) publishes the merge.
            event = _hold_events.get(key)
            if event is not None:
                event.set()
            return
        if outcome == "late_correction":
            logger.debug("jdtls corrected %s shortly after its hold was released", Path(uri).name)
        try:
            _analyze_and_publish(uri, trigger="jdtls")
        except FileNotFoundError:
            # The file was in the opened-URIs set but vanished from disk
            # between didOpen and this late jdtls publish. Benign.
            pass
        except Exception as e:
            logger.error("Error re-publishing diagnostics for %s: %s", Path(uri).name, e)

    def _on_jdtls_stopped(self) -> None:
        """Release every held file: no fresh jdtls publish can arrive from a dead process."""
        released = _release_all_holds()
        if released:
            logger.info("jdtls stopped: released %d files waiting for diagnostics", released)
        logger.info("%s", _freshness.summary())


server = JavaFunctionalLspServer()

# Debounce state for didChange events (only affects human typing in IDEs, not agents)
_pending: dict[str, asyncio.Task[None]] = {}
# Background tasks (prevent GC of fire-and-forget tasks)
_bg_tasks: set[asyncio.Task[None]] = set()
_DEBOUNCE_SECONDS = 0.15
_freshness = _JdtlsFreshness()
# Wakes the hold task of a URI (normalized) when its fresh jdtls publish arrives.
_hold_events: dict[str, asyncio.Event] = {}


def _fire_and_forget(coro: Coroutine[Any, Any, Any]) -> None:
    """Schedule *coro* as a background task, preventing GC until it finishes."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _apply_module_diff(proxy: JdtlsProxy, module_uri: str) -> None:
    """Send ``workspace/didChangeWatchedFiles`` for externally changed files.

    Called (fire-and-forget) when a module reaches READY state.  Pops the
    ``(diff, snapshot)`` pair computed by ``_kick_module_diff`` during module
    registration, notifies jdtls about changed files so it can do an
    incremental rebuild, then persists the updated snapshot to disk.

    No-ops if the diff computation is not yet finished or found no changes.
    """
    data = proxy.pop_module_data(module_uri)
    if data is None:
        # Race guard: READY may fire before _kick_module_diff finishes.
        # Await the in-flight task directly (no arbitrary sleep).
        await proxy.await_module_diff(module_uri)
        data = proxy.pop_module_data(module_uri)
    if data is None:
        return
    diff, current_snapshot = data

    if diff is not None and not diff.is_empty:
        from pygls.uris import from_fs_path, to_fs_path

        module_fs = to_fs_path(module_uri) or ""
        if module_fs:
            module_path = Path(module_fs)
            changes = (
                [
                    {"uri": u, "type": lsp.FileChangeType.Created}
                    for rel in diff.added
                    if (u := from_fs_path(str(module_path / rel)))
                ]
                + [
                    {"uri": u, "type": lsp.FileChangeType.Changed}
                    for rel in diff.modified
                    if (u := from_fs_path(str(module_path / rel)))
                ]
                + [
                    {"uri": u, "type": lsp.FileChangeType.Deleted}
                    for rel in diff.removed
                    if (u := from_fs_path(str(module_path / rel)))
                ]
            )
            if changes:
                await proxy.send_notification("workspace/didChangeWatchedFiles", {"changes": changes})
                logger.info(
                    "merkle: notified jdtls of %d externally changed file(s) in module",
                    len(changes),
                )

    # Always persist the current snapshot so next session has an up-to-date baseline.
    snapshot_path = _module_snapshot_path(module_uri)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, current_snapshot.save, snapshot_path)
    except OSError as exc:
        logger.warning("merkle: could not save updated snapshot: %s", exc)


def _handle_exception(exc_type: type[BaseException], exc_value: BaseException, exc_tb: Any) -> None:
    """Log uncaught exceptions for crash debugging."""
    logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))


sys.excepthook = _handle_exception


def _load_config(workspace_root: str | None) -> dict[str, Any]:
    """Load .java-functional-lsp.json from workspace root if it exists."""
    if not workspace_root:
        return {}
    config_path = Path(workspace_root) / ".java-functional-lsp.json"
    if config_path.exists():
        try:
            result: dict[str, Any] = json.loads(config_path.read_text())
            return result
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load config from %s: %s", config_path, e)
    return {}


def _to_lsp_diagnostic(diag: LintDiagnostic) -> lsp.Diagnostic:
    """Convert an internal diagnostic to an LSP diagnostic."""
    # dict[str, Any] mirrors the LSP `Diagnostic.data` shape so future non-string fields
    # (numbers, bools, nested objects) can be added without re-typing the function.
    data: dict[str, Any] | None = None
    if diag.data is not None:
        data = {
            "fixType": diag.data.fix_type,
            "targetLibrary": diag.data.target_library,
            "rationale": diag.data.rationale,
        }
        if diag.data.recommended_api is not None:
            data["recommendedApi"] = diag.data.recommended_api
        if diag.data.suggested_snippet is not None:
            data["suggestedSnippet"] = diag.data.suggested_snippet
    return lsp.Diagnostic(
        range=lsp.Range(
            start=lsp.Position(line=diag.line, character=diag.col),
            end=lsp.Position(line=diag.end_line, character=diag.end_col),
        ),
        severity=_SEVERITY_MAP.get(diag.severity, lsp.DiagnosticSeverity.Warning),
        code=diag.code,
        source=diag.source,
        message=diag.message,
        data=data,
    )


def _analyze_document(source_text: str, uri: str = "") -> list[lsp.Diagnostic]:
    """Run all custom analyzers on the given source text."""
    # Check excludes before parsing
    if uri:
        excludes: list[str] = server._config.get("excludes", [])
        if excludes:
            path_str = to_fs_path(uri) or uri
            if is_excluded(path_str, excludes):
                return []
    source_bytes = source_text.encode("utf-8")
    # Always do a fresh parse — incremental parsing requires tree.edit() with
    # exact byte offsets, which we don't track under Full document sync.
    tree = server._parser.parse(source_bytes)
    config = server._config

    all_diagnostics: list[LintDiagnostic] = []
    for analyzer in _ANALYZERS:
        try:
            diags = analyzer.analyze(tree, source_bytes, config)
            all_diagnostics.extend(diags)
        except Exception as e:
            logger.error("Analyzer %s failed: %s", type(analyzer).__name__, e)

    # Filter out diagnostics suppressed by @SuppressWarnings
    if all_diagnostics:
        root = tree.root_node
        all_diagnostics = [d for d in all_diagnostics if not is_suppressed(root, d.line, d.col, d.code)]

    return [_to_lsp_diagnostic(d) for d in all_diagnostics]


def _jdtls_raw_to_lsp_diagnostics(raw_diagnostics: list[Any]) -> list[lsp.Diagnostic]:
    """Convert raw jdtls diagnostic dicts to lsp.Diagnostic objects."""
    result: list[lsp.Diagnostic] = []
    for raw in raw_diagnostics:
        try:
            diag = _converter.structure(raw, lsp.Diagnostic)
            result.append(diag)
        except Exception:
            # If structuring fails, try manual conversion
            try:
                r = raw.get("range", {})
                start = r.get("start", {})
                end = r.get("end", {})
                result.append(
                    lsp.Diagnostic(
                        range=lsp.Range(
                            start=lsp.Position(line=start.get("line", 0), character=start.get("character", 0)),
                            end=lsp.Position(line=end.get("line", 0), character=end.get("character", 0)),
                        ),
                        severity=lsp.DiagnosticSeverity(raw.get("severity", 1)),
                        code=raw.get("code"),
                        source=raw.get("source", "jdtls"),
                        message=raw.get("message", ""),
                    )
                )
            except Exception as e:
                logger.debug("Could not convert jdtls diagnostic: %s", e)
    return result


_MAX_PATTERN_LENGTH = 500  # Cap regex length to mitigate ReDoS from pathological patterns

_M2E_SOURCES: frozenset[str] = frozenset({"org.eclipse.m2e"})
_M2E_MESSAGE_PATTERNS: re.Pattern[str] = re.compile(
    r"Plugin execution not covered by lifecycle configuration"
    r"|The project cannot be built until its prerequisite"
    r"|Failed to execute mojo"
)


def _is_m2e_marker(diag: dict[str, Any]) -> bool:
    """Return True if the diagnostic is an Eclipse/M2E lifecycle marker (not a Java error)."""
    if str(diag.get("code", "")) == "0":
        return True
    if diag.get("source", "") in _M2E_SOURCES:
        return True
    if _M2E_MESSAGE_PATTERNS.search(diag.get("message", "")):
        return True
    return False


def _compile_user_patterns(config: dict[str, Any]) -> list[re.Pattern[str]]:
    """Compile user-defined suppressJdtlsPatterns from config."""
    raw = config.get("suppressJdtlsPatterns", [])
    if not isinstance(raw, list):
        logger.warning("suppressJdtlsPatterns must be a list of regex strings, got %s", type(raw).__name__)
        return []
    patterns: list[re.Pattern[str]] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        if len(entry) > _MAX_PATTERN_LENGTH:
            logger.warning("suppressJdtlsPatterns entry too long (%d chars, max %d)", len(entry), _MAX_PATTERN_LENGTH)
            continue
        try:
            patterns.append(re.compile(entry))
        except re.error as e:
            logger.warning("Invalid suppressJdtlsPatterns regex %r: %s", entry, e)
    return patterns


def _is_jdtls_suppressed(diag: dict[str, Any], user_patterns: list[re.Pattern[str]]) -> bool:
    """Check if a jdtls diagnostic matches a user-configured suppress pattern.

    No built-in patterns — the root cause (stale jdtls caches) is fixed by
    clearing the cache on version change. Users can still suppress specific
    jdtls messages via suppressJdtlsPatterns in .java-functional-lsp.json.
    """
    if not user_patterns:
        return False
    msg = diag.get("message", "")
    for pat in user_patterns:
        if pat.search(msg):
            return True
    return False


def _run_analysis(source: str, uri: str, *, include_jdtls: bool = True) -> list[lsp.Diagnostic]:
    """Run custom analyzers on source text and merge with jdtls diagnostics.

    jdtls processing is isolated: if it fails, custom diagnostics still publish.
    """
    custom_diags = _analyze_document(source, uri)

    jdtls_diags: list[lsp.Diagnostic] = []
    if include_jdtls and server._proxy.is_available:
        try:
            raw = server._proxy.get_cached_diagnostics(uri)
            raw = [
                d for d in raw if not _is_m2e_marker(d) and not _is_jdtls_suppressed(d, server._user_suppress_patterns)
            ]
            jdtls_diags = _jdtls_raw_to_lsp_diagnostics(raw)
        except Exception as e:
            logger.warning("jdtls diagnostic processing failed for %s: %s", Path(uri).name, e)

    return jdtls_diags + custom_diags


def _serialize_params(params: Any) -> Any:
    """Convert lsprotocol objects to JSON-serializable dicts for jdtls."""
    try:
        return _converter.unstructure(params)
    except Exception:
        return params


# --- Lifecycle handlers ---


@server.feature(lsp.INITIALIZE)
def on_initialize(params: lsp.InitializeParams) -> lsp.InitializeResult:
    """Handle LSP initialize — store params for jdtls proxy."""
    server._init_params = _serialize_params(params)

    root = None
    if params.root_uri:
        root = to_fs_path(params.root_uri)
    elif params.root_path:
        root = params.root_path

    server._config = _load_config(root)
    server._user_suppress_patterns = _compile_user_patterns(server._config)

    # Determine jdtls mode: env var takes priority, then auto-detect JetBrains IDEs.
    client_name = (params.client_info.name if params.client_info else "")[:200]
    logger.info("LSP client: %s", client_name or "(unknown)")

    # Reset flags so re-initialization (non-standard but defensive) starts clean.
    # Bump generation so in-flight _register_jdtls_capabilities from a prior
    # session detects the stale context and aborts.
    global _jdtls_capabilities_registered
    server._skip_jdtls = False
    server._skip_jdtls_registration = False
    server._init_generation += 1
    server._session_opened_uris.clear()
    _jdtls_capabilities_registered = False
    _release_all_holds()
    server._hold_mode = _resolve_hold_mode(client_name)
    logger.info("jdtls diagnostics hold mode: %s", server._hold_mode)

    jdtls_override = os.environ.get("JAVA_FUNCTIONAL_LSP_JDTLS", "").strip().lower()
    if jdtls_override == "off":
        server._skip_jdtls = True
        logger.info("jdtls proxy disabled via JAVA_FUNCTIONAL_LSP_JDTLS=off")
    elif jdtls_override == "on":
        logger.info("jdtls proxy force-enabled via JAVA_FUNCTIONAL_LSP_JDTLS=on")
    elif jdtls_override == "no-register":
        server._skip_jdtls_registration = True
        logger.info("jdtls dynamic registration disabled via JAVA_FUNCTIONAL_LSP_JDTLS=no-register")
    elif jdtls_override:
        logger.warning("Unknown JAVA_FUNCTIONAL_LSP_JDTLS value %r; expected off/on/no-register", jdtls_override)
    elif any(token in client_name for token in _JDTLS_SKIP_CLIENTS):
        server._skip_jdtls = True
        logger.info(
            "JetBrains IDE detected (%s) — skipping jdtls proxy "
            "(IDE provides native Java support; override with JAVA_FUNCTIONAL_LSP_JDTLS=on)",
            client_name,
        )

    # Per-feature capability negotiation. Clients that claim LSP dynamicRegistration
    # support get the dynamic path (preserves PR #44 behavior — IDE keeps showing
    # its own diagnostic tooltips while jdtls warms up). Clients that don't
    # (notably Claude Code 2.1.x, which ignores client/registerCapability for
    # routing) get static advertisement so their LSP routing picks up our handlers.
    probe = ClientCapabilityProbe(params)
    negotiation = CapabilityNegotiator(probe).negotiate()
    server._dynamic_features = negotiation.dynamic

    # Wire pygls handlers eagerly for static-advertised features so they dispatch
    # immediately. Dynamic features have their handlers wired later in
    # _register_jdtls_capabilities, after jdtls warm-up.
    HandlerWiring(server, _JDTLS_HANDLERS).wire_eager(negotiation.static)

    return lsp.InitializeResult(capabilities=StaticCapabilityBuilder().build(negotiation.static))


@server.feature(lsp.INITIALIZED)
async def on_initialized(_: lsp.InitializedParams) -> None:
    """Check jdtls availability; actual start deferred to first didOpen."""
    logger.info(
        "java-functional-lsp initialized (rules: %s)",
        list(server._config.get("rules", {}).keys()) or "all defaults",
    )
    if server._skip_jdtls:
        logger.info("jdtls proxy disabled — custom rules only")
        return
    if server._proxy.check_available():
        logger.info("jdtls found on PATH — will start lazily on first file open")
    else:
        logger.info("jdtls not on PATH — running with custom rules only")


_JDTLS_REG_PREFIX = "jdtls-"

# Maps LSP method → handler function. Populated below (see _JDTLS_HANDLERS.update
# at the bottom of this file). Read by HandlerWiring during on_initialize (for
# static-advertised features) and again by _register_jdtls_capabilities (for
# dynamic features). The split is decided by CapabilityNegotiator.
_JDTLS_HANDLERS: dict[str, Any] = {}

# Set after first successful registration to prevent FeatureAlreadyRegisteredError.
_jdtls_capabilities_registered = False


def _build_jdtls_registrations(entries: tuple[CapabilityEntry, ...] = REGISTRY) -> list[lsp.Registration]:
    """Build LSP Registration objects for the given capability entries.

    Used by _register_jdtls_capabilities to send client/registerCapability for
    dynamic-path features. Each Registration carries the LSP method id, a unique
    id (jdtls-<suffix>), and the unstructured registration options.
    """
    result = []
    for entry in entries:
        result.append(
            lsp.Registration(
                id=f"{_JDTLS_REG_PREFIX}{entry.id_suffix}",
                method=entry.lsp_method,
                register_options=_converter.unstructure(entry.registration_options_factory()),
            )
        )
    return result


async def _register_jdtls_capabilities() -> None:
    """Send client/registerCapability for the entries the negotiator marked dynamic.

    Static-advertised features were already wired up during on_initialize. This
    function only handles the dynamic subset — features whose clients claimed
    LSP dynamicRegistration support, where deferring advertisement until jdtls
    is warm preserves the IDE's own diagnostic tooltips (PR #44 invariant).

    Idempotent: safe to call multiple times. Uses a generation counter to
    detect stale registrations from a prior initialize cycle.
    """
    global _jdtls_capabilities_registered
    if _jdtls_capabilities_registered:
        return

    generation = server._init_generation
    entries = server._dynamic_features

    try:
        HandlerWiring(server, _JDTLS_HANDLERS).wire_lazy(entries)

        registrations = _build_jdtls_registrations(entries)
        if registrations:
            await server.client_register_capability_async(lsp.RegistrationParams(registrations=registrations))

        # Bail if a re-initialize happened while we were awaiting.
        if generation != server._init_generation:
            logger.info("Discarding stale jdtls capability registration (re-initialize detected)")
            return

        _jdtls_capabilities_registered = True
        if entries:
            suffixes = ", ".join(e.id_suffix for e in entries)
            logger.info("jdtls capabilities registered dynamically: %s", suffixes)
        else:
            logger.info("No dynamic jdtls capabilities to register (all advertised statically)")
    except Exception:
        logger.warning("Failed to dynamically register jdtls capabilities", exc_info=True)


# --- Document sync (forward to jdtls + run custom analyzers) ---


def _analyze_and_publish(uri: str, *, include_jdtls: bool = True, trigger: str = "open") -> None:
    """Read document source, run analysis, publish results under the client's URI form."""
    client_uri = server._session_opened_uris.get(_normalize_uri(uri)) or uri
    doc = server.workspace.get_text_document(client_uri)
    diagnostics = _run_analysis(doc.source, client_uri, include_jdtls=include_jdtls)
    if logger.isEnabledFor(logging.DEBUG):
        java = sum(1 for d in diagnostics if d.source != _CUSTOM_SOURCE)
        logger.debug(
            "publish %s trigger=%s java=%d custom=%d", Path(client_uri).name, trigger, java, len(diagnostics) - java
        )
    server.text_document_publish_diagnostics(lsp.PublishDiagnosticsParams(uri=client_uri, diagnostics=diagnostics))


def _resolve_hold_mode(client_name: str) -> str:
    override = os.environ.get("JAVA_FUNCTIONAL_LSP_DIAG_HOLD", "").strip().lower()
    if override in _HOLD_MODES:
        return override
    if override:
        logger.warning("Unknown JAVA_FUNCTIONAL_LSP_DIAG_HOLD value %r; expected %s", override, "/".join(_HOLD_MODES))
    return _HOLD_CUSTOM_FIRST if any(token in client_name for token in _CUSTOM_FIRST_CLIENTS) else _HOLD_ALL


def _matches_diagnostic_filter(uri: str) -> bool:
    """jdtls never publishes for files matched by ``java.diagnostic.filter``, so don't wait for it."""
    settings = (server._config.get("jdtls") or {}).get("settings") or {}
    patterns = ((settings.get("java") or {}).get("diagnostic") or {}).get("filter") or []
    if not patterns:
        return False
    path = to_fs_path(uri) or uri
    return any(isinstance(p, str) and fnmatch.fnmatch(path, p) for p in patterns)


def _hold_eligible(uri: str) -> bool:
    """Whether an edit to *uri* should wait for jdtls to publish fresh diagnostics."""
    module_uri = _resolve_module_uri(uri)
    # Until the module is imported jdtls publishes late or only syntax errors (code 16).
    if module_uri is None or not server._proxy.modules.is_ready(module_uri):
        return False
    return not _matches_diagnostic_filter(uri)


def _release_all_holds() -> int:
    """Stop tracking every held file and wake its hold task; returns how many were held."""
    released = _freshness.reset()
    for event in _hold_events.values():
        event.set()
    return released


def _is_current_task(uri: str) -> bool:
    # On Python 3.10/3.11 wait_for can swallow a cancel() racing with the event,
    # leaving a superseded hold task running next to its replacement.
    return _pending.get(uri) is asyncio.current_task()


async def _hold_until_fresh(uri: str, key: str) -> None:
    """Publish once jdtls has re-validated *uri* after the latest edit, or its deadline passes."""
    event = _hold_events.setdefault(key, asyncio.Event())
    trigger = "jdtls"
    try:
        if server._hold_mode == _HOLD_CUSTOM_FIRST:
            _analyze_and_publish(uri, include_jdtls=False, trigger="custom-first")
        while _freshness.is_pending(key):
            if _freshness.expire(key):
                trigger = "timeout"
                logger.info("jdtls did not re-validate %s in time; publishing cached diagnostics", Path(uri).name)
                break
            deadline = _freshness.deadline(key)
            remaining = (deadline - _freshness.clock()) if deadline is not None else 0.0
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=max(remaining, 0.0))
            except asyncio.TimeoutError:
                pass
    finally:
        if _hold_events.get(key) is event:
            del _hold_events[key]
    if _is_current_task(uri):
        _analyze_and_publish(uri, trigger=trigger)


async def _deferred_validate(uri: str) -> None:
    """Debounced validation — waits before analyzing to batch rapid edits."""
    await asyncio.sleep(_DEBOUNCE_SECONDS)
    key = _normalize_uri(uri)
    try:
        if _freshness.is_pending(key):
            await _hold_until_fresh(uri, key)
        else:
            _analyze_and_publish(uri, trigger="change")
    except Exception as e:
        # Never leave a file pending without a hold task: its jdtls publish would be swallowed.
        _freshness.forget(key)
        logger.error("Validation failed for %s: %s", Path(uri).name, e)


def _forward_or_queue(method: str, serialized: Any) -> bool:
    """Forward a notification to jdtls if available, or queue it if starting.

    Returns True only when it was sent to a running jdtls.
    """
    if server._skip_jdtls:
        return False
    if server._proxy.is_available:
        _fire_and_forget(server._proxy.send_notification(method, serialized))
        return True
    if server._proxy._lazy_start_fired and not server._proxy._start_failed:
        server._proxy.queue_notification(method, serialized)
    return False


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
async def on_did_open(params: lsp.DidOpenTextDocumentParams) -> None:
    """Forward to jdtls (starting lazily if needed) and analyze immediately.

    Custom diagnostics always publish immediately regardless of jdtls state.
    jdtls startup is non-blocking — it runs in the background so the first
    didOpen response isn't delayed by jdtls cold-start.
    """
    uri = params.text_document.uri
    # Must precede any await: same-loop jdtls diagnostic callbacks need to
    # observe this URI before they arrive to avoid the publish getting gated.
    server._record_opened(uri)

    if server._skip_jdtls:
        # Skip all jdtls forwarding — custom diagnostics only.
        pass
    elif server._proxy.is_available:
        # Fast path: jdtls running. Forward didOpen + add module if new.
        serialized = _serialize_params(params)
        await server._proxy.send_notification("textDocument/didOpen", serialized)
        await server._proxy.add_module_if_new(uri)
    elif server._proxy._jdtls_on_path and not server._proxy._start_failed:
        # Queue the didOpen (whether this is the first file or a subsequent one during startup).
        serialized = _serialize_params(params)
        server._proxy.queue_notification("textDocument/didOpen", serialized)
        if not server._proxy._lazy_start_fired:
            # First file: kick off lazy start in background.
            server._proxy._lazy_start_fired = True
            _fire_and_forget(_lazy_start_jdtls(uri))

    # Custom diagnostics always publish immediately — never blocked by jdtls.
    try:
        _analyze_and_publish(uri)
    except Exception as e:
        logger.error("Analysis failed on open for %s: %s", uri, e)


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
async def on_did_change(params: lsp.DidChangeTextDocumentParams) -> None:
    """Forward to jdtls and schedule debounced re-analysis."""
    uri = params.text_document.uri
    if _forward_or_queue("textDocument/didChange", _serialize_params(params)):
        _freshness.note_forwarded_change()
        if server._hold_mode != _HOLD_OFF:
            key = _normalize_uri(uri)
            # jdtls publishes for files outside the opened set are gated out, so they could never release.
            if key in server._session_opened_uris and _hold_eligible(uri):
                _freshness.mark_pending(key)
            else:
                _freshness.record("ineligible")
    # Cancel pending validation, schedule new one (150ms debounce for IDE typing)
    if uri in _pending:
        _pending[uri].cancel()
    _pending[uri] = asyncio.create_task(_deferred_validate(uri))


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
async def on_did_save(params: lsp.DidSaveTextDocumentParams) -> None:
    """Forward to jdtls and re-analyze immediately (no debounce on save)."""
    _forward_or_queue("textDocument/didSave", _serialize_params(params))
    if _freshness.is_pending(_normalize_uri(params.text_document.uri)):
        # jdtls does not validate on save; the pending edit's hold publishes the fresh merge.
        return
    try:
        _analyze_and_publish(params.text_document.uri)
    except Exception as e:
        logger.error("Analysis failed on save for %s: %s", params.text_document.uri, e)


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
async def on_did_close(params: lsp.DidCloseTextDocumentParams) -> None:
    """Clean up cached state, clear diagnostics, and forward to jdtls."""
    uri = params.text_document.uri
    if uri in _pending:
        _pending[uri].cancel()
        del _pending[uri]
    _freshness.forget(_normalize_uri(uri))
    # Clear diagnostics for the closed document (LSP best practice)
    server.text_document_publish_diagnostics(lsp.PublishDiagnosticsParams(uri=uri, diagnostics=[]))
    _forward_or_queue("textDocument/didClose", _serialize_params(params))


_DIDCHANGE_CONFIG_COOLDOWN = 30.0  # seconds — rate-limit didChangeConfiguration forwarding
_last_config_forward: float = 0.0


@server.feature(lsp.WORKSPACE_DID_CHANGE_CONFIGURATION)
def on_did_change_configuration(params: lsp.DidChangeConfigurationParams) -> None:
    """Forward IDE configuration changes to jdtls (rate-limited)."""
    global _last_config_forward
    if not server._proxy.is_available:
        return
    now = time.monotonic()
    if now - _last_config_forward < _DIDCHANGE_CONFIG_COOLDOWN:
        logger.debug("jdtls: throttling didChangeConfiguration (%.0fs cooldown)", _DIDCHANGE_CONFIG_COOLDOWN)
        return
    _last_config_forward = now
    _fire_and_forget(server._proxy.send_notification("workspace/didChangeConfiguration", _serialize_params(params)))


async def _lazy_start_jdtls(file_uri: str) -> None:
    """Background task: start jdtls scoped to the module containing *file_uri*.

    Runs in the background so ``on_did_open`` returns immediately with custom
    diagnostics.  After jdtls initializes, the workspace is immediately expanded
    to the full IDE workspace root (inside-out) so jdtls discovers all sibling
    modules before any queued ``didOpen`` notifications are flushed.  This means
    ``add_module_if_new`` is a no-op for all subsequent file opens — jdtls
    already covers the whole project.
    """
    try:
        started = await server._proxy.ensure_started(server._init_params, file_uri, config=server._config)
        if started:
            logger.info("jdtls proxy active — full Java language support enabled")
            # Expand to full workspace BEFORE flushing queued notifications so
            # all files are processed in the full-workspace context (inside-out).
            # expand_full_workspace is a no-op for standalone projects where
            # the workspace root equals the initial module.
            await server._proxy.expand_full_workspace()
            if server._skip_jdtls_registration:
                logger.info("Skipping dynamic capability registration (no-register mode)")
            else:
                await _register_jdtls_capabilities()
            await server._proxy.flush_queued_notifications()
    except Exception:
        logger.warning("jdtls lazy start failed", exc_info=True)


# --- jdtls passthrough handlers (registered dynamically, NOT at module level) ---
#
# These are NOT decorated with @server.feature because pygls auto-advertises
# capabilities for decorated handlers. Instead, they are collected in
# _JDTLS_HANDLERS and registered inside _register_jdtls_capabilities() so
# they only activate after jdtls starts.


# Methods that operate on a single file and succeed before cross-file indexing
# is complete. A successful response from these does NOT mean the module is fully
# indexed, so we must NOT use them to transition the module to READY — doing so
# would cause cross-file requests (references, callHierarchy) to skip the wait
# and return empty results from an under-indexed jdtls instance.
_LIGHTWEIGHT_METHODS: frozenset[str] = frozenset(
    {
        "textDocument/documentHighlight",
        "textDocument/documentSymbol",
    }
)

# jdtls diagnostic code for files it opened but hasn't resolved into a Maven project yet.
# When this appears in a publishDiagnostics batch, jdtls has syntax info only — no classpath,
# no cross-file resolution.  We must NOT mark the module READY yet; wait for a re-publish
# without code 16 (after Maven project import finishes).
_JDTLS_NON_PROJECT_CODE: str = "16"


async def _ensure_module_and_forward(
    method: str,
    params: Any,
    file_uri: str,
) -> Any | None:
    """Forward a request to jdtls, ensuring the file's module is loaded.

    Uses ``ModuleRegistry`` for adaptive waiting:
    - **READY**: forward immediately (zero overhead on hot path)
    - **UNKNOWN**: add module, wait until ready (adaptive, not fixed sleep)
    - **ADDED**: module sent but not confirmed — wait until ready

    When a request that requires full project indexing succeeds, marks the
    module as READY so subsequent requests skip the wait entirely.
    Methods in ``_LIGHTWEIGHT_METHODS`` operate on a single file and succeed
    before cross-file indexing completes — their success is never used to
    signal readiness.
    """
    proxy = server._proxy
    if not proxy.is_available:
        return None

    module_uri = _resolve_module_uri(file_uri)
    short_mod = Path(to_fs_path(module_uri) or module_uri).name if module_uri else "<none>"

    # Hot path: module already confirmed working.
    if module_uri and proxy.modules.is_ready(module_uri):
        logger.info("jdtls: %s [hot] module=%s", method, short_mod)
        result = await proxy.send_request(method, _serialize_params(params))
        if result is None:
            logger.info("jdtls: %s [hot] → null (jdtls has no result for this file_uri)", method)
        return result

    # Cold path: add module if unknown, then wait for ready.
    logger.info("jdtls: %s [cold] module=%s groups=%d", method, short_mod, len(proxy._expanded_groups))
    new_module_uri = await proxy.add_module_if_new(file_uri)

    serialized = _serialize_params(params)
    result = await proxy.send_request(method, serialized)

    can_mark_ready = method not in _LIGHTWEIGHT_METHODS
    if result is not None:
        # Success — mark module as ready so future cross-file requests are instant.
        if can_mark_ready and module_uri:
            proxy.modules.mark_ready(module_uri)
        return result

    # Null result and module is not yet ready — wait then retry once.
    # Use a short timeout (5s) so single-caller case doesn't block for 30s.
    # If a concurrent request succeeds, Event.set() wakes us early.
    wait_uri = new_module_uri or module_uri
    if wait_uri and not proxy.modules.is_ready(wait_uri):
        logger.info("jdtls: %s [cold] waiting for module %s to become ready", method, short_mod)
        await proxy.modules.wait_until_ready(wait_uri, timeout=5.0)
    # Always retry once after waiting — even on timeout the module may be ready.
    result = await proxy.send_request(method, serialized)
    if result is not None and can_mark_ready and module_uri:
        proxy.modules.mark_ready(module_uri)
    logger.info("jdtls: %s [cold] retry → %s", method, "result" if result is not None else "null")
    return result


async def _on_completion(params: lsp.CompletionParams) -> lsp.CompletionList | None:
    """Forward completion request to jdtls."""
    result = await _ensure_module_and_forward("textDocument/completion", params, params.text_document.uri)
    if result is None:
        return None
    try:
        return _converter.structure(result, lsp.CompletionList)
    except Exception:
        return None


async def _on_hover(params: lsp.HoverParams) -> lsp.Hover | None:
    """Forward hover request to jdtls."""
    result = await _ensure_module_and_forward("textDocument/hover", params, params.text_document.uri)
    if result is None:
        return None
    try:
        return _converter.structure(result, lsp.Hover)
    except Exception:
        return None


async def _on_definition(params: lsp.DefinitionParams) -> list[lsp.Location] | None:
    """Forward go-to-definition request to jdtls."""
    result = await _ensure_module_and_forward("textDocument/definition", params, params.text_document.uri)
    if result is None:
        return None
    try:
        if isinstance(result, list):
            return [_converter.structure(loc, lsp.Location) for loc in result]
        return [_converter.structure(result, lsp.Location)]
    except Exception:
        return None


async def _on_references(params: lsp.ReferenceParams) -> list[lsp.Location] | None:
    """Forward find-references request to jdtls."""
    result = await _ensure_module_and_forward("textDocument/references", params, params.text_document.uri)
    if result is None:
        return None
    try:
        return [_converter.structure(loc, lsp.Location) for loc in result]
    except Exception:
        return None


async def _on_document_symbol(params: lsp.DocumentSymbolParams) -> list[lsp.DocumentSymbol] | None:
    """Forward document symbol request to jdtls."""
    result = await _ensure_module_and_forward("textDocument/documentSymbol", params, params.text_document.uri)
    if result is None:
        return None
    try:
        return [_converter.structure(sym, lsp.DocumentSymbol) for sym in result]
    except Exception:
        return None


async def _on_prepare_call_hierarchy(params: lsp.CallHierarchyPrepareParams) -> list[lsp.CallHierarchyItem] | None:
    """Forward prepareCallHierarchy request to jdtls."""
    result = await _ensure_module_and_forward("textDocument/prepareCallHierarchy", params, params.text_document.uri)
    if result is None:
        return None
    try:
        return [_converter.structure(item, lsp.CallHierarchyItem) for item in result]
    except Exception:
        return None


async def _on_incoming_calls(
    params: lsp.CallHierarchyIncomingCallsParams,
) -> list[lsp.CallHierarchyIncomingCall] | None:
    """Forward callHierarchy/incomingCalls request to jdtls."""
    result = await _ensure_module_and_forward("callHierarchy/incomingCalls", params, params.item.uri)
    if result is None:
        return None
    try:
        structured = [_converter.structure(c, lsp.CallHierarchyIncomingCall) for c in result]
        logger.info("jdtls: incomingCalls → %d callers", len(structured))
        return structured
    except Exception as e:
        logger.warning("jdtls: incomingCalls structuring failed: %s (raw=%r)", e, result)
        return None


async def _on_outgoing_calls(
    params: lsp.CallHierarchyOutgoingCallsParams,
) -> list[lsp.CallHierarchyOutgoingCall] | None:
    """Forward callHierarchy/outgoingCalls request to jdtls."""
    result = await _ensure_module_and_forward("callHierarchy/outgoingCalls", params, params.item.uri)
    if result is None:
        return None
    try:
        return [_converter.structure(c, lsp.CallHierarchyOutgoingCall) for c in result]
    except Exception:
        return None


async def _on_signature_help(params: lsp.SignatureHelpParams) -> lsp.SignatureHelp | None:
    """Forward signatureHelp request to jdtls."""
    result = await _ensure_module_and_forward("textDocument/signatureHelp", params, params.text_document.uri)
    if result is None:
        return None
    try:
        return _converter.structure(result, lsp.SignatureHelp)
    except Exception:
        logger.debug("Failed to structure signatureHelp result", exc_info=True)
        return None


async def _on_implementation(params: lsp.ImplementationParams) -> list[lsp.Location] | None:
    """Forward go-to-implementation request to jdtls."""
    result = await _ensure_module_and_forward("textDocument/implementation", params, params.text_document.uri)
    if result is None:
        return None
    try:
        if isinstance(result, list):
            return [_converter.structure(loc, lsp.Location) for loc in result]
        return [_converter.structure(result, lsp.Location)]
    except Exception:
        logger.debug("Failed to structure implementation result", exc_info=True)
        return None


async def _on_type_definition(params: lsp.TypeDefinitionParams) -> list[lsp.Location] | None:
    """Forward go-to-type-definition request to jdtls."""
    result = await _ensure_module_and_forward("textDocument/typeDefinition", params, params.text_document.uri)
    if result is None:
        return None
    try:
        if isinstance(result, list):
            return [_converter.structure(loc, lsp.Location) for loc in result]
        return [_converter.structure(result, lsp.Location)]
    except Exception:
        logger.debug("Failed to structure typeDefinition result", exc_info=True)
        return None


async def _on_declaration(params: lsp.DeclarationParams) -> list[lsp.Location] | None:
    """Forward go-to-declaration request to jdtls."""
    result = await _ensure_module_and_forward("textDocument/declaration", params, params.text_document.uri)
    if result is None:
        return None
    try:
        if isinstance(result, list):
            return [_converter.structure(loc, lsp.Location) for loc in result]
        return [_converter.structure(result, lsp.Location)]
    except Exception:
        logger.debug("Failed to structure declaration result", exc_info=True)
        return None


async def _on_document_highlight(params: lsp.DocumentHighlightParams) -> list[lsp.DocumentHighlight] | None:
    """Forward documentHighlight request to jdtls."""
    result = await _ensure_module_and_forward("textDocument/documentHighlight", params, params.text_document.uri)
    if result is None:
        return None
    try:
        return [_converter.structure(h, lsp.DocumentHighlight) for h in result]
    except Exception:
        logger.debug("Failed to structure documentHighlight result", exc_info=True)
        return None


async def _on_rename(params: lsp.RenameParams) -> lsp.WorkspaceEdit | None:
    """Forward rename request to jdtls."""
    result = await _ensure_module_and_forward("textDocument/rename", params, params.text_document.uri)
    if result is None:
        return None
    try:
        return _converter.structure(result, lsp.WorkspaceEdit)
    except Exception:
        logger.debug("Failed to structure rename result", exc_info=True)
        return None


async def _on_prepare_rename(
    params: lsp.PrepareRenameParams,
) -> lsp.Range | lsp.PrepareRenamePlaceholder | lsp.PrepareRenameDefaultBehavior | None:
    """Forward prepareRename request to jdtls.

    jdtls may return Range, PrepareRenamePlaceholder ({range, placeholder}),
    or PrepareRenameDefaultBehavior ({defaultBehavior}) — try each in order.
    """
    result = await _ensure_module_and_forward("textDocument/prepareRename", params, params.text_document.uri)
    if result is None:
        return None
    for target_type in (lsp.PrepareRenamePlaceholder, lsp.PrepareRenameDefaultBehavior, lsp.Range):
        try:
            return _converter.structure(result, target_type)  # type: ignore[return-value]
        except Exception:
            pass
    logger.debug("Failed to structure prepareRename result as any known type", exc_info=False)
    return None


async def _on_prepare_type_hierarchy(
    params: lsp.TypeHierarchyPrepareParams,
) -> list[lsp.TypeHierarchyItem] | None:
    """Forward prepareTypeHierarchy request to jdtls."""
    result = await _ensure_module_and_forward("textDocument/prepareTypeHierarchy", params, params.text_document.uri)
    if result is None:
        return None
    try:
        return [_converter.structure(item, lsp.TypeHierarchyItem) for item in result]
    except Exception:
        logger.debug("Failed to structure prepareTypeHierarchy result", exc_info=True)
        return None


async def _on_type_hierarchy_supertypes(
    params: lsp.TypeHierarchySupertypesParams,
) -> list[lsp.TypeHierarchyItem] | None:
    """Forward typeHierarchy/supertypes request to jdtls."""
    result = await _ensure_module_and_forward("typeHierarchy/supertypes", params, params.item.uri)
    if result is None:
        return None
    try:
        return [_converter.structure(item, lsp.TypeHierarchyItem) for item in result]
    except Exception:
        logger.debug("Failed to structure typeHierarchy/supertypes result", exc_info=True)
        return None


async def _on_type_hierarchy_subtypes(
    params: lsp.TypeHierarchySubtypesParams,
) -> list[lsp.TypeHierarchyItem] | None:
    """Forward typeHierarchy/subtypes request to jdtls."""
    result = await _ensure_module_and_forward("typeHierarchy/subtypes", params, params.item.uri)
    if result is None:
        return None
    try:
        return [_converter.structure(item, lsp.TypeHierarchyItem) for item in result]
    except Exception:
        logger.debug("Failed to structure typeHierarchy/subtypes result", exc_info=True)
        return None


async def _on_workspace_symbol(params: lsp.WorkspaceSymbolParams) -> list[lsp.WorkspaceSymbol] | None:
    """Forward workspace/symbol request to jdtls (no text_document — use proxy directly)."""
    proxy = server._proxy
    if not proxy.is_available:
        return None
    result = await proxy.send_request("workspace/symbol", _serialize_params(params))
    if result is None:
        return None
    try:
        return [_converter.structure(s, lsp.WorkspaceSymbol) for s in result]
    except Exception:
        logger.debug("Failed to structure workspace/symbol result", exc_info=True)
        return None


# Populate handler map for dynamic registration.
_JDTLS_HANDLERS.update(
    {
        lsp.TEXT_DOCUMENT_COMPLETION: _on_completion,
        lsp.TEXT_DOCUMENT_HOVER: _on_hover,
        lsp.TEXT_DOCUMENT_DEFINITION: _on_definition,
        lsp.TEXT_DOCUMENT_REFERENCES: _on_references,
        lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL: _on_document_symbol,
        lsp.TEXT_DOCUMENT_PREPARE_CALL_HIERARCHY: _on_prepare_call_hierarchy,
        lsp.CALL_HIERARCHY_INCOMING_CALLS: _on_incoming_calls,
        lsp.CALL_HIERARCHY_OUTGOING_CALLS: _on_outgoing_calls,
        lsp.TEXT_DOCUMENT_SIGNATURE_HELP: _on_signature_help,
        lsp.TEXT_DOCUMENT_IMPLEMENTATION: _on_implementation,
        lsp.TEXT_DOCUMENT_TYPE_DEFINITION: _on_type_definition,
        lsp.TEXT_DOCUMENT_DECLARATION: _on_declaration,
        lsp.TEXT_DOCUMENT_DOCUMENT_HIGHLIGHT: _on_document_highlight,
        lsp.TEXT_DOCUMENT_RENAME: _on_rename,
        lsp.TEXT_DOCUMENT_PREPARE_RENAME: _on_prepare_rename,
        lsp.TEXT_DOCUMENT_PREPARE_TYPE_HIERARCHY: _on_prepare_type_hierarchy,
        lsp.TYPE_HIERARCHY_SUPERTYPES: _on_type_hierarchy_supertypes,
        lsp.TYPE_HIERARCHY_SUBTYPES: _on_type_hierarchy_subtypes,
        lsp.WORKSPACE_SYMBOL: _on_workspace_symbol,
    }
)


# --- Code actions (quick fixes) ---

# Human-readable titles for code actions
_FIX_TITLES: dict[str, str] = {
    "frozen-mutation": "Switch to Vavr Immutable Collection",
    "null-check-to-monadic": "Convert to Option monadic flow",
    "null-return": "Replace with Option.none()",
    "try-catch-to-monadic": "Convert try/catch to Try monadic flow",
    "imperative-option-unwrap": "Convert to Option.map().getOrElse()",
    "mutable-dto": "Replace @Data with @Value",
}

# Guard against title/registry mismatch at import time.
assert set(_FIX_TITLES) == get_fix_registry_keys(), (
    f"_FIX_TITLES keys {set(_FIX_TITLES)} do not match fix registry keys {get_fix_registry_keys()}"
)

# Guard against a fix being registered against a rule code that no analyzer emits.
# This catches typos like registering "fronzen-mutation" — without the check, the title
# silently never appears in any client's code action menu because no diagnostic carries
# that code.
_UNKNOWN_FIXED_RULES = set(_FIX_TITLES) - KNOWN_RULES
assert not _UNKNOWN_FIXED_RULES, (
    f"_FIX_TITLES references rules that no analyzer emits: {sorted(_UNKNOWN_FIXED_RULES)}. "
    f"Known rules: {sorted(KNOWN_RULES)}"
)


@server.feature(lsp.TEXT_DOCUMENT_CODE_ACTION)
def on_code_action(params: lsp.CodeActionParams) -> list[lsp.CodeAction] | None:
    """Return quick-fix code actions for functional diagnostics."""
    doc = server.workspace.get_text_document(params.text_document.uri)
    uri = params.text_document.uri
    actions: list[lsp.CodeAction] = []

    # Parse the tree once and split source lines once — shared across all fix generators.
    tree = server._parser.parse(doc.source.encode("utf-8"))
    source_lines = doc.source.split("\n")

    for diag in params.context.diagnostics:
        if diag.source != "java-functional-lsp":
            continue
        rule_id = diag.code if isinstance(diag.code, str) else str(diag.code) if diag.code is not None else ""
        fix_fn = get_fix(rule_id)
        if fix_fn is None:
            continue

        try:
            workspace_edit = fix_fn(uri, doc.source, diag.range, server._config, tree=tree, lines=source_lines)
        except Exception as e:
            logger.error("Fix generator for %s failed: %s", rule_id, e)
            continue

        if workspace_edit is None:
            continue

        title = _FIX_TITLES.get(rule_id, f"Fix {rule_id}")
        actions.append(
            lsp.CodeAction(
                title=title,
                kind=lsp.CodeActionKind.QuickFix,
                diagnostics=[diag],
                edit=workspace_edit,
            )
        )

    return actions if actions else None


# --- Entry point ---


class _EternalStdinBuffer:
    """Wraps sys.stdin.buffer so EOF never propagates to the pygls reader.

    Claude Code (ENABLE_LSP_TOOL, versions ≤ 0.2.x) closes the LSP subprocess's
    stdin pipe immediately after process startup — before sending any LSP messages.
    pygls sees EOF on the first readline() and shuts down the server.

    This wrapper intercepts that EOF: instead of returning b'' (which pygls's
    run_async() treats as a signal to break its read loop and shut down), it
    blocks the calling thread indefinitely via threading.Event.wait().

    pygls uses a ThreadPoolExecutor for stdin reads (run_in_executor), so
    blocking one thread does not stall the asyncio event loop — all other
    coroutines (jdtls proxy, diagnostics publishing, etc.) remain responsive.

    When Claude Code is fixed upstream, this class becomes a transparent pass-
    through: data before EOF is forwarded unchanged; only the EOF itself is swallowed.
    """

    def __init__(self, buf: BinaryIO) -> None:
        self._buf = buf
        self._eof_gate = Event()  # never set — waits block forever

    def readline(self) -> bytes:
        line = self._buf.readline()
        if not line:
            self._eof_gate.wait()  # block forever instead of returning b''
        return line

    def read(self, n: int) -> bytes:
        data = self._buf.read(n)
        if not data:
            self._eof_gate.wait()
        return data


def main() -> None:
    """Entry point for the LSP server."""
    level = getattr(logging, os.environ.get("JAVA_FUNCTIONAL_LSP_LOG_LEVEL", "INFO").strip().upper(), None)
    logging.basicConfig(
        level=level if isinstance(level, int) else logging.INFO, format="%(name)s %(levelname)s: %(message)s"
    )
    server.start_io(stdin=_EternalStdinBuffer(sys.stdin.buffer))  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
