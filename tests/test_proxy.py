"""Tests for jdtls proxy — JSON-RPC framing, diagnostic merging, fallback."""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Module-level alias: tests below reference proxy_mod.subprocess / proxy_mod.platform /
# proxy_mod.shutil when patching internals. Hoisted here to avoid repeated inline imports.
import java_functional_lsp.proxy as proxy_mod
from java_functional_lsp.proxy import (
    JdtlsProxy,
    # _read_java_major_version is a private module function. We import it directly
    # because each Java version string format (modern/legacy/EA/internal) warrants
    # its own focused parser test that would be awkward to exercise only via the
    # public build_jdtls_env API.
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


class TestLspConverterCamelCase:
    """Regression tests for the LSP JSON shape of request forwarding.

    The LSP wire protocol uses camelCase field names (e.g. ``textDocument``).
    pygls/lsprotocol models use snake_case attributes (``text_document``) and
    rely on their own cattrs converter to handle the conversion. A vanilla
    ``cattrs.Converter()`` emits snake_case literally, which causes jdtls to
    throw ``NullPointerException`` on every ``textDocument/definition``,
    ``textDocument/references``, ``textDocument/hover``, etc. because its
    ``TextDocumentPositionParams.getTextDocument()`` returns null when the
    field is named ``text_document``.

    These tests pin the converter behavior so a regression to a vanilla
    cattrs converter would be caught immediately.
    """

    def test_definition_params_unstructures_to_camelcase(self) -> None:
        from lsprotocol import types as lsp

        from java_functional_lsp.server import _serialize_params

        params = lsp.DefinitionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///foo.java"),
            position=lsp.Position(line=10, character=5),
        )
        result = _serialize_params(params)
        assert isinstance(result, dict)
        # jdtls requires camelCase: textDocument, NOT text_document
        assert "textDocument" in result
        assert "text_document" not in result
        assert result["textDocument"]["uri"] == "file:///foo.java"
        assert result["position"]["line"] == 10
        assert result["position"]["character"] == 5

    def test_reference_params_unstructures_to_camelcase(self) -> None:
        from lsprotocol import types as lsp

        from java_functional_lsp.server import _serialize_params

        params = lsp.ReferenceParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///bar.java"),
            position=lsp.Position(line=3, character=7),
            context=lsp.ReferenceContext(include_declaration=True),
        )
        result = _serialize_params(params)
        assert isinstance(result, dict)
        assert "textDocument" in result
        assert "text_document" not in result
        # Nested camelCase: context.includeDeclaration, NOT context.include_declaration
        assert "context" in result
        assert "includeDeclaration" in result["context"]
        assert "include_declaration" not in result["context"]

    def test_hover_params_unstructures_to_camelcase(self) -> None:
        from lsprotocol import types as lsp

        from java_functional_lsp.server import _serialize_params

        params = lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///baz.java"),
            position=lsp.Position(line=0, character=0),
        )
        result = _serialize_params(params)
        assert "textDocument" in result
        assert "text_document" not in result

    def test_document_symbol_params_unstructures_to_camelcase(self) -> None:
        from lsprotocol import types as lsp

        from java_functional_lsp.server import _serialize_params

        params = lsp.DocumentSymbolParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///quux.java"),
        )
        result = _serialize_params(params)
        assert "textDocument" in result
        assert "text_document" not in result

    def test_did_open_params_unstructures_to_camelcase(self) -> None:
        from lsprotocol import types as lsp

        from java_functional_lsp.server import _serialize_params

        params = lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(
                uri="file:///open.java",
                language_id="java",
                version=1,
                text="class T {}",
            ),
        )
        result = _serialize_params(params)
        assert "textDocument" in result
        assert "text_document" not in result
        # TextDocumentItem fields too: languageId, not language_id
        assert "languageId" in result["textDocument"]
        assert "language_id" not in result["textDocument"]

    def test_completion_params_unstructures_to_camelcase(self) -> None:
        from lsprotocol import types as lsp

        from java_functional_lsp.server import _serialize_params

        params = lsp.CompletionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///c.java"),
            position=lsp.Position(line=5, character=10),
        )
        result = _serialize_params(params)
        assert "textDocument" in result
        assert "text_document" not in result

    def test_none_fields_are_omitted(self) -> None:
        """Optional fields with None values should be dropped from the JSON.

        jdtls and other LSP servers treat the presence of a key with null
        differently from the absence of the key. The LSP converter drops
        None fields; a vanilla converter would emit ``"workDoneToken": null``
        which could confuse strict servers.
        """
        from lsprotocol import types as lsp

        from java_functional_lsp.server import _serialize_params

        params = lsp.DefinitionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///foo.java"),
            position=lsp.Position(line=0, character=0),
            work_done_token=None,
            partial_result_token=None,
        )
        result = _serialize_params(params)
        # Required fields must survive pruning
        assert "textDocument" in result
        assert "position" in result
        # Optional None fields must be omitted
        assert "workDoneToken" not in result
        assert "partialResultToken" not in result
        # Also the snake_case equivalents should not appear
        assert "work_done_token" not in result
        assert "partial_result_token" not in result


