"""E2E through the real server + real jdtls with default hold constants (#109).

Maven projects driven the way Claude Code drives the server (client name "Claude Code",
files written to disk before didChange, no didSave):

- fixing a missing import never re-publishes the resolved error (vs ``off``: it does);
- the PostToolUse hook returns only after the fresh jdtls set was published;
- a cross-file constructor-arity change converges without a client didSave.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
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

HOOK = Path(__file__).parent.parent / "hooks" / "post_tool_lint.py"
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
_FOO_FIXED_NEW_ERROR = 'package p;\n\nimport q.Bar;\n\npublic class Foo {\n    Bar bar;\n    int n = "s";\n}\n'
_UNRESOLVED = "Bar cannot be resolved"
_TYPE_MISMATCH = "Type mismatch"
_READY_TIMEOUT_SEC = 150
_OBSERVE_SEC = 8.0
_QUIET_SEC = 4.0
_CONVERGE_TIMEOUT_SEC = 30


def _messages(diags: list[lsp.Diagnostic]) -> list[str]:
    return [d.message for d in diags]


def _errors(diags: list[lsp.Diagnostic]) -> list[str]:
    return [d.message for d in diags if d.severity == lsp.DiagnosticSeverity.Error]


def _has(diags: list[lsp.Diagnostic], text: str) -> bool:
    return any(text in m for m in _messages(diags))


def _foo(n: int) -> str:
    params = ", ".join(f"int a{i}" for i in range(n))
    return f"package p;\n\npublic class Foo {{\n    public Foo({params}) {{}}\n}}\n"


def _caller(n: int) -> str:
    args = ", ".join(str(i) for i in range(n))
    return f"package p;\n\npublic class Caller {{\n    Foo make() {{\n        return new Foo({args});\n    }}\n}}\n"


def _write_project(root: Path, files: dict[str, str]) -> dict[str, Path]:
    (root / "pom.xml").write_text(_POM)
    paths: dict[str, Path] = {}
    for rel, text in files.items():
        path = root / "src" / "main" / "java" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        paths[rel] = path
    return paths


class _PublishWatch:
    """Records every publish the server sends, per URI."""

    def __init__(self) -> None:
        self.history: dict[str, list[list[lsp.Diagnostic]]] = {}
        self.arrived: dict[str, asyncio.Event] = {}

    def on_publish(self, params: lsp.PublishDiagnosticsParams) -> None:
        self.history.setdefault(params.uri, []).append(list(params.diagnostics))
        self.arrived.setdefault(params.uri, asyncio.Event()).set()

    def latest(self, uri: str) -> list[lsp.Diagnostic]:
        return self.history.get(uri, [[]])[-1]

    async def _next(self, uri: str, timeout: float) -> bool:
        event = self.arrived.setdefault(uri, asyncio.Event())
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def wait_for(self, uri: str, predicate: Callable[[list[lsp.Diagnostic]], bool], timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while not (self.history.get(uri) and predicate(self.latest(uri))):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0 or not await self._next(uri, remaining):
                return False
        return True

    async def wait_quiet(self, uri: str, since: int) -> None:
        """Wait until *uri* published at least once after index *since*, then stayed quiet."""
        deadline = asyncio.get_running_loop().time() + _CONVERGE_TIMEOUT_SEC
        while asyncio.get_running_loop().time() < deadline:
            if not await self._next(uri, _QUIET_SEC) and len(self.history.get(uri, [])) > since:
                return


async def _start_server(root: Path, watch: _PublishWatch, **env: str) -> LanguageClient:
    client = LanguageClient("e2e-hold", "1.0")

    def on_publish(params: lsp.PublishDiagnosticsParams) -> None:  # pygls needs a plain function
        watch.on_publish(params)

    client.feature(lsp.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)(on_publish)
    await client.start_io(sys.executable, "-m", "java_functional_lsp", env={**os.environ, **env})
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


def _open(client: LanguageClient, path: Path) -> None:
    client.text_document_did_open(
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(uri=path.as_uri(), language_id="java", version=1, text=path.read_text())
        )
    )


def _agent_edit(client: LanguageClient, path: Path, text: str, version: int) -> None:
    """Like Claude Code's Edit tool: write the file, then didChange (no didSave)."""
    path.write_text(text)
    client.text_document_did_change(
        lsp.DidChangeTextDocumentParams(
            text_document=lsp.VersionedTextDocumentIdentifier(uri=path.as_uri(), version=version),
            content_changes=[lsp.TextDocumentContentChangeWholeDocument(text=text)],
        )
    )


def _run_hook(path: Path, tmpdir: Path) -> None:
    subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_input": {"file_path": str(path)}}),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "TMPDIR": str(tmpdir)},
    )


