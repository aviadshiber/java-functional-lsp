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
        assert len(regs) == 14
        methods = {r.method for r in regs}
        assert lsp.TEXT_DOCUMENT_HOVER in methods
        assert lsp.TEXT_DOCUMENT_DEFINITION in methods
        assert lsp.TEXT_DOCUMENT_REFERENCES in methods
        assert lsp.TEXT_DOCUMENT_COMPLETION in methods
        assert lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL in methods
        assert lsp.TEXT_DOCUMENT_PREPARE_CALL_HIERARCHY in methods
        assert lsp.TEXT_DOCUMENT_SIGNATURE_HELP in methods
        assert lsp.TEXT_DOCUMENT_IMPLEMENTATION in methods
        assert lsp.TEXT_DOCUMENT_TYPE_DEFINITION in methods
        assert lsp.TEXT_DOCUMENT_DECLARATION in methods
        assert lsp.TEXT_DOCUMENT_DOCUMENT_HIGHLIGHT in methods
        assert lsp.TEXT_DOCUMENT_RENAME in methods
        assert lsp.TEXT_DOCUMENT_PREPARE_TYPE_HIERARCHY in methods
        assert lsp.WORKSPACE_SYMBOL in methods
        # All IDs are unique and use the shared prefix
        ids = {r.id for r in regs}
        assert len(ids) == 14
        assert all(rid.startswith(_JDTLS_REG_PREFIX) for rid in ids)
        # Document-scoped capabilities have java document selector; workspace-scoped do not
        workspace_scoped = {lsp.WORKSPACE_SYMBOL}
        for r in regs:
            assert r.register_options is not None
            if r.method in workspace_scoped:
                assert "documentSelector" not in r.register_options
            else:
                selectors = r.register_options["documentSelector"]
                assert any(s.get("language") == "java" for s in selectors)
        # Completion has triggerCharacters
        comp = next(r for r in regs if r.method == lsp.TEXT_DOCUMENT_COMPLETION)
        assert comp.register_options is not None
        assert comp.register_options.get("triggerCharacters") == ["."]
        # SignatureHelp has triggerCharacters
        sig = next(r for r in regs if r.method == lsp.TEXT_DOCUMENT_SIGNATURE_HELP)
        assert sig.register_options is not None
        assert sig.register_options.get("triggerCharacters") == ["(", ","]
        # Rename advertises prepareProvider so the IDE sends textDocument/prepareRename
        rename = next(r for r in regs if r.method == lsp.TEXT_DOCUMENT_RENAME)
        assert rename.register_options is not None
        assert rename.register_options.get("prepareProvider") is True

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
        # Handlers were registered for all methods (including call hierarchy)
        assert lsp.TEXT_DOCUMENT_HOVER in registered_methods
        assert lsp.TEXT_DOCUMENT_COMPLETION in registered_methods
        assert lsp.TEXT_DOCUMENT_DEFINITION in registered_methods
        assert lsp.TEXT_DOCUMENT_REFERENCES in registered_methods
        assert lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL in registered_methods
        assert lsp.TEXT_DOCUMENT_PREPARE_CALL_HIERARCHY in registered_methods
        assert lsp.CALL_HIERARCHY_INCOMING_CALLS in registered_methods
        assert lsp.CALL_HIERARCHY_OUTGOING_CALLS in registered_methods
        assert lsp.TEXT_DOCUMENT_SIGNATURE_HELP in registered_methods
        assert lsp.TEXT_DOCUMENT_IMPLEMENTATION in registered_methods
        assert lsp.TEXT_DOCUMENT_TYPE_DEFINITION in registered_methods
        assert lsp.TEXT_DOCUMENT_DECLARATION in registered_methods
        assert lsp.TEXT_DOCUMENT_DOCUMENT_HIGHLIGHT in registered_methods
        assert lsp.TEXT_DOCUMENT_RENAME in registered_methods
        assert lsp.TEXT_DOCUMENT_PREPARE_RENAME in registered_methods
        assert lsp.TEXT_DOCUMENT_PREPARE_TYPE_HIERARCHY in registered_methods
        assert lsp.TYPE_HIERARCHY_SUPERTYPES in registered_methods
        assert lsp.TYPE_HIERARCHY_SUBTYPES in registered_methods
        assert lsp.WORKSPACE_SYMBOL in registered_methods
        # client_register_capability_async was called
        mock_reg.assert_called_once()
        # Success log emitted
        assert any("jdtls capabilities registered" in r.getMessage() for r in caplog.records)

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
        """_lazy_start_jdtls logs success and flushes queue (no eager expansion)."""
        import logging
        from unittest.mock import AsyncMock, MagicMock, patch

        import java_functional_lsp.server as srv_mod
        from java_functional_lsp.server import _lazy_start_jdtls
        from java_functional_lsp.server import server as srv

        mock_flush = AsyncMock()
        old_flag = srv_mod._jdtls_capabilities_registered
        srv_mod._jdtls_capabilities_registered = False
        try:
            with (
                caplog.at_level(logging.INFO, logger="java_functional_lsp.server"),
                patch.object(srv._proxy, "ensure_started", AsyncMock(return_value=True)),
                patch.object(srv._proxy, "flush_queued_notifications", mock_flush),
                patch.object(srv, "feature", MagicMock(return_value=lambda fn: fn)),
                patch.object(srv, "client_register_capability_async", AsyncMock()),
            ):
                await _lazy_start_jdtls("file:///test/F.java")
        finally:
            srv_mod._jdtls_capabilities_registered = old_flag
        assert any("jdtls proxy active" in r.getMessage() for r in caplog.records)
        mock_flush.assert_called_once()

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

    async def test_ensure_module_and_forward_ready_module_fast_path(self) -> None:
        """READY module → single send_request, no add_module call."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _ensure_module_and_forward
        from java_functional_lsp.server import server as srv

        srv._proxy.modules.mark_added("file:///mod")
        srv._proxy.modules.mark_ready("file:///mod")
        mock_send = AsyncMock(return_value={"result": "ok"})
        try:
            with (
                patch.object(srv._proxy, "send_request", mock_send),
                patch.object(srv._proxy, "_available", True),
                patch("java_functional_lsp.server._resolve_module_uri", return_value="file:///mod"),
            ):
                result = await _ensure_module_and_forward("textDocument/hover", {}, "file:///mod/F.java")
        finally:
            srv._proxy.modules.clear()
        assert result == {"result": "ok"}
        assert mock_send.call_count == 1

    async def test_ensure_module_and_forward_new_module_waits_and_retries(self) -> None:
        """UNKNOWN module → add, first request null, wait_until_ready, retry succeeds."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _ensure_module_and_forward
        from java_functional_lsp.server import server as srv

        mock_add = AsyncMock(return_value="file:///mod")
        mock_send = AsyncMock(side_effect=[None, {"result": "ok"}])

        async def mock_wait(uri: str, timeout: float = 30.0) -> bool:  # pyright: ignore[reportUnusedParameter]
            srv._proxy.modules.mark_ready(uri)
            return True

        try:
            with (
                patch.object(srv._proxy, "add_module_if_new", mock_add),
                patch.object(srv._proxy, "send_request", mock_send),
                patch.object(srv._proxy, "_available", True),
                patch.object(srv._proxy.modules, "wait_until_ready", mock_wait),
                patch("java_functional_lsp.server._resolve_module_uri", return_value="file:///mod"),
            ):
                result = await _ensure_module_and_forward("textDocument/hover", {}, "file:///mod/F.java")
        finally:
            srv._proxy.modules.clear()
        assert result == {"result": "ok"}
        assert mock_send.call_count == 2

    async def test_ensure_module_and_forward_success_marks_ready(self) -> None:
        """First successful request marks module as READY."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.proxy import ModuleState
        from java_functional_lsp.server import _ensure_module_and_forward
        from java_functional_lsp.server import server as srv

        mock_add = AsyncMock(return_value="file:///mod")
        mock_send = AsyncMock(return_value={"result": "ok"})
        try:
            with (
                patch.object(srv._proxy, "add_module_if_new", mock_add),
                patch.object(srv._proxy, "send_request", mock_send),
                patch.object(srv._proxy, "_available", True),
                patch("java_functional_lsp.server._resolve_module_uri", return_value="file:///mod"),
            ):
                await _ensure_module_and_forward("textDocument/hover", {}, "file:///mod/F.java")
            assert srv._proxy.modules.get_state("file:///mod") == ModuleState.READY
        finally:
            srv._proxy.modules.clear()

    async def test_ensure_module_and_forward_lightweight_methods_skip_transition(self) -> None:
        """Methods in _LIGHTWEIGHT_METHODS do not transition module to READY."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.proxy import ModuleState
        from java_functional_lsp.server import _LIGHTWEIGHT_METHODS, _ensure_module_and_forward
        from java_functional_lsp.server import server as srv

        mock_add = AsyncMock(return_value="file:///mod")
        raw_result = [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}}]
        mock_send = AsyncMock(return_value=raw_result)
        try:
            for lightweight_method in _LIGHTWEIGHT_METHODS:
                srv._proxy.modules.clear()
                with (
                    patch.object(srv._proxy, "add_module_if_new", mock_add),
                    patch.object(srv._proxy, "send_request", mock_send),
                    patch.object(srv._proxy, "_available", True),
                    patch("java_functional_lsp.server._resolve_module_uri", return_value="file:///mod"),
                ):
                    await _ensure_module_and_forward(lightweight_method, {}, "file:///mod/F.java")
                # Module should NOT be READY — lightweight op does not confirm full indexing.
                assert srv._proxy.modules.get_state("file:///mod") != ModuleState.READY, (
                    f"{lightweight_method} should not mark module as READY"
                )
        finally:
            srv._proxy.modules.clear()

    async def test_on_prepare_call_hierarchy_forwards_to_jdtls(self) -> None:
        """_on_prepare_call_hierarchy forwards request and structures result."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_prepare_call_hierarchy

        item = {
            "name": "parsePrimaryEnrichmentResponse",
            "kind": 6,
            "uri": "file:///mod/Foo.java",
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 10}},
            "selectionRange": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 10}},
        }
        params = lsp.CallHierarchyPrepareParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
        )
        mock_forward = AsyncMock(return_value=[item])
        with patch("java_functional_lsp.server._ensure_module_and_forward", mock_forward):
            result = await _on_prepare_call_hierarchy(params)
        assert result is not None
        assert len(result) == 1
        assert result[0].name == "parsePrimaryEnrichmentResponse"
        mock_forward.assert_called_once_with("textDocument/prepareCallHierarchy", params, "file:///mod/Foo.java")

    async def test_on_prepare_call_hierarchy_returns_none_on_null(self) -> None:
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_prepare_call_hierarchy

        params = lsp.CallHierarchyPrepareParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
        )
        with patch("java_functional_lsp.server._ensure_module_and_forward", AsyncMock(return_value=None)):
            result = await _on_prepare_call_hierarchy(params)
        assert result is None

    async def test_on_incoming_calls_forwards_using_item_uri(self) -> None:
        """_on_incoming_calls uses params.item.uri as the file_uri for module resolution."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_incoming_calls

        item = lsp.CallHierarchyItem(
            name="caller",
            kind=lsp.SymbolKind.Method,
            uri="file:///mod/Foo.java",
            range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=5)),
            selection_range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=5)),
        )
        params = lsp.CallHierarchyIncomingCallsParams(item=item)
        incoming = {
            "from": {
                "name": "caller",
                "kind": 6,
                "uri": "file:///mod/Foo.java",
                "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 6}},
                "selectionRange": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 6}},
            },
            "fromRanges": [{"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 6}}],
        }
        mock_forward = AsyncMock(return_value=[incoming])
        with patch("java_functional_lsp.server._ensure_module_and_forward", mock_forward):
            result = await _on_incoming_calls(params)
        assert result is not None
        assert len(result) == 1
        mock_forward.assert_called_once_with("callHierarchy/incomingCalls", params, "file:///mod/Foo.java")

    async def test_on_incoming_calls_returns_none_on_null(self) -> None:
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_incoming_calls

        item = lsp.CallHierarchyItem(
            name="x",
            kind=lsp.SymbolKind.Method,
            uri="file:///mod/Foo.java",
            range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=1)),
            selection_range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=1)),
        )
        params = lsp.CallHierarchyIncomingCallsParams(item=item)
        with patch("java_functional_lsp.server._ensure_module_and_forward", AsyncMock(return_value=None)):
            result = await _on_incoming_calls(params)
        assert result is None

    async def test_on_outgoing_calls_forwards_using_item_uri(self) -> None:
        """_on_outgoing_calls uses params.item.uri as the file_uri for module resolution."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_outgoing_calls

        item = lsp.CallHierarchyItem(
            name="callee",
            kind=lsp.SymbolKind.Method,
            uri="file:///mod/Bar.java",
            range=lsp.Range(start=lsp.Position(line=5, character=0), end=lsp.Position(line=5, character=6)),
            selection_range=lsp.Range(start=lsp.Position(line=5, character=0), end=lsp.Position(line=5, character=6)),
        )
        params = lsp.CallHierarchyOutgoingCallsParams(item=item)
        outgoing = {
            "to": {
                "name": "callee",
                "kind": 6,
                "uri": "file:///mod/Bar.java",
                "range": {"start": {"line": 5, "character": 0}, "end": {"line": 5, "character": 6}},
                "selectionRange": {"start": {"line": 5, "character": 0}, "end": {"line": 5, "character": 6}},
            },
            "fromRanges": [{"start": {"line": 5, "character": 0}, "end": {"line": 5, "character": 6}}],
        }
        mock_forward = AsyncMock(return_value=[outgoing])
        with patch("java_functional_lsp.server._ensure_module_and_forward", mock_forward):
            result = await _on_outgoing_calls(params)
        assert result is not None
        assert len(result) == 1
        mock_forward.assert_called_once_with("callHierarchy/outgoingCalls", params, "file:///mod/Bar.java")

    async def test_on_signature_help_forwards_to_jdtls(self) -> None:
        """_on_signature_help forwards and structures a SignatureHelp result."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_signature_help

        params = lsp.SignatureHelpParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=5, character=10),
        )
        raw = {"signatures": [{"label": "foo(int x)"}], "activeSignature": 0}
        mock_forward = AsyncMock(return_value=raw)
        with patch("java_functional_lsp.server._ensure_module_and_forward", mock_forward):
            result = await _on_signature_help(params)
        assert result is not None
        mock_forward.assert_called_once_with("textDocument/signatureHelp", params, "file:///mod/Foo.java")

    async def test_on_signature_help_returns_none_on_null(self) -> None:
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_signature_help

        params = lsp.SignatureHelpParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
        )
        with patch("java_functional_lsp.server._ensure_module_and_forward", AsyncMock(return_value=None)):
            assert await _on_signature_help(params) is None

    async def test_on_implementation_forwards_using_text_document_uri(self) -> None:
        """_on_implementation uses params.text_document.uri and handles list result."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_implementation

        params = lsp.ImplementationParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
        )
        loc = {
            "uri": "file:///mod/Impl.java",
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}},
        }
        mock_forward = AsyncMock(return_value=[loc])
        with patch("java_functional_lsp.server._ensure_module_and_forward", mock_forward):
            result = await _on_implementation(params)
        assert result is not None
        assert len(result) == 1
        mock_forward.assert_called_once_with("textDocument/implementation", params, "file:///mod/Foo.java")

    async def test_on_implementation_returns_none_on_null(self) -> None:
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_implementation

        params = lsp.ImplementationParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
        )
        with patch("java_functional_lsp.server._ensure_module_and_forward", AsyncMock(return_value=None)):
            assert await _on_implementation(params) is None

    async def test_on_document_highlight_forwards_to_jdtls(self) -> None:
        """_on_document_highlight forwards and structures list result."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_document_highlight

        params = lsp.DocumentHighlightParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
        )
        raw = [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}}]
        mock_forward = AsyncMock(return_value=raw)
        with patch("java_functional_lsp.server._ensure_module_and_forward", mock_forward):
            result = await _on_document_highlight(params)
        assert result is not None
        assert len(result) == 1
        mock_forward.assert_called_once_with("textDocument/documentHighlight", params, "file:///mod/Foo.java")

    async def test_on_rename_forwards_and_returns_workspace_edit(self) -> None:
        """_on_rename forwards and structures a WorkspaceEdit."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_rename

        params = lsp.RenameParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
            new_name="Bar",
        )
        raw: dict[str, object] = {"changes": {}}
        mock_forward = AsyncMock(return_value=raw)
        with patch("java_functional_lsp.server._ensure_module_and_forward", mock_forward):
            result = await _on_rename(params)
        assert result is not None
        mock_forward.assert_called_once_with("textDocument/rename", params, "file:///mod/Foo.java")

    async def test_on_rename_returns_none_on_null(self) -> None:
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_rename

        params = lsp.RenameParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
            new_name="Bar",
        )
        with patch("java_functional_lsp.server._ensure_module_and_forward", AsyncMock(return_value=None)):
            assert await _on_rename(params) is None

    async def test_on_prepare_type_hierarchy_forwards_to_jdtls(self) -> None:
        """_on_prepare_type_hierarchy forwards and structures TypeHierarchyItem list."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_prepare_type_hierarchy

        params = lsp.TypeHierarchyPrepareParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
        )
        item = {
            "name": "Foo",
            "kind": 5,
            "uri": "file:///mod/Foo.java",
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}},
            "selectionRange": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}},
        }
        mock_forward = AsyncMock(return_value=[item])
        with patch("java_functional_lsp.server._ensure_module_and_forward", mock_forward):
            result = await _on_prepare_type_hierarchy(params)
        assert result is not None
        assert len(result) == 1
        assert result[0].name == "Foo"
        mock_forward.assert_called_once_with("textDocument/prepareTypeHierarchy", params, "file:///mod/Foo.java")

    async def test_on_type_hierarchy_supertypes_uses_item_uri(self) -> None:
        """_on_type_hierarchy_supertypes uses params.item.uri for module resolution."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_type_hierarchy_supertypes

        item = lsp.TypeHierarchyItem(
            name="Foo",
            kind=lsp.SymbolKind.Class,
            uri="file:///mod/Foo.java",
            range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=3)),
            selection_range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=3)),
        )
        params = lsp.TypeHierarchySupertypesParams(item=item)
        parent = {
            "name": "Base",
            "kind": 5,
            "uri": "file:///mod/Base.java",
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}},
            "selectionRange": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}},
        }
        mock_forward = AsyncMock(return_value=[parent])
        with patch("java_functional_lsp.server._ensure_module_and_forward", mock_forward):
            result = await _on_type_hierarchy_supertypes(params)
        assert result is not None
        assert len(result) == 1
        assert result[0].name == "Base"
        mock_forward.assert_called_once_with("typeHierarchy/supertypes", params, "file:///mod/Foo.java")

    async def test_on_type_hierarchy_subtypes_uses_item_uri(self) -> None:
        """_on_type_hierarchy_subtypes uses params.item.uri for module resolution."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_type_hierarchy_subtypes

        item = lsp.TypeHierarchyItem(
            name="Base",
            kind=lsp.SymbolKind.Class,
            uri="file:///mod/Base.java",
            range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=4)),
            selection_range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=4)),
        )
        params = lsp.TypeHierarchySubtypesParams(item=item)
        child = {
            "name": "Impl",
            "kind": 5,
            "uri": "file:///mod/Impl.java",
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}},
            "selectionRange": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}},
        }
        mock_forward = AsyncMock(return_value=[child])
        with patch("java_functional_lsp.server._ensure_module_and_forward", mock_forward):
            result = await _on_type_hierarchy_subtypes(params)
        assert result is not None
        assert len(result) == 1
        assert result[0].name == "Impl"
        mock_forward.assert_called_once_with("typeHierarchy/subtypes", params, "file:///mod/Base.java")

    async def test_on_type_definition_forwards_to_jdtls(self) -> None:
        """_on_type_definition uses params.text_document.uri and handles list result."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_type_definition

        params = lsp.TypeDefinitionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
        )
        loc = {
            "uri": "file:///mod/FooType.java",
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 7}},
        }
        mock_forward = AsyncMock(return_value=[loc])
        with patch("java_functional_lsp.server._ensure_module_and_forward", mock_forward):
            result = await _on_type_definition(params)
        assert result is not None
        assert len(result) == 1
        mock_forward.assert_called_once_with("textDocument/typeDefinition", params, "file:///mod/Foo.java")

    async def test_on_declaration_forwards_to_jdtls(self) -> None:
        """_on_declaration uses params.text_document.uri and handles list result."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_declaration

        params = lsp.DeclarationParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
        )
        loc = {
            "uri": "file:///mod/IFoo.java",
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}},
        }
        mock_forward = AsyncMock(return_value=[loc])
        with patch("java_functional_lsp.server._ensure_module_and_forward", mock_forward):
            result = await _on_declaration(params)
        assert result is not None
        assert len(result) == 1
        mock_forward.assert_called_once_with("textDocument/declaration", params, "file:///mod/Foo.java")

    async def test_on_prepare_rename_forwards_to_jdtls(self) -> None:
        """_on_prepare_rename forwards and structures a Range result."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_prepare_rename

        params = lsp.PrepareRenameParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=3, character=5),
        )
        raw = {"start": {"line": 3, "character": 4}, "end": {"line": 3, "character": 7}}
        mock_forward = AsyncMock(return_value=raw)
        with patch("java_functional_lsp.server._ensure_module_and_forward", mock_forward):
            result = await _on_prepare_rename(params)
        assert result is not None
        mock_forward.assert_called_once_with("textDocument/prepareRename", params, "file:///mod/Foo.java")

    async def test_on_workspace_symbol_uses_proxy_directly(self) -> None:
        """_on_workspace_symbol bypasses _ensure_module_and_forward and calls proxy.send_request."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_workspace_symbol
        from java_functional_lsp.server import server as srv

        params = lsp.WorkspaceSymbolParams(query="Foo")
        raw = [
            {
                "name": "FooService",
                "kind": 5,
                "location": {
                    "uri": "file:///mod/FooService.java",
                    "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 10}},
                },
            }
        ]
        mock_send = AsyncMock(return_value=raw)
        with (
            patch.object(srv._proxy, "_available", True),
            patch.object(srv._proxy, "send_request", mock_send),
        ):
            result = await _on_workspace_symbol(params)
        assert result is not None
        assert len(result) == 1
        assert result[0].name == "FooService"
        mock_send.assert_called_once()
        assert mock_send.call_args[0][0] == "workspace/symbol"

    async def test_on_workspace_symbol_returns_none_when_unavailable(self) -> None:
        """_on_workspace_symbol returns None when jdtls is not available."""
        from unittest.mock import patch

        from java_functional_lsp.server import _on_workspace_symbol
        from java_functional_lsp.server import server as srv

        params = lsp.WorkspaceSymbolParams(query="anything")
        with patch.object(srv._proxy, "_available", False):
            result = await _on_workspace_symbol(params)
        assert result is None

    async def test_on_document_highlight_returns_none_on_null(self) -> None:
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_document_highlight

        params = lsp.DocumentHighlightParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
        )
        with patch("java_functional_lsp.server._ensure_module_and_forward", AsyncMock(return_value=None)):
            assert await _on_document_highlight(params) is None

    async def test_on_type_definition_returns_none_on_null(self) -> None:
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_type_definition

        params = lsp.TypeDefinitionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
        )
        with patch("java_functional_lsp.server._ensure_module_and_forward", AsyncMock(return_value=None)):
            assert await _on_type_definition(params) is None

    async def test_on_declaration_returns_none_on_null(self) -> None:
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_declaration

        params = lsp.DeclarationParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
        )
        with patch("java_functional_lsp.server._ensure_module_and_forward", AsyncMock(return_value=None)):
            assert await _on_declaration(params) is None

    async def test_on_prepare_rename_returns_none_on_null(self) -> None:
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_prepare_rename

        params = lsp.PrepareRenameParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
        )
        with patch("java_functional_lsp.server._ensure_module_and_forward", AsyncMock(return_value=None)):
            assert await _on_prepare_rename(params) is None

    async def test_on_prepare_rename_returns_placeholder_when_jdtls_sends_placeholder(self) -> None:
        """_on_prepare_rename handles PrepareRenamePlaceholder (the typical jdtls response)."""
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_prepare_rename

        params = lsp.PrepareRenameParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=3, character=5),
        )
        raw = {
            "range": {"start": {"line": 3, "character": 4}, "end": {"line": 3, "character": 7}},
            "placeholder": "foo",
        }
        mock_forward = AsyncMock(return_value=raw)
        with patch("java_functional_lsp.server._ensure_module_and_forward", mock_forward):
            result = await _on_prepare_rename(params)
        assert isinstance(result, lsp.PrepareRenamePlaceholder)
        assert result.placeholder == "foo"

    async def test_on_prepare_type_hierarchy_returns_none_on_null(self) -> None:
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_prepare_type_hierarchy

        params = lsp.TypeHierarchyPrepareParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///mod/Foo.java"),
            position=lsp.Position(line=0, character=0),
        )
        with patch("java_functional_lsp.server._ensure_module_and_forward", AsyncMock(return_value=None)):
            assert await _on_prepare_type_hierarchy(params) is None

    async def test_on_type_hierarchy_supertypes_returns_none_on_null(self) -> None:
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_type_hierarchy_supertypes

        item = lsp.TypeHierarchyItem(
            name="Foo",
            kind=lsp.SymbolKind.Class,
            uri="file:///mod/Foo.java",
            range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=3)),
            selection_range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=3)),
        )
        params = lsp.TypeHierarchySupertypesParams(item=item)
        with patch("java_functional_lsp.server._ensure_module_and_forward", AsyncMock(return_value=None)):
            assert await _on_type_hierarchy_supertypes(params) is None

    async def test_on_type_hierarchy_subtypes_returns_none_on_null(self) -> None:
        from unittest.mock import AsyncMock, patch

        from java_functional_lsp.server import _on_type_hierarchy_subtypes

        item = lsp.TypeHierarchyItem(
            name="Base",
            kind=lsp.SymbolKind.Class,
            uri="file:///mod/Base.java",
            range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=4)),
            selection_range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=4)),
        )
        params = lsp.TypeHierarchySubtypesParams(item=item)
        with patch("java_functional_lsp.server._ensure_module_and_forward", AsyncMock(return_value=None)):
            assert await _on_type_hierarchy_subtypes(params) is None

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
        ca = result[0]
        assert isinstance(ca, lsp.CodeAction)
        assert ca.edit is not None
        assert ca.edit.changes is not None
        assert any("Try.of" in e.new_text for e in ca.edit.changes[uri])

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
# --------------------------------------------------------------------------
# Lombok support tests
# --------------------------------------------------------------------------


