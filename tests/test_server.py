"""Integration tests for the LanguageServer — analyzer pipeline + code actions.

These tests drive the real ``java_functional_lsp.server`` module through the
same code paths IntelliJ/VS Code hit: ``_run_analysis`` for diagnostics and
``on_code_action`` for QuickFix generation. They bootstrap a real pygls
workspace, inject documents, and assert on the results.

They run in the regular unit matrix (no jdtls required, no ``@pytest.mark.e2e``).
"""

from __future__ import annotations

from typing import Any

from lsprotocol import types as lsp
from pygls.workspace import Workspace

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


def _ensure_workspace() -> None:
    """Bootstrap a minimal pygls workspace if not already initialized."""
    from java_functional_lsp.server import server

    if server.protocol._workspace is None:
        server.protocol._workspace = Workspace(
            root_uri="file:///test",
            sync_kind=lsp.TextDocumentSyncKind.Full,
        )


class TestServerHelpers:
    """Tests for server.py internal helpers."""

    def test_load_config_returns_empty_for_no_workspace(self) -> None:
        from java_functional_lsp.server import _load_config

        assert _load_config(None) == {}
        assert _load_config("") == {}

    def test_load_config_returns_empty_for_missing_file(self, tmp_path: Any) -> None:
        from java_functional_lsp.server import _load_config

        assert _load_config(str(tmp_path)) == {}

    def test_load_config_reads_json(self, tmp_path: Any) -> None:
        from java_functional_lsp.server import _load_config

        config_file = tmp_path / ".java-functional-lsp.json"
        config_file.write_text('{"rules": {"null-return": "off"}}')
        result = _load_config(str(tmp_path))
        assert result == {"rules": {"null-return": "off"}}

    def test_load_config_handles_invalid_json(self, tmp_path: Any) -> None:
        from java_functional_lsp.server import _load_config

        config_file = tmp_path / ".java-functional-lsp.json"
        config_file.write_text("not valid json {{{")
        result = _load_config(str(tmp_path))
        assert result == {}

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
            code="test-rule",
            message="test message",
            data=DiagnosticData(fix_type="FIX", target_library="lib", rationale="reason"),
        )
        result = _to_lsp_diagnostic(diag)
        assert result.severity == lsp.DiagnosticSeverity.Hint
        assert result.data is not None
        assert result.data["fixType"] == "FIX"

    def test_to_lsp_diagnostic_without_data(self) -> None:
        from java_functional_lsp.analyzers.base import Diagnostic as LintDiag
        from java_functional_lsp.analyzers.base import Severity
        from java_functional_lsp.server import _to_lsp_diagnostic

        diag = LintDiag(line=0, col=0, end_line=0, end_col=5, severity=Severity.WARNING, code="x", message="msg")
        result = _to_lsp_diagnostic(diag)
        assert result.data is None

    def test_analyze_document_with_excludes(self) -> None:
        from java_functional_lsp.server import _analyze_document, server

        old_config = server._config
        server._config = {"excludes": ["**/generated/**"]}
        try:
            result = _analyze_document(
                "class T { String f() { return null; } }",
                "file:///project/src/main/generated/Foo.java",
            )
            assert result == []
        finally:
            server._config = old_config

    def test_analyze_document_without_excludes(self) -> None:
        from java_functional_lsp.server import _analyze_document

        result = _analyze_document("class T { String f() { return null; } }", "file:///Foo.java")
        assert any(d.code == "null-return" for d in result)

    def test_serialize_params_camelcase(self) -> None:
        """The LSP converter must emit camelCase field names."""
        from java_functional_lsp.server import _serialize_params

        params = lsp.DefinitionParams(
            text_document=lsp.TextDocumentIdentifier(uri="file:///x.java"),
            position=lsp.Position(line=0, character=0),
        )
        result = _serialize_params(params)
        assert "textDocument" in result
        assert "text_document" not in result

    def test_handle_exception_logs(self, caplog: Any) -> None:
        """sys.excepthook is wired to _handle_exception for crash debugging."""
        import logging

        from java_functional_lsp.server import _handle_exception

        with caplog.at_level(logging.ERROR, logger="java_functional_lsp.server"):
            _handle_exception(ValueError, ValueError("test crash"), None)
        assert any("Uncaught exception" in r.getMessage() for r in caplog.records)

    def test_jdtls_raw_to_lsp_diagnostics_valid(self) -> None:
        """Raw jdtls diagnostic dicts should be structured into lsp.Diagnostic."""
        from java_functional_lsp.server import _jdtls_raw_to_lsp_diagnostics

        raw = [
            {
                "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 10}},
                "severity": 2,
                "code": "jdt.warning",
                "source": "Java",
                "message": "Unused import",
            }
        ]
        result = _jdtls_raw_to_lsp_diagnostics(raw)
        assert len(result) == 1
        assert result[0].message == "Unused import"
        assert result[0].source == "Java"

    def test_jdtls_raw_to_lsp_diagnostics_malformed(self) -> None:
        """Completely broken raw diagnostics should not crash, just be skipped."""
        from java_functional_lsp.server import _jdtls_raw_to_lsp_diagnostics

        result = _jdtls_raw_to_lsp_diagnostics([42, None, "not a dict"])
        assert result == []

    def test_on_jdtls_diagnostics_callback(self) -> None:
        """The server's _on_jdtls_diagnostics callback re-analyzes the document."""
        from unittest.mock import patch

        from java_functional_lsp.server import server

        _ensure_workspace()
        uri = "file:///test/Callback.java"
        server.workspace.put_text_document(
            lsp.TextDocumentItem(
                uri=uri, language_id="java", version=1, text="class T { String f() { return null; } }"
            ),
        )
        try:
            # Mock publish to avoid transport errors
            with patch.object(server, "text_document_publish_diagnostics") as mock_pub:
                server._on_jdtls_diagnostics(uri, [])
                mock_pub.assert_called_once()
                published = mock_pub.call_args[0][0]
                # Verify our custom analyzer found the null-return
                codes = [d.code for d in published.diagnostics]
                assert "null-return" in codes
        finally:
            server.workspace.remove_text_document(uri)


