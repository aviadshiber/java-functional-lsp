"""E2E (#110, v0.14.1): cross-group dependency modules are imported only as far as the open file needs.

Real server + real jdtls on ``${revision}`` reactors whose in-repo artifacts are missing from
an isolated, offline local repository (see ``e2e_maven``).

* ``test_cross_group_dependency_resolves``: one missing sibling is imported, and the round's
  post-idle refresh (not an older publish) confirms the file clean.
* ``test_parent_inherited_chain_stops_when_clean`` (also the "no re-publish" case): the needed
  type comes from a module that is declared only in an intermediate module's *parent* pom; the
  owner itself depends on a deeper module that the open file does not need. jdtls does not
  re-publish the file after round 1's import (verified by disabling the post-idle refresh: the
  file then keeps "The import com.example.mid cannot be resolved"), so only the refresh can
  decide round 2. The import must stop once the file is clean.
* ``test_sigterm_leaves_no_jdtls``: SIGTERM on the server stops its jdtls JVM too.

Skipped when jdtls, Java 21+, or Maven (for the one-time priming) is unavailable.
"""

from __future__ import annotations

import asyncio
import shutil
import signal
from pathlib import Path

import pytest

from java_functional_lsp.proxy import find_jdtls_java_home
from tests.e2e_maven import (
    FALSE_ERRORS,
    PRIME_TIMEOUT_SEC,
    deps_xml,
    errors,
    group_pom,
    imported_gas,
    jdtls_pid,
    leaf_pom,
    log_tail,
    maven_session,
    pid_alive,
    root_pom,
    semantic,
    wait_log,
    write_files,
    write_two_group_fixture,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("jdtls") is None, reason="jdtls binary not found on PATH"),
    pytest.mark.skipif(find_jdtls_java_home() is None, reason="no Java 21+ found"),
]

_SEMANTIC_TIMEOUT_SEC = 150
# Counted from the first semantic publish: build-idle (10 s quiet), the probe refresh, the
# import, build-idle again and the round's refresh, per round.
_CLEAN_TIMEOUT_SEC = 150
# After the file is clean, keep watching long enough for a cascade to show itself.
_CASCADE_WATCH_SEC = 25


@pytest.mark.timeout(PRIME_TIMEOUT_SEC * 2 + _SEMANTIC_TIMEOUT_SEC + _CLEAN_TIMEOUT_SEC + 120)
async def test_cross_group_dependency_resolves(tmp_path: Path) -> None:
    async with maven_session(tmp_path, write_two_group_fixture) as (session, root):
        uri = session.open(root / "groupB/it/src/test/java/com/example/it/MyIntegrationTest.java")
        if not await session.watch.wait_for(uri, semantic, _SEMANTIC_TIMEOUT_SEC):
            pytest.skip(f"jdtls never analyzed the test file (import too slow?): {session.watch.history.get(uri)}")
        clean = await session.watch.wait_for(uri, lambda d: semantic(d) and not errors(d), _CLEAN_TIMEOUT_SEC)
        remaining = errors(session.watch.latest(uri))
        assert clean, f"cross-group symbols still unresolved: {remaining}\n{log_tail(session.log)}"
        assert not any(marker in e for e in remaining for marker in FALSE_ERRORS)
        # The fix itself resolved it: common was imported as a dependency module, rather than
        # jdtls resolving it some other way (e.g. a leaked artifact). The round's log line follows
        # the refresh that published the clean set.
        await wait_log(session.log, "dependency-module import stopped", 60.0)
        assert imported_gas(session.log) == ["com.example:common"], log_tail(session.log)
        # The probe and the post-idle refresh both ran, and the round's refresh saw the clean set.
        stop = next((line for line in session.log if "dependency-module import stopped" in line), None)
        assert stop is not None, f"no dependency-module STOP line\n{log_tail(session.log)}"
        assert "(clean)" in stop
        assert int(stop.split("refreshes ")[1].split()[0].rstrip(";")) >= 2, stop
        assert any("dependency round 1" in line and "refresh clean" in line for line in session.log)


