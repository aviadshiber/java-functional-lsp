"""Main LSP server for java-functional-lsp.

Provides custom Java diagnostics via tree-sitter analysis.
Proxies to jdtls for full Java language features (completions, hover, go-to-def).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import cattrs
from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from .analyzers.base import Analyzer, Severity, get_parser
from .analyzers.base import Diagnostic as LintDiagnostic
from .analyzers.exception_checker import ExceptionChecker
from .analyzers.mutation_checker import MutationChecker
from .analyzers.null_checker import NullChecker
from .analyzers.spring_checker import SpringChecker
from .proxy import JdtlsProxy

logger = logging.getLogger(__name__)

_SEVERITY_MAP = {
    Severity.ERROR: lsp.DiagnosticSeverity.Error,
    Severity.WARNING: lsp.DiagnosticSeverity.Warning,
    Severity.INFO: lsp.DiagnosticSeverity.Information,
    Severity.HINT: lsp.DiagnosticSeverity.Hint,
}

_ANALYZERS: list[Analyzer] = [NullChecker(), ExceptionChecker(), MutationChecker(), SpringChecker()]

_converter = cattrs.Converter()


class JavaFunctionalLspServer(LanguageServer):
    def __init__(self) -> None:
        from . import __version__

        super().__init__("java-functional-lsp", __version__)
        self._parser = get_parser()
        self._config: dict[str, Any] = {}
        self._init_params: dict[str, Any] = {}
        self._proxy = JdtlsProxy(on_diagnostics=self._on_jdtls_diagnostics)

    def _on_jdtls_diagnostics(self, uri: str, diagnostics: list[Any]) -> None:
        """Called when jdtls publishes diagnostics — merge with custom and re-publish."""
        try:
            _publish_diagnostics(uri)
        except Exception as e:
            logger.error("Error re-publishing diagnostics for %s: %s", uri, e)


server = JavaFunctionalLspServer()


def _uri_to_path(uri: str) -> str:
    """Convert a file:// URI to a filesystem path."""
    parsed = urlparse(uri)
    return unquote(parsed.path)


def _load_config(workspace_root: str | None) -> dict[str, Any]:
    """Load .deeperdive-linter.json from workspace root if it exists."""
    if not workspace_root:
        return {}
    config_path = Path(workspace_root) / ".deeperdive-linter.json"
    if config_path.exists():
        try:
            result: dict[str, Any] = json.loads(config_path.read_text())
            return result
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
    """Run all custom analyzers on the given source text."""
    source_bytes = source_text.encode("utf-8")
    tree = server._parser.parse(source_bytes)
    config = server._config

    all_diagnostics: list[LintDiagnostic] = []
    for analyzer in _ANALYZERS:
        try:
            diags = analyzer.analyze(tree, source_bytes, config)
            all_diagnostics.extend(diags)
        except Exception as e:
            logger.error("Analyzer %s failed: %s", type(analyzer).__name__, e)

    return [_to_lsp_diagnostic(d) for d in all_diagnostics]


def _jdtls_raw_to_lsp_diagnostics(raw_diagnostics: list[Any]) -> list[lsp.Diagnostic]:
    """Convert raw jdtls diagnostic dicts to lsp.Diagnostic objects."""
    result: list[lsp.Diagnostic] = []
    for raw in raw_diagnostics:
        try:
            diag = _converter.structure(raw, lsp.Diagnostic)
            result.append(diag)
        except Exception:
            # If structuring fails, try manual conversion
            try:
                r = raw.get("range", {})
                start = r.get("start", {})
                end = r.get("end", {})
                result.append(
                    lsp.Diagnostic(
                        range=lsp.Range(
                            start=lsp.Position(line=start.get("line", 0), character=start.get("character", 0)),
                            end=lsp.Position(line=end.get("line", 0), character=end.get("character", 0)),
                        ),
                        severity=lsp.DiagnosticSeverity(raw.get("severity", 1)),
                        code=raw.get("code"),
                        source=raw.get("source", "jdtls"),
                        message=raw.get("message", ""),
                    )
                )
            except Exception as e:
                logger.debug("Could not convert jdtls diagnostic: %s", e)
    return result


def _publish_diagnostics(uri: str) -> None:
    """Merge custom + jdtls diagnostics and publish to client."""
    doc = server.workspace.get_text_document(uri)
    custom_diags = _analyze_document(doc.source)

    # Get cached jdtls diagnostics
    jdtls_diags: list[lsp.Diagnostic] = []
    if server._proxy.is_available:
        raw = server._proxy.get_cached_diagnostics(uri)
        jdtls_diags = _jdtls_raw_to_lsp_diagnostics(raw)

    merged = jdtls_diags + custom_diags
    server.text_document_publish_diagnostics(lsp.PublishDiagnosticsParams(uri=uri, diagnostics=merged))


def _serialize_params(params: Any) -> Any:
    """Convert lsprotocol objects to JSON-serializable dicts for jdtls."""
    try:
        return _converter.unstructure(params)
    except Exception:
        return params


