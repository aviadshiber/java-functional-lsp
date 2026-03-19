"""Main LSP server for java-functional-lsp.

Provides custom Java diagnostics via tree-sitter analysis.
"""

import json
import logging
from pathlib import Path
from urllib.parse import urlparse, unquote

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from .analyzers.base import Diagnostic as LintDiagnostic, Severity, get_parser
from .analyzers.null_checker import NullChecker
from .analyzers.exception_checker import ExceptionChecker
from .analyzers.mutation_checker import MutationChecker
from .analyzers.spring_checker import SpringChecker

logger = logging.getLogger(__name__)

_SEVERITY_MAP = {
    Severity.ERROR: lsp.DiagnosticSeverity.Error,
    Severity.WARNING: lsp.DiagnosticSeverity.Warning,
    Severity.INFO: lsp.DiagnosticSeverity.Information,
    Severity.HINT: lsp.DiagnosticSeverity.Hint,
}

_ANALYZERS = [NullChecker(), ExceptionChecker(), MutationChecker(), SpringChecker()]


class JavaFunctionalLspServer(LanguageServer):
    def __init__(self):
        super().__init__("java-functional-lsp", "0.1.0")
        self._parser = get_parser()
        self._config: dict = {}


server = JavaFunctionalLspServer()


def _uri_to_path(uri: str) -> str:
    """Convert a file:// URI to a filesystem path."""
    parsed = urlparse(uri)
    return unquote(parsed.path)


def _load_config(workspace_root: str | None) -> dict:
    """Load .deeperdive-linter.json from workspace root if it exists."""
    if not workspace_root:
        return {}
    config_path = Path(workspace_root) / ".deeperdive-linter.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load config from %s: %s", config_path, e)
    return {}


def _to_lsp_diagnostic(diag: LintDiagnostic) -> lsp.Diagnostic:
    """Convert an internal diagnostic to an LSP diagnostic."""
    return lsp.Diagnostic(
        range=lsp.Range(
            start=lsp.Position(line=diag.line, character=diag.col),
            end=lsp.Position(line=diag.end_line, character=diag.end_col),
        ),
        severity=_SEVERITY_MAP.get(diag.severity, lsp.DiagnosticSeverity.Warning),
        code=diag.code,
        source=diag.source,
        message=diag.message,
    )


def _analyze_document(source_text: str) -> list[lsp.Diagnostic]:
    """Run all analyzers on the given source text."""
    source_bytes = source_text.encode("utf-8")
    tree = server._parser.parse(source_bytes)
    config = server._config

    all_diagnostics = []
    for analyzer in _ANALYZERS:
        try:
            diags = analyzer.analyze(tree, source_bytes, config)
            all_diagnostics.extend(diags)
        except Exception as e:
            logger.error("Analyzer %s failed: %s", type(analyzer).__name__, e)

    return [_to_lsp_diagnostic(d) for d in all_diagnostics]


def _publish_diagnostics(uri: str):
    """Analyze a document and publish diagnostics."""
    doc = server.workspace.get_text_document(uri)
    diagnostics = _analyze_document(doc.source)
    server.publish_diagnostics(uri, diagnostics)


@server.feature(lsp.INITIALIZED)
def on_initialized(params: lsp.InitializedParams):
    """Load config after initialization."""
    root = None
    if server.workspace.root_uri:
        root = _uri_to_path(server.workspace.root_uri)
    elif server.workspace.root_path:
        root = server.workspace.root_path
    server._config = _load_config(root)
    logger.info("java-functional-lsp initialized (workspace: %s, rules: %s)",
                root, list(server._config.get("rules", {}).keys()) or "all defaults")


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def on_did_open(params: lsp.DidOpenTextDocumentParams):
    """Analyze document when opened."""
    _publish_diagnostics(params.text_document.uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def on_did_change(params: lsp.DidChangeTextDocumentParams):
    """Re-analyze document on change."""
    _publish_diagnostics(params.text_document.uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def on_did_save(params: lsp.DidSaveTextDocumentParams):
    """Re-analyze document on save."""
    _publish_diagnostics(params.text_document.uri)


def main():
    """Entry point for the LSP server."""
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")
    server.start_io()


if __name__ == "__main__":
    main()
