"""Shared helpers for the real-jdtls Maven e2e tests (#110).

Fixture reactors live under ``tmp_path`` and resolve against an isolated local repository
(never ``~/.m2``). That repository is primed once, online, with Maven's plugins only, by
building a two-group fixture; every ``com.example`` artifact is wiped before each test, so
an in-repo module is always "missing" as it is for an unpublished sibling. It is cached in
``~/.cache/java-functional-lsp/`` (override with ``JAVA_FUNCTIONAL_LSP_E2E_M2``).

The server runs with ``HOME``/``user.home``/``TMPDIR`` pointed into ``tmp_path`` so jdtls,
m2e and the proxy (Lombok lookup, jdtls data cache) stay away from the real home directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from lsprotocol import types as lsp
from pygls.lsp.client import LanguageClient

from java_functional_lsp.proxy import find_jdtls_java_home

PRIME_TIMEOUT_SEC = 300

_PLUGIN_MANAGEMENT = """\
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
"""


def root_pom(modules: list[str]) -> str:
    mods = "\n".join(f"    <module>{m}</module>" for m in modules)
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>root</artifactId>
  <version>${{revision}}</version>
  <packaging>pom</packaging>
  <properties>
    <revision>X-DEFAULT</revision>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
{_PLUGIN_MANAGEMENT}  <modules>
{mods}
  </modules>
</project>
"""


def deps_xml(*artifact_ids: str) -> str:
    """A ``<dependencies>`` block on ``com.example:<id>:${revision}`` for each id."""
    body = "".join(
        f"""
    <dependency>
      <groupId>com.example</groupId>
      <artifactId>{a}</artifactId>
      <version>${{revision}}</version>
    </dependency>"""
        for a in artifact_ids
    )
    return f"\n  <dependencies>{body}\n  </dependencies>" if artifact_ids else ""


def group_pom(name: str, modules: list[str], deps: str = "") -> str:
    mods = "\n".join(f"    <module>{m}</module>" for m in modules)
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
  <packaging>pom</packaging>{deps}
  <modules>
{mods}
  </modules>
</project>
"""


def leaf_pom(parent: str, name: str, deps: str = "") -> str:
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


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


# --- The two-group fixture: groupB/it's test extends groupA/common's base class. ---

BASE_JAVA = """\
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
TEST_JAVA = """\
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
JDTLS_MARKER = "Type mismatch"
FALSE_ERRORS = ("cannot be resolved", "is undefined for the type", "hierarchy of the type")


def write_two_group_fixture(root: Path) -> Path:
    """Write the two-group reactor under *root* and return MyIntegrationTest.java."""
    write_files(
        root,
        {
            "pom.xml": root_pom(["groupA", "groupB"]),
            "groupA/pom.xml": group_pom("groupA", ["common"]),
            "groupB/pom.xml": group_pom("groupB", ["it"]),
            "groupA/common/pom.xml": leaf_pom("groupA", "common"),
            "groupB/it/pom.xml": leaf_pom("groupB", "it", deps_xml("common")),
            "groupA/common/src/main/java/com/example/common/BaseIntegrationTest.java": BASE_JAVA,
            "groupB/it/src/test/java/com/example/it/MyIntegrationTest.java": TEST_JAVA,
        },
    )
    return root / "groupB/it/src/test/java/com/example/it/MyIntegrationTest.java"


def settings_xml(path: Path, repo: Path, *, offline: bool) -> Path:
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">\n'
        f"  <localRepository>{repo}</localRepository>\n"
        f"  <offline>{'true' if offline else 'false'}</offline>\n"
        "</settings>\n"
    )
    return path


def primed_repo(work: Path, fake_home: Path) -> Path:
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
        write_two_group_fixture(prime_root)
        settings = settings_xml(work / "settings-online.xml", repo, offline=False)
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
                    timeout=PRIME_TIMEOUT_SEC,
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


def write_offline_config(root: Path, settings: Path) -> None:
    """``.java-functional-lsp.json`` pointing m2e at the isolated, offline repository.

    ``jdtls.settings`` replaces the proxy's defaults, so the defaults are repeated here.
    """
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


class Watch:
    def __init__(self) -> None:
        self.history: dict[str, list[list[lsp.Diagnostic]]] = {}
        self.changed = asyncio.Event()

    def on_publish(self, params: lsp.PublishDiagnosticsParams) -> None:
        self.history.setdefault(params.uri, []).append(list(params.diagnostics))
        self.changed.set()

    def latest(self, uri: str) -> list[lsp.Diagnostic]:
        return self.history.get(uri, [[]])[-1]

    async def wait_for(self, uri: str, predicate: Callable[[list[lsp.Diagnostic]], bool], timeout: float) -> bool:
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


def errors(diags: list[lsp.Diagnostic]) -> list[str]:
    """Error messages other than the deliberate marker."""
    return [d.message for d in diags if d.severity == lsp.DiagnosticSeverity.Error and JDTLS_MARKER not in d.message]


def semantic(diags: list[lsp.Diagnostic]) -> bool:
    return any(JDTLS_MARKER in d.message for d in diags)


async def _collect(stream: asyncio.StreamReader | None, lines: list[str]) -> None:
    """Keep the server's stderr (its log) drained and captured."""
    if stream is None:
        return
    while line := await stream.readline():
        lines.append(line.decode(errors="replace").rstrip())