# groupB/app -> groupA/mid (Target extends Mid). The module is not named "target": jdtls
# excludes that directory name from the import.
# groupA's pom (mid's parent) declares groupC/owner (Mid extends owner's Base).
# groupC/owner -> groupC/deeper, which no signature Target sees ever mentions.
_MID = """\
package com.example.mid;

import com.example.owner.Base;

public class Mid extends Base {
    public int mid() {
        return 2;
    }
}
"""
_BASE = """\
package com.example.owner;

public class Base {
    public int base() {
        return 1;
    }
}
"""
_OWNER_HELPER = """\
package com.example.owner;

import com.example.deeper.Deeper;

class OwnerHelper {
    Deeper deeper() {
        return new Deeper();
    }
}
"""
_DEEPER = """\
package com.example.deeper;

public class Deeper {
}
"""
_TARGET = """\
package com.example.app;

import com.example.mid.Mid;

public class Target extends Mid {
    public int run() {
        return base() + mid();
    }

    public int marker() {
        return "marker";
    }
}
"""
_TARGET_PATH = "groupB/app/src/main/java/com/example/app/Target.java"


def _write_chain_fixture(root: Path) -> None:
    write_files(
        root,
        {
            "pom.xml": root_pom(["groupA", "groupB", "groupC"]),
            "groupA/pom.xml": group_pom("groupA", ["mid"], deps_xml("owner")),
            "groupB/pom.xml": group_pom("groupB", ["app"]),
            "groupC/pom.xml": group_pom("groupC", ["owner", "deeper"]),
            "groupA/mid/pom.xml": leaf_pom("groupA", "mid"),
            "groupB/app/pom.xml": leaf_pom("groupB", "app", deps_xml("mid")),
            "groupC/owner/pom.xml": leaf_pom("groupC", "owner", deps_xml("deeper")),
            "groupC/deeper/pom.xml": leaf_pom("groupC", "deeper"),
            "groupA/mid/src/main/java/com/example/mid/Mid.java": _MID,
            "groupC/owner/src/main/java/com/example/owner/Base.java": _BASE,
            "groupC/owner/src/main/java/com/example/owner/OwnerHelper.java": _OWNER_HELPER,
            "groupC/deeper/src/main/java/com/example/deeper/Deeper.java": _DEEPER,
            _TARGET_PATH: _TARGET,
        },
    )


@pytest.mark.timeout(PRIME_TIMEOUT_SEC * 2 + _SEMANTIC_TIMEOUT_SEC + 2 * _CLEAN_TIMEOUT_SEC + _CASCADE_WATCH_SEC + 120)
async def test_parent_inherited_chain_stops_when_clean(tmp_path: Path) -> None:
    async with maven_session(tmp_path, _write_chain_fixture) as (session, root):
        uri = session.open(root / _TARGET_PATH)
        if not await session.watch.wait_for(uri, semantic, _SEMANTIC_TIMEOUT_SEC):
            pytest.skip(f"jdtls never analyzed the target file: {session.watch.history.get(uri)}")
        clean = await session.watch.wait_for(uri, lambda d: semantic(d) and not errors(d), 2 * _CLEAN_TIMEOUT_SEC)
        assert clean, f"target still has errors: {errors(session.watch.latest(uri))}\n{log_tail(session.log)}"
        await wait_log(session.log, "dependency-module import stopped", 60.0)
        await asyncio.sleep(_CASCADE_WATCH_SEC)
        imported = imported_gas(session.log)
        assert "com.example:deeper" not in imported, f"cascade imported an unneeded module: {imported}"
        assert set(imported) == {"com.example:mid", "com.example:owner"}, log_tail(session.log)
        assert len(imported) <= 2, imported
        assert not errors(session.watch.latest(uri))


@pytest.mark.timeout(PRIME_TIMEOUT_SEC * 2 + _SEMANTIC_TIMEOUT_SEC + 60)
async def test_sigterm_leaves_no_jdtls(tmp_path: Path) -> None:
    async with maven_session(tmp_path, write_two_group_fixture) as (session, root):
        uri = session.open(root / "groupB/it/src/test/java/com/example/it/MyIntegrationTest.java")
        if not await session.watch.wait_for(uri, semantic, _SEMANTIC_TIMEOUT_SEC):
            pytest.skip("jdtls never analyzed the test file")
        pid = jdtls_pid(session.log)
        assert pid is not None, log_tail(session.log)
        assert pid_alive(pid)
        server = session.client._server
        assert server is not None
        server.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(server.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            pytest.fail("the server did not exit within 10 s of SIGTERM")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        while pid_alive(pid) and loop.time() < deadline:
            await asyncio.sleep(0.2)
        survived = pid_alive(pid)
        assert not survived, f"jdtls JVM pid {pid} survived SIGTERM of the server\n{log_tail(session.log)}"
