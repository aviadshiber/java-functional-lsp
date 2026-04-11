"""Integration tests for the LanguageServer — mock-free via real LSP transport.

These tests spawn the actual ``java-functional-lsp`` server as a subprocess,
connect via pygls ``LanguageClient`` over stdio pipes, and drive the full LSP
lifecycle: initialize → didOpen → publishDiagnostics → codeAction. No mocks,
no patching — the same bytes flow that a real IDE sends.

This is the layer that catches regressions invisible to unit tests:
- camelCase serialization (v0.7.2 bug: vanilla cattrs → snake_case)
- transport framing (Content-Length, JSON encoding)
- server initialization + workspace wiring
- diagnostic publishing timing
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest
from lsprotocol import types as lsp
from pygls.lsp.client import LanguageClient

_BUGGY_JAVA = """\
import java.util.List;

public class BuggyExample {
    public String firstOrNull(List<String> xs) {
        if (xs != null) {
            return xs.get(0);
        } else {
            return null;
        }
    }
}
"""

_TRY_CATCH_JAVA = """\
import java.io.IOException;

public class TryCatchExample {
    public String read() {
        try {
            return riskyRead();
        } catch (IOException e) {
            return "fallback";
        }
    }
}
"""

_CLEAN_JAVA = """\
public class Clean {
    public int add(int a, int b) {
        return a + b;
    }
}
"""


@pytest.fixture
async def lsp_client(tmp_path: Any) -> LanguageClient:  # type: ignore[misc]
    """Spawn the real java-functional-lsp server and return an initialized client.

    Uses pygls ``LanguageClient.start_io`` to connect via stdio — the exact
    transport IntelliJ/VS Code use. The server process is killed on teardown.
    """
    client = LanguageClient("test-client", "1.0")

    # Collect published diagnostics so tests can assert on them.
    client._published: dict[str, list[lsp.Diagnostic]] = {}  # type: ignore[attr-defined]

    @client.feature(lsp.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)
    def on_publish(params: lsp.PublishDiagnosticsParams) -> None:
        client._published[params.uri] = list(params.diagnostics)  # type: ignore[attr-defined]

    await client.start_io(sys.executable, "-m", "java_functional_lsp")

    result = await client.initialize_async(
        lsp.InitializeParams(
            process_id=os.getpid(),
            root_uri=tmp_path.as_uri(),
            root_path=str(tmp_path),
            capabilities=lsp.ClientCapabilities(
                text_document=lsp.TextDocumentClientCapabilities(
                    code_action=lsp.CodeActionClientCapabilities(
                        code_action_literal_support=lsp.ClientCodeActionLiteralOptions(
                            code_action_kind=lsp.ClientCodeActionKindOptions(
                                value_set=[lsp.CodeActionKind.QuickFix],
                            ),
                        ),
                    ),
                    publish_diagnostics=lsp.PublishDiagnosticsClientCapabilities(),
                ),
            ),
        )
    )
    assert result.capabilities is not None
    client._server_capabilities = result.capabilities  # type: ignore[attr-defined]
    client.initialized(lsp.InitializedParams())

    try:
        yield client
    finally:
        try:
            await client.shutdown_async(None)
            client.exit(None)
        except Exception:
            pass
        await client.stop()


async def _open_and_wait_for_diagnostics(
    client: LanguageClient,
    uri: str,
    source: str,
    *,
    timeout: float = 10.0,
) -> list[lsp.Diagnostic]:
    """Open a document and wait until publishDiagnostics arrives for its URI.

    The server publishes diagnostics asynchronously after didOpen. We poll
    the client's collected notifications until the URI appears or timeout.
    """
    client._published.pop(uri, None)  # type: ignore[attr-defined]

    client.text_document_did_open(
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(
                uri=uri,
                language_id="java",
                version=1,
                text=source,
            ),
        )
    )

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if uri in client._published:  # type: ignore[attr-defined]
            return client._published[uri]  # type: ignore[attr-defined]
        await asyncio.sleep(0.1)

    pytest.fail(f"Timed out waiting for publishDiagnostics on {uri}")
    return []  # unreachable, but satisfies type checker


# --------------------------------------------------------------------------
# Direct-call tests — exercise server internals for coverage
# --------------------------------------------------------------------------
#
# The subprocess tests below (TestLspLifecycle) are the real e2e tests, but
# pytest-cov can only instrument the test process, not spawned subprocesses.
# These direct-call tests exercise the same server.py code paths in-process
# so the coverage counter credits them.


def _ensure_workspace() -> None:
    """Bootstrap a minimal pygls workspace if not already initialized."""
    from pygls.workspace import Workspace

    from java_functional_lsp.server import server

    if server.protocol._workspace is None:
        server.protocol._workspace = Workspace(
            root_uri="file:///test",
            sync_kind=lsp.TextDocumentSyncKind.Full,
        )


class TestServerInternals:
    """Direct-call tests for server.py helpers — provides in-process coverage."""

    def test_load_config_no_workspace(self) -> None:
        from java_functional_lsp.server import _load_config

        assert _load_config(None) == {}

    def test_load_config_missing_file(self, tmp_path: Any) -> None:
        from java_functional_lsp.server import _load_config

        assert _load_config(str(tmp_path)) == {}

    def test_load_config_valid_json(self, tmp_path: Any) -> None:
        from java_functional_lsp.server import _load_config

        (tmp_path / ".java-functional-lsp.json").write_text('{"rules": {"null-return": "off"}}')
        assert _load_config(str(tmp_path)) == {"rules": {"null-return": "off"}}

    def test_load_config_invalid_json(self, tmp_path: Any) -> None:
        from java_functional_lsp.server import _load_config

        (tmp_path / ".java-functional-lsp.json").write_text("not json {{{")
        assert _load_config(str(tmp_path)) == {}

    def test_to_lsp_diagnostic_with_data(self) -> None:
        from java_functional_lsp.analyzers.base import Diagnostic as LintDiag
        from java_functional_lsp.analyzers.base import DiagnosticData, Severity
        from java_functional_lsp.server import _to_lsp_diagnostic

        diag = LintDiag(
            line=5,
            col=10,
            end_line=5,
            end_col=20,
            severity=Severity.HINT,
            code="test",
            message="msg",
            data=DiagnosticData(fix_type="FIX", target_library="lib", rationale="r"),
        )
        result = _to_lsp_diagnostic(diag)
        assert result.severity == lsp.DiagnosticSeverity.Hint
        assert result.data is not None
        assert result.data["fixType"] == "FIX"

    def test_to_lsp_diagnostic_without_data(self) -> None:
        from java_functional_lsp.analyzers.base import Diagnostic as LintDiag
        from java_functional_lsp.analyzers.base import Severity
        from java_functional_lsp.server import _to_lsp_diagnostic

        diag = LintDiag(line=0, col=0, end_line=0, end_col=5, severity=Severity.WARNING, code="x", message="m")
        assert _to_lsp_diagnostic(diag).data is None

    def test_analyze_document_with_excludes(self) -> None:
        from java_functional_lsp.server import _analyze_document, server

        old = server._config
        server._config = {"excludes": ["**/generated/**"]}
        try:
            assert _analyze_document("class T { String f() { return null; } }", "file:///generated/F.java") == []
        finally:
            server._config = old

    def test_analyze_document_produces_diagnostics(self) -> None:
        from java_functional_lsp.server import _analyze_document

        result = _analyze_document("class T { String f() { return null; } }", "file:///F.java")
        assert any(d.code == "null-return" for d in result)

    def test_handle_exception_logs(self, caplog: Any) -> None:
        import logging

        from java_functional_lsp.server import _handle_exception

        with caplog.at_level(logging.ERROR, logger="java_functional_lsp.server"):
            _handle_exception(ValueError, ValueError("crash"), None)
        assert any("Uncaught exception" in r.getMessage() for r in caplog.records)

    def test_jdtls_raw_to_lsp_diagnostics(self) -> None:
        from java_functional_lsp.server import _jdtls_raw_to_lsp_diagnostics

        raw = [
            {
                "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 10}},
                "severity": 2,
                "code": "x",
                "source": "Java",
                "message": "warn",
            }
        ]
        result = _jdtls_raw_to_lsp_diagnostics(raw)
        assert len(result) == 1
        assert result[0].message == "warn"

    def test_jdtls_raw_to_lsp_diagnostics_malformed(self) -> None:
        from java_functional_lsp.server import _jdtls_raw_to_lsp_diagnostics

        assert _jdtls_raw_to_lsp_diagnostics([42, None, "bad"]) == []

    def test_on_jdtls_diagnostics_callback(self) -> None:
        from unittest.mock import patch

        from java_functional_lsp.server import server

        _ensure_workspace()
        uri = "file:///test/Cb.java"
        server.workspace.put_text_document(
            lsp.TextDocumentItem(
                uri=uri, language_id="java", version=1, text="class T { String f() { return null; } }"
            ),
        )
        try:
            with patch.object(server, "text_document_publish_diagnostics") as mock_pub:
                server._on_jdtls_diagnostics(uri, [])
                mock_pub.assert_called_once()
                codes = [d.code for d in mock_pub.call_args[0][0].diagnostics]
                assert "null-return" in codes
        finally:
            server.workspace.remove_text_document(uri)

    def test_init_capabilities_exclude_jdtls_features(self) -> None:
        """Static capabilities must NOT include hover/definition/references/completion/documentSymbol.

        These are registered dynamically after jdtls starts, so the IDE doesn't
        suppress diagnostic tooltips while jdtls is unavailable.
        """
        from java_functional_lsp.server import on_initialize

        result = on_initialize(
            lsp.InitializeParams(
                process_id=1,
                root_uri="file:///tmp",
                capabilities=lsp.ClientCapabilities(),
            )
        )
        caps = result.capabilities
        assert caps.code_action_provider is not None
        assert caps.text_document_sync is not None
        assert caps.hover_provider is None
        assert caps.definition_provider is None
        assert caps.references_provider is None
        assert caps.completion_provider is None
        assert caps.document_symbol_provider is None

    def test_build_jdtls_registrations(self) -> None:
        """_build_jdtls_registrations returns one Registration per jdtls capability, each scoped to java files."""
        from java_functional_lsp.server import _JDTLS_REG_PREFIX, _build_jdtls_registrations

        regs = _build_jdtls_registrations()
        assert len(regs) == 5
        methods = {r.method for r in regs}
        assert lsp.TEXT_DOCUMENT_HOVER in methods
        assert lsp.TEXT_DOCUMENT_DEFINITION in methods
        assert lsp.TEXT_DOCUMENT_REFERENCES in methods
        assert lsp.TEXT_DOCUMENT_COMPLETION in methods
        assert lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL in methods
        # All IDs are unique and use the shared prefix
        ids = {r.id for r in regs}
        assert len(ids) == 5
        assert all(rid.startswith(_JDTLS_REG_PREFIX) for rid in ids)
        # All have java document selector with correct language
        for r in regs:
            assert r.register_options is not None
            selectors = r.register_options["documentSelector"]
            assert any(s.get("language") == "java" for s in selectors)
        # Completion has triggerCharacters
        comp = next(r for r in regs if r.method == lsp.TEXT_DOCUMENT_COMPLETION)
        assert comp.register_options.get("triggerCharacters") == ["."]

    async def test_register_jdtls_capabilities_logs_on_failure(self, caplog: Any) -> None:
        """_register_jdtls_capabilities logs a warning when the client rejects."""
        import logging
        from unittest.mock import AsyncMock, MagicMock, patch

        import java_functional_lsp.server as srv_mod
        from java_functional_lsp.server import server as srv

        # Patch both server.feature (to avoid FeatureAlreadyRegisteredError on
        # the shared singleton) and client_register_capability_async (to trigger error).
        mock_reg = AsyncMock(side_effect=Exception("no"))
        mock_feature = MagicMock(return_value=lambda fn: fn)
        old_flag = srv_mod._jdtls_capabilities_registered
        srv_mod._jdtls_capabilities_registered = False
        try:
            with (
                caplog.at_level(logging.WARNING, logger="java_functional_lsp.server"),
                patch.object(srv, "client_register_capability_async", mock_reg),
                patch.object(srv, "feature", mock_feature),
            ):
                await srv_mod._register_jdtls_capabilities()
        finally:
            srv_mod._jdtls_capabilities_registered = old_flag
        assert any("Failed to dynamically register" in r.getMessage() for r in caplog.records)

    async def test_register_jdtls_capabilities_happy_path(self, caplog: Any) -> None:
        """On success, handlers are registered and info log is emitted."""
        import logging
        from unittest.mock import AsyncMock, MagicMock, patch

        import java_functional_lsp.server as srv_mod
        from java_functional_lsp.server import server as srv

        mock_reg = AsyncMock(return_value=None)
        registered_methods: list[str] = []
        mock_feature = MagicMock(side_effect=lambda m: registered_methods.append(m) or (lambda fn: fn))
        old_flag = srv_mod._jdtls_capabilities_registered
        srv_mod._jdtls_capabilities_registered = False
        try:
            with (
                caplog.at_level(logging.INFO, logger="java_functional_lsp.server"),
                patch.object(srv, "client_register_capability_async", mock_reg),
                patch.object(srv, "feature", mock_feature),
            ):
                await srv_mod._register_jdtls_capabilities()
        finally:
            srv_mod._jdtls_capabilities_registered = old_flag
        # Handlers were registered for all 5 methods
        assert lsp.TEXT_DOCUMENT_HOVER in registered_methods
        assert lsp.TEXT_DOCUMENT_COMPLETION in registered_methods
        assert lsp.TEXT_DOCUMENT_DEFINITION in registered_methods
        assert lsp.TEXT_DOCUMENT_REFERENCES in registered_methods
        assert lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL in registered_methods
        # client_register_capability_async was called
        mock_reg.assert_called_once()
        # Success log emitted
        assert any("Dynamically registered" in r.getMessage() for r in caplog.records)

    async def test_register_jdtls_capabilities_idempotent(self) -> None:
        """Second call is a no-op (idempotency guard)."""
        from unittest.mock import AsyncMock, patch

        import java_functional_lsp.server as srv_mod
        from java_functional_lsp.server import server as srv

        mock_reg = AsyncMock()
        old_flag = srv_mod._jdtls_capabilities_registered
        srv_mod._jdtls_capabilities_registered = True
        try:
            with patch.object(srv, "client_register_capability_async", mock_reg):
                await srv_mod._register_jdtls_capabilities()
        finally:
            srv_mod._jdtls_capabilities_registered = old_flag
        mock_reg.assert_not_called()

    async def test_lazy_start_jdtls_success(self, caplog: Any) -> None:
        """_lazy_start_jdtls logs success, flushes queue, and expands workspace."""
        import logging
        from unittest.mock import AsyncMock, MagicMock, patch

        import java_functional_lsp.server as srv_mod
        from java_functional_lsp.server import _lazy_start_jdtls
        from java_functional_lsp.server import server as srv

        mock_flush = AsyncMock()
        mock_expand = AsyncMock()
        old_flag = srv_mod._jdtls_capabilities_registered
        srv_mod._jdtls_capabilities_registered = False
        try:
            with (
                caplog.at_level(logging.INFO, logger="java_functional_lsp.server"),
                patch.object(srv._proxy, "ensure_started", AsyncMock(return_value=True)),
                patch.object(srv._proxy, "flush_queued_notifications", mock_flush),
                patch.object(srv._proxy, "expand_full_workspace", mock_expand),
                patch.object(srv, "feature", MagicMock(return_value=lambda fn: fn)),
                patch.object(srv, "client_register_capability_async", AsyncMock()),
            ):
                await _lazy_start_jdtls("file:///test/F.java")
        finally:
            srv_mod._jdtls_capabilities_registered = old_flag
        assert any("jdtls proxy active" in r.getMessage() for r in caplog.records)
        mock_flush.assert_called_once()
        mock_expand.assert_called_once()

    async def test_lazy_start_jdtls_failure_logged(self, caplog: Any) -> None:
        """_lazy_start_jdtls logs warning on exception."""
        import logging
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _lazy_start_jdtls
        from java_functional_lsp.server import server as srv

        with (
            caplog.at_level(logging.WARNING, logger="java_functional_lsp.server"),
            patch.object(srv._proxy, "ensure_started", AsyncMock(side_effect=Exception("boom"))),
        ):
            await _lazy_start_jdtls("file:///test/F.java")
        assert any("lazy start failed" in r.getMessage() for r in caplog.records)

    async def test_lazy_start_jdtls_silent_failure(self) -> None:
        """When ensure_started returns False, flush/expand are not called."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _lazy_start_jdtls
        from java_functional_lsp.server import server as srv

        mock_flush = AsyncMock()
        mock_expand = AsyncMock()
        with (
            patch.object(srv._proxy, "ensure_started", AsyncMock(return_value=False)),
            patch.object(srv._proxy, "flush_queued_notifications", mock_flush),
            patch.object(srv._proxy, "expand_full_workspace", mock_expand),
        ):
            await _lazy_start_jdtls("file:///test/F.java")
        mock_flush.assert_not_called()
        mock_expand.assert_not_called()

    async def test_ensure_module_and_forward_retries_on_new_module(self) -> None:
        """When add_module_if_new returns True and first request returns None, retry once."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _MODULE_IMPORT_WAIT_SEC, _ensure_module_and_forward
        from java_functional_lsp.server import server as srv

        mock_add = AsyncMock(return_value=True)
        mock_send = AsyncMock(side_effect=[None, {"result": "ok"}])
        mock_sleep = AsyncMock()
        with (
            patch.object(srv._proxy, "add_module_if_new", mock_add),
            patch.object(srv._proxy, "send_request", mock_send),
            patch.object(srv._proxy, "_available", True),
            patch("java_functional_lsp.server.asyncio.sleep", mock_sleep),
        ):
            result = await _ensure_module_and_forward("textDocument/hover", {}, "file:///test/F.java")
        assert result == {"result": "ok"}
        assert mock_send.call_count == 2
        mock_sleep.assert_called_once_with(_MODULE_IMPORT_WAIT_SEC)

    async def test_ensure_module_and_forward_no_retry_on_known_module(self) -> None:
        """When add_module_if_new returns False and request returns None, no retry."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _ensure_module_and_forward
        from java_functional_lsp.server import server as srv

        mock_add = AsyncMock(return_value=False)
        mock_send = AsyncMock(return_value=None)
        with (
            patch.object(srv._proxy, "add_module_if_new", mock_add),
            patch.object(srv._proxy, "send_request", mock_send),
            patch.object(srv._proxy, "_available", True),
        ):
            result = await _ensure_module_and_forward("textDocument/hover", {}, "file:///test/F.java")
        assert result is None
        assert mock_send.call_count == 1

    async def test_ensure_module_and_forward_no_retry_on_success(self) -> None:
        """When first request succeeds, no retry regardless of module status."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _ensure_module_and_forward
        from java_functional_lsp.server import server as srv

        mock_add = AsyncMock(return_value=True)
        mock_send = AsyncMock(return_value={"result": "ok"})
        with (
            patch.object(srv._proxy, "add_module_if_new", mock_add),
            patch.object(srv._proxy, "send_request", mock_send),
            patch.object(srv._proxy, "_available", True),
        ):
            result = await _ensure_module_and_forward("textDocument/hover", {}, "file:///test/F.java")
        assert result == {"result": "ok"}
        assert mock_send.call_count == 1

    def test_serialize_params_camelcase(self) -> None:
        from java_functional_lsp.server import _serialize_params

        result = _serialize_params(
            lsp.DefinitionParams(
                text_document=lsp.TextDocumentIdentifier(uri="file:///x.java"),
                position=lsp.Position(line=0, character=0),
            )
        )
        assert "textDocument" in result
        assert "text_document" not in result

    def test_code_action_null_return(self) -> None:
        from java_functional_lsp.server import on_code_action, server

        _ensure_workspace()
        uri = "file:///test/CA1.java"
        server.workspace.put_text_document(
            lsp.TextDocumentItem(uri=uri, language_id="java", version=1, text=_BUGGY_JAVA),
        )
        try:
            diag = lsp.Diagnostic(
                range=lsp.Range(start=lsp.Position(line=7, character=19), end=lsp.Position(line=7, character=23)),
                message="m",
                severity=lsp.DiagnosticSeverity.Warning,
                code="null-return",
                source="java-functional-lsp",
            )
            result = on_code_action(
                lsp.CodeActionParams(
                    text_document=lsp.TextDocumentIdentifier(uri=uri),
                    range=diag.range,
                    context=lsp.CodeActionContext(diagnostics=[diag]),
                )
            )
        finally:
            server.workspace.remove_text_document(uri)
        assert result is not None
        assert result[0].title == "Replace with Option.none()"

    def test_code_action_try_catch(self) -> None:
        from java_functional_lsp.server import on_code_action, server

        _ensure_workspace()
        uri = "file:///test/CA2.java"
        server.workspace.put_text_document(
            lsp.TextDocumentItem(uri=uri, language_id="java", version=1, text=_TRY_CATCH_JAVA),
        )
        try:
            diag = lsp.Diagnostic(
                range=lsp.Range(start=lsp.Position(line=4, character=8), end=lsp.Position(line=4, character=11)),
                message="m",
                severity=lsp.DiagnosticSeverity.Hint,
                code="try-catch-to-monadic",
                source="java-functional-lsp",
            )
            result = on_code_action(
                lsp.CodeActionParams(
                    text_document=lsp.TextDocumentIdentifier(uri=uri),
                    range=diag.range,
                    context=lsp.CodeActionContext(diagnostics=[diag]),
                )
            )
        finally:
            server.workspace.remove_text_document(uri)
        assert result is not None
        assert any("Try.of" in e.new_text for e in result[0].edit.changes[uri])

    def test_code_action_filters_foreign(self) -> None:
        from java_functional_lsp.server import on_code_action, server

        _ensure_workspace()
        uri = "file:///test/CA3.java"
        server.workspace.put_text_document(
            lsp.TextDocumentItem(uri=uri, language_id="java", version=1, text=_BUGGY_JAVA),
        )
        try:
            diag = lsp.Diagnostic(
                range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=5)),
                message="x",
                severity=lsp.DiagnosticSeverity.Warning,
                code="jdtls-thing",
                source="Java",
            )
            result = on_code_action(
                lsp.CodeActionParams(
                    text_document=lsp.TextDocumentIdentifier(uri=uri),
                    range=diag.range,
                    context=lsp.CodeActionContext(diagnostics=[diag]),
                )
            )
        finally:
            server.workspace.remove_text_document(uri)
        assert result is None


# --------------------------------------------------------------------------
# Subprocess-based tests — zero mocks, real LSP transport
# --------------------------------------------------------------------------


@pytest.mark.timeout(30)
class TestLspLifecycle:
    """Full LSP lifecycle tests via real stdio transport — zero mocks."""

    async def test_initialize_reports_capabilities(self, lsp_client: LanguageClient) -> None:
        """Server advertises codeActionProvider and textDocumentSync but NOT jdtls features.

        jdtls-dependent capabilities (hover, definition, references, completion,
        documentSymbol) are registered dynamically after jdtls starts, so they
        should NOT appear in the static InitializeResult.
        """
        caps = lsp_client._server_capabilities  # type: ignore[attr-defined]
        assert caps is not None
        assert caps.code_action_provider is not None
        assert caps.text_document_sync is not None
        # jdtls features are NOT statically advertised (registered dynamically)
        assert caps.hover_provider is None
        assert caps.definition_provider is None
        assert caps.references_provider is None
        assert caps.completion_provider is None
        assert caps.document_symbol_provider is None

    async def test_null_return_diagnostic_published(self, lsp_client: LanguageClient) -> None:
        """didOpen a file with ``return null`` → server publishes null-return diagnostic."""
        uri = "file:///test/BuggyExample.java"
        diags = await _open_and_wait_for_diagnostics(lsp_client, uri, _BUGGY_JAVA)
        codes = [d.code for d in diags]
        assert "null-return" in codes

    async def test_null_check_to_monadic_diagnostic_published(self, lsp_client: LanguageClient) -> None:
        """The if(x != null) pattern produces a null-check-to-monadic hint."""
        uri = "file:///test/BuggyExample2.java"
        diags = await _open_and_wait_for_diagnostics(lsp_client, uri, _BUGGY_JAVA)
        codes = [d.code for d in diags]
        assert "null-check-to-monadic" in codes

    async def test_try_catch_to_monadic_diagnostic_published(self, lsp_client: LanguageClient) -> None:
        """try/catch with single return produces a try-catch-to-monadic hint."""
        uri = "file:///test/TryCatch.java"
        diags = await _open_and_wait_for_diagnostics(lsp_client, uri, _TRY_CATCH_JAVA)
        codes = [d.code for d in diags]
        assert "try-catch-to-monadic" in codes

    async def test_clean_file_produces_no_diagnostics(self, lsp_client: LanguageClient) -> None:
        """A clean Java file should produce zero diagnostics."""
        uri = "file:///test/Clean.java"
        diags = await _open_and_wait_for_diagnostics(lsp_client, uri, _CLEAN_JAVA)
        assert len(diags) == 0

    async def test_null_return_code_action_quickfix(self, lsp_client: LanguageClient) -> None:
        """Request code action on null-return diagnostic → QuickFix with Option.none().

        This is the full round-trip: didOpen → publishDiagnostics → codeAction
        request with the real diagnostic → server returns a WorkspaceEdit.
        """
        uri = "file:///test/BuggyAction.java"
        diags = await _open_and_wait_for_diagnostics(lsp_client, uri, _BUGGY_JAVA)
        null_diag = next((d for d in diags if d.code == "null-return"), None)
        assert null_diag is not None

        actions = await lsp_client.text_document_code_action_async(
            lsp.CodeActionParams(
                text_document=lsp.TextDocumentIdentifier(uri=uri),
                range=null_diag.range,
                context=lsp.CodeActionContext(diagnostics=[null_diag]),
            )
        )

        assert actions is not None
        assert len(actions) >= 1
        action = actions[0]
        assert action.title == "Replace with Option.none()"
        assert action.kind == lsp.CodeActionKind.QuickFix
        assert action.edit is not None
        assert action.edit.changes is not None
        edits = action.edit.changes[uri]
        assert any("Option.none()" in e.new_text for e in edits)
        assert any("import io.vavr.control.Option;" in e.new_text for e in edits)

    async def test_try_catch_code_action_quickfix(self, lsp_client: LanguageClient) -> None:
        """Request code action on try-catch-to-monadic → QuickFix with Try.of()."""
        uri = "file:///test/TryCatchAction.java"
        diags = await _open_and_wait_for_diagnostics(lsp_client, uri, _TRY_CATCH_JAVA)
        try_diag = next((d for d in diags if d.code == "try-catch-to-monadic"), None)
        assert try_diag is not None

        actions = await lsp_client.text_document_code_action_async(
            lsp.CodeActionParams(
                text_document=lsp.TextDocumentIdentifier(uri=uri),
                range=try_diag.range,
                context=lsp.CodeActionContext(diagnostics=[try_diag]),
            )
        )

        assert actions is not None
        assert len(actions) >= 1
        action = actions[0]
        assert action.title == "Convert try/catch to Try monadic flow"
        assert action.edit is not None
        assert action.edit.changes is not None
        edits = action.edit.changes[uri]
        assert any("Try.of(() -> riskyRead())" in e.new_text for e in edits)
        assert any("import io.vavr.control.Try;" in e.new_text for e in edits)

    async def test_code_action_ignores_foreign_diagnostics(self, lsp_client: LanguageClient) -> None:
        """Diagnostics from other sources get no code actions from our server."""
        uri = "file:///test/Foreign.java"
        await _open_and_wait_for_diagnostics(lsp_client, uri, _BUGGY_JAVA)

        foreign_diag = lsp.Diagnostic(
            range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=5)),
            message="Some jdtls warning",
            severity=lsp.DiagnosticSeverity.Warning,
            code="something-jdtls",
            source="Java",
        )
        actions = await lsp_client.text_document_code_action_async(
            lsp.CodeActionParams(
                text_document=lsp.TextDocumentIdentifier(uri=uri),
                range=foreign_diag.range,
                context=lsp.CodeActionContext(diagnostics=[foreign_diag]),
            )
        )
        assert actions is None or len(actions) == 0

    async def test_diagnostics_update_on_file_change(self, lsp_client: LanguageClient) -> None:
        """didChange with a fixed file should clear diagnostics.

        Opens a buggy file, verifies diagnostics arrive, then sends a
        didChange with clean source and verifies diagnostics are cleared.
        """
        uri = "file:///test/Changing.java"
        diags = await _open_and_wait_for_diagnostics(lsp_client, uri, _BUGGY_JAVA)
        assert len(diags) > 0

        # Clear the notification cache and send a change to clean source.
        lsp_client._published.pop(uri, None)  # type: ignore[attr-defined]
        lsp_client.text_document_did_change(
            lsp.DidChangeTextDocumentParams(
                text_document=lsp.VersionedTextDocumentIdentifier(uri=uri, version=2),
                content_changes=[lsp.TextDocumentContentChangeWholeDocument(text=_CLEAN_JAVA)],
            )
        )

        # Wait for fresh diagnostics (debounced, ~150ms + processing).
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while loop.time() < deadline:
            if uri in lsp_client._published:  # type: ignore[attr-defined]
                break
            await asyncio.sleep(0.1)

        assert uri in lsp_client._published, "Timed out waiting for updated diagnostics after didChange"  # type: ignore[attr-defined]
        new_diags = lsp_client._published[uri]  # type: ignore[attr-defined]
        assert len(new_diags) == 0, f"Expected zero diagnostics after fixing, got {[d.code for d in new_diags]}"
