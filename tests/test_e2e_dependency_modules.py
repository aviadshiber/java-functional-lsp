"""E2E (#110): a dependency on a reactor module in another Maven group resolves.

Real server + real jdtls on a two-group reactor with ``${revision}``::

    root/pom.xml               (revision X-DEFAULT, modules groupA, groupB)
    root/groupA/common         BaseIntegrationTest
    root/groupB/it             MyIntegrationTest extends BaseIntegrationTest (depends on common)

The client root is ``root``; the group walk scopes jdtls to ``groupB``, so ``common`` is
not imported and m2e looks for ``com.example:common:jar:X-DEFAULT`` in the local
repository. That repository is isolated (never ``~/.m2``), primed with Maven's plugins
only, and used offline, so the artifact is missing, as it is for an unpublished sibling.
Without the fix the test file keeps four false errors next to its one deliberate error ("cannot be resolved",
"undefined for the type"). With it, the proxy imports ``common`` from the pom.xml
"Missing artifact" diagnostic and refreshes the open file.

Skipped when jdtls, Java 21+, or Maven (for the one-time priming) is unavailable.
The primed repository is cached in ``~/.cache/java-functional-lsp/`` (override with
``JAVA_FUNCTIONAL_LSP_E2E_M2``).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
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

_ROOT_POM = """\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>root</artifactId>
  <version>${revision}</version>
  <packaging>pom</packaging>
  <properties>
    <revision>X-DEFAULT</revision>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
  <build>
    <pluginManagement>
      <plugins>
        <plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId>\
<version>3.11.0</version></plugin>
        <plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-resources-plugin</artifactId>\
<version>3.3.1</version></plugin>
        <plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId>\
<version>3.5.4</version></plugin>
      </plugins>
    </pluginManagement>
  </build>
  <modules>
    <module>groupA</module>
    <module>groupB</module>
  </modules>
</project>
"""


def _group_pom(name: str, module: str) -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>com.example</groupId>
    <artifactId>root</artifactId>
    <version>${{revision}}</version>
  </parent>
  <artifactId>{name}</artifactId>
  <packaging>pom</packaging>
  <modules>
    <module>{module}</module>
  </modules>
</project>
"""


def _leaf_pom(parent: str, name: str, deps: str = "") -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>com.example</groupId>
    <artifactId>{parent}</artifactId>
    <version>${{revision}}</version>
  </parent>
  <artifactId>{name}</artifactId>{deps}
</project>
"""


_IT_DEPS = """
  <dependencies>
    <dependency>
      <groupId>com.example</groupId>
      <artifactId>common</artifactId>
      <version>${revision}</version>
    </dependency>
  </dependencies>"""

_BASE = """\
package com.example.common;

public abstract class BaseIntegrationTest {
    protected String sut = "system-under-test";

    protected void cleanCaches() {
        sut = null;
    }
}
"""

# The type mismatch is a deterministic jdtls error that needs a resolved JRE: its presence proves a
# publish came from jdtls's semantic pass (the server's custom-only publish and jdtls's non-project,
# syntax-only publish both lack it). It is the only error the file should keep.
_TEST = """\
package com.example.it;

import com.example.common.BaseIntegrationTest;

public class MyIntegrationTest extends BaseIntegrationTest {
    public int run() {
        cleanCaches();
        return this.sut.length();
    }