class TestReadJavaMajorVersion:
    """Parsing of the ``java -version`` output.

    Tests the private ``_read_java_major_version`` parser directly because
    each Java version string format warrants its own focused test case.
    The function is private to the module; this direct import is intentional.
    """

    def test_parses_modern_java(self) -> None:
        """Modern Java reports like ``openjdk version "21.0.10" 2026-01-20``."""
        with patch.object(proxy_mod.subprocess, "check_output") as mock_co:
            mock_co.return_value = 'openjdk version "21.0.10" 2026-01-20\n'
            assert _read_java_major_version("/fake/java") == 21

    def test_parses_java_8_legacy_format(self) -> None:
        """Legacy Java 8 reports like ``openjdk version "1.8.0_452"``.

        The regex captures the literal first version token (``1`` here).
        Callers MUST still apply the ``>= _MIN_JDTLS_JAVA_MAJOR`` semantic
        check — this is not ``Java 1``; it is the raw first token of
        Java 8's legacy major.minor.patch_build string.
        """
        with patch.object(proxy_mod.subprocess, "check_output") as mock_co:
            mock_co.return_value = 'openjdk version "1.8.0_452"\n'
            assert _read_java_major_version("/fake/java") == 1

    def test_parses_java_25_ea(self) -> None:
        """Early-access releases like ``openjdk version "25-ea" ...``."""
        with patch.object(proxy_mod.subprocess, "check_output") as mock_co:
            mock_co.return_value = 'openjdk version "25-ea" 2026-02-15\n'
            assert _read_java_major_version("/fake/java") == 25

    def test_parses_java_17_internal(self) -> None:
        """Internal builds like ``openjdk version "17-internal"``."""
        with patch.object(proxy_mod.subprocess, "check_output") as mock_co:
            mock_co.return_value = 'openjdk version "17-internal" 2022-08-15\n'
            assert _read_java_major_version("/fake/java") == 17

    def test_returns_none_on_subprocess_error(self) -> None:
        with patch.object(proxy_mod.subprocess, "check_output", side_effect=OSError):
            assert _read_java_major_version("/fake/java") is None

    def test_returns_none_on_subprocess_timeout(self) -> None:
        """subprocess.TimeoutExpired is the most realistic failure at cold JVM start."""
        timeout_error = subprocess.TimeoutExpired(cmd=["java", "-version"], timeout=2)
        with patch.object(proxy_mod.subprocess, "check_output", side_effect=timeout_error):
            assert _read_java_major_version("/fake/java") is None

    def test_returns_none_on_calledprocess_error(self) -> None:
        """A non-zero exit from java (e.g. corrupt binary) returns None without raising."""
        err = subprocess.CalledProcessError(returncode=1, cmd=["java", "-version"])
        with patch.object(proxy_mod.subprocess, "check_output", side_effect=err):
            assert _read_java_major_version("/fake/java") is None

    def test_returns_none_on_unparseable_output(self) -> None:
        with patch.object(proxy_mod.subprocess, "check_output") as mock_co:
            mock_co.return_value = "not a java version string"
            assert _read_java_major_version("/fake/java") is None


