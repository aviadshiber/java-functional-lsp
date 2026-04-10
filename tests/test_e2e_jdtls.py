"""End-to-end tests that spawn a real jdtls subprocess.

These tests exercise the full request/response pipeline:

1. Python LSP server builds pygls-typed params (``lsp.DefinitionParams(...)``)
2. ``server._serialize_params`` converts them via the lsprotocol converter
3. ``JdtlsProxy.send_request`` frames + writes bytes to the jdtls stdin
4. Real jdtls subprocess parses the JSON and handles the request
5. Response comes back through the proxy and is deserialized

Unit tests with mocked subprocesses **cannot** catch JSON-shape regressions
because the mocks never parse the bytes. We learned this the hard way:

- **v0.7.1 fix** (``fix/jdtls-java-home-detection``): got jdtls to start at all
  after it was silently failing because the inherited ``JAVA_HOME`` pointed at
  Java 8. Every unit test of ``find_jdtls_java_home`` passed while users had
  no working jdtls.
- **v0.7.2 fix** (``fix/lsp-camelcase-serialization``): every forwarded request
  to jdtls was failing with ``NullPointerException`` because a vanilla
  ``cattrs.Converter()`` emitted snake_case field names (``text_document``)
  instead of camelCase (``textDocument``). Every unit test passed while
  go-to-definition, references, hover, and document symbol were all broken.

These tests guard the end-to-end pipeline so the next such bug is caught
before release.

Skip rules: the entire module is skipped when ``jdtls`` is not on PATH or
no Java 21+ installation can be found. Local developers with jdtls installed
see the tests run; CI installs jdtls in the dedicated ``e2e-test`` job.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from lsprotocol import types as lsp

from java_functional_lsp.proxy import JdtlsProxy, find_jdtls_java_home
from java_functional_lsp.server import _serialize_params

# Module-level skip: jdtls and Java 21+ are required. Check once at import
# time so the skip reason is clear and the entire test file is skipped at
# collection time rather than failing each test independently.
_JDTLS_ON_PATH = shutil.which("jdtls") is not None
_JAVA_21_AVAILABLE = find_jdtls_java_home() is not None

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _JDTLS_ON_PATH,
        reason="jdtls binary not found on PATH — install via `brew install jdtls` or equivalent",
    ),
    pytest.mark.skipif(
        not _JAVA_21_AVAILABLE,
        reason="no Java 21+ found — install via `brew install openjdk@21` or equivalent",
    ),
]

# jdtls cold-start is 10-20s; give each individual request a generous budget
# beyond the default REQUEST_TIMEOUT, and wrap the whole test in pytest-timeout
# so a hung jdtls doesn't wedge the suite.
_E2E_TEST_TIMEOUT_SEC = 120
_JDTLS_PARSE_WAIT_SEC = 2.5

_HELLO_JAVA = """\
public class Hello {
    private String greeting;

    public Hello(String greeting) {
        this.greeting = greeting;
    }

    public String greet() {
        return this.greeting;
    }

    public static void main(String[] args) {
        Hello h = new Hello("world");
        System.out.println(h.greet());
    }
}
"""


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal standalone Java workspace with a single Hello.java file.

    jdtls operates in "default project" mode for orphan files (no build config),
    which is enough to parse the file and serve document symbols. No pom.xml,
    no build.gradle, no .classpath — we don't need full classpath resolution
    to verify that the request shape reaches jdtls correctly.
    """
    src_file = tmp_path / "Hello.java"
    src_file.write_text(_HELLO_JAVA)
    return tmp_path, src_file


@pytest.fixture
async def proxy(workspace: tuple[Path, Path]) -> AsyncIterator[JdtlsProxy]:
    """Start a real JdtlsProxy bound to the workspace fixture.

    Yields an initialized proxy and tears it down on test exit. Uses a minimal
    ``InitializeParams`` dict that includes only the capabilities we exercise
    in the tests below, to keep jdtls's initial workspace scan cheap.
    """
    tmp_path, _ = workspace
    p = JdtlsProxy()

    init_params = {
        "processId": os.getpid(),
        "rootUri": tmp_path.as_uri(),
        "rootPath": str(tmp_path),
        "capabilities": {
            "textDocument": {
                "synchronization": {
                    "dynamicRegistration": False,
                    "willSave": False,
                    "willSaveWaitUntil": False,
                    "didSave": True,
                },
                "definition": {"dynamicRegistration": False, "linkSupport": False},
                "references": {"dynamicRegistration": False},
                "documentSymbol": {
                    "dynamicRegistration": False,
                    "hierarchicalDocumentSymbolSupport": True,
                },
                "hover": {"dynamicRegistration": False, "contentFormat": ["plaintext"]},
                "completion": {
                    "dynamicRegistration": False,
                    "completionItem": {"snippetSupport": False},
                },
            },
            "workspace": {"configuration": False, "workspaceFolders": False},
        },
        "initializationOptions": {},
        "trace": "off",
    }

    started = await p.start(init_params)
    if not started:
        pytest.fail(
            "JdtlsProxy.start() returned False — jdtls failed to initialize. "
            "Check the logs above for Java version / classpath issues."
        )

    try:
        yield p
    finally:
        await p.stop()