    public int marker() {
        return "marker";
    }
}
"""
_JDTLS_MARKER = "Type mismatch"
_FALSE_ERRORS = ("cannot be resolved", "is undefined for the type", "hierarchy of the type")

_SEMANTIC_TIMEOUT_SEC = 150
_CLEAN_TIMEOUT_SEC = 60
_PRIME_TIMEOUT_SEC = 300


def _write_fixture(root: Path) -> Path:
    """Write the reactor under *root* and return MyIntegrationTest.java."""
    files = {
        "pom.xml": _ROOT_POM,
        "groupA/pom.xml": _group_pom("groupA", "common"),
        "groupB/pom.xml": _group_pom("groupB", "it"),
        "groupA/common/pom.xml": _leaf_pom("groupA", "common"),
        "groupB/it/pom.xml": _leaf_pom("groupB", "it", _IT_DEPS),
        "groupA/common/src/main/java/com/example/common/BaseIntegrationTest.java": _BASE,
        "groupB/it/src/test/java/com/example/it/MyIntegrationTest.java": _TEST,
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root / "groupB/it/src/test/java/com/example/it/MyIntegrationTest.java"


def _settings_xml(path: Path, repo: Path, *, offline: bool) -> Path:
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">\n'
        f"  <localRepository>{repo}</localRepository>\n"
        f"  <offline>{'true' if offline else 'false'}</offline>\n"
        "</settings>\n"
    )
    return path


def _primed_repo(work: Path, fake_home: Path) -> Path:
    """Return a local repository holding Maven's plugins but no com.example artifacts.

    With a truly empty offline repository m2e cannot compute a build plan and creates no
    Java project at all, so the plugins are fetched once (online) by building the fixture.
    """
    repo = Path(
        os.environ.get("JAVA_FUNCTIONAL_LSP_E2E_M2")
        or Path.home() / ".cache" / "java-functional-lsp" / "e2e-m2-issue110-v1"
    )
    stamp = repo / ".primed"
    if not stamp.is_file():
        mvn = shutil.which("mvn")
        java_home = find_jdtls_java_home()
        if mvn is None or java_home is None:
            pytest.skip("Maven is needed once to prime the isolated local repository")
        repo.mkdir(parents=True, exist_ok=True)
        prime_root = work / "prime"
        _write_fixture(prime_root)
        settings = _settings_xml(work / "settings-online.xml", repo, offline=False)
        env = {
            **os.environ,
            "JAVA_HOME": java_home,
            "HOME": str(fake_home),
            "MAVEN_OPTS": f"-Duser.home={fake_home}",
        }
        for goal in ("test-compile", "clean"):
            cmd = [mvn, "-B", "-q", "-s", str(settings), "-gs", str(settings), f"-Dmaven.repo.local={repo}"]
            try:
                proc = subprocess.run(
                    [*cmd, "-f", str(prime_root / "pom.xml"), goal],
                    capture_output=True,
                    text=True,
                    timeout=_PRIME_TIMEOUT_SEC,
                    check=False,
                    env=env,
                )
            except (OSError, subprocess.SubprocessError) as e:
                pytest.skip(f"priming the local repository failed: {e}")
            if proc.returncode != 0:
                pytest.skip(f"priming the local repository failed (offline?): {proc.stdout[-500:]}")
        stamp.write_text("ok\n")
    # The reactor build never installs, but a stale com.example would make the test vacuous.
    shutil.rmtree(repo / "com" / "example", ignore_errors=True)
    return repo


class _Watch:
    def __init__(self) -> None:
        self.history: dict[str, list[list[lsp.Diagnostic]]] = {}
        self.changed = asyncio.Event()

    def on_publish(self, params: lsp.PublishDiagnosticsParams) -> None:
        self.history.setdefault(params.uri, []).append(list(params.diagnostics))
        self.changed.set()

    def latest(self, uri: str) -> list[lsp.Diagnostic]:
        return self.history.get(uri, [[]])[-1]

    async def wait_for(self, uri: str, predicate: object, timeout: float) -> bool:
        assert callable(predicate)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not (self.history.get(uri) and predicate(self.latest(uri))):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
        return True


async def _collect(stream: asyncio.StreamReader | None, lines: list[str]) -> None:
    """Keep the server's stderr (its log) drained and captured."""
    if stream is None:
        return
    while line := await stream.readline():
        lines.append(line.decode(errors="replace").rstrip())


async def _assert_dependency_module_imported(server_log: list[str]) -> None:
    """The fix itself resolved it: common was imported as a dependency module and the open
    file refreshed, rather than jdtls resolving it some other way (e.g. a leaked artifact)."""
    imports = [line for line in server_log if "importing 1 dependency module(s)" in line]
    assert imports, "\n".join(server_log)[-4000:]
    assert "com.example:common" in imports[0]
    # The refresh follows the pom's "Missing artifact" clearing after a debounce, and may land
    # after jdtls's own clean publish.
    for _ in range(100):
        if any("classpath of it updated, refreshing" in line for line in server_log):
            return
        await asyncio.sleep(0.1)
    raise AssertionError("open file never refreshed:\n" + "\n".join(server_log)[-4000:])


