"""jdtls proxy — manages a jdtls subprocess and forwards LSP messages via JSON-RPC over stdio."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from .merkle import _BUILD_FILES, ModuleSnapshot, TreeDiff

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0  # seconds — per-request timeout for normal operations
_INITIALIZE_TIMEOUT = 120.0  # seconds — module-scoped init can still be slow (Maven classpath resolution)
_START_RETRY_COOLDOWN = 300.0  # seconds — retry jdtls startup after transient failure
DEFAULT_JVM_MAX_HEAP = "4g"
_STDERR_LINE_MAX = 1000
_MAX_EXPANDED_GROUPS: int = 5  # hard cap on concurrent Maven group workspace folders (prevents jdtls OOM)
_WORKSPACE_CACHE_MAX_SIZE: int = 10  # default LRU cap — override via {"cache": {"maxWorkspaces": N}}

# Default jdtls initialization settings.  Sent via initializationOptions.settings
# so they apply BEFORE the Maven import scan (didChangeConfiguration is too late).
# Users can override via .java-functional-lsp.json: {"jdtls": {"settings": {...}}}
_DEFAULT_JDTLS_SETTINGS: dict[str, Any] = {
    "java": {
        "import": {
            "maven": {"enabled": True},
            "gradle": {"enabled": False},
            "exclusions": [
                "**/node_modules/**",
                "**/.metadata/**",
                "**/archetype-resources/**",
                "**/META-INF/maven/**",
                "**/target/**",
            ],
        },
        "configuration": {"updateBuildConfiguration": "automatic"},
        "maven": {"downloadSources": False},
    }
}

#: Minimum Java major version required by jdtls 1.57+. Anything below this
#: is rejected by the jdtls Python launcher with ``"jdtls requires at least
#: Java 21"`` before the server even starts.
_MIN_JDTLS_JAVA_MAJOR = 21

#: Distinct jdtls→client request methods tracked individually; the rest count as "<other>".
_MAX_DROPPED_METHODS = 50

#: Matches the first version token in ``java -version`` output. Handles:
#: - modern format: ``openjdk version "21.0.10" 2026-01-20``
#: - legacy Java 8: ``openjdk version "1.8.0_452"`` (captures ``1``; caller
#:   must still apply the ``>= _MIN_JDTLS_JAVA_MAJOR`` semantic check)
#: - early access: ``openjdk version "25-ea" 2026-02-15``
#: - internal: ``openjdk version "17-internal"``
_JAVA_VERSION_PATTERN = re.compile(r'version\s"(\d+)(?:\.\d+\.\d+(?:_\d+)?)?(?:-[^"]*)?"')

#: Timeout for ``java -version`` and ``/usr/libexec/java_home`` subprocess calls.
#: Cold JVM startup is typically 200-500ms; 2 seconds covers slow filesystems
#: and macOS Gatekeeper signing checks while bounding event-loop blockage for
#: hung/broken binaries. These calls run in a thread pool (see JdtlsProxy.start)
#: so they never block the asyncio event loop.
_JAVA_VERSION_CHECK_TIMEOUT_SEC = 2

#: Trusted-prefix allow-list for the PATH-based Java fallback. When deriving
#: JAVA_HOME from ``which java``, we reject anything whose parent.parent is
#: a system root prefix — a direct ``/usr/bin/java`` binary would otherwise
#: yield ``JAVA_HOME=/usr`` which is not a valid JDK home.
_SYSTEM_ROOT_PREFIXES = frozenset({"/", "/usr", "/usr/local", "/bin", "/sbin", "/opt"})

#: Allow-list of environment variables forwarded to the jdtls subprocess.
#: Everything else is dropped to avoid leaking secrets (AWS/GitHub/Anthropic
#: tokens, API keys) that may live in the LSP parent-process environment.
#: jdtls is a third-party binary and we cannot guarantee how it handles
#: inherited env vars (crash dumps, telemetry, verbose logs).
_JDTLS_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "TMPDIR",
        "TEMP",
        "TMP",
        "JAVA_HOME",
        "JAVA_TOOL_OPTIONS",
    }
)

#: Variable-name prefixes also forwarded (locale and XDG desktop config).
_JDTLS_ENV_PREFIX_ALLOWLIST = ("LC_", "XDG_", "JDTLS_")


def _redact_path(path: str | None) -> str:
    """Return a log-safe representation of a filesystem path.

    Logs only the basename and a path-length indicator to avoid leaking
    usernames and internal directory layouts through diagnostic output
    (CWE-532). Returns ``"<unset>"`` for None/empty paths.
    """
    if not path:
        return "<unset>"
    basename = os.path.basename(path.rstrip("/")) or path
    return f".../{basename}"


def _read_java_major_version(java_executable: str) -> int | None:
    """Return the major version of ``java_executable``, or None if unreadable.

    Runs ``java -version`` and parses the first ``"<major>..."`` token.
    Returns None on any subprocess or parse failure so callers can keep
    probing alternative candidates without raising.

    Note: for Java 8 (``"1.8.0_452"``) this captures ``1``. Callers must
    still apply the ``>= _MIN_JDTLS_JAVA_MAJOR`` semantic check — do not
    interpret the return value as "Java 1".
    """
    try:
        out = subprocess.check_output(
            [java_executable, "-version"],
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=_JAVA_VERSION_CHECK_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _JAVA_VERSION_PATTERN.search(out)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _java_home_has_suitable_version(java_home: str) -> bool:
    """Return True if ``java_home/bin/java`` exists and is Java 21 or later.

    Logs at debug level when the binary exists but the version check fails
    (e.g., permission denied, corrupt binary) so users can understand why
    their JAVA_HOME was rejected.
    """
    candidate = Path(java_home) / "bin" / "java"
    if not candidate.is_file():
        logger.debug("jdtls: JAVA_HOME candidate %s missing bin/java", _redact_path(java_home))
        return False
    major = _read_java_major_version(str(candidate))
    if major is None:
        logger.debug(
            "jdtls: could not read java -version from %s (not executable or unreadable)",
            _redact_path(java_home),
        )
        return False
    if major < _MIN_JDTLS_JAVA_MAJOR:
        logger.debug(
            "jdtls: JAVA_HOME candidate %s is Java %d (< %d)", _redact_path(java_home), major, _MIN_JDTLS_JAVA_MAJOR
        )
        return False
    return True


def _is_system_root_prefix(path: Path) -> bool:
    """Return True if ``path`` is a system root directory unsuitable as JAVA_HOME."""
    try:
        return str(path.resolve()) in _SYSTEM_ROOT_PREFIXES
    except OSError:
        return False


def find_jdtls_java_home(environ: Mapping[str, str] | None = None) -> str | None:
    """Find a JAVA_HOME that points at Java 21+ suitable for running jdtls.

    Checks in order:
    1. ``JDTLS_JAVA_HOME`` env var (explicit user override)
    2. ``JAVA_HOME`` if it points at Java 21+
    3. macOS: ``/usr/libexec/java_home -v 21+``
    4. ``java`` on PATH if it reports version >= 21

    Returns None if no suitable Java is found — callers should strip
    ``JAVA_HOME`` from the subprocess env so jdtls can fall back to its
    bundled default.

    Pass ``environ`` to override ``os.environ`` lookups (used by tests).
    """
    env = environ if environ is not None else os.environ

    # 1. Explicit override takes precedence over everything. If set but invalid,
    #    log a warning so users understand why their override was rejected and
    #    fall through to the remaining resolution steps.
    override = env.get("JDTLS_JAVA_HOME")
    if override:
        if _java_home_has_suitable_version(override):
            logger.debug("jdtls: using JDTLS_JAVA_HOME override")
            return override
        logger.warning(
            "jdtls: JDTLS_JAVA_HOME (%s) does not point at Java %d+; ignoring and probing other sources",
            _redact_path(override),
            _MIN_JDTLS_JAVA_MAJOR,
        )

    # 2. Current JAVA_HOME if it's already Java 21+.
    existing = env.get("JAVA_HOME")
    if existing and _java_home_has_suitable_version(existing):
        logger.debug("jdtls: using inherited JAVA_HOME")
        return existing

    # 3. macOS: ask the OS for a Java 21+ install. /usr/libexec/java_home -v 21+
    #    guarantees the returned path is Java 21+ (it errors otherwise), so we
    #    trust its output and skip a redundant java -version re-check.
    if platform.system() == "Darwin":
        try:
            out = subprocess.check_output(
                ["/usr/libexec/java_home", "-v", f"{_MIN_JDTLS_JAVA_MAJOR}+"],
                stderr=subprocess.DEVNULL,
                universal_newlines=True,
                timeout=_JAVA_VERSION_CHECK_TIMEOUT_SEC,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        if out and Path(out).is_dir():
            logger.debug("jdtls: resolved via /usr/libexec/java_home -v %d+", _MIN_JDTLS_JAVA_MAJOR)
            return out

    # 4. java on PATH — derive JAVA_HOME as <java>/../.. if the version is new enough.
    #    Pass the caller's PATH explicitly so tests and alternative environments
    #    can control which java is found.
    java_on_path = shutil.which("java", path=env.get("PATH"))
    if java_on_path is not None:
        major = _read_java_major_version(java_on_path)
        if major is not None and major >= _MIN_JDTLS_JAVA_MAJOR:
            # Resolve symlinks first so the parent walk yields the real Home directory
            # (e.g., /usr/bin/java → /opt/jdk21/bin/java → JAVA_HOME=/opt/jdk21).
            resolved = Path(java_on_path).resolve()
            derived_home = resolved.parent.parent
            # Reject system-root prefixes: a bare /usr/bin/java would otherwise
            # yield JAVA_HOME=/usr which jdtls cannot use.
            if _is_system_root_prefix(derived_home):
                logger.debug(
                    "jdtls: PATH java at %s resolves to system prefix %s; skipping",
                    java_on_path,
                    derived_home,
                )
            elif (derived_home / "bin" / "java").is_file():
                logger.debug("jdtls: resolved via java on PATH")
                return str(derived_home)

    logger.debug("jdtls: no Java %d+ found in any source", _MIN_JDTLS_JAVA_MAJOR)
    return None


def build_jdtls_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the environment for the jdtls subprocess.

    Uses an **allow-list** approach rather than copying the full parent env:
    only variables in ``_JDTLS_ENV_ALLOWLIST`` or matching a prefix in
    ``_JDTLS_ENV_PREFIX_ALLOWLIST`` are forwarded. This prevents secrets
    (AWS/GitHub/Anthropic tokens) that may live in the LSP parent-process
    env from leaking into a third-party subprocess.

    ``JAVA_HOME`` handling:

    - If a Java 21+ installation is found (via ``find_jdtls_java_home``),
      ``JAVA_HOME`` is set to its path in the returned env.
    - Otherwise ``JAVA_HOME`` is omitted so the jdtls launcher falls back
      to its own bundled default — this handles the common case where an
      IDE launches the LSP with ``JAVA_HOME`` inherited from a project SDK
      too old for jdtls.

    Pass ``environ`` to override the base environment (used by tests);
    when None, starts from ``os.environ``.
    """
    source = environ if environ is not None else os.environ
    # Detection runs against the full source env so find_jdtls_java_home
    # can inspect JDTLS_JAVA_HOME / JAVA_HOME / PATH before we filter.
    suitable = find_jdtls_java_home(source)

    # Build the filtered env. dict(...) produces an independent copy so
    # callers of build_jdtls_env cannot mutate the source mapping by
    # mutating the returned dict.
    filtered: dict[str, str] = {
        key: value
        for key, value in source.items()
        if key in _JDTLS_ENV_ALLOWLIST or key.startswith(_JDTLS_ENV_PREFIX_ALLOWLIST)
    }

    if suitable is not None:
        filtered["JAVA_HOME"] = suitable
        logger.info("jdtls: using JAVA_HOME=%s", _redact_path(suitable))
    elif "JAVA_HOME" in filtered:
        stripped = filtered.pop("JAVA_HOME")
        logger.warning(
            "jdtls: no Java %d+ found (inherited JAVA_HOME=%s); stripping it so jdtls can use its bundled fallback",
            _MIN_JDTLS_JAVA_MAJOR,
            _redact_path(stripped),
        )

    return filtered