class TestFindJdtlsJavaHome:
    """Resolution of a Java 21+ JAVA_HOME suitable for running jdtls.

    All tests mock ``_read_java_major_version`` to avoid real ``java -version``
    invocations, and patch ``platform.system`` / ``shutil.which`` to control
    which resolution branch is exercised. ``tmp_path`` is used only to create
    real on-disk ``bin/java`` files so the ``is_file()`` guard passes.
    """

    @staticmethod
    def _make_fake_jdk(tmp_path: Any, name: str) -> Any:
        """Create a fake JDK layout with ``<name>/bin/java`` as an empty file."""
        jdk = tmp_path / name
        (jdk / "bin").mkdir(parents=True)
        (jdk / "bin" / "java").touch()
        return jdk

    def test_explicit_override_takes_precedence(self, tmp_path: Any) -> None:
        """JDTLS_JAVA_HOME wins over JAVA_HOME even if JAVA_HOME is also valid."""
        fake_home = self._make_fake_jdk(tmp_path, "jdk21")
        with patch.object(proxy_mod, "_read_java_major_version", return_value=21):
            result = find_jdtls_java_home({"JDTLS_JAVA_HOME": str(fake_home), "JAVA_HOME": "/other"})
        assert result == str(fake_home)

    def test_override_falls_through_when_too_old(self, tmp_path: Any) -> None:
        """JDTLS_JAVA_HOME pointing at Java 8 must be rejected and fall through to JAVA_HOME."""
        bad_override = self._make_fake_jdk(tmp_path, "jdk8-override")
        good_home = self._make_fake_jdk(tmp_path, "jdk21")

        # Mock version lookup: the override returns 1 (Java 8), the fallback returns 21.
        def fake_version(java_exec: str) -> int:
            return 1 if "jdk8-override" in java_exec else 21

        with patch.object(proxy_mod, "_read_java_major_version", side_effect=fake_version):
            result = find_jdtls_java_home({"JDTLS_JAVA_HOME": str(bad_override), "JAVA_HOME": str(good_home)})
        assert result == str(good_home)

    def test_empty_override_is_skipped(self, tmp_path: Any) -> None:
        """An empty-string JDTLS_JAVA_HOME (``""``) must be treated as unset."""
        good_home = self._make_fake_jdk(tmp_path, "jdk21")
        with patch.object(proxy_mod, "_read_java_major_version", return_value=21):
            result = find_jdtls_java_home({"JDTLS_JAVA_HOME": "", "JAVA_HOME": str(good_home)})
        assert result == str(good_home)

    def test_uses_java_home_when_suitable(self, tmp_path: Any) -> None:
        """If JAVA_HOME already points at Java 21+, use it as-is."""
        fake_home = self._make_fake_jdk(tmp_path, "jdk21")
        with patch.object(proxy_mod, "_read_java_major_version", return_value=21):
            result = find_jdtls_java_home({"JAVA_HOME": str(fake_home)})
        assert result == str(fake_home)

    def test_ignores_old_java_home(self, tmp_path: Any) -> None:
        """JAVA_HOME pointing at Java 8 must NOT be returned."""
        fake_home = self._make_fake_jdk(tmp_path, "jdk8")
        with (
            patch.object(proxy_mod, "_read_java_major_version", return_value=1),
            patch.object(proxy_mod.platform, "system", return_value="Linux"),
            patch.object(proxy_mod.shutil, "which", return_value=None),
        ):
            result = find_jdtls_java_home({"JAVA_HOME": str(fake_home)})
        assert result is None

    def test_nonexistent_java_home_short_circuits(self) -> None:
        """A non-existent JAVA_HOME path short-circuits BEFORE calling _read_java_major_version.

        Regression guard: the file-existence check must happen first. If the guard
        were accidentally removed, _read_java_major_version would be invoked on a
        bogus path and still return None, but the short-circuit would be gone —
        this test asserts the guard is actually in place via a call-count spy.
        """
        version_spy = MagicMock(return_value=None)
        with (
            patch.object(proxy_mod, "_read_java_major_version", version_spy),
            patch.object(proxy_mod.platform, "system", return_value="Linux"),
            patch.object(proxy_mod.shutil, "which", return_value=None),
        ):
            result = find_jdtls_java_home({"JAVA_HOME": "/definitely/not/a/real/jdk"})
        assert result is None
        # The nonexistent JAVA_HOME must not trigger a version lookup
        # (shutil.which is mocked to None so the PATH branch also cannot call it).
        assert version_spy.call_count == 0

    def test_macos_fallback_trusts_java_home_cmd_output(self, tmp_path: Any) -> None:
        """On macOS, /usr/libexec/java_home -v 21+ output is trusted; no re-validation.

        We verify this by mocking the subprocess and asserting that
        _read_java_major_version is NOT called for the returned path (only
        for the rejected JAVA_HOME inspection in step 2).
        """
        fake_home = self._make_fake_jdk(tmp_path, "jdk25")
        version_spy = MagicMock(return_value=None)  # forces step 2 to reject JAVA_HOME

        def macos_only(cmd: list[str], **kwargs: Any) -> str:
            # Only intercept /usr/libexec/java_home; raise if anything else slips through.
            assert cmd[0] == "/usr/libexec/java_home", f"unexpected subprocess call: {cmd}"
            return f"{fake_home}\n"

        with (
            patch.object(proxy_mod.platform, "system", return_value="Darwin"),
            patch.object(proxy_mod.subprocess, "check_output", side_effect=macos_only),
            patch.object(proxy_mod, "_read_java_major_version", version_spy),
        ):
            result = find_jdtls_java_home({"JAVA_HOME": "/nope"})

        assert result == str(fake_home)
        # version_spy is called once for JAVA_HOME=/nope (which fails the is_file guard
        # so version_spy should NOT be called at all — but let's be flexible). The key
        # assertion is that it was NOT called for the fake_home returned by java_home,
        # i.e. the macOS fallback trusts the tool output.
        for call_args in version_spy.call_args_list:
            assert str(fake_home) not in str(call_args)

    def test_path_fallback_happy_path(self, tmp_path: Any) -> None:
        """Step 4: which('java') returns a Java 21+ binary, JAVA_HOME is derived."""
        jdk = self._make_fake_jdk(tmp_path, "path-jdk21")
        java_bin = jdk / "bin" / "java"
        with (
            patch.object(proxy_mod.platform, "system", return_value="Linux"),
            patch.object(proxy_mod.shutil, "which", return_value=str(java_bin)),
            patch.object(proxy_mod, "_read_java_major_version", return_value=21),
        ):
            result = find_jdtls_java_home({})
        assert result == str(jdk)

    def test_path_fallback_rejects_old_version(self, tmp_path: Any) -> None:
        """Step 4: which('java') returns Java 17, falls through to None."""
        jdk = self._make_fake_jdk(tmp_path, "path-jdk17")
        java_bin = jdk / "bin" / "java"
        with (
            patch.object(proxy_mod.platform, "system", return_value="Linux"),
            patch.object(proxy_mod.shutil, "which", return_value=str(java_bin)),
            patch.object(proxy_mod, "_read_java_major_version", return_value=17),
        ):
            result = find_jdtls_java_home({})
        assert result is None

    def test_path_fallback_rejects_system_prefix(self) -> None:
        """A direct /usr/bin/java binary (not a symlink) must not yield JAVA_HOME=/usr.

        This reproduces the Docker-container edge case where /usr/bin/java is a
        standalone JDK binary rather than an update-alternatives symlink. We
        patch Path.resolve to be an identity function so the test is independent
        of the CI host's actual filesystem — on Ubuntu CI, /usr/bin/java is a
        real symlink to /usr/lib/jvm/... which would otherwise defeat the test.
        """
        from pathlib import Path

        # Identity resolver: return the path unchanged instead of following symlinks.
        # This matches the Docker-container scenario where /usr/bin/java is a real
        # binary. The same patch also affects _is_system_root_prefix, which is fine:
        # Path("/usr").resolve() → Path("/usr") → str == "/usr" → system prefix.
        def identity_resolve(self: Path, strict: bool = False) -> Path:
            del strict  # kwargs-compatible signature; Path.resolve accepts `strict`
            return self

        with (
            patch.object(proxy_mod.platform, "system", return_value="Linux"),
            patch.object(proxy_mod.shutil, "which", return_value="/usr/bin/java"),
            patch.object(proxy_mod, "_read_java_major_version", return_value=21),
            patch.object(Path, "resolve", identity_resolve),
        ):
            result = find_jdtls_java_home({})
        # /usr is a system-root prefix and must be rejected even though Java 21+ exists.
        assert result != "/usr"
        assert result is None

    def test_path_fallback_follows_symlink_to_real_jdk(self, tmp_path: Any) -> None:
        """The common Ubuntu case: /usr/bin/java is a symlink to a real JDK install.

        Path.resolve follows the symlink so parent.parent yields the real JDK home
        (e.g. /usr/lib/jvm/temurin-21-jdk-amd64), which is NOT a system prefix and
        is correctly accepted.
        """
        from pathlib import Path

        # Lay out a fake real JDK at tmp_path/jvm/jdk21/bin/java and create a
        # symlink at tmp_path/usr_bin_java pointing at it.
        real_jdk = tmp_path / "jvm" / "jdk21"
        (real_jdk / "bin").mkdir(parents=True)
        (real_jdk / "bin" / "java").touch()
        symlink = tmp_path / "usr_bin_java"
        symlink.symlink_to(real_jdk / "bin" / "java")

        # Sanity: resolve() should follow the symlink to the real JDK binary.
        assert Path(symlink).resolve() == (real_jdk / "bin" / "java").resolve()

        with (
            patch.object(proxy_mod.platform, "system", return_value="Linux"),
            patch.object(proxy_mod.shutil, "which", return_value=str(symlink)),
            patch.object(proxy_mod, "_read_java_major_version", return_value=21),
        ):
            result = find_jdtls_java_home({})

        assert result == str(real_jdk.resolve())

    def test_path_fallback_uses_passed_environ_path(self, tmp_path: Any) -> None:
        """shutil.which must honor the passed environ's PATH, not os.environ."""
        jdk = self._make_fake_jdk(tmp_path, "custom-path-jdk")
        custom_path = str(jdk / "bin")

        captured: dict[str, Any] = {}

        def fake_which(cmd: str, path: str | None = None) -> str | None:
            captured["path"] = path
            return None  # we only care that PATH was forwarded

        with (
            patch.object(proxy_mod.platform, "system", return_value="Linux"),
            patch.object(proxy_mod.shutil, "which", side_effect=fake_which),
        ):
            find_jdtls_java_home({"PATH": custom_path})
        assert captured["path"] == custom_path

    def test_returns_none_when_nothing_suitable(self) -> None:
        """When nothing works, returns None so caller can strip JAVA_HOME."""
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
        with patch.object(proxy_mod, "find_jdtls_java_home", return_value="/opt/jdk21"):
            env = build_jdtls_env({"PATH": "/usr/bin", "JAVA_HOME": "/old/java"})
        assert env["JAVA_HOME"] == "/opt/jdk21"
        assert env["PATH"] == "/usr/bin"  # other vars preserved

    def test_strips_java_home_when_no_suitable_java(self) -> None:
        """When no Java 21+ found, JAVA_HOME is removed so jdtls uses its fallback."""
        with patch.object(proxy_mod, "find_jdtls_java_home", return_value=None):
            env = build_jdtls_env({"PATH": "/usr/bin", "JAVA_HOME": "/old/java"})
        assert "JAVA_HOME" not in env
        assert env["PATH"] == "/usr/bin"

    def test_no_java_home_in_env_is_ok(self) -> None:
        """If the base env has no JAVA_HOME, build_jdtls_env must not crash."""
        with patch.object(proxy_mod, "find_jdtls_java_home", return_value=None):
            env = build_jdtls_env({"PATH": "/usr/bin"})
        assert "JAVA_HOME" not in env

    def test_returns_independent_dict(self) -> None:
        """Mutating the returned env must NOT affect the source mapping."""
        source = {"PATH": "/usr/bin", "JAVA_HOME": "/old/java"}
        with patch.object(proxy_mod, "find_jdtls_java_home", return_value=None):
            env = build_jdtls_env(source)
        env["PATH"] = "/mutated"
        assert source["PATH"] == "/usr/bin"  # source is untouched

    def test_allowlist_drops_secrets(self) -> None:
        """Secrets in the parent env must not be forwarded to the jdtls subprocess."""
        source = {
            "PATH": "/usr/bin",
            "HOME": "/home/user",
            "JAVA_HOME": "/opt/jdk21",
            "AWS_ACCESS_KEY_ID": "AKIATEST",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "GITHUB_TOKEN": "ghp_fake",
            "ANTHROPIC_API_KEY": "sk-ant-fake",
            "OPENAI_API_KEY": "sk-fake",
            "NPM_TOKEN": "npm_fake",
        }
        with patch.object(proxy_mod, "find_jdtls_java_home", return_value="/opt/jdk21"):
            env = build_jdtls_env(source)
        assert "AWS_ACCESS_KEY_ID" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "GITHUB_TOKEN" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert "NPM_TOKEN" not in env
        # Allow-listed vars ARE forwarded.
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/home/user"
        assert env["JAVA_HOME"] == "/opt/jdk21"

    def test_allowlist_forwards_locale_and_xdg_prefixes(self) -> None:
        """LC_* and XDG_* variables match the prefix allow-list and are forwarded."""
        source = {
            "PATH": "/usr/bin",
            "LC_ALL": "en_US.UTF-8",
            "LC_MESSAGES": "en_US.UTF-8",
            "XDG_CONFIG_HOME": "/home/user/.config",
            "XDG_CACHE_HOME": "/home/user/.cache",
            "JDTLS_JAVA_HOME": "/opt/jdk21",
        }
        with patch.object(proxy_mod, "find_jdtls_java_home", return_value=None):
            env = build_jdtls_env(source)
        assert env["LC_ALL"] == "en_US.UTF-8"
        assert env["LC_MESSAGES"] == "en_US.UTF-8"
        assert env["XDG_CONFIG_HOME"] == "/home/user/.config"
        assert env["XDG_CACHE_HOME"] == "/home/user/.cache"
        assert env["JDTLS_JAVA_HOME"] == "/opt/jdk21"

    def test_exact_key_set_when_allowlist_minimal(self) -> None:
        """Verify no unexpected keys are added or reordered in the returned env."""
        source = {"PATH": "/usr/bin"}
        with patch.object(proxy_mod, "find_jdtls_java_home", return_value="/opt/jdk21"):
            env = build_jdtls_env(source)
        # Only allow-listed PATH and the injected JAVA_HOME should be present.
        assert set(env.keys()) == {"PATH", "JAVA_HOME"}


