"""End-to-end tests: start the LSP server over stdio, send real JSON-RPC messages, verify diagnostics."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


def _encode_lsp(obj: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC message with Content-Length header."""
    body = json.dumps(obj).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _read_lsp(proc: subprocess.Popen[bytes]) -> dict[str, Any] | None:
    """Read one LSP message from the server's stdout."""
    assert proc.stdout is not None
    headers: dict[str, str] = {}
    while True:
        line = proc.stdout.readline()
        if not line:
            return None
        line_str = line.decode("ascii").strip()
        if line_str == "":
            break
        key, _, value = line_str.partition(": ")
        headers[key] = value
    length = int(headers.get("Content-Length", "0"))
    if length == 0:
        return None
    body = proc.stdout.read(length)
    return json.loads(body)


def _send(proc: subprocess.Popen[bytes], msg: dict[str, Any]) -> None:
    """Send an LSP message to the server."""
    assert proc.stdin is not None
    proc.stdin.write(_encode_lsp(msg))
    proc.stdin.flush()


def _read_until_method(proc: subprocess.Popen[bytes], method: str, max_messages: int = 20) -> dict[str, Any] | None:
    """Read messages until one with the given method appears."""
    for _ in range(max_messages):
        msg = _read_lsp(proc)
        if msg is None:
            return None
        if msg.get("method") == method:
            return msg
    return None


