"""Tests for jdtls proxy — JSON-RPC framing, diagnostic merging, fallback."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from java_functional_lsp.proxy import (
    JdtlsProxy,
    _read_java_major_version,
    build_jdtls_env,
    encode_message,
    find_jdtls_java_home,
    read_message,
)


class TestEncodeMessage:
    def test_encodes_with_content_length(self) -> None:
        msg = {"jsonrpc": "2.0", "id": 1, "method": "test"}
        encoded = encode_message(msg)
        header, body = encoded.split(b"\r\n\r\n", 1)
        assert header.startswith(b"Content-Length: ")
        content_length = int(header.split(b": ")[1])
        assert content_length == len(body)
        assert json.loads(body) == msg


class TestReadMessage:
    @pytest.mark.asyncio
    async def test_reads_content_length_framed_message(self) -> None:
        msg = {"jsonrpc": "2.0", "id": 1, "result": "hello"}
        encoded = encode_message(msg)
        reader = asyncio.StreamReader()
        reader.feed_data(encoded)
        result = await read_message(reader)
        assert result == msg

    @pytest.mark.asyncio
    async def test_returns_none_on_eof(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_eof()
        result = await read_message(reader)
        assert result is None


class TestJdtlsProxy:
    def test_not_available_by_default(self) -> None:
        proxy = JdtlsProxy()
        assert not proxy.is_available

    def test_empty_diagnostics_cache(self) -> None:
        proxy = JdtlsProxy()
        assert proxy.get_cached_diagnostics("file:///test.java") == []

    @pytest.mark.asyncio
    async def test_start_fails_without_jdtls(self) -> None:
        with patch("java_functional_lsp.proxy.shutil.which", return_value=None):
            proxy = JdtlsProxy()
            result = await proxy.start({"processId": None, "rootUri": "file:///tmp"})
            assert result is False
            assert not proxy.is_available

    @pytest.mark.asyncio
    async def test_send_request_returns_none_when_not_started(self) -> None:
        proxy = JdtlsProxy()
        result = await proxy.send_request("test/method", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_send_notification_noop_when_not_started(self) -> None:
        proxy = JdtlsProxy()
        # Should not raise
        await proxy.send_notification("test/method", {})

    def test_dispatch_response(self) -> None:
        proxy = JdtlsProxy()
        loop = asyncio.new_event_loop()
        future: asyncio.Future[Any] = loop.create_future()
        proxy._pending[1] = future

        proxy._dispatch_message({"id": 1, "result": {"hello": "world"}})
        assert future.done()
        assert future.result() == {"hello": "world"}
        loop.close()

    def test_dispatch_error_response(self) -> None:
        proxy = JdtlsProxy()
        loop = asyncio.new_event_loop()
        future: asyncio.Future[Any] = loop.create_future()
        proxy._pending[2] = future

        proxy._dispatch_message({"id": 2, "error": {"code": -1, "message": "fail"}})
        assert future.done()
        assert future.result() is None
        loop.close()

    def test_dispatch_diagnostics_notification(self) -> None:
        received: list[tuple[str, list[Any]]] = []

        def on_diag(uri: str, diags: list[Any]) -> None:
            received.append((uri, diags))

        proxy = JdtlsProxy(on_diagnostics=on_diag)
        proxy._dispatch_message(
            {
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": "file:///test.java",
                    "diagnostics": [{"message": "error here"}],
                },
            }
        )

        assert len(received) == 1
        assert received[0][0] == "file:///test.java"
        assert len(received[0][1]) == 1
        assert proxy.get_cached_diagnostics("file:///test.java") == [{"message": "error here"}]


class TestDiagnosticMerging:
    """Test that custom + jdtls diagnostics are properly merged."""

    def test_merge_both_sources(self) -> None:
        """Custom diagnostics + jdtls diagnostics should both appear."""

        from java_functional_lsp.server import _jdtls_raw_to_lsp_diagnostics

        raw_jdtls = [
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 5},
                },
                "severity": 1,
                "source": "jdtls",
                "message": "Cannot resolve symbol",
            }
        ]
        result = _jdtls_raw_to_lsp_diagnostics(raw_jdtls)
        assert len(result) == 1
        assert result[0].source == "jdtls"
        assert result[0].message == "Cannot resolve symbol"

    def test_empty_jdtls_diagnostics(self) -> None:
        from java_functional_lsp.server import _jdtls_raw_to_lsp_diagnostics

        assert _jdtls_raw_to_lsp_diagnostics([]) == []

    def test_malformed_jdtls_diagnostic_is_skipped(self) -> None:
        from java_functional_lsp.server import _jdtls_raw_to_lsp_diagnostics

        # Completely broken entry should be skipped
        result = _jdtls_raw_to_lsp_diagnostics([{"garbage": True}])
        assert len(result) == 1  # fallback conversion still works with defaults

    def test_multiple_jdtls_diagnostics(self) -> None:
        from java_functional_lsp.server import _jdtls_raw_to_lsp_diagnostics

        raw = [
            {
                "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 5}},
                "severity": 1,
                "source": "jdtls",
                "message": "Error 1",
            },
            {
                "range": {"start": {"line": 5, "character": 0}, "end": {"line": 5, "character": 10}},
                "severity": 2,
                "source": "jdtls",
                "message": "Warning 1",
            },
        ]
        result = _jdtls_raw_to_lsp_diagnostics(raw)
        assert len(result) == 2


class TestReadMessageEdgeCases:
    @pytest.mark.asyncio
    async def test_reads_multiple_messages(self) -> None:
        msg1 = {"jsonrpc": "2.0", "id": 1, "result": "first"}
        msg2 = {"jsonrpc": "2.0", "id": 2, "result": "second"}
        reader = asyncio.StreamReader()
        reader.feed_data(encode_message(msg1) + encode_message(msg2))
        assert await read_message(reader) == msg1
        assert await read_message(reader) == msg2

    @pytest.mark.asyncio
    async def test_handles_extra_headers(self) -> None:
        """LSP messages may have Content-Type header too."""
        body = b'{"jsonrpc":"2.0","id":1,"result":"ok"}'
        raw = f"Content-Length: {len(body)}\r\nContent-Type: application/vscode-jsonrpc\r\n\r\n".encode() + body
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        result = await read_message(reader)
        assert result is not None
        assert result["result"] == "ok"


class TestProxyCapabilities:
    def test_empty_capabilities_by_default(self) -> None:
        proxy = JdtlsProxy()
        assert proxy.capabilities == {}

    def test_dispatch_unknown_notification_is_silent(self) -> None:
        proxy = JdtlsProxy()
        # Should not raise
        proxy._dispatch_message({"method": "window/logMessage", "params": {"message": "test"}})

    def test_dispatch_unknown_response_id(self) -> None:
        proxy = JdtlsProxy()
        # Response with no matching pending future — should not raise
        proxy._dispatch_message({"id": 999, "result": None})

    @pytest.mark.asyncio
    async def test_stop_noop_when_not_started(self) -> None:
        proxy = JdtlsProxy()
        # Should not raise
        await proxy.stop()


class TestServerHelpers:
    def test_serialize_params(self) -> None:
        from lsprotocol import types as lsp

        from java_functional_lsp.server import _serialize_params

        params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///test.java"),
            position=lsp.Position(line=1, character=5),
        )
        result = _serialize_params(params)
        assert isinstance(result, dict)
        assert "textDocument" in result or "text_document" in result

    def test_to_lsp_diagnostic(self) -> None:
        from java_functional_lsp.analyzers.base import Diagnostic, Severity
        from java_functional_lsp.server import _to_lsp_diagnostic

        diag = Diagnostic(line=1, col=5, end_line=1, end_col=10, severity=Severity.WARNING, code="test", message="msg")
        result = _to_lsp_diagnostic(diag)
        assert result.message == "msg"
        assert result.code == "test"
        assert result.source == "java-functional-lsp"

    def test_analyze_document(self) -> None:
        from java_functional_lsp.server import _analyze_document

        diags = _analyze_document("class T { String f() { return null; } }")
        codes = [d.code for d in diags]
        assert "null-return" in codes


class TestReadJavaMajorVersion:
    """Parsing of the ``java -version`` output."""

    def test_parses_modern_java(self) -> None:
        """Modern Java reports like ``openjdk version "21.0.10" 2026-01-20``."""
        import java_functional_lsp.proxy as proxy_mod

        with patch.object(proxy_mod.subprocess, "check_output") as mock_co:
            mock_co.return_value = 'openjdk version "21.0.10" 2026-01-20\n'
            assert _read_java_major_version("/fake/java") == 21

    def test_parses_java_8_legacy_format(self) -> None:
        """Legacy Java 8 reports like ``openjdk version "1.8.0_452"`` — major should be 1."""
        import java_functional_lsp.proxy as proxy_mod

        with patch.object(proxy_mod.subprocess, "check_output") as mock_co:
            mock_co.return_value = 'openjdk version "1.8.0_452"\n'
            assert _read_java_major_version("/fake/java") == 1

    def test_parses_java_25_ea(self) -> None:
        """Early-access releases like ``openjdk version "25-ea" ...``."""
        import java_functional_lsp.proxy as proxy_mod

        with patch.object(proxy_mod.subprocess, "check_output") as mock_co:
            mock_co.return_value = 'openjdk version "25-ea" 2026-02-15\n'
            assert _read_java_major_version("/fake/java") == 25

    def test_returns_none_on_subprocess_error(self) -> None:
        import java_functional_lsp.proxy as proxy_mod

        with patch.object(proxy_mod.subprocess, "check_output", side_effect=OSError):
            assert _read_java_major_version("/fake/java") is None

    def test_returns_none_on_unparseable_output(self) -> None:
        import java_functional_lsp.proxy as proxy_mod

        with patch.object(proxy_mod.subprocess, "check_output") as mock_co:
            mock_co.return_value = "not a java version string"
            assert _read_java_major_version("/fake/java") is None


class TestFindJdtlsJavaHome:
    """Resolution of a Java 21+ JAVA_HOME suitable for running jdtls."""

    def test_explicit_override_takes_precedence(self, tmp_path: Any) -> None:
        """JDTLS_JAVA_HOME wins over JAVA_HOME even if JAVA_HOME is also valid."""
        import java_functional_lsp.proxy as proxy_mod

        # Create a fake JAVA_HOME layout so the file-existence check passes.
        fake_home = tmp_path / "jdk21"
        (fake_home / "bin").mkdir(parents=True)
        (fake_home / "bin" / "java").touch()

        with patch.object(proxy_mod, "_read_java_major_version", return_value=21):
            result = find_jdtls_java_home({"JDTLS_JAVA_HOME": str(fake_home), "JAVA_HOME": "/other"})
        assert result == str(fake_home)

    def test_uses_java_home_when_suitable(self, tmp_path: Any) -> None:
        """If JAVA_HOME already points at Java 21+, use it as-is."""
        import java_functional_lsp.proxy as proxy_mod

        fake_home = tmp_path / "jdk21"
        (fake_home / "bin").mkdir(parents=True)
        (fake_home / "bin" / "java").touch()

        with patch.object(proxy_mod, "_read_java_major_version", return_value=21):
            result = find_jdtls_java_home({"JAVA_HOME": str(fake_home)})
        assert result == str(fake_home)

    def test_ignores_old_java_home(self, tmp_path: Any) -> None:
        """JAVA_HOME pointing at Java 8 must NOT be returned."""
        import java_functional_lsp.proxy as proxy_mod

        fake_home = tmp_path / "jdk8"
        (fake_home / "bin").mkdir(parents=True)
        (fake_home / "bin" / "java").touch()

        # Also force macOS fallback + PATH fallback to fail so we get None.
        with (
            patch.object(proxy_mod, "_read_java_major_version", return_value=1),
            patch.object(proxy_mod.platform, "system", return_value="Linux"),
            patch.object(proxy_mod.shutil, "which", return_value=None),
        ):
            result = find_jdtls_java_home({"JAVA_HOME": str(fake_home)})
        assert result is None

    def test_ignores_nonexistent_java_home(self) -> None:
        """JAVA_HOME pointing at a non-existent directory is skipped."""
        import java_functional_lsp.proxy as proxy_mod

        with (
            patch.object(proxy_mod.platform, "system", return_value="Linux"),
            patch.object(proxy_mod.shutil, "which", return_value=None),
        ):
            result = find_jdtls_java_home({"JAVA_HOME": "/definitely/not/a/real/jdk"})
        assert result is None

    def test_macos_fallback_to_java_home_cmd(self, tmp_path: Any) -> None:
        """On macOS, falls back to /usr/libexec/java_home -v 21+ when JAVA_HOME is bad."""
        import java_functional_lsp.proxy as proxy_mod

        fake_home = tmp_path / "jdk25"
        (fake_home / "bin").mkdir(parents=True)
        (fake_home / "bin" / "java").touch()

        with (
            patch.object(proxy_mod.platform, "system", return_value="Darwin"),
            patch.object(proxy_mod.subprocess, "check_output", return_value=f"{fake_home}\n"),
            patch.object(proxy_mod, "_read_java_major_version", return_value=25),
        ):
            result = find_jdtls_java_home({"JAVA_HOME": "/nope"})
        assert result == str(fake_home)

    def test_returns_none_when_nothing_suitable(self) -> None:
        """When nothing works, returns None so caller can strip JAVA_HOME."""
        import java_functional_lsp.proxy as proxy_mod

        with (
            patch.object(proxy_mod.platform, "system", return_value="Linux"),
            patch.object(proxy_mod.shutil, "which", return_value=None),
        ):
            result = find_jdtls_java_home({})
        assert result is None


class TestBuildJdtlsEnv:
    """Environment construction for the jdtls subprocess."""

    def test_sets_java_home_when_found(self) -> None:
        """When find_jdtls_java_home returns a path, JAVA_HOME is set in the env."""
        import java_functional_lsp.proxy as proxy_mod

        with patch.object(proxy_mod, "find_jdtls_java_home", return_value="/opt/jdk21"):
            env = build_jdtls_env({"PATH": "/usr/bin", "JAVA_HOME": "/old/java"})
        assert env["JAVA_HOME"] == "/opt/jdk21"
        assert env["PATH"] == "/usr/bin"  # other vars preserved

    def test_strips_java_home_when_no_suitable_java(self) -> None:
        """When no Java 21+ found, JAVA_HOME is removed so jdtls uses its fallback."""
        import java_functional_lsp.proxy as proxy_mod

        with patch.object(proxy_mod, "find_jdtls_java_home", return_value=None):
            env = build_jdtls_env({"PATH": "/usr/bin", "JAVA_HOME": "/old/java"})
        assert "JAVA_HOME" not in env
        assert env["PATH"] == "/usr/bin"

    def test_no_java_home_in_env_is_ok(self) -> None:
        """If the base env has no JAVA_HOME, build_jdtls_env must not crash."""
        import java_functional_lsp.proxy as proxy_mod

        with patch.object(proxy_mod, "find_jdtls_java_home", return_value=None):
            env = build_jdtls_env({"PATH": "/usr/bin"})
        assert "JAVA_HOME" not in env
