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

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0  # seconds — per-request timeout for normal operations
_INITIALIZE_TIMEOUT = 120.0  # seconds — module-scoped init can still be slow (Maven classpath resolution)
_START_RETRY_COOLDOWN = 300.0  # seconds — retry jdtls startup after transient failure
DEFAULT_JVM_MAX_HEAP = "4g"
_STDERR_LINE_MAX = 1000

#: Minimum Java major version required by jdtls 1.57+. Anything below this
#: is rejected by the jdtls Python launcher with ``"jdtls requires at least
#: Java 21"`` before the server even starts.
_MIN_JDTLS_JAVA_MAJOR = 21

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


_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")
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
    return module_uri or None


def _version_key(name: str) -> tuple[int, ...]:
    """Parse a version string into a tuple for semantic comparison."""
    parts: list[int] = []
    for segment in re.split(r"[.\-]", name):
        try:
            parts.append(int(segment))
        except ValueError:
            break
    return tuple(parts)


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


class JdtlsProxy:
    """Manages a jdtls subprocess and provides async request/notification forwarding."""

    def __init__(self, on_diagnostics: Callable[[str, list[Any]], None] | None = None) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._diagnostics_cache: dict[str, list[Any]] = {}
        self._on_diagnostics = on_diagnostics
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
        self._workspace_expanded = False

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
        return list(self._diagnostics_cache.get(uri, []))

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

        # Discover Lombok jar and build jdtls env BEFORE computing the data-dir
        # hash, so that adding/changing the Lombok agent invalidates stale indexes.
        loop = asyncio.get_running_loop()
        jdtls_env, lombok_jar = await loop.run_in_executor(None, lambda: (build_jdtls_env(), _find_lombok_jar(config)))
        if lombok_jar:
            logger.info("jdtls: using Lombok agent from %s", _redact_path(lombok_jar))

        # Data-dir hash: includes module URI + Lombok jar path so that changing
        # either gives jdtls a fresh index (avoids stale caches that cause
        # AssertionFailedExceptions and silently break annotation processing).
        hash_source = module_root_uri or original_root
        if lombok_jar:
            hash_source += f"|lombok={lombok_jar}"
        data_hash = hashlib.sha256(hash_source.encode()).hexdigest()[:12]
        data_dir = Path.home() / ".cache" / "jdtls-data" / data_hash
        data_dir.mkdir(parents=True, exist_ok=True)

        # Deep copy to avoid mutating server._init_params.
        effective_params = copy.deepcopy(init_params)
        effective_root_uri = module_root_uri or original_root
        if module_root_uri:
            effective_params["rootUri"] = module_root_uri
            from pygls.uris import to_fs_path

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

        # Track the initial module as already loaded (mark ADDED before await).
        self._initial_module_uri = module_root_uri
        self.modules.mark_added(effective_root_uri)

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
                return False

            self._jdtls_capabilities = result.get("capabilities", {})
            logger.info("jdtls initialized (capabilities: %s)", list(self._jdtls_capabilities.keys()))

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
        self._queued_notifications.append((method, params))

    async def flush_queued_notifications(self) -> None:
        """Send all queued notifications to jdtls."""
        queue = list(self._queued_notifications)
        self._queued_notifications.clear()
        for method, params in queue:
            await self.send_notification(method, params)

    async def add_module_if_new(self, file_uri: str) -> str | None:
        """Add the module containing *file_uri* to jdtls if not already added.

        Returns the module URI if a new module was added (UNKNOWN → ADDED),
        or ``None`` if already known or unavailable. The returned URI can be
        used with ``modules.wait_until_ready()`` for adaptive waiting.

        Calls ``modules.mark_added()`` before any ``await`` to prevent
        duplicate add calls from concurrent coroutines.
        """
        if not self._available:
            return None
        module_uri = _resolve_module_uri(file_uri)
        if module_uri is None or self.modules.was_added(module_uri):
            return None
        # Mark ADDED before await — atomic in asyncio, prevents duplicate sends.
        self.modules.mark_added(module_uri)
        from pygls.uris import to_fs_path

        logger.info("jdtls: adding module %s", _redact_path(to_fs_path(module_uri)))
        mod_name = Path(to_fs_path(module_uri) or module_uri).name
        await self.send_notification(
            _WORKSPACE_DID_CHANGE_FOLDERS,
            {"event": {"added": [{"uri": module_uri, "name": mod_name}], "removed": []}},
        )
        return module_uri

    async def expand_full_workspace(self) -> None:
        """Expand jdtls workspace to the full monorepo root (background task).

        Removes the initial module-scoped folder and adds the full root to
        avoid double-indexing.
        """
        if self._workspace_expanded or not self._available or not self._original_root_uri:
            return
        from pygls.uris import from_fs_path, to_fs_path

        root_path = to_fs_path(self._original_root_uri) or self._original_root_uri
        root_uri = from_fs_path(root_path) or self._original_root_uri
        if self.modules.was_added(root_uri):
            self._workspace_expanded = True
            return
        self.modules.mark_added(root_uri)

        # Remove initial module folder to avoid double-indexing.
        removed: list[dict[str, str]] = []
        if self._initial_module_uri and self._initial_module_uri != root_uri:
            ini_path = to_fs_path(self._initial_module_uri) or self._initial_module_uri
            removed.append({"uri": self._initial_module_uri, "name": Path(ini_path).name})

        logger.info("jdtls: expanding to full workspace %s", _redact_path(root_path))
        await self.send_notification(
            _WORKSPACE_DID_CHANGE_FOLDERS,
            {"event": {"added": [{"uri": root_uri, "name": Path(root_path).name}], "removed": removed}},
        )
        self._workspace_expanded = True

    async def stop(self) -> None:
        """Shutdown jdtls subprocess gracefully."""
        self._available = False

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()

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
                    self._available = False
                    break

                self._dispatch_message(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("jdtls reader loop error: %s", e)
            self._available = False

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

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        """Handle a notification from jdtls."""
        method = msg.get("method", "")
        params = msg.get("params", {})

        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri", "")
            diagnostics = params.get("diagnostics", [])
            self._diagnostics_cache[uri] = diagnostics
            if self._on_diagnostics:
                self._on_diagnostics(uri, diagnostics)
        # Other notifications (window/logMessage, etc.) are silently ignored