def encode_message(body: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC message with Content-Length header."""
    content = json.dumps(body).encode("utf-8")
    header = f"Content-Length: {len(content)}\r\n\r\n".encode("ascii")
    return header + content


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read a Content-Length framed JSON-RPC message from a stream."""
    try:
        # Read headers until blank line
        content_length = -1
        while True:
            line = await reader.readline()
            if not line:
                return None  # EOF
            line_str = line.decode("ascii").strip()
            if not line_str:
                break  # End of headers
            if line_str.lower().startswith("content-length:"):
                content_length = int(line_str.split(":", 1)[1].strip())

        if content_length < 0:
            return None

        # Read body
        body_bytes = await reader.readexactly(content_length)
        result: dict[str, Any] = json.loads(body_bytes)
        return result
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        return None


_WORKSPACE_DID_CHANGE_FOLDERS = "workspace/didChangeWorkspaceFolders"
_MAX_QUEUED_NOTIFICATIONS = 200
_MODULE_READY_TIMEOUT = 30.0


class ModuleState:
    """Module import states — UNKNOWN → ADDED → READY."""

    UNKNOWN = "unknown"
    ADDED = "added"
    READY = "ready"


class ModuleRegistry:
    """Thread-safe (asyncio) registry tracking jdtls module import states.

    Uses a plain dict for O(1) hot-path lookups and per-module ``asyncio.Event``
    for adaptive waiting — coroutines blocked on ``wait_until_ready()`` wake
    instantly when ``mark_ready()`` is called, instead of a fixed sleep.

    Safe without locks because asyncio is single-threaded: dict mutations that
    don't span an ``await`` are atomic.
    """

    def __init__(self) -> None:
        self._states: dict[str, str] = {}
        self._ready_events: dict[str, asyncio.Event] = {}

    def get_state(self, uri: str) -> str:
        """O(1) state lookup. Returns ModuleState constant."""
        return self._states.get(uri, ModuleState.UNKNOWN)

    def is_ready(self, uri: str) -> bool:
        """O(1) hot-path check — zero overhead when module is ready."""
        return self._states.get(uri) == ModuleState.READY

    def was_added(self, uri: str) -> bool:
        """True if module was sent to jdtls (ADDED or READY)."""
        return uri in self._states

    def mark_added(self, uri: str) -> None:
        """Mark module as sent to jdtls. Pre-creates the Event for waiters.

        Must be called before any ``await`` to prevent duplicate add_module calls.
        """
        self._states[uri] = ModuleState.ADDED
        self._ready_events.setdefault(uri, asyncio.Event())

    def mark_ready(self, uri: str) -> None:
        """Mark module as confirmed working. Wakes all coroutines waiting on it."""
        self._states[uri] = ModuleState.READY
        event = self._ready_events.pop(uri, None)
        if event is not None:
            event.set()

    def uris(self) -> list[str]:
        """Return all tracked module URIs (any state)."""
        return list(self._states)

    def clear(self) -> None:
        """Reset all state. Used by tests."""
        self._states.clear()
        self._ready_events.clear()

    async def wait_until_ready(self, uri: str, timeout: float = _MODULE_READY_TIMEOUT) -> bool:
        """Suspend until the module is ready or timeout expires.

        Returns True if ready, False on timeout. If already READY, returns
        immediately without suspending.
        """
        event = self._ready_events.setdefault(uri, asyncio.Event())
        if event.is_set():
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


@lru_cache(maxsize=256)
def _cached_module_root(dir_path: str) -> str | None:
    """Cached walk up from *dir_path* to find nearest directory with a build file."""
    current = Path(dir_path)
    while True:
        if any((current / bf).is_file() for bf in _BUILD_FILES):
            return str(current)
        parent = current.parent
        if parent == current:
            return None
        current = parent


def find_module_root(file_path: str) -> str | None:
    """Walk up from *file_path* to find the nearest directory containing a build file.

    Returns the directory path, or ``None`` if no build file is found before
    reaching the filesystem root. Results are cached by parent directory.

    **Note:** cache entries are never invalidated. Build files added after the
    first lookup for a given directory will not be detected until process restart.
    """
    return _cached_module_root(str(Path(file_path).parent))


def _resolve_module_uri(file_uri: str) -> str | None:
    """Convert a file URI to the URI of its nearest module root, or None."""
    from pygls.uris import from_fs_path, to_fs_path

    file_path = to_fs_path(file_uri)
    if not file_path:
        return None
    module_root = find_module_root(file_path)
    if module_root is None:
        return None
    module_uri = from_fs_path(module_root)
    return module_uri if module_uri else None


def _version_key(name: str) -> tuple[int, ...]:
    """Parse a version string into a tuple for semantic comparison."""
    parts: list[int] = []
    for segment in re.split(r"[.\-]", name):
        try:
            parts.append(int(segment))
        except ValueError:
            break
    return tuple(parts)


@lru_cache(maxsize=64)
def _find_repo_boundary(resolved_module: Path) -> Path:
    """Return the repository/reactor boundary enclosing *resolved_module*.

    The boundary is the nearest ancestor (the module itself included) that
    contains ``.git`` — a directory, or a file for git worktrees/submodules.
    Without any ``.git`` ancestor it is the topmost directory reachable from the
    module through consecutive ``pom.xml``-bearing ancestors (the reactor root).

    *resolved_module* must already be resolved (``Path.resolve()``) so symlinked
    and real paths share one cache entry and the walk cannot escape via links.
    """
    current = resolved_module
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    top = resolved_module
    while top.parent != top and (top.parent / "pom.xml").is_file():
        top = top.parent
    return top


@lru_cache(maxsize=64)
def _find_maven_group_root(initial_module: Path, workspace_root: Path) -> Path:
    """Return the tightest Maven multi-module parent of *initial_module*.

    Walks up from ``initial_module.parent`` toward (but NOT including) the
    exclusive upper bound, returning the first ancestor whose ``pom.xml``
    contains a ``<modules>`` section.  Falls back to ``initial_module``
    itself when no intermediate group pom exists (e.g. modules that sit
    directly under the reactor root), keeping the scope as tight as
    possible and avoiding accidental full-monorepo indexing.

    The exclusive upper bound is the repository/reactor boundary (see
    ``_find_repo_boundary``), or *workspace_root* when the client root lies
    below that boundary.  A client root *above* the repository (e.g. ``~/git``)
    therefore never makes the repository's aggregator pom a candidate (#110).

    This keeps the jdtls index bounded to a module *group* (typically
    5-20 modules) rather than the full IDE workspace (potentially 200+ modules)
    which would cause ``OutOfMemoryError: Java heap space`` during indexing.

    Both *initial_module* and *workspace_root* are resolved to real paths
    before walking to avoid symlink-based boundary escapes.
    """
    workspace_root = workspace_root.resolve()
    resolved_module = initial_module.resolve()
    current = resolved_module.parent
    # Case 1: module is outside the workspace entirely — scope to workspace_root
    # (also fires when initial_module == workspace_root, since parent is then
    # outside workspace_root).
    if not current.is_relative_to(workspace_root):
        return workspace_root
    boundary = _find_repo_boundary(resolved_module)
    # The client root stays the bound only when it is strictly below the boundary.
    bound = workspace_root if workspace_root != boundary and workspace_root.is_relative_to(boundary) else boundary
    # Case 2: the module IS the bound (e.g. the repository root) — nothing to walk.
    if not current.is_relative_to(bound):
        return resolved_module
    # Walk up to (but NOT including) the bound.  Inspecting the reactor root's
    # own pom.xml would include the entire monorepo (e.g. 1000+ modules) → OOM.
    # `current.parent == current` detects the filesystem root (POSIX `/` or Windows drive).
    while current not in (bound, current.parent):
        pom = current / "pom.xml"
        if pom.is_file():
            try:
                if "<modules>" in pom.read_text(encoding="utf-8", errors="ignore"):
                    return current
            except OSError:
                pass
        current = current.parent
    # Case 3: no intermediate group pom found — use the initial module as its own scope
    # (tightest possible; avoids full-monorepo indexing for modules directly under the bound).
    return resolved_module


@lru_cache(maxsize=256)
def _module_snapshot_path(module_uri: str) -> Path:
    """Return the path for the persistent snapshot file for *module_uri*.

    Snapshots live under ``~/.cache/jdtls-snapshots/`` — separate from the
    jdtls data-dir tree so they survive version-bump cache wipes.
    Each module gets a 12-char BLAKE2b subdirectory (6-byte digest).
    """
    h = hashlib.blake2b(module_uri.encode(), digest_size=6).hexdigest()
    return Path.home() / ".cache" / "jdtls-snapshots" / h / ".snapshot.json"


def _compute_module_diff(
    module_uri: str,
) -> tuple[TreeDiff | None, ModuleSnapshot] | None:
    """Blocking: build current snapshot and diff against stored one.

    Returns ``(diff, current_snapshot)`` or ``None`` on error / size limit.
    ``diff`` is ``None`` when there is no stored snapshot (first session) or
    the snapshots are identical — the caller should still save the snapshot in
    both cases.

    Intended to run inside a thread-pool executor.
    """
    from pygls.uris import to_fs_path

    module_fs = to_fs_path(module_uri)
    if not module_fs:
        return None
    module_root = Path(module_fs)
    if not module_root.is_dir():
        return None

    current = ModuleSnapshot.build(module_root)
    if current is None:
        return None  # file count exceeded limit

    snapshot_path = _module_snapshot_path(module_uri)
    stored = ModuleSnapshot.load(snapshot_path)

    if stored is None:
        # First session: no diff to compute — caller (_apply_module_diff) will
        # save the baseline snapshot after the module reaches READY.
        return (None, current)

    diff = stored.diff(current)
    return (diff, current)


def _dir_mtime(path: Path) -> float:
    """Return ``st_mtime`` of *path*, or ``float('inf')`` on any OSError.

    Returning infinity on error sorts the directory as the *newest* entry,
    so stat-inaccessible directories are conservatively kept rather than
    silently evicted.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return float("inf")


def _parse_max_workspaces(config: Mapping[str, Any] | None) -> int:
    """Parse and validate ``cache.maxWorkspaces`` from the project config.

    Returns *_WORKSPACE_CACHE_MAX_SIZE* when the key is absent, when the value
    cannot be converted to ``int``, or when the result is not positive.  Logs a
    warning in each error case so the user can diagnose the misconfiguration.
    """
    raw = (config or {}).get("cache", {}).get("maxWorkspaces")
    if raw is None:
        return _WORKSPACE_CACHE_MAX_SIZE
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "jdtls: invalid cache.maxWorkspaces %r — using default %d",
            raw,
            _WORKSPACE_CACHE_MAX_SIZE,
        )
        return _WORKSPACE_CACHE_MAX_SIZE
    if value <= 0:
        logger.warning(
            "jdtls: cache.maxWorkspaces must be >= 1, got %d — using default %d",
            value,
            _WORKSPACE_CACHE_MAX_SIZE,
        )
        return _WORKSPACE_CACHE_MAX_SIZE
    return value


def _evict_lru_workspaces(cache_root: Path, *, max_size: int = _WORKSPACE_CACHE_MAX_SIZE) -> None:
    """Evict least-recently-used jdtls workspace directories when count exceeds *max_size*.

    Implements Caffeine's ``maximumSize`` eviction policy for a filesystem cache.
    Directories are ordered by ``st_mtime`` (last modification time), which is a
    reliable proxy for last-used time because jdtls always writes to its workspace
    on startup.  On macOS APFS (``noatime`` default) ``atime`` is not updated on
    reads, so ``mtime`` is used exclusively.

    The *max_size* most-recently-modified real directories are kept; the rest are
    removed.  Dotfiles (e.g. ``.version``), symlinks, and non-directory entries
    are excluded from the eviction candidates.

    A directory whose ``stat()`` raises ``OSError`` is treated as the *newest*
    entry (``float('inf')`` mtime) so it is conservatively kept.

    Runs in the executor thread (``_blocking_startup``) — never blocks the event loop.

    Users can tune the cap via ``.java-functional-lsp.json``:
    ``{"cache": {"maxWorkspaces": 20}}``
    """
    if not cache_root.is_dir():
        return
    try:
        dirs = [d for d in cache_root.iterdir() if d.is_dir() and not d.is_symlink() and not d.name.startswith(".")]
    except OSError as e:
        logger.warning("jdtls: cannot scan cache root for eviction: %s", e)
        return
    if len(dirs) <= max_size:
        return
    dirs.sort(key=_dir_mtime, reverse=True)  # newest first
    for stale in dirs[max_size:]:
        try:
            shutil.rmtree(stale)
            logger.info("jdtls: evicted workspace cache %s (LRU)", stale.name)
        except OSError as e:
            logger.warning("jdtls: failed to evict workspace cache %s: %s", stale.name, e)


def _wipe_data_dir(data_dir: Path) -> None:
    """Remove *data_dir* so the next jdtls startup gets a clean cold start.

    Called after an initialize timeout to recover from a corrupted OSGi
    data-dir (e.g. left behind after an OOM crash).
    """
    shutil.rmtree(data_dir, ignore_errors=True)
    if data_dir.exists():
        logger.warning(
            "jdtls: failed to wipe data-dir %s — next retry may hang again",
            _redact_path(str(data_dir)),
        )
    else:
        logger.info("jdtls: wiped data-dir %s after init timeout", _redact_path(str(data_dir)))


def _compute_cache_marker() -> str:
    """Compute a stable cache marker from jdtls version path + Java version.

    Our Python package version changes (pip upgrades) do NOT affect the jdtls
    Eclipse workspace index — only jdtls binary upgrades or Java version
    changes do.  Resolves through symlinks so Homebrew Cellar version changes
    (e.g. ``/opt/homebrew/Cellar/jdtls/1.58.0/...``) are detected even when
    the wrapper script at ``/opt/homebrew/bin/jdtls`` doesn't change.
    """
    parts: list[str] = []
    jdtls_path = shutil.which("jdtls")
    if jdtls_path:
        resolved = str(Path(jdtls_path).resolve())
        parts.append(hashlib.sha256(resolved.encode()).hexdigest()[:12])
    parts.append(str(_MIN_JDTLS_JAVA_MAJOR))
    return "|".join(parts)


def _clear_cache_on_version_change(cache_root: Path) -> None:
    """Clear jdtls data cache when the jdtls installation changes.

    Stores a ``.version`` marker in the cache root.  The marker is derived
    from the resolved jdtls binary path + Java version requirement — NOT
    from our Python ``__version__``.  This preserves warm caches across
    java-functional-lsp upgrades (which don't affect the Eclipse index).
    """
    current_marker = _compute_cache_marker()
    marker = cache_root / ".version"
    try:
        if marker.exists() and marker.read_text().strip() == current_marker:
            return
        # jdtls or Java version changed, or first run — clear stale caches.
        if cache_root.exists():
            shutil.rmtree(cache_root)
            logger.info("jdtls: cleared stale cache (marker changed to %s)", current_marker)
        cache_root.mkdir(parents=True, exist_ok=True)
        marker.write_text(current_marker)
    except OSError as e:
        logger.warning("jdtls: failed to manage cache version marker: %s", e)


def _find_lombok_jar(config: Mapping[str, Any] | None = None) -> str | None:
    """Find lombok.jar from configurable and auto-discovered locations.

    Search order (first match wins):
    1. Project config (.java-functional-lsp.json): ``{"lombok": "/path/to/lombok.jar"}``
    2. Environment variable: ``LOMBOK_JAR``
    3. Maven cache: ``~/.m2/repository/org/projectlombok/lombok/*/lombok-*.jar``
    4. Dedicated directory: ``~/.jdtls-libs/lombok.jar``
    """
    # 1. Project config
    if config and config.get("lombok"):
        p = Path(config["lombok"]).expanduser()
        if p.is_file():
            return str(p)
        logger.warning("Lombok path from config does not exist: %s", _redact_path(str(p)))

    # 2. Environment variable
    env_path = os.environ.get("LOMBOK_JAR")
    if env_path:
        p = Path(env_path).expanduser()
        if p.is_file():
            return str(p)
        logger.warning("LOMBOK_JAR does not exist: %s", _redact_path(env_path))

    # 3. Maven cache (latest version, semantic sort)
    m2_lombok = Path.home() / ".m2" / "repository" / "org" / "projectlombok" / "lombok"
    if m2_lombok.is_dir():
        version_dirs = [d for d in m2_lombok.iterdir() if d.is_dir()]
        for version_dir in sorted(version_dirs, key=lambda d: _version_key(d.name), reverse=True):
            jar = version_dir / f"lombok-{version_dir.name}.jar"
            if jar.is_file():
                return str(jar)

    # 4. Dedicated directory
    fallback = Path.home() / ".jdtls-libs" / "lombok.jar"
    if fallback.is_file():
        return str(fallback)

    return None


def _build_effective_params(
    init_params: dict[str, Any],
    module_root_uri: str | None,
    original_root: str,
    config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the ``initialize`` request params for jdtls.

    Deep-copies *init_params*, scopes to *module_root_uri* if provided,
    injects workspaceFolders capability, and injects Maven/Gradle settings
    from *config* (or defaults) into ``initializationOptions.settings``.
    """
    from pygls.uris import to_fs_path

    effective_params = copy.deepcopy(init_params)
    if module_root_uri:
        effective_params["rootUri"] = module_root_uri
        effective_params["rootPath"] = to_fs_path(module_root_uri)
        logger.info(
            "jdtls: scoping to module %s (full root: %s)",
            _redact_path(module_root_uri),
            _redact_path(original_root),
        )

    # Inject workspaceFolders capability for later expansion.
    caps = effective_params.setdefault("capabilities", {})
    ws = caps.setdefault("workspace", {})
    ws["workspaceFolders"] = True

    # Inject jdtls Maven/Gradle settings into initializationOptions.
    # Must be in initializationOptions (not didChangeConfiguration) so they
    # apply before the Maven import scan starts.
    jdtls_settings = copy.deepcopy(_DEFAULT_JDTLS_SETTINGS)
    if config and "jdtls" in config and "settings" in config["jdtls"]:
        jdtls_settings = copy.deepcopy(config["jdtls"]["settings"])
    init_opts = effective_params.setdefault("initializationOptions", {})
    init_opts["settings"] = jdtls_settings
    logger.debug("jdtls: initializationOptions.settings = %s", jdtls_settings)

    return effective_params


#: Forwarded jdtls log text is capped at this many characters (after sanitizing).
_JDTLS_LOG_MAX_CHARS = 2048
#: Raw text is pre-capped before redaction to bound regex cost on huge stack traces.
#: Far above ``_JDTLS_LOG_MAX_CHARS`` so a secret straddling the final cut is still redacted.
_JDTLS_LOG_PRECAP_CHARS = 16384
#: Token bucket per message kind: at most this many lines per window.
_JDTLS_LOG_RATE = 20
_JDTLS_LOG_WINDOW_SEC = 10.0
#: Bound on remembered message fingerprints used for in-window dedupe.
_JDTLS_LOG_DEDUPE_MAX = 512
#: Pom.xml diagnostics summary: messages shown per change, each capped at this length.
_POM_DIAG_SHOWN = 3
_POM_DIAG_MSG_MAX_CHARS = 300

#: LSP ``MessageType`` (window/logMessage) → proxy log level. Info/Log are DEBUG.
_LOG_MESSAGE_LEVELS: dict[int, int] = {1: logging.WARNING, 2: logging.INFO}

_URL_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s@]+@")
_SECRET_QUERY_RE = re.compile(
    r"([?&;][^=&;#\s]*(?:token|key|secret|password|auth|sig)[^=&;#\s]*=)[^&;#\s]*",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(r"(authorization\s*[:=]\s*)(?:(bearer|basic|token|digest)\s+)?[^\s,;]+", re.IGNORECASE)
_BEARER_RE = re.compile(r"\b(bearer)\s+[A-Za-z0-9._~+/=\-]+", re.IGNORECASE)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _auth_header_sub(match: re.Match[str]) -> str:
    scheme = match.group(2)
    return f"{match.group(1)}{scheme} ***" if scheme else f"{match.group(1)}***"


def _sanitize_jdtls_log(text: str, max_chars: int = _JDTLS_LOG_MAX_CHARS) -> str:
    """Return a log-safe single-line version of jdtls-provided *text*.

    Redacts URL userinfo (``scheme://***@``), secret-looking query parameters
    (``token``/``key``/``secret``/``password``/``auth``/``sig``), and
    ``Authorization``/``Bearer`` values; escapes CR/LF; strips every other
    C0/C1 control character (incl. ESC, so no terminal escape injection); and
    truncates to *max_chars*.  Redaction runs before truncation so a cut can
    never split a secret out of its matching context.
    """
    text = text[:_JDTLS_LOG_PRECAP_CHARS]
    text = _URL_USERINFO_RE.sub(r"\1***@", text)
    text = _SECRET_QUERY_RE.sub(r"\1***", text)
    text = _AUTH_HEADER_RE.sub(_auth_header_sub, text)
    text = _BEARER_RE.sub(r"\1 ***", text)
    text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", " ")
    text = _CONTROL_CHARS_RE.sub("", text)
    if len(text) > max_chars:
        text = text[:max_chars] + "...(truncated)"
    return text


class _JdtlsLogForwarder:
    """Forward jdtls log notifications to the proxy logger, sanitized and rate limited.

    Each message kind (e.g. ``logMessage:1``) has its own token bucket of
    ``_JDTLS_LOG_RATE`` lines per ``_JDTLS_LOG_WINDOW_SEC``.  A message identical
    to one logged within the window is dropped.  Dropped lines are counted and
    reported as one ``suppressed N jdtls log messages`` line once the window
    has rolled over.  The clock is injectable for tests.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # kind -> (tokens, last refill)
        self._recent: dict[tuple[str, str], float] = {}  # (kind, text) -> last logged at
        self._suppressed = 0
        self._report_at = clock()

    def forward(self, level: int, kind: str, raw: object) -> None:
        """Log *raw* under *kind* at *level* unless rate limited or duplicated."""
        if not logger.isEnabledFor(level):
            return
        now = self._clock()
        self._maybe_report(now)
        text = _sanitize_jdtls_log(raw if isinstance(raw, str) else str(raw))
        key = (kind, text)
        last = self._recent.get(key)
        if last is not None and now - last < _JDTLS_LOG_WINDOW_SEC:
            self._suppressed += 1
            return
        if not self._take_token(kind, now):
            self._suppressed += 1
            return
        if len(self._recent) >= _JDTLS_LOG_DEDUPE_MAX:
            self._recent = {k: t for k, t in self._recent.items() if now - t < _JDTLS_LOG_WINDOW_SEC}
            if len(self._recent) >= _JDTLS_LOG_DEDUPE_MAX:
                self._recent.clear()
        self._recent[key] = now
        logger.log(level, "jdtls[%s]: %s", kind, text)

    def _take_token(self, kind: str, now: float) -> bool:
        tokens, last = self._buckets.get(kind, (float(_JDTLS_LOG_RATE), now))
        tokens = min(float(_JDTLS_LOG_RATE), tokens + (now - last) * _JDTLS_LOG_RATE / _JDTLS_LOG_WINDOW_SEC)
        if tokens < 1.0:
            self._buckets[kind] = (tokens, now)
            return False
        self._buckets[kind] = (tokens - 1.0, now)
        return True

    def _maybe_report(self, now: float) -> None:
        if now - self._report_at < _JDTLS_LOG_WINDOW_SEC:
            return
        if self._suppressed:
            logger.info("jdtls: suppressed %d jdtls log messages", self._suppressed)
            self._suppressed = 0
        self._report_at = now

    def flush(self) -> None:
        """Report any pending suppressed count (called on stop)."""
        if self._suppressed:
            logger.info("jdtls: suppressed %d jdtls log messages", self._suppressed)
            self._suppressed = 0
        self._report_at = self._clock()


class JdtlsProxy:
    """Manages a jdtls subprocess and provides async request/notification forwarding."""

    def __init__(
        self,
        on_diagnostics: Callable[[str, list[Any]], None] | None = None,
        uri_key: Callable[[str], str] = lambda uri: uri,
        on_stopped: Callable[[], None] | None = None,
    ) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        # Keyed by uri_key(uri): jdtls may echo a URI in a different encoding than
        # the client sent, so lookups must go through the same canonical key.
        self._diagnostics_cache: dict[str, list[Any]] = {}
        self._uri_key = uri_key
        self._on_diagnostics = on_diagnostics
        self._on_stopped = on_stopped
        self._dropped_request_counts: dict[str, int] = {}
        # jdtls window/logMessage + language/status → proxy log (sanitized, rate limited).
        self._log_forwarder = _JdtlsLogForwarder()
        # uri_key(pom.xml URI) → Error diagnostic messages last seen (m2e import errors, #110).
        self._pom_errors: dict[str, tuple[str, ...]] = {}
        self._available = False
        self._jdtls_capabilities: dict[str, Any] = {}
        # Lazy-start state
        self._start_lock = asyncio.Lock()
        self._starting = False
        self._start_failed = False
        self._start_failed_at: float | None = None
        self._jdtls_on_path = False
        self._lazy_start_fired = False
        self._queued_notifications: deque[tuple[str, Any]] = deque(maxlen=_MAX_QUEUED_NOTIFICATIONS)
        self._original_root_uri: str | None = None
        self._initial_module_uri: str | None = None
        self.modules = ModuleRegistry()
        self.has_lombok = False
        self._expanded_groups: set[str] = set()
        # Merkle snapshot diff: module_uri → (diff | None, current_snapshot)
        # Populated by _kick_module_diff (background task on add_module_if_new).
        # Consumed by server._apply_module_diff after module reaches READY.
        self._module_diff_results: dict[str, tuple[TreeDiff | None, ModuleSnapshot]] = {}
        # Task references for in-flight diff computations, keyed by module_uri.
        # Allows _apply_module_diff to await the task directly if READY fires
        # before _kick_module_diff stores its result.
        self._pending_diff_tasks: dict[str, asyncio.Task[None]] = {}
        # Background tasks created by this proxy (prevents GC of fire-and-forget tasks).
        self._proxy_bg_tasks: set[asyncio.Task[Any]] = set()

    @property
    def is_available(self) -> bool:
        """Whether jdtls is running and responsive."""
        return self._available

    @property
    def capabilities(self) -> dict[str, Any]:
        """jdtls server capabilities from initialize response."""
        return self._jdtls_capabilities

    def get_cached_diagnostics(self, uri: str) -> list[Any]:
        """Get the latest jdtls diagnostics for a URI."""
        return list(self._diagnostics_cache.get(self._uri_key(uri), []))

    def _mark_stopped(self) -> None:
        """Drop state that belongs to the dead jdtls process and notify the owner."""
        if not self._available and not self._diagnostics_cache:
            return  # already handled (reader EOF, then stop())
        self._available = False
        cached = len(self._diagnostics_cache)
        self._diagnostics_cache.clear()
        self._pom_errors.clear()
        if cached:
            logger.info("jdtls stopped: cleared cached diagnostics for %d files", cached)
        if self._on_stopped:
            self._on_stopped()

    def check_available(self) -> bool:
        """Check if jdtls is on PATH (lightweight, no subprocess started)."""
        self._jdtls_on_path = shutil.which("jdtls") is not None
        if not self._jdtls_on_path:
            logger.warning("jdtls not found on PATH — running in standalone mode (custom rules only)")
        return self._jdtls_on_path

    async def start(
        self,
        init_params: dict[str, Any],
        *,
        module_root_uri: str | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> bool:
        """Start jdtls subprocess and initialize it.

        If *module_root_uri* is provided, jdtls is scoped to that module for
        fast startup and the data-dir hash is based on the module URI (so each
        module gets its own isolated index). Otherwise the workspace root is used.
        """
        jdtls_path = shutil.which("jdtls")
        if not jdtls_path:
            return False

        original_root: str = init_params.get("rootUri") or init_params.get("rootPath") or str(Path.cwd())
        self._original_root_uri = original_root
        effective_root_uri = module_root_uri or original_root

        # Mark ADDED before any await to prevent duplicate module additions
        # from concurrent coroutines during the executor yield below.
        self._initial_module_uri = module_root_uri
        self.modules.mark_added(effective_root_uri)

        # All blocking startup I/O in a single executor call: cache version check
        # (may rmtree on upgrade), Lombok discovery, and jdtls env build.
        cache_root = Path.home() / ".cache" / "jdtls-data"

        def _blocking_startup() -> tuple[dict[str, str], str | None]:
            _clear_cache_on_version_change(cache_root)
            max_workspaces = _parse_max_workspaces(config)
            _evict_lru_workspaces(cache_root, max_size=max_workspaces)
            return build_jdtls_env(), _find_lombok_jar(config)

        loop = asyncio.get_running_loop()
        jdtls_env, lombok_jar = await loop.run_in_executor(None, _blocking_startup)
        if lombok_jar:
            logger.info("jdtls: using Lombok agent from %s", _redact_path(lombok_jar))

        hash_source = module_root_uri or original_root
        data_hash = hashlib.sha256(hash_source.encode()).hexdigest()[:12]
        data_dir = cache_root / data_hash
        data_dir.mkdir(parents=True, exist_ok=True)

        effective_params = _build_effective_params(init_params, module_root_uri, original_root, config)

        jdtls_cmd: list[str] = [
            jdtls_path,
            "-data",
            str(data_dir),
            f"--jvm-arg=-Xmx{DEFAULT_JVM_MAX_HEAP}",
        ]
        if lombok_jar:
            jdtls_cmd.append(f"--jvm-arg=-javaagent:{lombok_jar}")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *jdtls_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=jdtls_env,
            )
            logger.info(
                "jdtls subprocess started (pid=%s, data=%s, JAVA_HOME=%s)",
                self._process.pid,
                data_dir,
                _redact_path(jdtls_env.get("JAVA_HOME")),
            )

            assert self._process.stdout is not None
            self._reader_task = asyncio.create_task(self._reader_loop(self._process.stdout))
            if self._process.stderr is not None:
                self._stderr_task = asyncio.create_task(self._stderr_reader(self._process.stderr))

            result = await self.send_request("initialize", effective_params, timeout=_INITIALIZE_TIMEOUT)
            if result is None:
                logger.error("jdtls initialize request failed or timed out")
                await self.stop()
                await asyncio.get_running_loop().run_in_executor(None, _wipe_data_dir, data_dir)
                return False

            self._jdtls_capabilities = result.get("capabilities", {})
            logger.info("jdtls initialized (capabilities: %s)", list(self._jdtls_capabilities.keys()))
            server_info = result.get("serverInfo") or {}
            logger.info("jdtls server: %s %s", server_info.get("name", "?"), server_info.get("version", "?"))

            await self.send_notification("initialized", {})
            self._available = True
            self.has_lombok = lombok_jar is not None
            return True

        except (OSError, FileNotFoundError) as e:
            logger.error("Failed to start jdtls: %s", e)
            return False

    async def ensure_started(
        self,
        init_params: dict[str, Any],
        file_uri: str,
        config: Mapping[str, Any] | None = None,
    ) -> bool:
        """Start jdtls lazily, scoped to the module containing *file_uri*.

        Thread-safe: uses asyncio.Lock to prevent double-start from rapid
        didOpen calls. Sets ``_start_failed`` on failure to prevent retries,
        but allows retry after a cooldown period (5 minutes) so transient
        failures (Maven Central timeout, JVM OOM) don't permanently disable jdtls.
        """
        if self._available:
            return True
        if not self._jdtls_on_path:
            return False
        if self._start_failed:
            if self._start_failed_at and (time.monotonic() - self._start_failed_at > _START_RETRY_COOLDOWN):
                self._start_failed = False
                self._start_failed_at = None
                logger.info("jdtls: retrying after previous failure (cooldown elapsed)")
            else:
                return False

        async with self._start_lock:
            if self._available:
                return True

            self._starting = True
            try:
                module_uri = _resolve_module_uri(file_uri)
                started = await self.start(init_params, module_root_uri=module_uri, config=config)
                if not started:
                    self._start_failed = True
                    self._start_failed_at = time.monotonic()
                    self._queued_notifications.clear()
                return started
            except Exception:
                self._start_failed = True
                self._start_failed_at = time.monotonic()
                self._queued_notifications.clear()
                raise
            finally:
                self._starting = False

    def queue_notification(self, method: str, params: Any) -> None:
        """Buffer a notification for replay after jdtls starts.

        Uses a ``deque(maxlen=200)`` so oldest entries are dropped in O(1)
        when the queue overflows during long jdtls startup.
        """
        if len(self._queued_notifications) >= _MAX_QUEUED_NOTIFICATIONS:
            logger.warning(
                "jdtls: notification queue full (%d cap) — oldest entry dropped; some didOpen events may be missed",
                _MAX_QUEUED_NOTIFICATIONS,
            )
        self._queued_notifications.append((method, params))

    async def flush_queued_notifications(self) -> None:
        """Send all queued notifications to jdtls."""
        queue = list(self._queued_notifications)
        self._queued_notifications.clear()
        for method, params in queue:
            await self.send_notification(method, params)

    async def _kick_module_diff(self, module_uri: str) -> None:
        """Background task: compute snapshot diff and stash it for later use.

        Result is stored in ``_module_diff_results`` BEFORE the task is removed
        from ``_pending_diff_tasks`` to eliminate the TOCTOU window where
        ``await_module_diff`` could find neither a running task nor a result.
        """
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _compute_module_diff, module_uri)
        # Store result first, then remove from pending — preserves invariant that
        # once the task is gone from _pending_diff_tasks the result is already readable.
        if result is not None:
            self._module_diff_results[module_uri] = result
        self._pending_diff_tasks.pop(module_uri, None)

    def pop_module_data(self, module_uri: str) -> tuple[TreeDiff | None, ModuleSnapshot] | None:
        """Pop and return stashed ``(diff, snapshot)`` for *module_uri*, or ``None``."""
        return self._module_diff_results.pop(module_uri, None)

    async def await_module_diff(self, module_uri: str) -> None:
        """Wait for the in-flight diff task for *module_uri* to complete, if any.

        Called when the READY signal fires before ``_kick_module_diff`` has
        stored its result — awaiting the task directly avoids a time-based
        sleep and ensures the diff is never silently dropped.

        Swallows ``CancelledError`` so that a ``stop()`` call during the wait
        does not propagate and crash the caller.
        """
        task = self._pending_diff_tasks.pop(module_uri, None)
        if task is not None and not task.done():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass  # result storage is _kick_module_diff's responsibility

    async def add_module_if_new(self, file_uri: str) -> str | None:
        """Add the module's Maven group to jdtls if not already expanded.

        When the user navigates to a file in a new Maven group, we add the
        group root (not the individual module) as a workspace folder.  This
        gives jdtls the full Maven classpath for cross-module navigation
        within that group.

        Returns the module URI if a new group was expanded (for use with
        ``modules.wait_until_ready()``), or ``None`` if already covered.

        Capped at ``_MAX_EXPANDED_GROUPS`` to prevent jdtls OOM.
        """
        if not self._available:
            return None
        from pygls.uris import from_fs_path, to_fs_path

        module_uri = _resolve_module_uri(file_uri)
        if module_uri is None or self.modules.was_added(module_uri):
            return None

        # Resolve the Maven group root for this module.
        module_path = to_fs_path(module_uri)
        workspace_path = to_fs_path(self._original_root_uri) if self._original_root_uri else None
        if module_path and workspace_path:
            group_root = _find_maven_group_root(Path(module_path), Path(workspace_path))
            group_uri = from_fs_path(str(group_root)) or module_uri
            logger.debug(
                "jdtls: group scope for %s: boundary %s, group root %s",
                _redact_path(module_path),
                _redact_path(str(_find_repo_boundary(Path(module_path).resolve()))),
                _redact_path(str(group_root)),
            )
        else:
            group_uri = module_uri

        # Already expanded this group — module is covered, nothing to do.
        if group_uri in self._expanded_groups:
            return None

        # Hard cap: refuse expansion beyond the limit.
        if len(self._expanded_groups) >= _MAX_EXPANDED_GROUPS:
            logger.warning(
                "jdtls: refusing to expand group for %s — %d groups already expanded (max %d). Restart IDE to reset.",
                Path(module_path or module_uri).name,
                len(self._expanded_groups),
                _MAX_EXPANDED_GROUPS,
            )
            return None

        # Remove individually-added child modules within this group to avoid
        # double-indexing once the group root covers them.  Must run BEFORE
        # mark_added so we don't remove the module we're about to add.
        group_path = Path(to_fs_path(group_uri) or group_uri)
        removed: list[dict[str, str]] = []
        for uri in self.modules.uris():
            if uri == group_uri:
                continue
            p = Path(to_fs_path(uri) or uri)
            if p == group_path or p.is_relative_to(group_path):
                removed.append({"uri": uri, "name": p.name})

        # Mark ADDED before await — atomic in asyncio, prevents duplicate sends.
        self.modules.mark_added(module_uri)
        self.modules.mark_added(group_uri)
        self._expanded_groups.add(group_uri)

        group_name = Path(to_fs_path(group_uri) or group_uri).name
        logger.info(
            "jdtls: expanding to group %s (group %d/%d)", group_name, len(self._expanded_groups), _MAX_EXPANDED_GROUPS
        )
        await self.send_notification(
            _WORKSPACE_DID_CHANGE_FOLDERS,
            {"event": {"added": [{"uri": group_uri, "name": group_name}], "removed": removed}},
        )

        # Fire-and-forget: compute snapshot diff in background.
        diff_task = asyncio.create_task(self._kick_module_diff(module_uri))
        self._proxy_bg_tasks.add(diff_task)
        diff_task.add_done_callback(self._proxy_bg_tasks.discard)
        self._pending_diff_tasks[module_uri] = diff_task
        return module_uri

    async def expand_full_workspace(self) -> None:
        """Expand jdtls workspace to the initial module's Maven group root.

        Called once during lazy start.  Subsequent groups are expanded
        on-demand by ``add_module_if_new()`` as the user navigates to
        files in other Maven groups.

        Removes all individually-added module folders to avoid double-indexing
        with the newly added group root.
        """
        if not self._available or not self._original_root_uri:
            return
        from pygls.uris import from_fs_path, to_fs_path

        workspace_path = to_fs_path(self._original_root_uri) or self._original_root_uri

        # Find the tightest multi-module Maven parent of the initial module.
        group_root_path: str = workspace_path
        if self._initial_module_uri:
            initial_path = to_fs_path(self._initial_module_uri) or self._initial_module_uri
            group_root_path = str(_find_maven_group_root(Path(initial_path), Path(workspace_path)))
            logger.info(
                "jdtls: group scope: client root %s, repository boundary %s, group root %s",
                _redact_path(workspace_path),
                _redact_path(str(_find_repo_boundary(Path(initial_path).resolve()))),
                _redact_path(group_root_path),
            )

        root_path = group_root_path
        root_uri = from_fs_path(root_path) or self._original_root_uri

        # Already expanded (e.g. initial module IS the group root, or called twice).
        if root_uri in self._expanded_groups or self.modules.was_added(root_uri):
            self._expanded_groups.add(root_uri)
            return
        self.modules.mark_added(root_uri)

        # Remove individually-added modules that are children of the new root.
        # This covers the initial scoped module and any modules added by
        # add_module_if_new during the ensure_started window.
        root_path_obj = Path(root_path)
        removed: list[dict[str, str]] = []
        for uri in self.modules.uris():
            if uri == root_uri:
                continue
            p = Path(to_fs_path(uri) or uri)
            if p == root_path_obj or p.is_relative_to(root_path_obj):
                removed.append({"uri": uri, "name": p.name})

        logger.info("jdtls: expanding workspace to module group %s", _redact_path(root_path))
        await self.send_notification(
            _WORKSPACE_DID_CHANGE_FOLDERS,
            {"event": {"added": [{"uri": root_uri, "name": Path(root_path).name}], "removed": removed}},
        )
        self._expanded_groups.add(root_uri)

    async def stop(self) -> None:
        """Shutdown jdtls subprocess gracefully."""
        self._mark_stopped()
        self._log_forwarder.flush()

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()

        for task in list(self._proxy_bg_tasks):
            task.cancel()
        self._proxy_bg_tasks.clear()
        self._module_diff_results.clear()
        self._pending_diff_tasks.clear()

        if self._process and self._process.returncode is None:
            try:
                await self.send_request("shutdown", None, timeout=5.0)
                await self.send_notification("exit", None)
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, OSError):
                self._process.kill()
                await self._process.wait()

        # Cancel all pending requests
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    async def send_request(self, method: str, params: Any, timeout: float = REQUEST_TIMEOUT) -> Any | None:
        """Send a JSON-RPC request and wait for the response."""
        if not self._process or self._process.stdin is None:
            return None

        request_id = self._next_id
        self._next_id += 1

        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params

        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future

        try:
            self._process.stdin.write(encode_message(msg))
            await self._process.stdin.drain()
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning("jdtls request %s timed out after %.1fs", method, timeout)
            self._pending.pop(request_id, None)
            return None
        except (OSError, ConnectionError) as e:
            logger.error("jdtls communication error on %s: %s", method, e)
            self._pending.pop(request_id, None)
            self._available = False
            return None

    async def send_notification(self, method: str, params: Any) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self._process or self._process.stdin is None:
            return

        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            msg["params"] = params

        try:
            self._process.stdin.write(encode_message(msg))
            await self._process.stdin.drain()
        except (OSError, ConnectionError) as e:
            logger.error("jdtls notification error on %s: %s", method, e)
            self._available = False

    async def _reader_loop(self, reader: asyncio.StreamReader) -> None:
        """Background task: read jdtls stdout and dispatch messages."""
        try:
            while True:
                msg = await read_message(reader)
                if msg is None:
                    logger.warning("jdtls stdout closed — subprocess may have exited")
                    self._mark_stopped()
                    break

                self._dispatch_message(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("jdtls reader loop error: %s", e)
            self._mark_stopped()

    async def _stderr_reader(self, stderr: asyncio.StreamReader) -> None:
        """Background task: log jdtls stderr output for debugging.

        Uses read() with a fixed buffer instead of readline() to avoid
        asyncio.LimitOverrunError on long lines (>64KB), which would kill
        the task and deadlock jdtls when the stderr pipe buffer fills.
        """
        try:
            while True:
                chunk = await stderr.read(8192)
                if not chunk:
                    break
                for line in chunk.decode("utf-8", errors="replace").splitlines():
                    text = line.rstrip()
                    if not text:
                        continue
                    # Truncate long lines to avoid flooding logs
                    if len(text) > _STDERR_LINE_MAX:
                        text = text[:_STDERR_LINE_MAX] + "..."
                    if "SEVERE" in text or "ERROR" in text or "Exception" in text:
                        logger.error("jdtls stderr: %s", text)
                    else:
                        logger.debug("jdtls stderr: %s", text)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("jdtls stderr reader error: %s", e)

    def _dispatch_message(self, msg: dict[str, Any]) -> None:
        """Route a message from jdtls to the appropriate handler."""
        if "id" in msg and "method" not in msg:
            # Response to a request we sent
            request_id = msg["id"]
            future = self._pending.pop(request_id, None)
            if future and not future.done():
                if "error" in msg:
                    logger.warning("jdtls error response (id=%s): %s", request_id, msg["error"])
                    future.set_result(None)
                else:
                    future.set_result(msg.get("result"))
        elif "method" in msg and "id" not in msg:
            # Notification from jdtls
            self._handle_notification(msg)
        elif "method" in msg:
            self._note_dropped_request(str(msg["method"]))

    def _note_dropped_request(self, method: str) -> None:
        """Count jdtls→client requests the proxy leaves unanswered (method names only, never params)."""
        method = method[:100]
        if method not in self._dropped_request_counts and len(self._dropped_request_counts) >= _MAX_DROPPED_METHODS:
            method = "<other>"
        count = self._dropped_request_counts.get(method, 0) + 1
        self._dropped_request_counts[method] = count
        if count == 1:
            logger.info("jdtls request %r dropped (no client-side handler)", method)
        else:
            logger.debug("jdtls request %r dropped (%d times)", method, count)

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        """Handle a notification from jdtls."""
        method = msg.get("method", "")
        params = msg.get("params", {})

        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri", "")
            diagnostics = params.get("diagnostics", [])
            self._diagnostics_cache[self._uri_key(uri)] = diagnostics
            if self._on_diagnostics:
                self._on_diagnostics(uri, diagnostics)
            if isinstance(uri, str) and uri.endswith("/pom.xml"):
                self._note_pom_diagnostics(uri, diagnostics)
        elif method == "window/logMessage" and isinstance(params, dict):
            msg_type = params.get("type")
            level = _LOG_MESSAGE_LEVELS.get(msg_type, logging.DEBUG) if isinstance(msg_type, int) else logging.DEBUG
            self._log_forwarder.forward(level, f"log:{msg_type}", params.get("message", ""))
        elif method == "language/status" and isinstance(params, dict):
            status_type = params.get("type")
            level = logging.INFO if status_type == "Error" else logging.DEBUG
            self._log_forwarder.forward(level, f"status:{str(status_type)[:32]}", params.get("message", ""))
        # Other notifications are silently ignored

    def _note_pom_diagnostics(self, uri: str, diagnostics: Any) -> None:
        """Log a one-line summary whenever the Error diagnostics set of a pom.xml changes.

        m2e reports import failures (e.g. ``Missing artifact g:a:t:v``) only as
        diagnostics on the module's pom.xml, which the server never publishes
        (it filters to ``.java``).  Logging them here makes the cause of false
        "cannot be resolved" errors visible (#110).  Does not alter what is
        published to the client.
        """
        errors: tuple[str, ...] = ()
        if isinstance(diagnostics, list):
            errors = tuple(
                str(d.get("message", ""))
                for d in diagnostics
                if isinstance(d, dict) and d.get("severity") == 1  # DiagnosticSeverity.Error
            )
        key = self._uri_key(uri)
        if self._pom_errors.get(key, ()) == errors:
            return
        if errors:
            self._pom_errors[key] = errors
        else:
            self._pom_errors.pop(key, None)
        shown = "; ".join(_sanitize_jdtls_log(m, _POM_DIAG_MSG_MAX_CHARS) for m in errors[:_POM_DIAG_SHOWN])
        more = f" (+{len(errors) - _POM_DIAG_SHOWN} more)" if len(errors) > _POM_DIAG_SHOWN else ""
        logger.info(
            "jdtls: pom.xml errors changed for %s: %d error(s)%s%s",
            self._pom_display_path(uri),
            len(errors),
            f": {shown}" if shown else "",
            more,
        )

    def _pom_display_path(self, uri: str) -> str:
        """Return *uri*'s path relative to the client root, else a redacted basename."""
        from pygls.uris import to_fs_path

        path = to_fs_path(uri)
        root = to_fs_path(self._original_root_uri) if self._original_root_uri else None
        if path and root:
            try:
                return _sanitize_jdtls_log(str(Path(path).relative_to(root)), _POM_DIAG_MSG_MAX_CHARS)
            except ValueError:
                pass
        return _sanitize_jdtls_log(_redact_path(path), _POM_DIAG_MSG_MAX_CHARS)
