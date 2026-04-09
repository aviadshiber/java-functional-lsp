"""jdtls proxy — manages a jdtls subprocess and forwards LSP messages via JSON-RPC over stdio."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0  # seconds
DEFAULT_JVM_MAX_HEAP = "4g"
_STDERR_LINE_MAX = 1000
_MIN_JDTLS_JAVA_MAJOR = 21
_JAVA_VERSION_PATTERN = re.compile(r'version\s"(\d+)(?:\.\d+\.\d+(?:_\d+)?)?(?:-[^"]*)?"')
_JAVA_VERSION_CHECK_TIMEOUT_SEC = 5


def _read_java_major_version(java_executable: str) -> int | None:
    """Return the major version of ``java_executable``, or None if unreadable.

    Runs ``java -version`` and parses the first ``"<major>..."`` token.
    Returns None on any subprocess or parse failure so callers can keep
    probing alternative candidates without raising.
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
    """Return True if ``java_home/bin/java`` exists and is Java 21 or later."""
    candidate = Path(java_home) / "bin" / "java"
    if not candidate.is_file():
        return False
    major = _read_java_major_version(str(candidate))
    return major is not None and major >= _MIN_JDTLS_JAVA_MAJOR


def find_jdtls_java_home(environ: dict[str, str] | None = None) -> str | None:
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

    # 1. Explicit override takes precedence over everything.
    override = env.get("JDTLS_JAVA_HOME")
    if override and _java_home_has_suitable_version(override):
        return override

    # 2. Current JAVA_HOME if it's already Java 21+.
    existing = env.get("JAVA_HOME")
    if existing and _java_home_has_suitable_version(existing):
        return existing

    # 3. macOS: ask the OS for a Java 21+ install.
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
        if out and Path(out).is_dir() and _java_home_has_suitable_version(out):
            return out

    # 4. java on PATH — derive JAVA_HOME as <java>/../.. if the version is new enough.
    java_on_path = shutil.which("java")
    if java_on_path is not None:
        major = _read_java_major_version(java_on_path)
        if major is not None and major >= _MIN_JDTLS_JAVA_MAJOR:
            # Resolve symlinks first so the parent walk yields the real Home directory.
            resolved = Path(java_on_path).resolve()
            derived_home = resolved.parent.parent
            if (derived_home / "bin" / "java").is_file():
                return str(derived_home)

    return None


def build_jdtls_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Build the environment for the jdtls subprocess.

    If a Java 21+ installation is found, sets ``JAVA_HOME`` to its path.
    Otherwise strips ``JAVA_HOME`` from the inherited env so the jdtls
    launcher falls back to its bundled default — this handles the common
    case where an IDE launches the LSP with ``JAVA_HOME`` inherited from
    the project SDK (which may be Java 8/11/17 and too old for jdtls).

    Pass ``environ`` to override the base environment (used by tests);
    when None, starts from ``os.environ``.
    """
    base = dict(environ) if environ is not None else os.environ.copy()
    suitable = find_jdtls_java_home(base)
    if suitable is not None:
        base["JAVA_HOME"] = suitable
        logger.info("jdtls: using JAVA_HOME=%s", suitable)
        return base

    stripped = base.pop("JAVA_HOME", None)
    if stripped is not None:
        logger.warning(
            "jdtls: no Java %d+ found (JAVA_HOME was %s); stripping JAVA_HOME so jdtls can use its bundled fallback",
            _MIN_JDTLS_JAVA_MAJOR,
            stripped,
        )
    return base


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

    async def start(self, init_params: dict[str, Any]) -> bool:
        """Start jdtls subprocess and initialize it."""
        jdtls_path = shutil.which("jdtls")
        if not jdtls_path:
            logger.warning("jdtls not found on PATH — running in standalone mode (custom rules only)")
            return False

        # jdtls requires a -data directory for workspace metadata (index, classpath, build state).
        # Use ~/.cache/jdtls-data/<hash> so it persists across reboots and LSP restarts.
        # Fallback order mirrors LSP spec: rootUri → rootPath → cwd.
        root_uri = init_params.get("rootUri") or init_params.get("rootPath") or str(Path.cwd())
        workspace_hash = hashlib.sha256(root_uri.encode()).hexdigest()[:12]
        data_dir = Path.home() / ".cache" / "jdtls-data" / workspace_hash
        data_dir.mkdir(parents=True, exist_ok=True)

        # Build a clean environment for jdtls: detect Java 21+ and set JAVA_HOME
        # explicitly, or strip JAVA_HOME if the inherited value points at an older
        # Java (e.g. an IDE launched us with a project SDK of Java 8). Without this,
        # jdtls 1.57+ fails with "jdtls requires at least Java 21" during its
        # Python launcher's version check.
        jdtls_env = build_jdtls_env()

        try:
            self._process = await asyncio.create_subprocess_exec(
                jdtls_path,
                "-data",
                str(data_dir),
                f"--jvm-arg=-Xmx{DEFAULT_JVM_MAX_HEAP}",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=jdtls_env,
            )
            logger.info(
                "jdtls subprocess started (pid=%s, data=%s, JAVA_HOME=%s)",
                self._process.pid,
                data_dir,
                jdtls_env.get("JAVA_HOME", "<unset>"),
            )

            # Start background readers for stdout (JSON-RPC) and stderr (diagnostics/errors)
            assert self._process.stdout is not None
            self._reader_task = asyncio.create_task(self._reader_loop(self._process.stdout))
            if self._process.stderr is not None:
                self._stderr_task = asyncio.create_task(self._stderr_reader(self._process.stderr))

            # Send initialize request
            result = await self.send_request("initialize", init_params)
            if result is None:
                logger.error("jdtls initialize request failed or timed out")
                await self.stop()
                return False

            self._jdtls_capabilities = result.get("capabilities", {})
            logger.info("jdtls initialized (capabilities: %s)", list(self._jdtls_capabilities.keys()))

            # Send initialized notification
            await self.send_notification("initialized", {})
            self._available = True
            return True

        except (OSError, FileNotFoundError) as e:
            logger.error("Failed to start jdtls: %s", e)
            return False

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
