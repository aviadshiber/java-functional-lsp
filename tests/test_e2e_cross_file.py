"""E2E: does jdtls converge a dependent file after a cross-file signature change? (#109, H2)

Reproduces the issue's arity variant against a real jdtls: ``Foo`` gains a constructor
parameter, then ``Caller``'s call site is updated. If jdtls validates dependents one run
late, Caller's last publish still reports the old arity until some later edit. Marked
xfail(strict=False) because it measures jdtls behavior: an XPASS means the cross-file
variant needs no wrapper-side nudge.
"""

from __future__ import annotations

import asyncio
import os
import shutil
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
_CONVERGE_TIMEOUT_SEC = 20


def _errors(diags: list[Any]) -> list[str]:
    return [d.get("message", "") for d in diags if isinstance(d, dict) and d.get("severity") == 1]


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
    foo_uri, caller_uri = foo.as_uri(), caller.as_uri()

    latest: dict[str, list[Any]] = {}
    caller_history: list[list[str]] = []
    arrived = asyncio.Event()

    def on_diagnostics(uri: str, diags: list[Any]) -> None:
        latest[uri] = diags
        if uri == caller_uri:
            caller_history.append(_errors(diags))
        arrived.set()

    proxy = JdtlsProxy(on_diagnostics=on_diagnostics)
    init_params = {
        "processId": os.getpid(),
        "rootUri": tmp_path.as_uri(),
        "rootPath": str(tmp_path),
        "capabilities": {"workspace": {"configuration": False, "workspaceFolders": False}},
        "initializationOptions": {},
    }
    assert await proxy.start(init_params), "jdtls failed to start"
    try:
        for uri, text in ((foo_uri, _FOO_V1), (caller_uri, _CALLER_V1)):
            await proxy.send_notification(
                "textDocument/didOpen",
                {"textDocument": {"uri": uri, "languageId": "java", "version": 1, "text": text}},
            )

        async def wait_for(predicate: Any, timeout: float) -> bool:
            deadline = asyncio.get_running_loop().time() + timeout
            while not predicate():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return False
                arrived.clear()
                try:
                    await asyncio.wait_for(arrived.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return False
            return True

        def project_ready() -> bool:
            diags = latest.get(caller_uri)
            return diags is not None and not any(str(d.get("code")) == _NON_PROJECT_CODE for d in diags)

        if not await wait_for(project_ready, _READY_TIMEOUT_SEC):
            pytest.skip("jdtls did not import the Maven project in time")
        assert _errors(latest[caller_uri]) == []

        # Mimic an agent's Edit tool: write the file, then didChange + didSave.
        caller_history.clear()
        for path, uri, text in ((foo, foo_uri, _FOO_V2), (caller, caller_uri, _CALLER_V2)):
            path.write_text(text)
            await proxy.send_notification(
                "textDocument/didChange",
                {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": text}]},
            )
            if send_save:
                await proxy.send_notification("textDocument/didSave", {"textDocument": {"uri": uri}})
        converged = await wait_for(lambda: _errors(latest.get(caller_uri, [])) == [], _CONVERGE_TIMEOUT_SEC)
        # A trailing publish can re-introduce the old-arity error after a clean one.
        await asyncio.sleep(5)
        history = f"Caller publishes after the edit (errors per publish): {caller_history}"
        assert converged, history
        assert _errors(latest.get(caller_uri, [])) == [], history
    finally:
        await proxy.stop()