async def _open_document(proxy: JdtlsProxy, src_file: Path) -> str:
    """Send textDocument/didOpen for ``src_file`` and return its URI."""
    uri = src_file.as_uri()
    await proxy.send_notification(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": uri,
                "languageId": "java",
                "version": 1,
                "text": src_file.read_text(),
            }
        },
    )
    # Give jdtls a moment to parse the file. We can't synchronously wait for
    # parsing — LSP exposes no notification for that — so we sleep conservatively.
    await asyncio.sleep(_JDTLS_PARSE_WAIT_SEC)
    return uri


def _assert_no_npe_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Fail the test if any NullPointerException appeared in jdtls stderr.

    The proxy's ``_stderr_reader`` logs each stderr line via
    ``logger.error("jdtls stderr: %s", line)``. We scan the captured records
    for NPE markers — this catches regressions where the wire format is
    wrong even when send_request returns a non-error response (e.g., when
    jdtls falls back to a default value after logging the exception).
    """
    npe_lines = [record.getMessage() for record in caplog.records if "NullPointerException" in record.getMessage()]
    assert not npe_lines, (
        "jdtls threw NullPointerException — probably a request-shape bug "
        "(camelCase vs snake_case). Captured lines:\n" + "\n".join(npe_lines)
    )


@pytest.mark.timeout(_E2E_TEST_TIMEOUT_SEC)
class TestJdtlsEndToEnd:
    """End-to-end request/response tests against a real jdtls subprocess."""

    async def test_initialize_handshake_succeeds(self, proxy: JdtlsProxy) -> None:
        """Proves jdtls came up and announced its capabilities.

        This is implicit in the ``proxy`` fixture (which asserts start()
        returned True), but an explicit test here makes the boundary clear:
        if this fails, every other test will fail too, and the fixture is
        to blame.
        """
        assert proxy.is_available
        caps = proxy.capabilities
        # jdtls always advertises these — their presence confirms a clean init.
        assert "definitionProvider" in caps
        assert "documentSymbolProvider" in caps

    async def test_document_symbol_round_trip(
        self,
        workspace: tuple[Path, Path],
        proxy: JdtlsProxy,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Send textDocument/documentSymbol via _serialize_params and verify response.

        Document-symbol only needs the file to be parsed (no classpath resolution),
        so it's the most reliable cross-environment e2e check. This is the
        canonical camelCase regression test: if ``_serialize_params`` emits
        ``text_document`` instead of ``textDocument``, jdtls logs NPE and
        returns an error response, which we detect via caplog.
        """
        _, src_file = workspace
        uri = await _open_document(proxy, src_file)

        with caplog.at_level(logging.ERROR, logger="java_functional_lsp.proxy"):
            params = lsp.DocumentSymbolParams(
                text_document=lsp.TextDocumentIdentifier(uri=uri),
            )
            serialized = _serialize_params(params)

            # Wire-format sanity: the serialization layer we send to jdtls MUST
            # use camelCase field names. This is redundant with the unit tests
            # in TestLspConverterCamelCase but pins the exact dict shape at the
            # boundary of the real subprocess call.
            assert "textDocument" in serialized, f"_serialize_params emitted wrong field names: {serialized.keys()}"
            assert "text_document" not in serialized

            result = await proxy.send_request("textDocument/documentSymbol", serialized)

        # Primary assertion: jdtls returned something. With a correctly-shaped
        # request, jdtls always returns a list (possibly empty) for a parsable
        # file. None here means our proxy surfaced an error or timeout — both
        # are regression signals.
        assert result is not None, (
            "jdtls returned None for documentSymbol on a valid file. "
            "This usually means the request shape was rejected — check caplog "
            "for jdtls stderr output."
        )
        assert isinstance(result, list)
        # Hello.java declares a top-level class, so we expect at least one symbol.
        assert len(result) > 0, (
            "jdtls returned an empty symbol list for a file with a top-level class. "
            "Either jdtls didn't finish parsing or the file wasn't opened correctly."
        )

        _assert_no_npe_in_logs(caplog)

    async def test_definition_request_does_not_npe(
        self,
        workspace: tuple[Path, Path],
        proxy: JdtlsProxy,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The exact scenario from the v0.7.2 bug report: textDocument/definition.

        The NPE surfaced in ``NavigateToDefinitionHandler.definition`` when
        ``TextDocumentPositionParams.getTextDocument()`` returned null. This
        test sends a real definition request through the real serialization
        path and asserts no NPE appears in jdtls stderr, regardless of whether
        jdtls actually resolves the symbol (which depends on classpath state).
        """
        _, src_file = workspace
        uri = await _open_document(proxy, src_file)

        with caplog.at_level(logging.ERROR, logger="java_functional_lsp.proxy"):
            # Position the cursor on the `greeting` identifier at line 8 col 20
            # inside `return this.greeting;`. Exact column doesn't matter — we
            # care that the REQUEST reaches jdtls with a valid textDocument.
            params = lsp.DefinitionParams(
                text_document=lsp.TextDocumentIdentifier(uri=uri),
                position=lsp.Position(line=8, character=20),
            )
            serialized = _serialize_params(params)
            assert "textDocument" in serialized
            assert "text_document" not in serialized

            # We don't assert on the RESULT of this call — jdtls may return None
            # or [] depending on classpath state. The critical assertion is that
            # no NPE appeared in stderr, because an NPE would prove the camelCase
            # regression has come back.
            await proxy.send_request("textDocument/definition", serialized)

        _assert_no_npe_in_logs(caplog)

    async def test_hover_request_does_not_npe(
        self,
        workspace: tuple[Path, Path],
        proxy: JdtlsProxy,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Hover is another TextDocumentPositionParams-shaped request.

        Verifying hover in addition to definition catches regressions that
        might only affect a subset of position-based handlers.
        """
        _, src_file = workspace
        uri = await _open_document(proxy, src_file)

        with caplog.at_level(logging.ERROR, logger="java_functional_lsp.proxy"):
            params = lsp.HoverParams(
                text_document=lsp.TextDocumentIdentifier(uri=uri),
                position=lsp.Position(line=0, character=13),
            )
            serialized = _serialize_params(params)
            assert "textDocument" in serialized

            await proxy.send_request("textDocument/hover", serialized)

        _assert_no_npe_in_logs(caplog)

    async def test_did_open_notification_does_not_npe(
        self,
        workspace: tuple[Path, Path],
        proxy: JdtlsProxy,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Notifications go through the same serialization path as requests.

        didOpen uses ``DidOpenTextDocumentParams`` which wraps a
        ``TextDocumentItem`` with snake_case ``language_id``. A camelCase bug
        here would cause jdtls to fail parsing the notification silently
        (no response, so no request-side error) but the NPE would appear in
        stderr when jdtls tries to look up the file by URI later.
        """
        _, src_file = workspace
        uri = src_file.as_uri()

        with caplog.at_level(logging.ERROR, logger="java_functional_lsp.proxy"):
            params = lsp.DidOpenTextDocumentParams(
                text_document=lsp.TextDocumentItem(
                    uri=uri,
                    language_id="java",
                    version=1,
                    text=src_file.read_text(),
                ),
            )
            serialized = _serialize_params(params)
            assert "textDocument" in serialized
            assert "languageId" in serialized["textDocument"]
            assert "language_id" not in serialized["textDocument"]

            await proxy.send_notification("textDocument/didOpen", serialized)
            await asyncio.sleep(_JDTLS_PARSE_WAIT_SEC)

        _assert_no_npe_in_logs(caplog)

    async def test_references_request_does_not_npe(
        self,
        workspace: tuple[Path, Path],
        proxy: JdtlsProxy,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """textDocument/references through _serialize_params.

        ReferenceParams is a compound type: it wraps ``text_document``,
        ``position``, AND a nested ``context`` with its own snake_case field
        ``include_declaration``. A vanilla cattrs converter would emit
        ``context.include_declaration`` which jdtls would see as a null
        ReferenceContext — guaranteed NPE.
        """
        _, src_file = workspace
        uri = await _open_document(proxy, src_file)

        with caplog.at_level(logging.ERROR, logger="java_functional_lsp.proxy"):
            params = lsp.ReferenceParams(
                text_document=lsp.TextDocumentIdentifier(uri=uri),
                position=lsp.Position(line=8, character=20),  # `greeting` inside greet()
                context=lsp.ReferenceContext(include_declaration=True),
            )
            serialized = _serialize_params(params)
            assert "textDocument" in serialized
            assert "text_document" not in serialized
            # Nested field is also camelCase
            assert "context" in serialized
            assert "includeDeclaration" in serialized["context"]
            assert "include_declaration" not in serialized["context"]

            await proxy.send_request("textDocument/references", serialized)

        _assert_no_npe_in_logs(caplog)

    async def test_completion_request_does_not_npe(
        self,
        workspace: tuple[Path, Path],
        proxy: JdtlsProxy,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """textDocument/completion through _serialize_params.

        Completion exercises a distinct jdtls code path from definition/hover
        (content assist rather than symbol resolution) and uses
        ``CompletionParams`` which inherits TextDocumentPositionParams. A
        request-shape bug here would NPE the completion handler.
        """
        _, src_file = workspace
        uri = await _open_document(proxy, src_file)

        with caplog.at_level(logging.ERROR, logger="java_functional_lsp.proxy"):
            # Position after `h.` on the main() line where completion makes sense.
            params = lsp.CompletionParams(
                text_document=lsp.TextDocumentIdentifier(uri=uri),
                position=lsp.Position(line=13, character=29),
            )
            serialized = _serialize_params(params)
            assert "textDocument" in serialized
            assert "text_document" not in serialized

            await proxy.send_request("textDocument/completion", serialized)

        _assert_no_npe_in_logs(caplog)