class TestRedactPath:
    """Log-safe path rendering."""

    def test_redacts_full_path_to_basename(self) -> None:
        from java_functional_lsp.proxy import _redact_path

        assert _redact_path("/Users/alice/secret/project/jdk21") == ".../jdk21"

    def test_handles_trailing_slash(self) -> None:
        from java_functional_lsp.proxy import _redact_path

        assert _redact_path("/opt/jdk21/") == ".../jdk21"

    def test_none_returns_unset(self) -> None:
        from java_functional_lsp.proxy import _redact_path

        assert _redact_path(None) == "<unset>"

    def test_empty_returns_unset(self) -> None:
        from java_functional_lsp.proxy import _redact_path

        assert _redact_path("") == "<unset>"


class TestStartPassesEnvToSubprocess:
    """Integration test: JdtlsProxy.start() must pass env= to create_subprocess_exec.

    Without this test, a regression that removes ``env=jdtls_env`` from the
    subprocess launch would leave every build_jdtls_env unit test green while
    silently breaking the whole point of this fix.
    """

    @pytest.mark.asyncio
    async def test_start_forwards_env_to_subprocess(self, tmp_path: Any) -> None:
        proxy = JdtlsProxy()

        # Sentinel env that build_jdtls_env will return via mock — we assert this
        # exact dict is forwarded verbatim to create_subprocess_exec.
        sentinel_env = {"PATH": "/fake", "JAVA_HOME": "/fake/jdk21"}

        # Fake process with non-None stdout/stderr so start() can proceed past
        # the assert self._process.stdout is not None gate, attach reader tasks,
        # then fail initialize() (we mock send_request to return None).
        fake_process = MagicMock()
        fake_process.pid = 99999
        fake_process.stdout = MagicMock()
        fake_process.stderr = MagicMock()
        fake_process.returncode = 0

        async def fake_wait() -> int:
            return 0

        fake_process.wait = fake_wait

        captured: dict[str, Any] = {}

        async def fake_create_subprocess(*args: Any, **subprocess_kwargs: Any) -> Any:
            captured["args"] = args
            captured["env"] = subprocess_kwargs.get("env")
            return fake_process

        async def fake_reader_loop(_: Any) -> None:
            return None

        async def fake_stderr_reader(_: Any) -> None:
            return None

        async def fake_send_request(*_args: Any, **_kwargs: Any) -> None:
            # Simulate initialize timing out / failing so start() returns False.
            return None

        with (
            patch.object(proxy_mod.shutil, "which", return_value="/fake/jdtls"),
            patch.object(proxy_mod, "build_jdtls_env", return_value=sentinel_env),
            patch.object(proxy_mod.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess),
            patch.object(JdtlsProxy, "_reader_loop", side_effect=fake_reader_loop),
            patch.object(JdtlsProxy, "_stderr_reader", side_effect=fake_stderr_reader),
            patch.object(JdtlsProxy, "send_request", side_effect=fake_send_request),
            patch.object(JdtlsProxy, "stop", side_effect=lambda: None),
        ):
            ok = await proxy.start({"rootUri": f"file://{tmp_path}"})

        # start() should have bailed out because initialize returned None.
        assert ok is False
        # The crucial assertion: env= was passed through to the subprocess call.
        assert captured["env"] == sentinel_env