class TestUserSuppressPatterns:
    """Tests for configurable suppressJdtlsPatterns."""

    def test_user_pattern_matches(self) -> None:
        from java_functional_lsp.server import _compile_user_patterns, _is_jdtls_suppressed

        patterns = _compile_user_patterns({"suppressJdtlsPatterns": [r"The method generateReport\(\) is undefined"]})
        diag = {"message": "The method generateReport() is undefined for the type InternalService"}
        assert _is_jdtls_suppressed(diag, patterns) is True

    def test_user_pattern_does_not_match_unrelated(self) -> None:
        from java_functional_lsp.server import _compile_user_patterns, _is_jdtls_suppressed

        patterns = _compile_user_patterns({"suppressJdtlsPatterns": [r"The method generateReport\(\) is undefined"]})
        diag = {"message": "The method foo() is undefined for the type String"}
        assert _is_jdtls_suppressed(diag, patterns) is False

    def test_no_patterns_passes_everything(self) -> None:
        """With no user patterns, nothing is suppressed."""
        from java_functional_lsp.server import _is_jdtls_suppressed

        diag = {"message": "The method builder() is undefined for the type Foo"}
        assert _is_jdtls_suppressed(diag, []) is False

    def test_invalid_regex_skipped(self) -> None:
        from java_functional_lsp.server import _compile_user_patterns

        patterns = _compile_user_patterns({"suppressJdtlsPatterns": [r"valid", r"[invalid"]})
        assert len(patterns) == 1
        assert patterns[0].search("valid") is not None

    def test_non_list_ignored(self) -> None:
        from java_functional_lsp.server import _compile_user_patterns

        patterns = _compile_user_patterns({"suppressJdtlsPatterns": "not a list"})
        assert patterns == []

    def test_empty_config(self) -> None:
        from java_functional_lsp.server import _compile_user_patterns

        patterns = _compile_user_patterns({})
        assert patterns == []