@pytest.mark.timeout(_READY_TIMEOUT_SEC + 60)
@pytest.mark.parametrize("mode", ["custom-first", "off"])
async def test_fixing_import_never_republishes_resolved_error(tmp_path: Path, mode: str) -> None:
    foo = _write_project(tmp_path, {"q/Bar.java": _BAR, "p/Foo.java": _FOO_BROKEN})["p/Foo.java"]
    uri = foo.as_uri()
    watch = _PublishWatch()
    client = await _start_server(tmp_path, watch, JAVA_FUNCTIONAL_LSP_DIAG_HOLD=mode)
    try:
        _open(client, foo)
        if not await watch.wait_for(uri, lambda d: _has(d, _UNRESOLVED), _READY_TIMEOUT_SEC):
            pytest.skip("jdtls did not report the unresolved type in time (project import too slow)")
        before_fix = len(watch.history[uri])
        _agent_edit(client, foo, _FOO_FIXED, 2)
        await asyncio.sleep(_OBSERVE_SEC)
        after_fix = watch.history[uri][before_fix:]
        assert after_fix, "server published nothing after the fix"
        stale = [i for i, diags in enumerate(after_fix) if _has(diags, _UNRESOLVED)]
        if mode == "off":
            assert stale, "expected the pre-fix behavior to re-publish the resolved error"
        else:
            assert not stale, f"publishes {stale} after the fix still carried '{_UNRESOLVED}'"
        assert not _has(after_fix[-1], _UNRESOLVED)
    finally:
        await _stop_server(client)


@pytest.mark.timeout(_READY_TIMEOUT_SEC + 60)
async def test_hook_returns_after_fresh_jdtls_publish(tmp_path: Path) -> None:
    """When the PostToolUse hook returns, the client already holds jdtls's result for the edit."""
    markers = tmp_path / "tmp"
    markers.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    foo = _write_project(project, {"q/Bar.java": _BAR, "p/Foo.java": _FOO_BROKEN})["p/Foo.java"]
    uri = foo.as_uri()
    watch = _PublishWatch()
    client = await _start_server(project, watch, TMPDIR=str(markers))
    try:
        _open(client, foo)
        if not await watch.wait_for(uri, lambda d: _has(d, _UNRESOLVED), _READY_TIMEOUT_SEC):
            pytest.skip("jdtls did not report the unresolved type in time (project import too slow)")
        _agent_edit(client, foo, _FOO_FIXED_NEW_ERROR, 2)
        await asyncio.to_thread(_run_hook, foo, markers)
        await asyncio.sleep(0.1)  # let the client read what the server sent before the hook returned
        latest = watch.latest(uri)
        assert not _has(latest, _UNRESOLVED), _messages(latest)
        assert _has(latest, _TYPE_MISMATCH), f"jdtls's new error missing when the hook returned: {_messages(latest)}"
    finally:
        await _stop_server(client)


@pytest.mark.timeout(_READY_TIMEOUT_SEC + 3 * _CONVERGE_TIMEOUT_SEC + 60)
async def test_cross_file_arity_change_converges_without_client_did_save(tmp_path: Path) -> None:
    """Without didSave jdtls left Caller stale in ~2/5 runs; the server now sends it (three cycles)."""
    paths = _write_project(tmp_path, {"p/Foo.java": _foo(2), "p/Caller.java": _caller(1)})
    foo, caller = paths["p/Foo.java"], paths["p/Caller.java"]
    caller_uri = caller.as_uri()
    watch = _PublishWatch()
    client = await _start_server(tmp_path, watch)
    try:
        _open(client, foo)
        _open(client, caller)
        # Caller starts with the wrong arity: only a project-mode jdtls reports that.
        if not await watch.wait_for(caller_uri, lambda d: bool(_errors(d)), _READY_TIMEOUT_SEC):
            pytest.skip("jdtls did not import the Maven project in time")
        version = 2
        _agent_edit(client, caller, _caller(2), version)
        assert await watch.wait_for(caller_uri, lambda d: not _errors(d), _CONVERGE_TIMEOUT_SEC)
        for arity in (3, 4, 5):
            version += 1
            since = len(watch.history[caller_uri])
            _agent_edit(client, foo, _foo(arity), version)
            _agent_edit(client, caller, _caller(arity), version)
            await watch.wait_quiet(caller_uri, since)
            published = [_errors(d) for d in watch.history[caller_uri][since:]]
            assert not _errors(watch.latest(caller_uri)), f"arity {arity}: Caller publishes after the edit: {published}"
    finally:
        await _stop_server(client)
