"""E2E: does jdtls converge a dependent file after a cross-file signature change? (#109, H2)

Reproduces the issue's arity variant against a real jdtls: ``Foo`` gains a constructor
parameter, then ``Caller``'s call site is updated, both written to disk like an agent's
Edit tool does. With didSave jdtls converges every time; without it, in roughly 2 of 5
runs jdtls's only post-edit publish for Caller still reports the old arity and is never
corrected (xfail, non-strict — the wrapper-side fix is tracked as #109 Part B).
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from java_functional_lsp.proxy import JdtlsProxy, find_jdtls_java_home

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("jdtls") is None, reason="jdtls binary not found on PATH"),
    pytest.mark.skipif(find_jdtls_java_home() is None, reason="no Java 21+ found"),
]

_POM = """\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>crossfile</artifactId>
  <version>1</version>
  <properties>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
  </properties>
</project>
"""
_FOO_V1 = "package p;\n\npublic class Foo {\n    public Foo(int a, int b) {}\n}\n"
_FOO_V2 = "package p;\n\npublic class Foo {\n    public Foo(int a, int b, int c) {}\n}\n"
_CALLER_V1 = "package p;\n\npublic class Caller {\n    Foo make() {\n        return new Foo(1, 2);\n    }\n}\n"
_CALLER_V2 = "package p;\n\npublic class Caller {\n    Foo make() {\n        return new Foo(1, 2, 3);\n    }\n}\n"
_NON_PROJECT_CODE = "16"
_READY_TIMEOUT_SEC = 120
_CONVERGE_TIMEOUT_SEC = 30
_QUIET_SEC = 4.0


def _errors(diags: list[Any]) -> list[str]:
    return [d.get("message", "") for d in diags if isinstance(d, dict) and d.get("severity") == 1]


@dataclass
class _CallerWatch:
    """Records every jdtls publish for Caller."""

    caller_uri: str
    history: list[list[str]] = field(default_factory=list)
    last: list[Any] = field(default_factory=list)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)

    def on_diagnostics(self, uri: str, diags: list[Any]) -> None:
        if uri == self.caller_uri:
            self.last = diags
            self.history.append(_errors(diags))
            self.arrived.set()

    def project_ready(self) -> bool:
        non_project = any(str(d.get("code")) == _NON_PROJECT_CODE for d in self.last if isinstance(d, dict))
        return not non_project and self.history[-1] == []

    async def next_publish(self, timeout: float) -> bool:
        self.arrived.clear()
        try:
            await asyncio.wait_for(self.arrived.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def wait_project_ready(self) -> bool:
        """Wait for a Caller publish without the non-project (code 16) marker."""
        deadline = asyncio.get_running_loop().time() + _READY_TIMEOUT_SEC
        while asyncio.get_running_loop().time() < deadline:
            if await self.next_publish(deadline - asyncio.get_running_loop().time()) and self.project_ready():
                return True
        return False

    async def wait_quiet(self) -> None:
        """Wait until Caller has published at least once and then stayed quiet."""
        deadline = asyncio.get_running_loop().time() + _CONVERGE_TIMEOUT_SEC
        while asyncio.get_running_loop().time() < deadline:
            if not await self.next_publish(_QUIET_SEC) and self.history:
                return


async def _start_jdtls(root: Path, watch: _CallerWatch) -> JdtlsProxy:
    proxy = JdtlsProxy(on_diagnostics=watch.on_diagnostics)
    init_params = {
        "processId": os.getpid(),
        "rootUri": root.as_uri(),
        "rootPath": str(root),
        "capabilities": {"workspace": {"configuration": False, "workspaceFolders": False}},
        "initializationOptions": {},
    }
    assert await proxy.start(init_params), "jdtls failed to start"
    return proxy


@pytest.mark.timeout(_READY_TIMEOUT_SEC + _CONVERGE_TIMEOUT_SEC + 60)
@pytest.mark.parametrize(
    "send_save",
    [
        pytest.param(True, id="didSave"),
        pytest.param(
            False,
            id="no-didSave",
            marks=pytest.mark.xfail(
                strict=False,
                reason="H2: without didSave jdtls re-validates Caller against the old Foo (#109 Part B)",
            ),
        ),
    ],
)
async def test_caller_converges_after_cross_file_arity_change(tmp_path: Path, send_save: bool) -> None:
    src = tmp_path / "src" / "main" / "java" / "p"
    src.mkdir(parents=True)
    (tmp_path / "pom.xml").write_text(_POM)
    foo, caller = src / "Foo.java", src / "Caller.java"
    foo.write_text(_FOO_V1)
    caller.write_text(_CALLER_V1)
    watch = _CallerWatch(caller_uri=caller.as_uri())
    proxy = await _start_jdtls(tmp_path, watch)
    try:
        for path, text in ((foo, _FOO_V1), (caller, _CALLER_V1)):
            await proxy.send_notification(
                "textDocument/didOpen",
                {"textDocument": {"uri": path.as_uri(), "languageId": "java", "version": 1, "text": text}},
            )
        if not await watch.wait_project_ready():
            if os.environ.get("CI"):
                pytest.fail("jdtls did not import the Maven project in time")
            pytest.skip("jdtls did not import the Maven project in time")

        # Mimic an agent's Edit tool: write the file, then didChange (+ didSave).
        watch.history.clear()
        for path, text in ((foo, _FOO_V2), (caller, _CALLER_V2)):
            path.write_text(text)
            uri = path.as_uri()
            await proxy.send_notification(
                "textDocument/didChange",
                {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": text}]},
            )
            if send_save:
                await proxy.send_notification("textDocument/didSave", {"textDocument": {"uri": uri}})
        # A trailing publish can re-introduce the old-arity error after a clean one,
        # so judge the last post-edit publish once jdtls has gone quiet.
        await watch.wait_quiet()
        assert watch.history, "jdtls never re-published Caller after the edit"
        assert watch.history[-1] == [], f"Caller publishes after the edit (errors per publish): {watch.history}"
    finally:
        await proxy.stop()