def _errors(diags: list[lsp.Diagnostic]) -> list[str]:
    """Error messages other than the deliberate marker."""
    return [d.message for d in diags if d.severity == lsp.DiagnosticSeverity.Error and _JDTLS_MARKER not in d.message]


def _semantic(diags: list[lsp.Diagnostic]) -> bool:
    return any(_JDTLS_MARKER in d.message for d in diags)


@pytest.mark.timeout(_PRIME_TIMEOUT_SEC * 2 + _SEMANTIC_TIMEOUT_SEC + _CLEAN_TIMEOUT_SEC + 60)
async def test_cross_group_dependency_resolves(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    markers = tmp_path / "tmp"
    markers.mkdir()
    repo = _primed_repo(tmp_path, fake_home)
    root = tmp_path / "root"
    test_file = _write_fixture(root)
    settings = _settings_xml(tmp_path / "settings.xml", repo, offline=True)
    # jdtls.settings replaces the proxy's defaults, so the defaults are repeated here.
    (root / ".java-functional-lsp.json").write_text(
        json.dumps(
            {
                "jdtls": {
                    "settings": {
                        "java": {
                            "import": {
                                "maven": {"enabled": True, "offline": {"enabled": True}},
                                "gradle": {"enabled": False},
                                "exclusions": ["**/node_modules/**", "**/.metadata/**", "**/target/**"],
                            },
                            "configuration": {
                                "updateBuildConfiguration": "automatic",
                                "maven": {"userSettings": str(settings), "globalSettings": str(settings)},
                            },
                            "maven": {"downloadSources": False},
                        }
                    }
                }
            }
        )
    )

    watch = _Watch()
    client = LanguageClient("e2e-dependency-modules", "1.0")

    def on_publish(params: lsp.PublishDiagnosticsParams) -> None:  # pygls needs a plain function
        watch.on_publish(params)

    client.feature(lsp.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)(on_publish)
    env = {
        **os.environ,
        # Keep jdtls, m2e and the proxy (Lombok lookup, jdtls data cache) away from the real ~/.m2 and ~/.cache.
        "HOME": str(fake_home),
        "JAVA_TOOL_OPTIONS": f"-Duser.home={fake_home}",
        "TMPDIR": str(markers),
    }
    env.pop("JAVA_FUNCTIONAL_LSP_DEPENDENCY_MODULES", None)
    env["JAVA_FUNCTIONAL_LSP_LOG_LEVEL"] = "INFO"
    await client.start_io(sys.executable, "-m", "java_functional_lsp", env=env)
    server_log: list[str] = []
    stderr_task = asyncio.create_task(_collect(client._server.stderr, server_log))
    uri = test_file.as_uri()
    try:
        await client.initialize_async(
            lsp.InitializeParams(
                process_id=os.getpid(),
                root_uri=root.as_uri(),
                capabilities=lsp.ClientCapabilities(),
                client_info=lsp.ClientInfo(name="Claude Code"),
            )
        )
        client.initialized(lsp.InitializedParams())
        client.text_document_did_open(
            lsp.DidOpenTextDocumentParams(
                text_document=lsp.TextDocumentItem(uri=uri, language_id="java", version=1, text=_TEST)
            )
        )
        if not await watch.wait_for(uri, _semantic, _SEMANTIC_TIMEOUT_SEC):
            pytest.skip(f"jdtls never analyzed the test file (import too slow?): {watch.history.get(uri)}")
        clean = await watch.wait_for(uri, lambda d: _semantic(d) and not _errors(d), _CLEAN_TIMEOUT_SEC)
        errors = _errors(watch.latest(uri))
        assert clean, f"cross-group symbols still unresolved: {errors}"
        assert not any(marker in e for e in errors for marker in _FALSE_ERRORS)
        await _assert_dependency_module_imported(server_log)
    finally:
        try:
            await asyncio.wait_for(client.shutdown_async(None), timeout=5.0)
            client.exit(None)
        except Exception:
            pass
        try:
            await asyncio.wait_for(client.stop(), timeout=5.0)
        except Exception:
            pass
        stderr_task.cancel()