class TestAnalyzerPipeline:
    """Verify the full analyzer chain produces the expected diagnostics."""

    def test_null_return_diagnostic(self) -> None:
        from java_functional_lsp.server import _run_analysis

        diags = _run_analysis(_BUGGY_JAVA, "file:///test/BuggyExample.java")
        codes = [d.code for d in diags]
        assert "null-return" in codes

    def test_null_check_to_monadic_diagnostic(self) -> None:
        from java_functional_lsp.server import _run_analysis

        diags = _run_analysis(_BUGGY_JAVA, "file:///test/BuggyExample.java")
        codes = [d.code for d in diags]
        assert "null-check-to-monadic" in codes

    def test_try_catch_to_monadic_diagnostic(self) -> None:
        from java_functional_lsp.server import _run_analysis

        diags = _run_analysis(_TRY_CATCH_JAVA, "file:///test/TryCatch.java")
        codes = [d.code for d in diags]
        assert "try-catch-to-monadic" in codes

    def test_no_diagnostics_on_clean_file(self) -> None:
        from java_functional_lsp.server import _run_analysis

        clean = "public class Clean {\n    public int add(int a, int b) {\n        return a + b;\n    }\n}\n"
        diags = _run_analysis(clean, "file:///test/Clean.java")
        assert len(diags) == 0


class TestCodeActionPipeline:
    """Verify the code-action handler produces valid WorkspaceEdits."""

    def test_null_return_quickfix(self) -> None:
        """null-return diagnostic → QuickFix with Option.none() + auto-import."""
        from java_functional_lsp.server import on_code_action, server

        _ensure_workspace()
        uri = "file:///test/BuggyExample.java"
        server.workspace.put_text_document(
            lsp.TextDocumentItem(uri=uri, language_id="java", version=1, text=_BUGGY_JAVA),
        )
        try:
            diag = lsp.Diagnostic(
                range=lsp.Range(start=lsp.Position(line=7, character=19), end=lsp.Position(line=7, character=23)),
                message="Avoid returning null.",
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
        assert len(result) >= 1
        action = result[0]
        assert action.kind == lsp.CodeActionKind.QuickFix
        assert action.title == "Replace with Option.none()"
        assert action.edit is not None
        assert action.edit.changes is not None
        edits = action.edit.changes[uri]
        assert any("Option.none()" in e.new_text for e in edits)
        assert any("import io.vavr.control.Option;" in e.new_text for e in edits)

    def test_try_catch_to_monadic_quickfix(self) -> None:
        """try-catch-to-monadic diagnostic → QuickFix with Try.of() + auto-import."""
        from java_functional_lsp.server import on_code_action, server

        _ensure_workspace()
        uri = "file:///test/TryCatch.java"
        server.workspace.put_text_document(
            lsp.TextDocumentItem(uri=uri, language_id="java", version=1, text=_TRY_CATCH_JAVA),
        )
        try:
            # Diagnostic on the `try` keyword (line 4, cols 8-11)
            diag = lsp.Diagnostic(
                range=lsp.Range(start=lsp.Position(line=4, character=8), end=lsp.Position(line=4, character=11)),
                message="Imperative try/catch.",
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
        assert len(result) >= 1
        action = result[0]
        assert action.kind == lsp.CodeActionKind.QuickFix
        assert action.title == "Convert try/catch to Try monadic flow"
        assert action.edit is not None
        assert action.edit.changes is not None
        edits = action.edit.changes[uri]
        assert any("Try.of(() -> riskyRead())" in e.new_text for e in edits)
        assert any("import io.vavr.control.Try;" in e.new_text for e in edits)

    def test_ignores_non_java_functional_lsp_diagnostics(self) -> None:
        """Diagnostics from other sources (jdtls) are filtered out."""
        from java_functional_lsp.server import on_code_action, server

        _ensure_workspace()
        uri = "file:///test/BuggyExample.java"
        server.workspace.put_text_document(
            lsp.TextDocumentItem(uri=uri, language_id="java", version=1, text=_BUGGY_JAVA),
        )
        try:
            diag = lsp.Diagnostic(
                range=lsp.Range(start=lsp.Position(line=7, character=19), end=lsp.Position(line=7, character=23)),
                message="Some jdtls warning",
                severity=lsp.DiagnosticSeverity.Warning,
                code="something-jdtls-specific",
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

    def test_code_action_with_no_diagnostics(self) -> None:
        """Empty diagnostic context → no code actions."""
        from java_functional_lsp.server import on_code_action, server

        _ensure_workspace()
        uri = "file:///test/Clean.java"
        server.workspace.put_text_document(
            lsp.TextDocumentItem(uri=uri, language_id="java", version=1, text="class Clean {}"),
        )
        try:
            result = on_code_action(
                lsp.CodeActionParams(
                    text_document=lsp.TextDocumentIdentifier(uri=uri),
                    range=lsp.Range(start=lsp.Position(line=0, character=0), end=lsp.Position(line=0, character=5)),
                    context=lsp.CodeActionContext(diagnostics=[]),
                )
            )
        finally:
            server.workspace.remove_text_document(uri)

        assert result is None
