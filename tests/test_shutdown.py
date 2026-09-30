"""Tests for the bounded shutdown path and the v0.14.1 proxy plumbing (#107, #110).

``JdtlsProxy.stop()`` is driven against real child processes standing in for jdtls: one that
answers ``shutdown`` and exits on ``exit``, one that ignores both (SIGTERM ends it), and one
that also ignores SIGTERM (SIGKILL ends it). Each must finish within ~3 s.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from lsprotocol import types as lsp
from pygls.workspace import Workspace

from java_functional_lsp import server as server_mod
from java_functional_lsp.freshness_marker import content_digest
from java_functional_lsp.proxy import JdtlsProxy, _build_effective_params, _data_dir_hash

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

_FAKE_JDTLS = r"""
import json, sys
def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        k, _, v = line.decode().partition(":")
        headers[k.strip().lower()] = v.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
while True:
    msg = read()
    if msg is None or msg.get("method") == "exit":
        sys.exit(0)
    if msg.get("method") in ("shutdown", "error/please"):
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": None}
        if msg["method"] == "error/please":
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": "nope"}}
        body = json.dumps(reply).encode()
        sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        sys.stdout.buffer.flush()
"""
_DEAF = "import time; time.sleep(60)"
_STUBBORN = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"


async def _proxy_with(script: str) -> tuple[JdtlsProxy, asyncio.subprocess.Process]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
    )
    proxy = JdtlsProxy()
    proxy._process = proc
    assert proc.stdout is not None
    proxy._reader_task = asyncio.create_task(proxy._reader_loop(proc.stdout))
    proxy._available = True
    await asyncio.sleep(0.3)  # let the interpreter start (and install its SIGTERM handler)
    return proxy, proc


class TestBoundedStop:
    @pytest.mark.timeout(20)
    async def test_cooperative_jdtls_answers_shutdown_and_exits(self, caplog: pytest.LogCaptureFixture) -> None:
        proxy, proc = await _proxy_with(_FAKE_JDTLS)
        started = time.monotonic()
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.proxy"):
            await proxy.stop()
        assert time.monotonic() - started < 1.0
        assert proc.returncode == 0
        assert "timed out" not in caplog.text  # the shutdown reply was read before the reader was cancelled
        assert not proxy.is_available

    @pytest.mark.timeout(20)
    async def test_deaf_jdtls_gets_sigterm(self) -> None:
        proxy, proc = await _proxy_with(_DEAF)
        started = time.monotonic()
        await proxy.stop()
        assert time.monotonic() - started < 3.5
        assert proc.returncode == -signal.SIGTERM

    @pytest.mark.timeout(20)
    async def test_stubborn_jdtls_gets_sigkill_within_three_seconds(self) -> None:
        proxy, proc = await _proxy_with(_STUBBORN)
        started = time.monotonic()
        await proxy.stop()
        elapsed = time.monotonic() - started
        assert proc.returncode == -signal.SIGKILL
        assert 1.5 < elapsed < 3.5

    @pytest.mark.timeout(20)
    async def test_stop_is_idempotent(self) -> None:
        proxy, proc = await _proxy_with(_DEAF)
        await asyncio.gather(proxy.stop(), proxy.stop())
        await proxy.stop()
        assert proc.returncode is not None


class TestServerShutdown:
    @pytest.fixture(autouse=True)
    def _exits(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        codes: list[int] = []
        monkeypatch.setattr(server_mod, "_hard_exit", codes.append)
        monkeypatch.setattr(server_mod, "_watchdog_exit", codes.append)
        self.codes = codes
        return codes

    async def test_exit_stops_jdtls_then_hard_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stop = AsyncMock()
        monkeypatch.setattr(server_mod.server._proxy, "stop", stop)
        monkeypatch.setattr(server_mod, "_process_mode", True)
        monkeypatch.setattr(server_mod.server.protocol, "_shutdown", True, raising=False)
        await server_mod.on_exit(None)
        stop.assert_awaited_once()
        assert self.codes == [0]

    async def test_exit_without_shutdown_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server_mod.server._proxy, "stop", AsyncMock())
        monkeypatch.setattr(server_mod, "_process_mode", True)
        monkeypatch.setattr(server_mod.server.protocol, "_shutdown", False, raising=False)
        await server_mod.on_exit(None)
        assert self.codes == [1]

    async def test_exit_is_bounded_when_stop_hangs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def hang() -> None:
            await asyncio.sleep(60)

        monkeypatch.setattr(server_mod.server._proxy, "stop", hang)
        monkeypatch.setattr(server_mod, "_process_mode", True)
        monkeypatch.setattr(server_mod, "_STOP_TIMEOUT_SEC", 0.05)
        started = time.monotonic()
        await server_mod.on_exit(None)
        assert time.monotonic() - started < 1.0
        assert len(self.codes) == 1

    async def test_in_process_exit_never_hard_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server_mod.server._proxy, "stop", AsyncMock())
        monkeypatch.setattr(server_mod, "_process_mode", False)
        await server_mod.on_exit(None)
        assert self.codes == []

    async def test_shutdown_stops_jdtls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stop = AsyncMock()
        monkeypatch.setattr(server_mod.server._proxy, "stop", stop)
        await server_mod.on_shutdown(None)
        stop.assert_awaited_once()
        assert self.codes == []

    async def test_first_signal_stops_then_exits_second_exits_at_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        timers: list[tuple[float, Any]] = []
        timers_fns: list[Any] = []

        class FakeTimer:
            daemon = False

            def __init__(self, interval: float, fn: Any, args: tuple[Any, ...]) -> None:
                timers.append((interval, args))
                timers_fns.append(fn)

            def start(self) -> None:
                pass

        monkeypatch.setattr(server_mod.threading, "Timer", FakeTimer)
        monkeypatch.setattr(server_mod, "_signals_received", 0)
        stop = AsyncMock()
        monkeypatch.setattr(server_mod.server._proxy, "stop", stop)
        server_mod._on_signal(signal.SIGTERM)
        assert timers == [(server_mod._SIGNAL_WATCHDOG_SEC, (143,))]  # the hard 5 s watchdog
        assert timers_fns == [server_mod._watchdog_exit]  # never logging.shutdown() from the watchdog
        await asyncio.sleep(0.05)
        stop.assert_awaited_once()
        assert self.codes == [143]
        server_mod._on_signal(signal.SIGTERM)
        assert self.codes == [143, 143]

    async def test_signal_handlers_are_registered_in_the_running_loop(self) -> None:
        loop = asyncio.get_running_loop()
        server_mod._install_signal_handlers()
        try:
            assert loop.remove_signal_handler(signal.SIGTERM)
            assert loop.remove_signal_handler(signal.SIGHUP)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            loop.remove_signal_handler(signal.SIGHUP)

    def test_signal_handlers_need_a_running_loop(self) -> None:
        server_mod._install_signal_handlers()  # no loop: logged at DEBUG, never raises


class TestNonAgentClient:
    def test_document_digest_and_open_uris_do_not_depend_on_the_client(self, tmp_path: Path) -> None:
        srv = server_mod.server
        uri = (tmp_path / "A.java").as_uri()
        created = srv.protocol._workspace is None
        if created:
            srv.protocol._workspace = Workspace(root_uri=tmp_path.as_uri(), sync_kind=lsp.TextDocumentSyncKind.Full)
        srv._agent_host = False  # e.g. an IDE client: no freshness marker, the gate still works
        try:
            srv.workspace.put_text_document(lsp.TextDocumentItem(uri=uri, language_id="java", version=1, text="x"))
            deps = srv._proxy.dependency_modules
            assert uri in deps._open_list()
            assert deps._doc_digest(uri) == content_digest(b"x")
            assert deps._doc_digest((tmp_path / "B.java").as_uri()) is None
        finally:
            srv.workspace.remove_text_document(uri)
            if created:
                srv.protocol._workspace = None


class TestProxyPlumbing:
    def test_data_dir_key_is_tagged_ws2(self) -> None:
        uri = "file:///repo/module"
        assert _data_dir_hash(uri) == hashlib.sha256(f"ws2|{uri}".encode()).hexdigest()[:12]
        assert _data_dir_hash(uri) != hashlib.sha256(uri.encode()).hexdigest()[:12]  # v0.14.0's key

    def test_progress_report_provider_is_requested_and_merged(self) -> None:
        params = {
            "rootUri": "file:///r",
            "initializationOptions": {"extendedClientCapabilities": {"classFileContentsSupport": True}},
        }
        effective = _build_effective_params(params, None, "file:///r", None)
        extended = effective["initializationOptions"]["extendedClientCapabilities"]
        assert extended == {"classFileContentsSupport": True, "progressReportProvider": True}
        assert params["initializationOptions"]["extendedClientCapabilities"] == {"classFileContentsSupport": True}
        bare = _build_effective_params({"initializationOptions": None}, None, "file:///r", None)
        assert bare["initializationOptions"]["extendedClientCapabilities"] == {"progressReportProvider": True}

    async def test_response_hook_runs_in_wire_order(self) -> None:
        proxy = JdtlsProxy()
        order: list[str] = []
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        proxy._pending[5] = future
        proxy._response_hooks[5] = lambda: order.append("hook")
        future.add_done_callback(lambda _f: order.append("future-callback"))
        proxy._dispatch_message({"jsonrpc": "2.0", "id": 5, "result": None})
        order.append("next-message")
        await asyncio.sleep(0)
        assert order == ["hook", "next-message", "future-callback"]
        assert 5 not in proxy._response_hooks

    async def test_request_reports_answered_errors_and_timeouts(self) -> None:
        proxy, proc = await _proxy_with(_FAKE_JDTLS)
        try:
            assert await proxy._request("shutdown", None, timeout=5.0) == (True, None)
            assert await proxy._request("other/unanswered", None, timeout=0.2) == (False, None)
            # A JSON-RPC error response: not answered, and send_request still returns None.
            assert await proxy._request("error/please", None, timeout=5.0) == (False, None)
            assert await proxy.send_request("error/please", None, timeout=5.0) is None
            assert proxy._pending == {}
        finally:
            await proxy.stop()
        assert proc.returncode is not None

    async def test_refresh_is_not_answered_by_an_error(self) -> None:
        proxy, _proc = await _proxy_with(_FAKE_JDTLS)
        try:

            async def error_request(method: str, params: Any, timeout: float = 0, on_response: Any = None) -> Any:
                return await JdtlsProxy._request(proxy, "error/please", params, timeout, on_response)

            proxy._request = error_request  # type: ignore[method-assign]
            assert await proxy._refresh_file_diagnostics("file:///x/A.java") is False
        finally:
            await proxy.stop()

    @pytest.mark.timeout(20)
    async def test_request_pending_during_stop_is_not_answered(self) -> None:
        proxy, _proc = await _proxy_with(_FAKE_JDTLS)
        pending = asyncio.create_task(proxy._request("other/unanswered", None, timeout=30.0))
        await asyncio.sleep(0.1)
        await proxy.stop()
        assert await asyncio.wait_for(pending, timeout=5.0) == (False, None)

    def test_odd_pom_diagnostic_never_breaks_the_reader(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proxy = JdtlsProxy()

        def boom(*_a: Any) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(proxy.dependency_modules, "note_pom", boom)
        monkeypatch.setattr(proxy.dependency_modules, "note_publish", boom)
        monkeypatch.setattr(proxy.dependency_modules.idle, "note_progress", boom)
        err = {"severity": 1, "message": "Missing artifact g:a:jar"}
        for uri in ("file:///r/pom.xml", "file:///r/A.java"):
            proxy._handle_notification(
                {"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": [err]}}
            )
        proxy._handle_notification({"method": "language/progressReport", "params": {"id": "x"}})


def _frame(msg: dict[str, Any]) -> bytes:
    body = json.dumps(msg).encode()
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


async def _read_frame(stdout: asyncio.StreamReader) -> dict[str, Any]:
    length = 0
    while True:
        line = (await stdout.readline()).strip()
        if not line:
            break
        name, _, value = line.decode().partition(":")
        if name.strip().lower() == "content-length":
            length = int(value)
    parsed: dict[str, Any] = json.loads(await stdout.readexactly(length))
    return parsed


class TestServerProcessExit:
    """A real ``python -m java_functional_lsp`` process (jdtls off): pygls 2.x awaits the user
    SHUTDOWN/EXIT handlers, and EXIT ends the process without hanging on the stdin reader."""

    async def _server(self) -> asyncio.subprocess.Process:
        env = {**os.environ, "JAVA_FUNCTIONAL_LSP_JDTLS": "off"}
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "java_functional_lsp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"processId": None, "capabilities": {}}}
        proc.stdin.write(_frame(init))
        await proc.stdin.drain()
        while (await asyncio.wait_for(_read_frame(proc.stdout), timeout=15.0)).get("id") != 1:
            pass
        proc.stdin.write(_frame({"jsonrpc": "2.0", "method": "initialized", "params": {}}))
        return proc

    async def _exit(self, proc: asyncio.subprocess.Process) -> int:
        assert proc.stdin is not None
        proc.stdin.write(_frame({"jsonrpc": "2.0", "method": "exit"}))
        await proc.stdin.drain()
        try:
            return await asyncio.wait_for(proc.wait(), timeout=10.0)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    @pytest.mark.timeout(40)
    async def test_shutdown_then_exit_exits_0(self) -> None:
        proc = await self._server()
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(_frame({"jsonrpc": "2.0", "id": 2, "method": "shutdown"}))
        await proc.stdin.drain()
        while (await asyncio.wait_for(_read_frame(proc.stdout), timeout=15.0)).get("id") != 2:
            pass
        assert await self._exit(proc) == 0

    @pytest.mark.timeout(40)
    async def test_exit_without_shutdown_exits_1(self) -> None:
        proc = await self._server()
        assert await self._exit(proc) == 1