class TestJdtlsIsolation:
    """Tests that jdtls failures don't suppress custom diagnostics."""

    def test_jdtls_exception_does_not_suppress_custom_diags(self) -> None:
        """If jdtls diagnostic processing throws, custom diagnostics still publish."""
        from unittest.mock import patch

        from java_functional_lsp.server import _run_analysis

        java_source = "public class Foo { public String bar() { return null; } }"

        with patch("java_functional_lsp.server.server") as mock_server:
            mock_server._proxy.is_available = True
            mock_server._proxy.get_cached_diagnostics.side_effect = RuntimeError("jdtls corrupt")
            mock_server._user_suppress_patterns = []
            mock_server._config = {}
            mock_server._parser = __import__("java_functional_lsp.analyzers.base", fromlist=["get_parser"]).get_parser()

            result = _run_analysis(java_source, "file:///test/Foo.java")

        custom_diags = [d for d in result if d.source == "java-functional-lsp"]
        assert len(custom_diags) > 0, "Custom diagnostics should publish even when jdtls fails"

    def test_jdtls_unavailable_still_publishes_custom_diags(self) -> None:
        """When jdtls is not available, custom diagnostics still publish."""
        from unittest.mock import patch

        from java_functional_lsp.server import _run_analysis

        java_source = "public class Foo { public String bar() { return null; } }"

        with patch("java_functional_lsp.server.server") as mock_server:
            mock_server._proxy.is_available = False
            mock_server._config = {}
            mock_server._parser = __import__("java_functional_lsp.analyzers.base", fromlist=["get_parser"]).get_parser()

            result = _run_analysis(java_source, "file:///test/Foo.java")

        custom_diags = [d for d in result if d.source == "java-functional-lsp"]
        assert len(custom_diags) > 0, "Custom diagnostics should publish without jdtls"

    def test_analyze_and_publish_exception_does_not_crash_handler(self) -> None:
        """Handler-level try/except prevents server crash when _analyze_and_publish raises."""
        from unittest.mock import patch

        from java_functional_lsp.server import _deferred_validate

        with patch("java_functional_lsp.server._analyze_and_publish", side_effect=RuntimeError("boom")):
            import asyncio

            loop = asyncio.new_event_loop()
            try:
                # _deferred_validate should catch the exception and log, not raise
                loop.run_until_complete(_deferred_validate("file:///test/Foo.java"))
            finally:
                loop.close()
        # If we get here without exception, the handler caught it correctly


