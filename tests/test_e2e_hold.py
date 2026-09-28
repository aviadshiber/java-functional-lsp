"""E2E through the real server + real jdtls with default hold constants (#109).

Opens a Maven project where ``Foo`` references ``q.Bar`` without importing it, waits
for jdtls to report "Bar cannot be resolved", then adds the import. With the hold
(``custom-first``, what Claude Code gets) no publish after the fix may still carry the
resolved error; with ``off`` the previous behavior re-publishes it at the debounce.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest
from lsprotocol import types as lsp
from pygls.lsp.client import LanguageClient

from java_functional_lsp.proxy import find_jdtls_java_home

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("jdtls") is None, reason="jdtls binary not found on PATH"),
    pytest.mark.skipif(find_jdtls_java_home() is None, reason="no Java 21+ found"),
]

_POM = """\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>hold</artifactId>
  <version>1</version>
  <properties>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
  </properties>
</project>
"""
_BAR = "package q;\n\npublic class Bar {}\n"
_FOO_BROKEN = "package p;\n\npublic class Foo {\n    Bar bar;\n}\n"
_FOO_FIXED = "package p;\n\nimport q.Bar;\n\npublic class Foo {\n    Bar bar;\n}\n"
_UNRESOLVED = "Bar cannot be resolved"
_READY_TIMEOUT_SEC = 150
_OBSERVE_SEC = 8.0


def _has_unresolved(diags: list[lsp.Diagnostic]) -> bool:
    return any(_UNRESOLVED in d.message for d in diags)


def _write_project(root: Path) -> Path:
    (root / "pom.xml").write_text(_POM)
    src = root / "src" / "main" / "java"
    (src / "p").mkdir(parents=True)
    (src / "q").mkdir(parents=True)
    (src / "q" / "Bar.java").write_text(_BAR)
    foo = src / "p" / "Foo.java"
    foo.write_text(_FOO_BROKEN)
    return foo


class _FooWatch:
    """Records every publish the server sends for Foo."""

    def __init__(self, foo_uri: str) -> None:
        self.foo_uri = foo_uri
        self.history: list[list[lsp.Diagnostic]] = []
        self.arrived = asyncio.Event()

    def on_publish(self, params: lsp.PublishDiagnosticsParams) -> None:
        if params.uri == self.foo_uri:
            self.history.append(list(params.diagnostics))
            self.arrived.set()

    async def wait_unresolved(self) -> bool:
        deadline = asyncio.get_running_loop().time() + _READY_TIMEOUT_SEC
        while not (self.history and _has_unresolved(self.history[-1])):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            self.arrived.clear()
            try:
                await asyncio.wait_for(self.arrived.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
        return True


async def _start_server(root: Path, mode: str, watch: _FooWatch) -> LanguageClient:
    client = LanguageClient("e2e-hold", "1.0")

    def on_publish(params: lsp.PublishDiagnosticsParams) -> None:  # pygls needs a plain function
        watch.on_publish(params)

    client.feature(lsp.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)(on_publish)
    env = {**os.environ, "JAVA_FUNCTIONAL_LSP_DIAG_HOLD": mode}
    await client.start_io(sys.executable, "-m", "java_functional_lsp", env=env)
    await client.initialize_async(
        lsp.InitializeParams(
            process_id=os.getpid(),
            root_uri=root.as_uri(),
            capabilities=lsp.ClientCapabilities(),
            client_info=lsp.ClientInfo(name="Claude Code"),
        )
    )
    client.initialized(lsp.InitializedParams())
    return client


async def _stop_server(client: LanguageClient) -> None:
    try:
        await asyncio.wait_for(client.shutdown_async(None), timeout=5.0)
        client.exit(None)
    except Exception:
        pass
    try:
        await asyncio.wait_for(client.stop(), timeout=5.0)
    except Exception:
        pass


@pytest.mark.timeout(_READY_TIMEOUT_SEC + 60)
@pytest.mark.parametrize("mode", ["custom-first", "off"])
async def test_fixing_import_never_republishes_resolved_error(tmp_path: Path, mode: str) -> None:
    foo = _write_project(tmp_path)
    foo_uri = foo.as_uri()
    watch = _FooWatch(foo_uri)
    client = await _start_server(tmp_path, mode, watch)
    try:
        client.text_document_did_open(
            lsp.DidOpenTextDocumentParams(
                text_document=lsp.TextDocumentItem(uri=foo_uri, language_id="java", version=1, text=_FOO_BROKEN)
            )
        )
        if not await watch.wait_unresolved():
            pytest.skip("jdtls did not report the unresolved type in time (project import too slow)")

        history = watch.history
        before_fix = len(history)
        foo.write_text(_FOO_FIXED)
        client.text_document_did_change(
            lsp.DidChangeTextDocumentParams(
                text_document=lsp.VersionedTextDocumentIdentifier(uri=foo_uri, version=2),
                content_changes=[lsp.TextDocumentContentChangeWholeDocument(text=_FOO_FIXED)],
            )
        )
        await asyncio.sleep(_OBSERVE_SEC)
        after_fix = history[before_fix:]
        assert after_fix, "server published nothing after the fix"
        stale = [i for i, diags in enumerate(after_fix) if _has_unresolved(diags)]
        if mode == "off":
            assert stale, "expected the pre-fix behavior to re-publish the resolved error"
        else:
            assert not stale, f"publishes {stale} after the fix still carried '{_UNRESOLVED}'"
        assert not _has_unresolved(after_fix[-1])
    finally:
        await _stop_server(client)