_IMPORT_LINE_RE = re.compile(r"imported \d+ dependency module\(s\) \([^)]*\): (.*?) -> build idle")
_GA_RE = re.compile(r"(com\.example:[A-Za-z0-9_.\-]+)")
_JDTLS_PID_RE = re.compile(r"jdtls subprocess started \(pid=(\d+)")


def imported_gas(server_log: list[str]) -> list[str]:
    """Every GA the proxy imported as a dependency module, in import order."""
    result: list[str] = []
    for line in server_log:
        match = _IMPORT_LINE_RE.search(line)
        if match:
            result.extend(_GA_RE.findall(match.group(1)))
    return result


def jdtls_pid(server_log: list[str]) -> int | None:
    for line in server_log:
        match = _JDTLS_PID_RE.search(line)
        if match:
            return int(match.group(1))
    return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Session:
    client: LanguageClient
    watch: Watch
    log: list[str] = field(default_factory=list)

    def open(self, path: Path) -> str:
        uri = path.as_uri()
        self.client.text_document_did_open(
            lsp.DidOpenTextDocumentParams(
                text_document=lsp.TextDocumentItem(uri=uri, language_id="java", version=1, text=path.read_text())
            )
        )
        return uri


@asynccontextmanager
async def maven_session(
    tmp_path: Path, write_fixture: Callable[[Path], object], *, env_extra: dict[str, str] | None = None
) -> AsyncIterator[tuple[Session, Path]]:
    """Start the real server on a reactor written by *write_fixture* under ``tmp_path/root``."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    markers = tmp_path / "tmp"
    markers.mkdir()
    repo = primed_repo(tmp_path, fake_home)
    root = tmp_path / "root"
    write_fixture(root)
    write_offline_config(root, settings_xml(tmp_path / "settings.xml", repo, offline=True))

    watch = Watch()
    client = LanguageClient("e2e-dependency-modules", "1.0")

    def on_publish(params: lsp.PublishDiagnosticsParams) -> None:  # pygls needs a plain function
        watch.on_publish(params)

    client.feature(lsp.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)(on_publish)
    env = {
        **os.environ,
        "HOME": str(fake_home),
        "JAVA_TOOL_OPTIONS": f"-Duser.home={fake_home}",
        "TMPDIR": str(markers),
    }
    for knob in ("JAVA_FUNCTIONAL_LSP_DEPENDENCY_MODULES", "JAVA_FUNCTIONAL_LSP_DEPENDENCY_ROUNDS"):
        env.pop(knob, None)
    env["JAVA_FUNCTIONAL_LSP_LOG_LEVEL"] = "INFO"
    env.update(env_extra or {})
    await client.start_io(sys.executable, "-m", "java_functional_lsp", env=env)
    session = Session(client, watch)
    stderr_task = asyncio.create_task(_collect(client._server.stderr, session.log))  # type: ignore[union-attr]
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
        yield session, root
    finally:
        # Keep the server log for post-mortems (the path is in pytest's tmp dir).
        with suppress(OSError):
            (tmp_path / "server.log").write_text("\n".join(session.log))
        pid = jdtls_pid(session.log)
        with suppress(Exception):
            await asyncio.wait_for(client.shutdown_async(None), timeout=5.0)
            client.exit(None)
        with suppress(Exception):
            await asyncio.wait_for(client.stop(), timeout=5.0)
        stderr_task.cancel()
        # Never leave a 4 GB JVM behind, whatever the code under test did.
        if pid is not None and pid_alive(pid):
            with suppress(OSError):
                os.kill(pid, 9)


async def wait_log(server_log: list[str], needle: str, timeout: float) -> bool:
    """Wait until a server log line contains *needle*."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not any(needle in line for line in server_log):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.1)
    return True


def log_tail(server_log: list[str], n: int = 4000) -> str:
    return "\n".join(server_log)[-n:]