@pytest.fixture
def server():
    """Start the LSP server as a subprocess, shut down cleanly after test."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "java_functional_lsp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    yield proc
    # Graceful LSP shutdown: send shutdown request, then exit notification
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 9999, "method": "shutdown", "params": None})
        _read_lsp(proc)  # consume shutdown response
        _send(proc, {"jsonrpc": "2.0", "method": "exit", "params": None})
        proc.wait(timeout=5)
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _initialize(proc: subprocess.Popen[bytes], root_uri: str = "file:///tmp") -> dict[str, Any] | None:
    """Send initialize + initialized, return the initialize response."""
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "processId": None,
                "capabilities": {},
                "rootUri": root_uri,
            },
        },
    )
    response = _read_lsp(proc)
    _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
    return response


def _did_open(proc: subprocess.Popen[bytes], uri: str, text: str, version: int = 1) -> None:
    """Send textDocument/didOpen notification."""
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": uri,
                    "languageId": "java",
                    "version": version,
                    "text": text,
                }
            },
        },
    )


def _did_change(proc: subprocess.Popen[bytes], uri: str, text: str, version: int = 2) -> None:
    """Send textDocument/didChange notification with full content."""
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "method": "textDocument/didChange",
            "params": {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            },
        },
    )


def _did_close(proc: subprocess.Popen[bytes], uri: str) -> None:
    """Send textDocument/didClose notification."""
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "method": "textDocument/didClose",
            "params": {"textDocument": {"uri": uri}},
        },
    )


def _did_save(proc: subprocess.Popen[bytes], uri: str) -> None:
    """Send textDocument/didSave notification."""
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "method": "textDocument/didSave",
            "params": {"textDocument": {"uri": uri}},
        },
    )


def _wait_diagnostics(proc: subprocess.Popen[bytes]) -> dict[str, Any] | None:
    """Wait for a publishDiagnostics notification."""
    return _read_until_method(proc, "textDocument/publishDiagnostics")


# Per-test timeout prevents CI from hanging if the server crashes mid-test
pytestmark = pytest.mark.timeout(15)


class TestE2EInitialize:
    def test_server_capabilities(self, server: subprocess.Popen[bytes]) -> None:
        """Server should advertise text document sync capability."""
        response = _initialize(server)
        assert response is not None
        result = response.get("result", {})
        caps = result.get("capabilities", {})
        assert "textDocumentSync" in caps

    def test_server_name(self, server: subprocess.Popen[bytes]) -> None:
        """Server should identify itself."""
        response = _initialize(server)
        assert response is not None
        info = response["result"].get("serverInfo", {})
        assert "java-functional" in info.get("name", "").lower()


class TestE2EDiagnostics:
    def test_diagnostics_on_open(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Opening a Java file with null return should produce diagnostics."""
        java_file = tmp_path / "Test.java"
        java_file.write_text("class T { String f() { return null; } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        params = msg["params"]
        assert params["uri"] == uri
        codes = [d["code"] for d in params["diagnostics"]]
        assert "null-return" in codes

    def test_clean_file_no_diagnostics(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """A clean Java file should produce empty diagnostics."""
        java_file = tmp_path / "Clean.java"
        java_file.write_text('class T { String f() { return "ok"; } }')
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        assert len(msg["params"]["diagnostics"]) == 0

    def test_diagnostics_on_change(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Editing a file to introduce a violation should produce diagnostics.

        didChange is debounced (150ms), so we follow with didSave for immediate results.
        """
        java_file = tmp_path / "Change.java"
        java_file.write_text('class T { String f() { return "ok"; } }')
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())
        _wait_diagnostics(server)  # consume initial clean diagnostics

        _did_change(server, uri, "class T { String f() { return null; } }")
        _did_save(server, uri)
        # May receive multiple notifications (debounce + save) — collect all codes seen
        all_codes: set[str] = set()
        for _ in range(5):
            msg = _wait_diagnostics(server)
            if msg is None:
                break
            for d in msg["params"]["diagnostics"]:
                all_codes.add(d["code"])
            if "null-return" in all_codes:
                break
        assert "null-return" in all_codes, f"Expected null-return, got {all_codes}"

    def test_diagnostics_on_save(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Save should trigger immediate diagnostics."""
        java_file = tmp_path / "Save.java"
        java_file.write_text("class T { void f() { throw new RuntimeException(); } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())
        _wait_diagnostics(server)

        _did_save(server, uri)
        msg = _wait_diagnostics(server)
        assert msg is not None
        codes = [d["code"] for d in msg["params"]["diagnostics"]]
        assert "throw-statement" in codes


class TestE2EMultipleRules:
    def test_multiple_violations(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """A file with multiple violations should report all of them."""
        java_file = tmp_path / "Multi.java"
        java_file.write_text(
            "class T {\n"
            "  String f() { return null; }\n"
            "  void g() { throw new RuntimeException(); }\n"
            "  void h() { for (int i = 0; i < 10; i++) {} }\n"
            "}"
        )
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        codes = {d["code"] for d in msg["params"]["diagnostics"]}
        assert "null-return" in codes
        assert "throw-statement" in codes
        assert "imperative-loop" in codes

    def test_diagnostic_source(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """All diagnostics should have source='java-functional-lsp'."""
        java_file = tmp_path / "Source.java"
        java_file.write_text("class T { String f() { return null; } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        for diag in msg["params"]["diagnostics"]:
            assert diag["source"] == "java-functional-lsp"


class TestE2EDidClose:
    def test_close_then_reopen(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Closing a file then reopening should still produce diagnostics."""
        java_file = tmp_path / "Close.java"
        java_file.write_text("class T { String f() { return null; } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())
        _wait_diagnostics(server)

        _did_close(server, uri)

        # Re-open should still work
        _did_open(server, uri, java_file.read_text(), version=2)
        msg = _wait_diagnostics(server)
        assert msg is not None
        codes = [d["code"] for d in msg["params"]["diagnostics"]]
        assert "null-return" in codes


class TestE2EConfig:
    def test_rule_disabled_by_config(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Rules disabled in .java-functional-lsp.json should not produce diagnostics."""
        config = tmp_path / ".java-functional-lsp.json"
        config.write_text('{"rules": {"null-return": "off"}}')
        java_file = tmp_path / "Config.java"
        java_file.write_text("class T { String f() { return null; } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        codes = [d["code"] for d in msg["params"]["diagnostics"]]
        assert "null-return" not in codes

    def test_excludes_pattern(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Files matching excludes patterns should produce no diagnostics."""
        config = tmp_path / ".java-functional-lsp.json"
        config.write_text('{"excludes": ["**/generated/**"]}')
        gen_dir = tmp_path / "generated"
        gen_dir.mkdir()
        java_file = gen_dir / "Gen.java"
        java_file.write_text("class T { String f() { return null; } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        assert len(msg["params"]["diagnostics"]) == 0
