"""Main LSP server for java-functional-lsp.

Provides custom Java diagnostics via tree-sitter analysis.
Proxies to jdtls for full Java language features (completions, hover, go-to-def).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from lsprotocol import types as lsp
from lsprotocol.converters import get_converter
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path

from .analyzers.base import Analyzer, Severity, get_parser, is_excluded, is_suppressed
from .analyzers.base import Diagnostic as LintDiagnostic
from .analyzers.exception_checker import ExceptionChecker
from .analyzers.functional_checker import FunctionalChecker
from .analyzers.mutation_checker import MutationChecker
from .analyzers.null_checker import NullChecker
from .analyzers.spring_checker import SpringChecker
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


class JavaFunctionalLspServer(LanguageServer):
    def __init__(self) -> None:
        from . import __version__

        super().__init__("java-functional-lsp", __version__)
        self._parser = get_parser()
        self._config: dict[str, Any] = {}
        self._init_params: dict[str, Any] = {}
        self._proxy = JdtlsProxy(on_diagnostics=self._on_jdtls_diagnostics)
        self._user_suppress_patterns: list[re.Pattern[str]] = []
        self._skip_jdtls: bool = False
        self._skip_jdtls_registration: bool = False
        self._init_generation: int = 0

    def _on_jdtls_diagnostics(self, uri: str, _diagnostics: list[Any]) -> None:
        """Called when jdtls publishes diagnostics — merge with custom and re-publish.

        Also marks the file's module as READY, since receiving diagnostics from
        jdtls is a reliable signal that the module has been indexed (more reliable
        than a first non-None response which may be semantically empty).

        When a module transitions to READY, fires ``_apply_module_diff`` to
        notify jdtls about any externally changed files (e.g., after git pull).
        """
        if not uri.endswith(".java"):
            return
        module_uri = _resolve_module_uri(uri)
        if module_uri:
            self._proxy.modules.mark_ready(module_uri)
            # Fire-and-forget: apply Merkle diff and save updated snapshot.
            _fire_and_forget(_apply_module_diff(self._proxy, module_uri))
        try:
            _analyze_and_publish(uri)
        except Exception as e:
            logger.error("Error re-publishing diagnostics for %s: %s", uri, e)


server = JavaFunctionalLspServer()

# Debounce state for didChange events (only affects human typing in IDEs, not agents)
_pending: dict[str, asyncio.Task[None]] = {}
# Background tasks (prevent GC of fire-and-forget tasks)
_bg_tasks: set[asyncio.Task[None]] = set()
_DEBOUNCE_SECONDS = 0.15


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
    data = None
    if diag.data is not None:
        data = {
            "fixType": diag.data.fix_type,
            "targetLibrary": diag.data.target_library,
            "rationale": diag.data.rationale,
        }
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


def _run_analysis(source: str, uri: str) -> list[lsp.Diagnostic]:
    """Run custom analyzers on source text and merge with jdtls diagnostics.

    jdtls processing is isolated: if it fails, custom diagnostics still publish.
    """
    custom_diags = _analyze_document(source, uri)

    jdtls_diags: list[lsp.Diagnostic] = []
    if server._proxy.is_available:
        try:
            raw = server._proxy.get_cached_diagnostics(uri)
            raw = [d for d in raw if not _is_jdtls_suppressed(d, server._user_suppress_patterns)]
            jdtls_diags = _jdtls_raw_to_lsp_diagnostics(raw)
        except Exception as e:
            logger.warning("jdtls diagnostic processing failed for %s: %s", uri, e)

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
    _jdtls_capabilities_registered = False

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

    return lsp.InitializeResult(
        capabilities=lsp.ServerCapabilities(
            text_document_sync=lsp.TextDocumentSyncOptions(
                open_close=True,
                change=lsp.TextDocumentSyncKind.Full,
                save=lsp.SaveOptions(include_text=True),
            ),
            # Only advertise capabilities we own (custom diagnostics + code actions).
            # jdtls-dependent features (hover, definition, references, completion,
            # documentSymbol) are registered dynamically after jdtls starts — see
            # on_initialized(). This prevents us from claiming hover when jdtls
            # isn't ready, which would suppress the IDE's diagnostic tooltips.
            code_action_provider=lsp.CodeActionOptions(
                code_action_kinds=[lsp.CodeActionKind.QuickFix],
            ),
        )
    )


@server.feature(lsp.INITIALIZED)
async def on_initialized(_params: lsp.InitializedParams) -> None:
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


_JAVA_SELECTOR = [lsp.TextDocumentFilterLanguage(language="java")]

_JDTLS_REG_PREFIX = "jdtls-"

# jdtls-dependent capabilities registered dynamically after the proxy starts.
# Each entry: (id_suffix, LSP method, registration options class, extra kwargs).
_JDTLS_CAPABILITIES: list[tuple[str, str, type[Any], dict[str, Any]]] = [
    ("completion", lsp.TEXT_DOCUMENT_COMPLETION, lsp.CompletionRegistrationOptions, {"trigger_characters": ["."]}),
    ("hover", lsp.TEXT_DOCUMENT_HOVER, lsp.HoverRegistrationOptions, {}),
    ("definition", lsp.TEXT_DOCUMENT_DEFINITION, lsp.DefinitionRegistrationOptions, {}),
    ("references", lsp.TEXT_DOCUMENT_REFERENCES, lsp.ReferenceRegistrationOptions, {}),
    ("document-symbol", lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL, lsp.DocumentSymbolRegistrationOptions, {}),
]

# Maps LSP method → handler function for dynamic registration.
_JDTLS_HANDLERS: dict[str, Any] = {}

# Set after first successful registration to prevent FeatureAlreadyRegisteredError.
_jdtls_capabilities_registered = False


def _build_jdtls_registrations() -> list[lsp.Registration]:
    """Build LSP Registration objects for jdtls-dependent capabilities."""
    return [
        lsp.Registration(
            id=f"{_JDTLS_REG_PREFIX}{suffix}",
            method=method,
            register_options=_converter.unstructure(opts_cls(document_selector=_JAVA_SELECTOR, **extra)),
        )
        for suffix, method, opts_cls, extra in _JDTLS_CAPABILITIES
    ]


async def _register_jdtls_capabilities() -> None:
    """Dynamically register jdtls-dependent capabilities after the proxy starts.

    We don't advertise these in the static InitializeResult because doing so
    would make the IDE defer hover/definition/etc to us even before jdtls is
    ready, which suppresses the IDE's built-in diagnostic tooltips.

    Idempotent: safe to call multiple times (e.g., proxy restart).
    Uses a generation counter to detect stale registrations from a prior
    initialize cycle (defensive against non-standard re-initialization).
    """
    global _jdtls_capabilities_registered
    if _jdtls_capabilities_registered:
        return

    generation = server._init_generation

    try:
        # Register handlers so pygls dispatches incoming requests to them.
        for method, handler in _JDTLS_HANDLERS.items():
            server.feature(method)(handler)

        # Tell the client we now support these capabilities.
        registrations = _build_jdtls_registrations()
        await server.client_register_capability_async(lsp.RegistrationParams(registrations=registrations))

        # Bail if a re-initialize happened while we were awaiting.
        if generation != server._init_generation:
            logger.info("Discarding stale jdtls capability registration (re-initialize detected)")
            return

        _jdtls_capabilities_registered = True
        logger.info("Dynamically registered jdtls capabilities (hover, definition, references, completion, symbol)")
    except Exception:
        logger.warning("Failed to dynamically register jdtls capabilities", exc_info=True)


# --- Document sync (forward to jdtls + run custom analyzers) ---


def _analyze_and_publish(uri: str) -> None:
    """Read document source, run analysis, publish results."""
    doc = server.workspace.get_text_document(uri)
    diagnostics = _run_analysis(doc.source, uri)
    server.text_document_publish_diagnostics(lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics))


async def _deferred_validate(uri: str) -> None:
    """Debounced validation — waits before analyzing to batch rapid edits."""
    await asyncio.sleep(_DEBOUNCE_SECONDS)
    try:
        _analyze_and_publish(uri)
    except Exception as e:
        logger.error("Validation failed for %s: %s", uri, e)


def _forward_or_queue(method: str, serialized: Any) -> None:
    """Forward a notification to jdtls if available, or queue it if starting."""
    if server._skip_jdtls:
        return
    if server._proxy.is_available:
        _fire_and_forget(server._proxy.send_notification(method, serialized))
    elif server._proxy._lazy_start_fired and not server._proxy._start_failed:
        server._proxy.queue_notification(method, serialized)


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
async def on_did_open(params: lsp.DidOpenTextDocumentParams) -> None:
    """Forward to jdtls (starting lazily if needed) and analyze immediately.

    Custom diagnostics always publish immediately regardless of jdtls state.
    jdtls startup is non-blocking — it runs in the background so the first
    didOpen response isn't delayed by jdtls cold-start.
    """
    uri = params.text_document.uri

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
    _forward_or_queue("textDocument/didChange", _serialize_params(params))
    # Cancel pending validation, schedule new one (150ms debounce for IDE typing)
    if uri in _pending:
        _pending[uri].cancel()
    _pending[uri] = asyncio.create_task(_deferred_validate(uri))


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
async def on_did_save(params: lsp.DidSaveTextDocumentParams) -> None:
    """Forward to jdtls and re-analyze immediately (no debounce on save)."""
    _forward_or_queue("textDocument/didSave", _serialize_params(params))
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
    # Clear diagnostics for the closed document (LSP best practice)
    server.text_document_publish_diagnostics(lsp.PublishDiagnosticsParams(uri=uri, diagnostics=[]))
    _forward_or_queue("textDocument/didClose", _serialize_params(params))


async def _lazy_start_jdtls(file_uri: str) -> None:
    """Background task: start jdtls scoped to the module containing *file_uri*.

    Runs in the background so ``on_did_open`` returns immediately with custom
    diagnostics. After jdtls initializes, registers capabilities and flushes
    queued notifications. Workspace expansion is NOT done eagerly — modules
    are loaded on-demand via ``add_module_if_new()`` as files are opened.
    """
    try:
        started = await server._proxy.ensure_started(server._init_params, file_uri, config=server._config)
        if started:
            logger.info("jdtls proxy active — full Java language support enabled")
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


async def _ensure_module_and_forward(method: str, params: Any, file_uri: str) -> Any | None:
    """Forward a request to jdtls, ensuring the file's module is loaded.

    Uses ``ModuleRegistry`` for adaptive waiting:
    - **READY**: forward immediately (zero overhead on hot path)
    - **UNKNOWN**: add module, wait until ready (adaptive, not fixed sleep)
    - **ADDED**: module sent but not confirmed — wait until ready

    When a request succeeds, marks the module as READY so subsequent
    requests skip the wait entirely.
    """
    proxy = server._proxy
    if not proxy.is_available:
        return None

    module_uri = _resolve_module_uri(file_uri)

    # Hot path: module already confirmed working.
    if module_uri and proxy.modules.is_ready(module_uri):
        return await proxy.send_request(method, _serialize_params(params))

    # Cold path: add module if unknown, then wait for ready.
    new_module_uri = await proxy.add_module_if_new(file_uri)

    serialized = _serialize_params(params)
    result = await proxy.send_request(method, serialized)

    if result is not None:
        # Success — mark module as ready so future requests are instant.
        if module_uri:
            proxy.modules.mark_ready(module_uri)
        return result

    # Null result and module is not yet ready — wait then retry once.
    # Use a short timeout (5s) so single-caller case doesn't block for 30s.
    # If a concurrent request succeeds, Event.set() wakes us early.
    wait_uri = new_module_uri or module_uri
    if wait_uri and not proxy.modules.is_ready(wait_uri):
        await proxy.modules.wait_until_ready(wait_uri, timeout=5.0)
    # Always retry once after waiting — even on timeout the module may be ready.
    result = await proxy.send_request(method, serialized)
    if result is not None and module_uri:
        proxy.modules.mark_ready(module_uri)
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


# Populate handler map for dynamic registration.
_JDTLS_HANDLERS.update(
    {
        lsp.TEXT_DOCUMENT_COMPLETION: _on_completion,
        lsp.TEXT_DOCUMENT_HOVER: _on_hover,
        lsp.TEXT_DOCUMENT_DEFINITION: _on_definition,
        lsp.TEXT_DOCUMENT_REFERENCES: _on_references,
        lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL: _on_document_symbol,
    }
)


# --- Code actions (quick fixes) ---

# Human-readable titles for code actions
_FIX_TITLES: dict[str, str] = {
    "frozen-mutation": "Switch to Vavr Immutable Collection",
    "null-check-to-monadic": "Convert to Option monadic flow",
    "null-return": "Replace with Option.none()",
    "try-catch-to-monadic": "Convert try/catch to Try monadic flow",
}

# Guard against title/registry mismatch at import time
assert set(_FIX_TITLES) == get_fix_registry_keys(), (
    f"_FIX_TITLES keys {set(_FIX_TITLES)} do not match fix registry keys {get_fix_registry_keys()}"
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


def main() -> None:
    """Entry point for the LSP server."""
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")
    server.start_io()


if __name__ == "__main__":
    main()