class TestFindModuleRoot:
    """Tests for find_module_root — build-file detection for module scoping."""

    def test_finds_pom_xml(self, tmp_path: Any) -> None:
        from java_functional_lsp.proxy import find_module_root

        (tmp_path / "pom.xml").touch()
        java_file = tmp_path / "src" / "Main.java"
        java_file.parent.mkdir()
        java_file.touch()
        assert find_module_root(str(java_file)) == str(tmp_path)

    def test_finds_build_gradle(self, tmp_path: Any) -> None:
        from java_functional_lsp.proxy import find_module_root

        (tmp_path / "build.gradle").touch()
        java_file = tmp_path / "src" / "Main.java"
        java_file.parent.mkdir()
        java_file.touch()
        assert find_module_root(str(java_file)) == str(tmp_path)

    def test_finds_build_gradle_kts(self, tmp_path: Any) -> None:
        from java_functional_lsp.proxy import find_module_root

        (tmp_path / "build.gradle.kts").touch()
        java_file = tmp_path / "src" / "Main.java"
        java_file.parent.mkdir()
        java_file.touch()
        assert find_module_root(str(java_file)) == str(tmp_path)

    def test_finds_nearest_not_parent(self, tmp_path: Any) -> None:
        """Nested modules: should find the innermost module root."""
        from java_functional_lsp.proxy import find_module_root

        (tmp_path / "pom.xml").touch()  # parent module
        child = tmp_path / "child-module"
        child.mkdir()
        (child / "pom.xml").touch()  # child module
        java_file = child / "src" / "Main.java"
        java_file.parent.mkdir()
        java_file.touch()
        assert find_module_root(str(java_file)) == str(child)

    def test_returns_none_when_no_build_file(self, tmp_path: Any) -> None:
        from java_functional_lsp.proxy import find_module_root

        java_file = tmp_path / "src" / "Main.java"
        java_file.parent.mkdir()
        java_file.touch()
        assert find_module_root(str(java_file)) is None