class TestJdtlsSkipDetection:
    """Tests for auto-detecting JetBrains IDEs and skipping jdtls."""

    def _make_init_params(self, client_name: str | None = None) -> Any:
        """Create minimal InitializeParams with optional client_info."""
        return lsp.InitializeParams(
            capabilities=lsp.ClientCapabilities(),
            root_uri="file:///tmp/test",
            client_info=lsp.ClientInfo(name=client_name) if client_name else None,
        )

    def _run_init(self, monkeypatch: Any, client_name: str | None, env_value: str | None = None) -> None:
        """Run on_initialize with controlled env and client_info."""
        from java_functional_lsp.server import on_initialize, server

        server._skip_jdtls = False
        server._skip_jdtls_registration = False
        if env_value is not None:
            monkeypatch.setenv("JAVA_FUNCTIONAL_LSP_JDTLS", env_value)
        else:
            monkeypatch.delenv("JAVA_FUNCTIONAL_LSP_JDTLS", raising=False)
        on_initialize(self._make_init_params(client_name))

    def test_intellij_client_info_skips_jdtls(self, monkeypatch: Any) -> None:
        """IntelliJ IDEA detected → _skip_jdtls = True."""
        from java_functional_lsp.server import server

        self._run_init(monkeypatch, "IntelliJ IDEA")
        assert server._skip_jdtls is True

    def test_jetbrains_product_skips_jdtls(self, monkeypatch: Any) -> None:
        """Any JetBrains product detected → _skip_jdtls = True."""
        from java_functional_lsp.server import server

        self._run_init(monkeypatch, "JetBrains Rider 2025.2")
        assert server._skip_jdtls is True

    def test_vscode_allows_jdtls(self, monkeypatch: Any) -> None:
        """VS Code → _skip_jdtls = False (jdtls needed)."""
        from java_functional_lsp.server import server

        self._run_init(monkeypatch, "Visual Studio Code")
        assert server._skip_jdtls is False

    def test_no_client_info_allows_jdtls(self, monkeypatch: Any) -> None:
        """No client_info → _skip_jdtls = False (safe default)."""
        from java_functional_lsp.server import server

        self._run_init(monkeypatch, None)
        assert server._skip_jdtls is False

    def test_env_var_off_overrides_detection(self, monkeypatch: Any) -> None:
        """JAVA_FUNCTIONAL_LSP_JDTLS=off disables jdtls even for non-JetBrains client."""
        from java_functional_lsp.server import server

        self._run_init(monkeypatch, "Visual Studio Code", env_value="off")
        assert server._skip_jdtls is True

    def test_env_var_on_overrides_intellij_detection(self, monkeypatch: Any) -> None:
        """JAVA_FUNCTIONAL_LSP_JDTLS=on force-enables jdtls even for IntelliJ."""
        from java_functional_lsp.server import server

        self._run_init(monkeypatch, "IntelliJ IDEA", env_value="on")
        assert server._skip_jdtls is False

    def test_env_var_no_register(self, monkeypatch: Any) -> None:
        """JAVA_FUNCTIONAL_LSP_JDTLS=no-register keeps jdtls but skips registration."""
        from java_functional_lsp.server import server

        self._run_init(monkeypatch, "Visual Studio Code", env_value="no-register")
        assert server._skip_jdtls is False
        assert server._skip_jdtls_registration is True

    def test_custom_diagnostics_publish_when_jdtls_skipped(self) -> None:
        """Tree-sitter analysis works normally when jdtls is skipped."""
        from unittest.mock import patch

        from java_functional_lsp.server import _run_analysis

        java_source = "public class Foo { public String bar() { return null; } }"

        with patch("java_functional_lsp.server.server") as mock_server:
            mock_server._proxy.is_available = False
            mock_server._skip_jdtls = True
            mock_server._config = {}
            mock_server._parser = __import__("java_functional_lsp.analyzers.base", fromlist=["get_parser"]).get_parser()

            result = _run_analysis(java_source, "file:///test/Foo.java")

        custom_diags = [d for d in result if d.source == "java-functional-lsp"]
        assert len(custom_diags) > 0, "Custom diagnostics should publish when jdtls is skipped"
        assert any(d.code == "null-return" for d in custom_diags), "null-return diagnostic expected"