# --- Lifecycle handlers ---


@server.feature(lsp.INITIALIZE)
def on_initialize(params: lsp.InitializeParams) -> lsp.InitializeResult:
    """Handle LSP initialize — store params for jdtls proxy."""
    server._init_params = _serialize_params(params)

    root = None
    if params.root_uri:
        root = _uri_to_path(params.root_uri)
    elif params.root_path:
        root = params.root_path

    server._config = _load_config(root)

    return lsp.InitializeResult(
        capabilities=lsp.ServerCapabilities(
            text_document_sync=lsp.TextDocumentSyncOptions(
                open_close=True,
                change=lsp.TextDocumentSyncKind.Full,
                save=lsp.SaveOptions(include_text=True),
            ),
            completion_provider=lsp.CompletionOptions(trigger_characters=["."]),
            hover_provider=True,
            definition_provider=True,
            references_provider=True,
            document_symbol_provider=True,
        )
    )


@server.feature(lsp.INITIALIZED)
async def on_initialized(params: lsp.InitializedParams) -> None:
    """Start jdtls proxy after initialization."""
    logger.info(
        "java-functional-lsp initialized (rules: %s)",
        list(server._config.get("rules", {}).keys()) or "all defaults",
    )
    started = await server._proxy.start(server._init_params)
    if started:
        logger.info("jdtls proxy active — full Java language support enabled")
    else:
        logger.info("jdtls proxy unavailable — running with custom rules only")


# --- Document sync (forward to jdtls + run custom analyzers) ---


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
async def on_did_open(params: lsp.DidOpenTextDocumentParams) -> None:
    """Forward to jdtls and analyze."""
    if server._proxy.is_available:
        await server._proxy.send_notification("textDocument/didOpen", _serialize_params(params))
    _publish_diagnostics(params.text_document.uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
async def on_did_change(params: lsp.DidChangeTextDocumentParams) -> None:
    """Forward to jdtls and re-analyze."""
    if server._proxy.is_available:
        await server._proxy.send_notification("textDocument/didChange", _serialize_params(params))
    _publish_diagnostics(params.text_document.uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
async def on_did_save(params: lsp.DidSaveTextDocumentParams) -> None:
    """Forward to jdtls and re-analyze."""
    if server._proxy.is_available:
        await server._proxy.send_notification("textDocument/didSave", _serialize_params(params))
    _publish_diagnostics(params.text_document.uri)


# --- Forwarded features (jdtls passthrough) ---


@server.feature(lsp.TEXT_DOCUMENT_COMPLETION)
async def on_completion(params: lsp.CompletionParams) -> lsp.CompletionList | None:
    """Forward completion request to jdtls."""
    if not server._proxy.is_available:
        return None
    result = await server._proxy.send_request("textDocument/completion", _serialize_params(params))
    if result is None:
        return None
    try:
        return _converter.structure(result, lsp.CompletionList)
    except Exception:
        return None


@server.feature(lsp.TEXT_DOCUMENT_HOVER)
async def on_hover(params: lsp.HoverParams) -> lsp.Hover | None:
    """Forward hover request to jdtls."""
    if not server._proxy.is_available:
        return None
    result = await server._proxy.send_request("textDocument/hover", _serialize_params(params))
    if result is None:
        return None
    try:
        return _converter.structure(result, lsp.Hover)
    except Exception:
        return None


@server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
async def on_definition(params: lsp.DefinitionParams) -> list[lsp.Location] | None:
    """Forward go-to-definition request to jdtls."""
    if not server._proxy.is_available:
        return None
    result = await server._proxy.send_request("textDocument/definition", _serialize_params(params))
    if result is None:
        return None
    try:
        if isinstance(result, list):
            return [_converter.structure(loc, lsp.Location) for loc in result]
        return [_converter.structure(result, lsp.Location)]
    except Exception:
        return None


@server.feature(lsp.TEXT_DOCUMENT_REFERENCES)
async def on_references(params: lsp.ReferenceParams) -> list[lsp.Location] | None:
    """Forward find-references request to jdtls."""
    if not server._proxy.is_available:
        return None
    result = await server._proxy.send_request("textDocument/references", _serialize_params(params))
    if result is None:
        return None
    try:
        return [_converter.structure(loc, lsp.Location) for loc in result]
    except Exception:
        return None


@server.feature(lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
async def on_document_symbol(params: lsp.DocumentSymbolParams) -> list[lsp.DocumentSymbol] | None:
    """Forward document symbol request to jdtls."""
    if not server._proxy.is_available:
        return None
    result = await server._proxy.send_request("textDocument/documentSymbol", _serialize_params(params))
    if result is None:
        return None
    try:
        return [_converter.structure(sym, lsp.DocumentSymbol) for sym in result]
    except Exception:
        return None


# --- Entry point ---


def main() -> None:
    """Entry point for the LSP server."""
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")
    server.start_io()


if __name__ == "__main__":
    main()