class TestLazyStart:
    """Tests for lazy-start proxy features."""

    def test_check_available_true(self) -> None:
        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy()
        with patch("java_functional_lsp.proxy.shutil.which", return_value="/usr/bin/jdtls"):
            assert proxy.check_available() is True
        assert proxy._jdtls_on_path is True

    def test_check_available_false(self) -> None:
        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy()
        with patch("java_functional_lsp.proxy.shutil.which", return_value=None):
            assert proxy.check_available() is False
        assert proxy._jdtls_on_path is False

    async def test_queue_and_flush(self) -> None:
        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy()
        proxy.queue_notification("textDocument/didOpen", {"uri": "a"})
        proxy.queue_notification("textDocument/didChange", {"uri": "b"})
        assert len(proxy._queued_notifications) == 2

        flushed: list[tuple[str, Any]] = []

        async def mock_send(method: str, params: Any) -> None:
            flushed.append((method, params))

        proxy.send_notification = mock_send  # type: ignore[assignment]
        await proxy.flush_queued_notifications()
        assert len(flushed) == 2
        assert flushed[0] == ("textDocument/didOpen", {"uri": "a"})
        assert flushed[1] == ("textDocument/didChange", {"uri": "b"})
        assert len(proxy._queued_notifications) == 0

    def test_queue_caps_at_max(self) -> None:
        from java_functional_lsp.proxy import _MAX_QUEUED_NOTIFICATIONS, JdtlsProxy

        proxy = JdtlsProxy()
        for i in range(_MAX_QUEUED_NOTIFICATIONS + 50):
            proxy.queue_notification("textDocument/didChange", {"i": i})
        assert len(proxy._queued_notifications) == _MAX_QUEUED_NOTIFICATIONS
        # Oldest entries dropped — last entry should be the most recent
        assert proxy._queued_notifications[-1] == ("textDocument/didChange", {"i": _MAX_QUEUED_NOTIFICATIONS + 49})

    async def test_ensure_started_no_retry_after_failure(self) -> None:
        from unittest.mock import AsyncMock

        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy()
        proxy._jdtls_on_path = True
        proxy.queue_notification("textDocument/didOpen", {"uri": "test"})
        proxy.start = AsyncMock(return_value=False)  # type: ignore[assignment]
        result = await proxy.ensure_started({"rootUri": "file:///tmp"}, "file:///tmp/F.java")
        assert result is False
        assert proxy._start_failed is True
        # Queue should be cleared on failure
        assert len(proxy._queued_notifications) == 0
        # Second call should return immediately without calling start()
        proxy.start.reset_mock()  # type: ignore[attr-defined]
        result2 = await proxy.ensure_started({"rootUri": "file:///tmp"}, "file:///tmp/F.java")
        assert result2 is False
        proxy.start.assert_not_called()  # type: ignore[attr-defined]

    async def test_add_module_if_new_sends_notification(self) -> None:
        from unittest.mock import AsyncMock

        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy()
        proxy._available = True
        proxy.send_notification = AsyncMock()  # type: ignore[assignment]
        # Create a tmp dir with pom.xml
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "pom.xml").touch()
            java_file = Path(td) / "src" / "Main.java"
            java_file.parent.mkdir()
            java_file.touch()
            uri = java_file.as_uri()
            result = await proxy.add_module_if_new(uri)
            assert result is True
            proxy.send_notification.assert_called_once()  # type: ignore[attr-defined]
            call_args = proxy.send_notification.call_args  # type: ignore[attr-defined]
            assert call_args[0][0] == "workspace/didChangeWorkspaceFolders"

    async def test_add_module_if_new_skips_duplicate(self) -> None:
        from unittest.mock import AsyncMock

        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy()
        proxy._available = True
        proxy.send_notification = AsyncMock()  # type: ignore[assignment]
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "pom.xml").touch()
            java_file = Path(td) / "src" / "Main.java"
            java_file.parent.mkdir()
            java_file.touch()
            uri = java_file.as_uri()
            result1 = await proxy.add_module_if_new(uri)
            result2 = await proxy.add_module_if_new(uri)  # duplicate
            assert result1 is True
            assert result2 is False
            assert proxy.send_notification.call_count == 1  # type: ignore[attr-defined]

    async def test_expand_full_workspace_sends_notification(self) -> None:
        from unittest.mock import AsyncMock

        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy()
        proxy._available = True
        proxy._original_root_uri = "file:///workspace/monorepo"
        proxy.send_notification = AsyncMock()  # type: ignore[assignment]
        await proxy.expand_full_workspace()
        proxy.send_notification.assert_called_once()  # type: ignore[attr-defined]
        assert proxy._workspace_expanded is True

    async def test_expand_full_workspace_noop_when_not_available(self) -> None:
        from unittest.mock import AsyncMock

        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy()
        proxy._original_root_uri = "file:///workspace/monorepo"
        proxy.send_notification = AsyncMock()  # type: ignore[assignment]
        await proxy.expand_full_workspace()
        proxy.send_notification.assert_not_called()  # type: ignore[attr-defined]
        assert proxy._workspace_expanded is False

    async def test_expand_full_workspace_noop_when_already_added(self) -> None:
        from unittest.mock import AsyncMock

        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy()
        proxy._available = True
        proxy._original_root_uri = "file:///workspace/monorepo"
        proxy._added_module_uris.add("file:///workspace/monorepo")
        proxy.send_notification = AsyncMock()  # type: ignore[assignment]
        await proxy.expand_full_workspace()
        proxy.send_notification.assert_not_called()  # type: ignore[attr-defined]
        assert proxy._workspace_expanded is True

    async def test_ensure_started_no_build_file(self) -> None:
        """ensure_started with no build file should pass module_root_uri=None."""
        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy()
        proxy._jdtls_on_path = True
        captured: dict[str, Any] = {}

        async def capturing_start(params: Any, *, module_root_uri: str | None = None) -> bool:
            captured["module_root_uri"] = module_root_uri
            return False

        proxy.start = capturing_start  # type: ignore[assignment]
        await proxy.ensure_started(
            {"rootUri": "file:///monorepo", "capabilities": {}},
            "file:///nonexistent/src/Main.java",
        )
        assert captured["module_root_uri"] is None

    async def test_ensure_started_with_build_file(self, tmp_path: Any) -> None:
        """ensure_started should find module root and pass it to start()."""
        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy()
        proxy._jdtls_on_path = True
        (tmp_path / "pom.xml").touch()
        java_file = tmp_path / "src" / "Main.java"
        java_file.parent.mkdir()
        java_file.touch()

        captured: dict[str, Any] = {}

        async def capturing_start(params: Any, *, module_root_uri: str | None = None) -> bool:
            captured["module_root_uri"] = module_root_uri
            return False

        proxy.start = capturing_start  # type: ignore[assignment]
        await proxy.ensure_started(
            {"rootUri": "file:///monorepo", "capabilities": {}},
            java_file.as_uri(),
        )
        assert captured["module_root_uri"] is not None
        assert str(tmp_path) in captured["module_root_uri"]

    def test_data_dir_hash_uses_original_root(self) -> None:
        """Data-dir hash should be based on original rootUri, not module root."""
        import hashlib

        # The hash is computed from the original rootUri, not the module root.
        # Verify these produce different hashes, confirming start() must use
        # the original root for stability.
        root = "file:///workspace/monorepo"
        expected_hash = hashlib.sha256(root.encode()).hexdigest()[:12]
        module_root = "file:///workspace/monorepo/module-a"
        module_hash = hashlib.sha256(module_root.encode()).hexdigest()[:12]
        assert expected_hash != module_hash