class TestFindLombokJar:
    """Tests for _find_lombok_jar()."""

    def test_config_path_wins(self, tmp_path: Any) -> None:
        from java_functional_lsp.proxy import _find_lombok_jar

        jar = tmp_path / "lombok.jar"
        jar.touch()
        result = _find_lombok_jar({"lombok": str(jar)})
        assert result == str(jar)

    def test_env_var_used(self, tmp_path: Any, monkeypatch: Any) -> None:
        from java_functional_lsp.proxy import _find_lombok_jar

        jar = tmp_path / "lombok.jar"
        jar.touch()
        monkeypatch.setenv("LOMBOK_JAR", str(jar))
        result = _find_lombok_jar()
        assert result == str(jar)

    def test_config_wins_over_env(self, tmp_path: Any, monkeypatch: Any) -> None:
        from java_functional_lsp.proxy import _find_lombok_jar

        config_jar = tmp_path / "config-lombok.jar"
        config_jar.touch()
        env_jar = tmp_path / "env-lombok.jar"
        env_jar.touch()
        monkeypatch.setenv("LOMBOK_JAR", str(env_jar))
        result = _find_lombok_jar({"lombok": str(config_jar)})
        assert result == str(config_jar)

    def test_maven_cache_semantic_sort(self, tmp_path: Any, monkeypatch: Any) -> None:
        from java_functional_lsp.proxy import _find_lombok_jar

        monkeypatch.delenv("LOMBOK_JAR", raising=False)
        m2 = tmp_path / ".m2" / "repository" / "org" / "projectlombok" / "lombok"
        for v in ["1.18.4", "1.18.30", "1.9.2"]:
            d = m2 / v
            d.mkdir(parents=True)
            (d / f"lombok-{v}.jar").touch()
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = _find_lombok_jar()
        assert result is not None
        assert "1.18.30" in result  # Semantic sort picks 1.18.30 over 1.9.2

    def test_returns_none_when_nothing_found(self, tmp_path: Any, monkeypatch: Any) -> None:
        from java_functional_lsp.proxy import _find_lombok_jar

        monkeypatch.delenv("LOMBOK_JAR", raising=False)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = _find_lombok_jar()
        assert result is None

    def test_warns_on_missing_config_path(self, caplog: Any) -> None:
        import logging

        from java_functional_lsp.proxy import _find_lombok_jar

        with caplog.at_level(logging.WARNING, logger="java_functional_lsp.proxy"):
            _find_lombok_jar({"lombok": "/nonexistent/lombok.jar"})
        assert any("does not exist" in r.getMessage() for r in caplog.records)


