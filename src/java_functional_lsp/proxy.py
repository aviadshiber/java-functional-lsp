"""jdtls proxy — manages a jdtls subprocess and forwards LSP messages via JSON-RPC over stdio."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0  # seconds


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

        try:
            self._process = await asyncio.create_subprocess_exec(
                jdtls_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("jdtls subprocess started (pid=%s)", self._process.pid)

            # Start background reader
            assert self._process.stdout is not None
            self._reader_task = asyncio.create_task(self._reader_loop(self._process.stdout))

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