class TestCacheClear:
    """Tests for _clear_cache_on_version_change()."""

    def test_clears_on_version_mismatch(self, tmp_path: Any) -> None:
        from java_functional_lsp.proxy import _clear_cache_on_version_change

        cache = tmp_path / "jdtls-data"
        cache.mkdir()
        stale_dir = cache / "abc123"
        stale_dir.mkdir()
        (cache / ".version").write_text("0.7.9")

        _clear_cache_on_version_change(cache)

        assert not stale_dir.exists(), "Stale data-dir should be cleared"
        assert cache.exists(), "Cache root should be recreated"
        from java_functional_lsp import __version__

        assert (cache / ".version").read_text() == __version__

    def test_keeps_cache_on_same_version(self, tmp_path: Any) -> None:
        from java_functional_lsp import __version__
        from java_functional_lsp.proxy import _clear_cache_on_version_change

        cache = tmp_path / "jdtls-data"
        cache.mkdir()
        existing_dir = cache / "abc123"
        existing_dir.mkdir()
        (cache / ".version").write_text(__version__)

        _clear_cache_on_version_change(cache)

        assert existing_dir.exists(), "Existing data-dir should be preserved"

    def test_creates_marker_on_first_run(self, tmp_path: Any) -> None:
        from java_functional_lsp.proxy import _clear_cache_on_version_change

        cache = tmp_path / "jdtls-data"
        _clear_cache_on_version_change(cache)

        from java_functional_lsp import __version__

        assert (cache / ".version").read_text() == __version__


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
        assert isinstance(action, lsp.CodeAction)
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
        assert isinstance(action, lsp.CodeAction)
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
