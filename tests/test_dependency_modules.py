"""Tests for the demand-driven import of missing in-repo Maven modules (#110, v0.14.1).

The round controller is driven by a fake jdtls (``_Fake``): it records imports and
refreshes, publishes diagnostics for the open file during a refresh (clean once every
needed module is imported), and publishes the pom markers a real import would surface.
All waits are milliseconds (``Timing``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from java_functional_lsp.dependency_modules import (
    DEFAULT_BUDGET,
    DEFAULT_ROUNDS,
    ENV_BUDGET,
    ENV_ROUNDS,
    MAX_BUDGET,
    MAX_ROUNDS,
    STOP_BUDGET,
    STOP_BUSY,
    STOP_CLEAN,
    STOP_NO_CANDIDATES_JAR,
    STOP_NO_CANDIDATES_MARKERS,
    STOP_NO_CANDIDATES_NONE,
    STOP_NO_FRESH,
    STOP_REASONS,
    STOP_REFRESHES,
    STOP_ROUNDS,
    STOP_WALL_CLOCK,
    BuildIdle,
    DependencyModules,
    Limits,
    Timing,
    has_demand,
    is_demand_diagnostic,
    path_from_uri,
    resolve_budget,
    resolve_rounds,
)
from java_functional_lsp.proxy import JdtlsProxy, _find_maven_group_root, _find_repo_boundary
from java_functional_lsp.reactor import Dependency, Marker, ReactorIndex

# --- knobs -----------------------------------------------------------------------------------


class TestKnobs:
    def test_repo_config_value_is_truncated_in_the_log(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="java_functional_lsp.dependency_modules"):
            assert resolve_budget({"jdtls": {"dependencyModules": "x" * 10_000}}, {}) == DEFAULT_BUDGET
        assert "invalid jdtls.dependencyModules" in caplog.text
        assert len(caplog.records[0].getMessage()) < 200

    def test_defaults(self) -> None:
        assert resolve_budget({}, {}) == DEFAULT_BUDGET == 30
        assert resolve_rounds({}, {}) == DEFAULT_ROUNDS == 6

    def test_env_may_raise_up_to_the_hard_max(self, caplog: pytest.LogCaptureFixture) -> None:
        assert resolve_budget({}, {ENV_BUDGET: "70"}) == 70
        assert resolve_rounds({}, {ENV_ROUNDS: "9"}) == 9
        with caplog.at_level(logging.WARNING):
            assert resolve_budget({}, {ENV_BUDGET: "5000"}) == MAX_BUDGET == 200
            assert resolve_rounds({}, {ENV_ROUNDS: "99"}) == MAX_ROUNDS == 20
        assert "above the maximum" in caplog.text

    def test_repo_config_can_only_lower(self, caplog: pytest.LogCaptureFixture) -> None:
        assert resolve_budget({"jdtls": {"dependencyModules": 9}}, {}) == 9
        assert resolve_rounds({"jdtls": {"dependencyRounds": 2}}, {}) == 2
        with caplog.at_level(logging.WARNING):
            assert resolve_budget({"jdtls": {"dependencyModules": 150}}, {}) == DEFAULT_BUDGET
            assert resolve_rounds({"jdtls": {"dependencyRounds": 10}}, {}) == DEFAULT_ROUNDS
        assert "can only lower" in caplog.text

    def test_env_wins_over_config(self) -> None:
        assert resolve_budget({"jdtls": {"dependencyModules": 9}}, {ENV_BUDGET: "3"}) == 3
        assert resolve_budget({"jdtls": {"dependencyModules": 9}}, {ENV_BUDGET: "50"}) == 50

    def test_zero_disables_and_negative_is_zero(self) -> None:
        assert resolve_budget({}, {ENV_BUDGET: "0"}) == 0
        assert resolve_budget({}, {ENV_BUDGET: "-4"}) == 0
        assert resolve_budget({"jdtls": {"dependencyModules": -1}}, {}) == 0

    @pytest.mark.parametrize("raw", ["lots", "1.5"])
    def test_invalid_env_uses_default(self, raw: str, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            assert resolve_budget({}, {ENV_BUDGET: raw}) == DEFAULT_BUDGET
        assert "invalid" in caplog.text

    @pytest.mark.parametrize("raw", [True, [1], {"max": 1}, "x"])
    def test_invalid_config_uses_default(self, raw: Any) -> None:
        assert resolve_budget({"jdtls": {"dependencyModules": raw}}, {}) == DEFAULT_BUDGET

    def test_reads_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_BUDGET, "11")
        monkeypatch.setenv(ENV_ROUNDS, "3")
        assert resolve_budget() == 11
        assert resolve_rounds() == 3


class TestPathFromUri:
    @pytest.mark.parametrize(
        ("uri", "expected"),
        [
            ("file:/a/b/it/", "/a/b/it"),
            ("file:///a/b/it", "/a/b/it"),
            ("file:///a/with%20space/", "/a/with space"),
        ],
    )
    def test_forms(self, uri: str, expected: str) -> None:
        assert path_from_uri(uri) == Path(expected)

    @pytest.mark.parametrize("uri", ["jdt://contents/x", "", None, 5])
    def test_non_file(self, uri: Any) -> None:
        assert path_from_uri(uri) is None


# --- demand classification -------------------------------------------------------------------


def _err(message: str, code: str | None = None, severity: int = 1) -> dict[str, Any]:
    diag: dict[str, Any] = {"severity": severity, "message": message, "range": {}}
    if code is not None:
        diag["code"] = code
    return diag


class TestDemand:
    @pytest.mark.parametrize(
        "diag",
        [
            # Real jdtls 1.61 messages (spike S1 / S2 logs).
            _err("The import com.example.common cannot be resolved", "268435846"),
            _err("BaseIntegrationTest cannot be resolved to a type", "16777218"),
            _err("The method cleanCaches() is undefined for the type MyIntegrationTest"),
            _err("sut cannot be resolved or is not a field"),
            _err("The hierarchy of the type DeltaProductConfigReportConsumerConfiguration is inconsistent"),
            _err("The type com.taboola.X cannot be resolved. It is indirectly referenced from required .class files"),
            _err("The method configure() of type Foo must override or implement a supertype method"),
            _err("unknown text", "16777218"),  # by problem id alone
        ],
    )
    def test_demand(self, diag: dict[str, Any]) -> None:
        assert is_demand_diagnostic(diag)

    @pytest.mark.parametrize(
        "diag",
        [
            _err("Type mismatch: cannot convert from String to int"),
            _err("X cannot be resolved to a type", severity=2),  # a warning
            _err("The project cannot be built until build path errors are resolved", "0"),
            _err("The container 'Maven Dependencies' references non existing library '/m2/x.jar'", "964"),
            _err("X cannot be resolved", "16"),  # non-project file
            {"severity": 1, "message": None},
            "not a dict",
        ],
    )
    def test_not_demand(self, diag: Any) -> None:
        assert not is_demand_diagnostic(diag)

    def test_scan_is_bounded(self) -> None:
        noise = [_err("Type mismatch")] * 500
        assert not has_demand([*noise, _err("X cannot be resolved to a type")])
        assert has_demand([_err("X cannot be resolved to a type"), *noise])
        assert not has_demand("nope")


# --- build-idle ------------------------------------------------------------------------------


def _progress(task_id: str, task: str = "Building", *, complete: bool = False) -> dict[str, Any]:
    return {"id": task_id, "task": task, "subTask": None, "status": "0% ", "complete": complete}


class TestBuildIdle:
    async def test_idle_after_the_quiet_window(self) -> None:
        idle = BuildIdle(quiet=0.03)
        assert not idle.is_idle()
        assert await idle.wait(None, timeout=1.0)

    async def test_open_task_blocks_until_complete_then_quiet(self) -> None:
        idle = BuildIdle(quiet=0.03)
        idle.note_progress(_progress("a"))
        assert not await idle.wait(None, timeout=0.1)  # still open: never idle
        idle.note_progress(_progress("a", complete=True))
        assert not idle.is_idle()  # the complete event itself restarts the quiet window
        assert await idle.wait(None, timeout=1.0)

    async def test_indexing_tasks_never_block(self) -> None:
        idle = BuildIdle(quiet=0.03)
        await idle.wait(None, timeout=1.0)
        idle.note_progress(_progress("s", "Searching..."))
        idle.note_progress(_progress("i", "Indexing sources"))
        assert idle.open_tasks == 0
        assert idle.is_idle()

    async def test_since_extends_the_window(self) -> None:
        idle = BuildIdle(quiet=0.05)
        await idle.wait(None, timeout=1.0)
        assert idle.is_idle()
        since = time.monotonic()
        assert not idle.is_idle(since)  # idle, but not for *quiet* seconds after *since*
        assert await idle.wait(since, timeout=1.0)
        assert time.monotonic() - since >= 0.05

    def test_open_ids_are_bounded(self) -> None:
        idle = BuildIdle(quiet=10.0)
        for i in range(BuildIdle.MAX_OPEN + 50):
            idle.note_progress(_progress(str(i)))
        assert idle.open_tasks == BuildIdle.MAX_OPEN
        assert "0" not in idle._open  # the oldest were dropped
        assert not idle.is_idle()

    async def test_single_timer(self) -> None:
        idle = BuildIdle(quiet=10.0)
        handles = []
        for i in range(3):
            idle.note_progress(_progress(str(i), complete=True))
            handles.append(idle._timer)
        assert all(h is not None for h in handles)
        assert [h.cancelled() for h in handles if h is not None] == [True, True, False]
        idle.reset()
        assert idle._timer is None

    async def test_timeout(self) -> None:
        idle = BuildIdle(quiet=0.02)
        idle.note_progress(_progress("x"))
        assert not await idle.wait(None, timeout=0.05)

    def test_malformed_progress_is_ignored(self) -> None:
        idle = BuildIdle(quiet=0.01)
        for params in (None, [], {"task": "Building"}, {"id": 3}):
            idle.note_progress(params)
        assert idle.open_tasks == 0


# --- the round controller --------------------------------------------------------------------

ROOT = Path("/repo")
TARGET = ROOT / "groupB" / "target"
A = ROOT / "groupA"
MID = A / "mid"
OWNER = ROOT / "groupC" / "owner"
DEEPER = ROOT / "groupC" / "deeper"
X = ROOT / "groupD" / "x"
Y = ROOT / "groupD" / "y"
HUB = ROOT / "groupD" / "hub"
TJ = ROOT / "groupD" / "testlib"
MODULES = {
    "g:target": TARGET,
    "g:mid": MID,
    "g:owner": OWNER,
    "g:deeper": DEEPER,
    "g:x": X,
    "g:y": Y,
    "g:hub": HUB,
    "g:testlib": TJ,
}
MAIN_FILE = f"file://{TARGET}/src/main/java/T.java"
TEST_FILE = f"file://{TARGET}/src/test/java/TTest.java"
DEMAND = [_err("Base cannot be resolved to a type", "16777218")]
CLEAN: list[dict[str, Any]] = []
FAST = Timing(idle_quiet=0.01, round_timeout=0.5, refresh_backoff=0.01, marker_wait=0.1)


def _index(target_deps: tuple[Dependency, ...] = (Dependency("g:mid"),)) -> ReactorIndex:
    """target -> mid; mid's parent groupA declares owner; owner -> deeper."""
    index = ReactorIndex(ROOT, dict(MODULES))
    index.poms = {**MODULES, "g:groupA": A}
    index.dependencies = {TARGET: target_deps, A: (Dependency("g:owner"),), OWNER: (Dependency("g:deeper"),)}
    index.parent_of = {MID: "g:groupA"}
    return index


class _Fake:
    """A fake jdtls around one DependencyModules."""

    def __init__(
        self,
        *,
        index: ReactorIndex | None = None,
        needed: set[str] | None = None,
        limits: Limits | None = None,
        timing: Timing = FAST,
        open_files: list[str] | None = None,
        markers: dict[str, list[tuple[Path, list[Marker]]]] | None = None,
    ) -> None:
        self.index = index or _index()
        self.needed = needed if needed is not None else {"g:mid", "g:owner"}
        self.open = open_files if open_files is not None else [MAIN_FILE]
        self.digest: dict[str, str] = dict.fromkeys(self.open, "d1")
        self.sent: list[list[str]] = []
        self.refreshed: list[str] = []
        self.inflight = 0
        self.max_inflight = 0
        self.answer = True
        self.publish = True
        self.refresh_delay = 0.0
        self.notified: list[str] = []
        self.on_refresh: Callable[[str], None] | None = None
        # GA imported -> pom publishes it causes (the next layer's markers).
        self.markers = (
            markers
            if markers is not None
            else {
                "g:mid": [(MID / "pom.xml", [Marker("g:owner")]), (TARGET / "pom.xml", [Marker("g:owner")])],
                "g:owner": [(OWNER / "pom.xml", [Marker("g:deeper")]), (TARGET / "pom.xml", [Marker("g:deeper")])],
            }
        )
        self.deps = DependencyModules(
            send_folders=self._send,
            covered_roots=lambda: [ROOT / "groupB"],
            reactor_root_for=lambda _d: ROOT,
            to_uri=lambda p: f"file://{p}",
            send_refresh=self._refresh,
            open_uris=lambda: list(self.open),
            doc_digest=lambda u: self.digest.get(u),  # noqa: PLW0108 (self.digest is reassigned)
            module_of=self._module_of,
            notify=self.notified.append,
            limits=limits or Limits(),
            timing=timing,
            index_builder=lambda _r: self.index,
        )

    @staticmethod
    def _module_of(path: Path) -> Path | None:
        for module in MODULES.values():
            if path.is_relative_to(module):
                return module
        return None

    @property
    def imported(self) -> list[str]:
        return [ga for batch in self.sent for ga in batch]

    async def _send(self, added: list[dict[str, str]], removed: list[dict[str, str]]) -> None:
        by_dir = {str(d): ga for ga, d in MODULES.items()}
        batch = [by_dir[a["uri"].removeprefix("file://")] for a in added]
        self.sent.append(batch)
        for ga in batch:
            for pom, markers in self.markers.get(ga, []):
                self.deps.note_pom(pom, markers)

    def diagnostics(self) -> list[dict[str, Any]]:
        return CLEAN if self.needed <= set(self.imported) else DEMAND

    async def _refresh(self, uri: str, on_response: Callable[[], None]) -> bool:
        self.refreshed.append(uri)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(self.refresh_delay or 0.001)
            if self.publish:
                self.deps.note_publish(uri, self.diagnostics())
            if self.on_refresh is not None:
                self.on_refresh(uri)
            if self.answer:
                on_response()
            return self.answer
        finally:
            self.inflight -= 1

    def start(self, uri: str = MAIN_FILE, *, pom_markers: list[Marker] | None = None) -> None:
        """jdtls imported the demand module: its pom's markers, then the file's first (stale) publish."""
        self.deps.note_pom(TARGET / "pom.xml", pom_markers if pom_markers is not None else [Marker("g:mid")])
        self.deps.note_publish(uri, DEMAND)

    async def settle(self, timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        await asyncio.sleep(0)
        while self.deps.running:
            assert loop.time() < deadline, "the round controller never stopped"
            await asyncio.sleep(0.01)


class TestRounds:
    async def test_parent_inherited_chain_stops_when_clean(self) -> None:
        fake = _Fake()
        fake.start()
        await fake.settle()
        # Round 1: mid (target's own edge); round 2: owner (inherited from mid's parent groupA).
        # deeper is in the frontier and reported missing, but the file is clean: never imported.
        assert fake.sent == [["g:mid"], ["g:owner"]]
        assert fake.deps.last_stop == STOP_CLEAN
        assert fake.deps.rounds == 2
        assert len(fake.notified) == 1
        assert "(clean)" in fake.notified[0]

    async def test_clean_at_the_probe_imports_nothing(self) -> None:
        fake = _Fake(needed=set())
        fake.start()
        await fake.settle()
        assert fake.sent == []
        assert fake.refreshed == [MAIN_FILE]
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_frontier_union_and_carry_forward(self) -> None:
        index = _index((Dependency("g:x"), Dependency("g:y")))
        markers = [Marker("g:x"), Marker("g:y")]
        fake = _Fake(index=index, needed={"g:x", "g:y"}, limits=Limits(per_round=1), markers={})
        fake.start(pom_markers=markers)
        await fake.settle()
        # y was deferred by the per-round cap and carried into round 2.
        assert fake.sent == [["g:x"], ["g:y"]]
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_candidates_are_frontier_and_markers(self) -> None:
        index = _index((Dependency("g:x"), Dependency("g:y")))
        # deeper is reported missing too, but it is not in the frontier.
        fake = _Fake(index=index, needed={"g:y"}, markers={})
        fake.start(pom_markers=[Marker("g:y"), Marker("g:deeper")])
        await fake.settle()
        assert fake.sent == [["g:y"]]

    async def test_order_is_declaration_order_with_hubs_last(self) -> None:
        index = _index((Dependency("g:hub"), Dependency("g:y"), Dependency("g:x")))
        index.fan_in = {"g:hub": 40}
        fake = _Fake(index=index, needed={"g:x"}, markers={})
        fake.start(pom_markers=[Marker("g:hub"), Marker("g:x"), Marker("g:y")])
        await fake.settle()
        assert fake.sent == [["g:y", "g:x", "g:hub"]]

    async def test_test_jar_edges_only_for_test_files_and_test_jar_markers(self) -> None:
        index = _index((Dependency("g:testlib", "test", test_jar=True),))
        fake = _Fake(index=index, needed={"g:testlib"}, open_files=[TEST_FILE], markers={})
        fake.start(TEST_FILE, pom_markers=[Marker("g:testlib", test_jar=True)])
        await fake.settle()
        assert fake.sent == [["g:testlib"]]

        # A plain-jar marker of the same GA does not match the test-jar edge.
        fake = _Fake(index=index, needed={"g:testlib"}, open_files=[TEST_FILE], markers={})
        fake.start(TEST_FILE, pom_markers=[Marker("g:testlib")])
        await fake.settle()
        assert fake.sent == []
        assert fake.deps.last_stop == STOP_NO_CANDIDATES_JAR

        # A main-source file never follows test-scope edges.
        fake = _Fake(index=index, needed={"g:testlib"}, markers={})
        fake.start(pom_markers=[Marker("g:testlib", test_jar=True)])
        await fake.settle()
        assert fake.sent == []
        assert fake.deps.last_stop == STOP_NO_CANDIDATES_NONE


class TestStopReasons:
    def test_reasons_are_a_fixed_enum(self) -> None:
        assert set(STOP_REASONS) == {
            "clean",
            "no-candidates(none)",
            "no-candidates(markers-pending)",
            "no-candidates(owner-has-jar)",
            "budget",
            "rounds",
            "wall-clock",
            "jdtls-busy",
            "no-fresh-diagnostics",
            "refresh-cap",
        }

    async def test_no_candidates_none(self) -> None:
        fake = _Fake(index=_index(()))
        fake.start()
        await fake.settle()
        assert fake.deps.last_stop == STOP_NO_CANDIDATES_NONE

    async def test_no_candidates_markers_pending(self) -> None:
        fake = _Fake()
        fake.deps.note_publish(MAIN_FILE, DEMAND)  # the demand module's pom never publishes
        await fake.settle()
        assert fake.sent == []
        assert fake.deps.last_stop == STOP_NO_CANDIDATES_MARKERS

    async def test_no_candidates_owner_has_jar(self) -> None:
        fake = _Fake()
        fake.start(pom_markers=[])  # the pom published, mid is not missing: a (stale) jar resolves it
        await fake.settle()
        assert fake.sent == []
        assert fake.deps.last_stop == STOP_NO_CANDIDATES_JAR
        assert "stale" in fake.notified[0]

    async def test_markers_of_the_previous_round_are_awaited(self) -> None:
        fake = _Fake(markers={})
        fake.start()

        def late_markers(_uri: str) -> None:
            if fake.imported == ["g:mid"]:
                loop = asyncio.get_running_loop()
                loop.call_later(0.05, fake.deps.note_pom, MID / "pom.xml", [Marker("g:owner")])

        fake.on_refresh = late_markers
        await fake.settle()
        assert fake.sent == [["g:mid"], ["g:owner"]]
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_budget_then_later_demand_stops_at_once(self) -> None:
        fake = _Fake(limits=Limits(budget=1), open_files=[MAIN_FILE, f"file://{X}/src/main/java/X.java"])
        fake.digest = dict.fromkeys(fake.open, "d1")
        fake.start()
        await fake.settle()
        assert fake.sent == [["g:mid"]]
        assert fake.deps.last_stop == STOP_BUDGET
        assert fake.deps.used == 1
        refreshes = len(fake.refreshed)
        fake.deps.note_publish(fake.open[1], DEMAND)  # a new module re-arms, but the budget is spent
        await fake.settle()
        assert fake.deps.last_stop == STOP_BUDGET
        assert len(fake.refreshed) == refreshes
        assert len(fake.notified) == 1  # a repeated session-limit stop is not re-announced

    async def test_budget_cuts_a_round_short(self) -> None:
        index = _index((Dependency("g:x"), Dependency("g:y"), Dependency("g:hub")))
        extra = f"file://{OWNER}/src/main/java/O.java"
        fake = _Fake(
            index=index,
            needed={"g:x", "g:y", "g:hub"},
            limits=Limits(budget=2, per_round=8),
            open_files=[MAIN_FILE, extra],
            markers={},
        )
        fake.digest = dict.fromkeys(fake.open, "d1")
        fake.start(pom_markers=[Marker("g:x"), Marker("g:y"), Marker("g:hub")])
        await fake.settle()
        assert fake.sent == [["g:x", "g:y"]]  # 3 candidates, only 2 left in the budget
        assert fake.deps.used == fake.deps.budget == 2
        assert fake.deps.last_stop == STOP_BUDGET
        refreshes = len(fake.refreshed)
        fake.deps.note_publish(extra, DEMAND)
        await fake.settle()
        assert fake.deps.last_stop == STOP_BUDGET
        assert len(fake.refreshed) == refreshes

    async def test_rounds(self) -> None:
        fake = _Fake(limits=Limits(rounds=1))
        fake.start()
        await fake.settle()
        assert fake.sent == [["g:mid"]]
        assert fake.deps.last_stop == STOP_ROUNDS

    async def test_wall_clock(self) -> None:
        fake = _Fake(limits=Limits(wall_clock=0.0))
        fake.start()
        await fake.settle()
        assert fake.sent == [["g:mid"]]
        assert fake.deps.last_stop == STOP_WALL_CLOCK

    async def test_wall_clock_excludes_refresh_time(self) -> None:
        fake = _Fake(limits=Limits(wall_clock=0.15))
        fake.refresh_delay = 0.2  # each refresh alone exceeds the wall clock
        fake.start()
        await fake.settle()
        assert fake.sent == [["g:mid"], ["g:owner"]]
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_wall_clock_counts_import_phases_not_session_age(self) -> None:
        other = f"file://{X}/src/main/java/X.java"
        index = _index()
        index.dependencies[X] = (Dependency("g:y"),)
        fake = _Fake(
            index=index, needed={"g:mid"}, limits=Limits(wall_clock=0.15), open_files=[MAIN_FILE, other], markers={}
        )
        fake.start()
        await fake.settle()
        assert fake.deps.last_stop == STOP_CLEAN
        await asyncio.sleep(0.3)  # idle time between phases is not import time
        fake.needed = {"g:mid", "g:y"}
        fake.deps.note_pom(X / "pom.xml", [Marker("g:y")])
        fake.deps.note_publish(other, DEMAND)  # a new module re-arms
        await fake.settle()
        assert fake.sent == [["g:mid"], ["g:y"]]
        assert fake.deps.last_stop == STOP_CLEAN
        assert fake.deps._import_time() < 0.15

    async def test_busy_before_the_probe(self) -> None:
        fake = _Fake(timing=Timing(idle_quiet=0.01, round_timeout=0.05, refresh_backoff=0.01, marker_wait=0.1))
        fake.deps.idle.note_progress(_progress("build"))  # never completes
        fake.start()
        await fake.settle()
        assert fake.refreshed == []
        assert fake.deps.last_stop == STOP_BUSY
        # Nothing was imported: the module is un-armed, so its next demand publish tries again.
        assert fake.deps._armed == set()
        fake.deps.idle.note_progress(_progress("build", complete=True))
        fake.deps.note_publish(MAIN_FILE, DEMAND)
        await fake.settle()
        assert fake.sent == [["g:mid"], ["g:owner"]]
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_repeated_busy_is_announced_once(self) -> None:
        fake = _Fake(timing=Timing(idle_quiet=0.01, round_timeout=0.03, refresh_backoff=0.01, marker_wait=0.1))
        fake.deps.idle.note_progress(_progress("build"))  # never completes
        for _ in range(3):
            fake.start()
            await fake.settle()
        assert fake.deps.last_stop == STOP_BUSY
        assert len(fake.notified) == 1

    async def test_busy_beyond_one_round_timeout_resumes_when_idle_returns(self) -> None:
        """A build that outlasts one round timeout is not a dead end (v0.14.2): the busy wait
        retries and the phase resumes once BuildIdle next reports idle, instead of stopping and
        un-arming the module at the first timeout. Only Timing fields that exist on v0.14.1
        (round_timeout/idle_quiet/refresh_backoff/marker_wait) are set here, so this test also
        serves as the fail-before reproduction against the pre-fix code."""
        fake = _Fake(timing=Timing(idle_quiet=0.01, round_timeout=0.05, refresh_backoff=0.01, marker_wait=0.1))
        fake.deps.idle.note_progress(_progress("build"))  # busy past this round's timeout
        loop = asyncio.get_running_loop()
        # Completes well after one round_timeout (0.05s) but well within any busy budget.
        loop.call_later(0.18, fake.deps.idle.note_progress, _progress("build", complete=True))
        fake.start()
        await fake.settle()
        assert fake.sent == [["g:mid"], ["g:owner"]]
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_no_fresh_at_the_probe_re_arms(self) -> None:
        fake = _Fake()
        fake.publish = False
        fake.start()
        await fake.settle()
        assert fake.deps.last_stop == STOP_NO_FRESH
        assert fake.deps._armed == set()
        fake.publish = True
        fake.start()
        await fake.settle()
        assert fake.sent == [["g:mid"], ["g:owner"]]
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_busy_after_an_import(self) -> None:
        fake = _Fake(timing=Timing(idle_quiet=0.01, round_timeout=0.1, refresh_backoff=0.01, marker_wait=0.1))
        original = fake._send

        async def send_and_build(added: list[dict[str, str]], removed: list[dict[str, str]]) -> None:
            await original(added, removed)
            fake.deps.idle.note_progress(_progress("import"))  # the import never finishes building

        fake.deps._send_folders = send_and_build
        fake.start()
        await fake.settle()
        assert fake.sent == [["g:mid"]]
        assert fake.refreshed == [MAIN_FILE]  # the probe only: no refresh into a busy jdtls
        assert fake.deps.last_stop == STOP_BUSY

    async def test_no_fresh_diagnostics_after_one_retry(self) -> None:
        fake = _Fake()
        fake.publish = False  # jdtls answers but never re-validates the file
        fake.start()
        await fake.settle()
        assert fake.refreshed == [MAIN_FILE, MAIN_FILE]
        assert fake.deps.last_stop == STOP_NO_FRESH

    async def test_unanswered_refresh_is_retried_once(self) -> None:
        fake = _Fake()
        fake.answer = False  # e.g. the 20 s timeout
        fake.start()
        await fake.settle()
        assert fake.refreshed == [MAIN_FILE, MAIN_FILE]
        assert fake.deps.last_stop == STOP_NO_FRESH

    async def test_retry_recovers(self) -> None:
        fake = _Fake(needed=set())
        calls = 0
        original = fake._refresh

        async def flaky(uri: str, on_response: Callable[[], None]) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                return False
            return await original(uri, on_response)

        fake.deps._send_refresh = flaky
        fake.start()
        await fake.settle()
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_demand_during_a_phase_gets_its_own_phase(self, caplog: pytest.LogCaptureFixture) -> None:
        other = f"file://{X}/src/main/java/X.java"
        index = _index()
        index.dependencies[X] = (Dependency("g:y"),)
        fake = _Fake(index=index, needed={"g:mid"}, open_files=[MAIN_FILE, other], markers={})
        fake.digest = dict.fromkeys(fake.open, "d1")

        def second_file_demands(uri: str) -> None:
            if uri == MAIN_FILE and fake.imported == ["g:mid"] and not fake.deps._pending_modules:
                assert fake.deps.running  # round 1 is in flight: X is only queued
                fake.deps.note_pom(X / "pom.xml", [Marker("g:y")])
                fake.deps.note_publish(other, DEMAND)

        fake.on_refresh = second_file_demands
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.dependency_modules"):
            fake.start()
            await fake.settle()
        # X had its own phase after phase 1 stopped: its probe refresh (clean here).
        assert fake.refreshed == [MAIN_FILE, MAIN_FILE, other]
        assert fake.sent == [["g:mid"]]
        stops = [r.getMessage() for r in caplog.records if "import stopped" in r.getMessage()]
        assert len(stops) == 2

    async def test_extra_quiet_window_catches_a_late_marker(self) -> None:
        timing = Timing(idle_quiet=0.2, round_timeout=2.0, refresh_backoff=0.01, marker_wait=0.05)
        fake = _Fake(needed={"g:mid"}, timing=timing, markers={})
        fake.start(pom_markers=[])  # the pom published, but no marker yet

        def late_marker(_uri: str) -> None:
            if not fake.imported:  # the probe: m2e reports mid missing within the extra window
                loop = asyncio.get_running_loop()
                loop.call_later(0.1, fake.deps.note_pom, TARGET / "pom.xml", [Marker("g:mid")])

        fake.on_refresh = late_marker
        await fake.settle()
        assert fake.sent == [["g:mid"]]
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_failing_phase_is_contained_and_re_arms(self, caplog: pytest.LogCaptureFixture) -> None:
        fake = _Fake(needed={"g:mid"})
        original = fake._send
        fail = True

        async def broken_send(added: list[dict[str, str]], removed: list[dict[str, str]]) -> None:
            if fail:
                raise RuntimeError("boom")
            await original(added, removed)

        fake.deps._send_folders = broken_send
        with caplog.at_level(logging.WARNING, logger="java_functional_lsp.dependency_modules"):
            fake.start()
            await fake.settle()
        assert not fake.deps.running
        assert "dependency-module import failed" in caplog.text
        assert fake.deps._armed == set()  # the same module's next demand tries again
        fail = False
        fake.deps.registry.clear()
        fake.deps._session.record.clear()
        fake.start()
        await fake.settle()
        assert fake.sent == [["g:mid"]]
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_failing_phase_keeps_the_queue(self) -> None:
        other = f"file://{X}/src/main/java/X.java"
        index = _index(())
        index.dependencies[X] = (Dependency("g:y"),)
        fake = _Fake(index=index, needed={"g:y"}, open_files=[MAIN_FILE, other], markers={})
        fake.digest = dict.fromkeys(fake.open, "d1")
        original = fake._refresh

        async def refresh(uri: str, on_response: Callable[[], None]) -> bool:
            if uri == MAIN_FILE:
                fake.deps.note_pom(X / "pom.xml", [Marker("g:y")])
                fake.deps.note_publish(other, DEMAND)  # queued while the first phase runs
                raise RuntimeError("boom")
            return await original(uri, on_response)

        fake.deps._send_refresh = refresh
        fake.start()
        await fake.settle()
        assert fake.sent == [["g:y"]]  # the queued module still got its phase
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_one_phase_is_one_reactor_root(self) -> None:
        other_root = Path("/other")
        roots = {X: other_root}
        fake = _Fake()
        fake.deps._reactor_root_for = lambda d: roots.get(d, ROOT)
        fake.deps._pending_modules = [TARGET, X, MID]
        assert fake.deps._take_phase() == [TARGET, MID]
        assert fake.deps._pending_modules == [X]
        assert fake.deps._take_phase() == [X]
        assert fake.deps._pending_modules == []

    async def test_disabled_does_nothing(self) -> None:
        fake = _Fake(limits=Limits(budget=0))
        fake.start()
        await asyncio.sleep(0.05)
        assert not fake.deps.running
        assert fake.refreshed == []
        assert fake.sent == []


class TestFreshness:
    async def test_stale_in_flight_publish_is_not_fresh(self) -> None:
        fake = _Fake(needed=set())
        fake.publish = False
        fake.start()
        # A publish that was already in flight arrives before the refresh is sent: it never
        # fills the refresh window, so the round has no fresh diagnostics (one retry, then STOP).
        fake.deps.note_publish(MAIN_FILE, DEMAND)
        await fake.settle()
        assert fake.deps.last_stop == STOP_NO_FRESH

    async def test_publish_after_the_response_is_not_fresh(self) -> None:
        fake = _Fake(needed=set())
        fake.publish = False
        original = fake._refresh

        async def late(uri: str, on_response: Callable[[], None]) -> bool:
            answered = await original(uri, on_response)
            fake.deps.note_publish(uri, CLEAN)  # after the response was dispatched
            return answered

        fake.deps._send_refresh = late
        fake.start()
        await fake.settle()
        assert fake.deps.last_stop == STOP_NO_FRESH

    async def test_last_publish_in_the_window_decides(self) -> None:
        fake = _Fake(needed={"g:mid"})

        def extra(uri: str) -> None:
            fake.deps.note_publish(uri, DEMAND if not fake.imported else CLEAN)

        fake.on_refresh = extra
        fake.start()
        await fake.settle()
        assert fake.sent == [["g:mid"]]
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_uri_spelling_is_keyed(self) -> None:
        fake = _Fake(needed=set())
        fake.deps._uri_key = lambda u: u.replace("file:///", "file:/")
        original = fake._refresh

        async def echo_other_form(uri: str, on_response: Callable[[], None]) -> bool:
            fake.publish = False
            fake.deps.note_publish(uri.replace("file:///", "file:/"), CLEAN)  # jdtls's own spelling
            return await original(uri, on_response)

        fake.deps._send_refresh = echo_other_form
        fake.start()
        await fake.settle()
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_edit_during_refresh_is_not_fresh(self) -> None:
        fake = _Fake(needed=set())
        edits = iter(["edited", "edited-again"])
        fake.on_refresh = lambda uri: fake.digest.__setitem__(uri, next(edits))
        fake.start()
        await fake.settle()
        # Both publishes describe content the document no longer has.
        assert fake.refreshed == [MAIN_FILE, MAIN_FILE]
        assert fake.deps.last_stop == STOP_NO_FRESH

    async def test_retry_after_an_edit_recovers(self) -> None:
        fake = _Fake(needed=set())
        fake.on_refresh = lambda uri: fake.digest.__setitem__(uri, "edited")
        fake.start()
        await fake.settle()
        assert fake.refreshed == [MAIN_FILE, MAIN_FILE]
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_unsolicited_publish_never_decides_a_round(self) -> None:
        fake = _Fake()
        original = fake._send

        async def send_and_publish(added: list[dict[str, str]], removed: list[dict[str, str]]) -> None:
            await original(added, removed)
            fake.deps.note_publish(MAIN_FILE, CLEAN)  # jdtls re-validated on its own, mid-build

        fake.deps._send_folders = send_and_publish
        fake.start()
        await fake.settle()
        # Each round still refreshed the file and decided on that fresh publish.
        assert fake.refreshed == [MAIN_FILE] * 3
        assert fake.sent == [["g:mid"], ["g:owner"]]
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_closed_demand_file_is_not_refreshed(self) -> None:
        fake = _Fake()
        original = fake._send

        async def send_and_close(added: list[dict[str, str]], removed: list[dict[str, str]]) -> None:
            await original(added, removed)
            fake.open.clear()

        fake.deps._send_folders = send_and_close
        fake.start()
        await fake.settle()
        assert fake.refreshed == [MAIN_FILE]  # the probe only
        assert fake.deps.last_stop == STOP_CLEAN

    async def test_refresh_cap_per_round_and_one_in_flight(self) -> None:
        files = [f"file://{TARGET}/src/main/java/F{i}.java" for i in range(5)]
        fake = _Fake(needed=set(), open_files=files)
        for uri in files:
            fake.deps.note_publish(uri, DEMAND)
        fake.deps.note_pom(TARGET / "pom.xml", [Marker("g:mid")])
        await fake.settle()
        assert fake.refreshed == files[::-1][:3]  # most recently opened first, at most 3
        assert fake.max_inflight == 1

    async def test_refresh_cap_per_session(self) -> None:
        other = f"file://{X}/src/main/java/X.java"
        fake = _Fake(limits=Limits(refreshes_per_session=1), open_files=[MAIN_FILE, other])
        fake.digest = dict.fromkeys(fake.open, "d1")
        fake.start()
        await fake.settle()
        assert fake.refreshed == [MAIN_FILE]
        # A session limit, not "jdtls did not re-validate the file".
        assert fake.deps.last_stop == STOP_REFRESHES
        assert "refresh cap" in fake.notified[0] or "refresh-cap" in fake.notified[0]
        fake.deps.note_publish(other, DEMAND)  # a new module: stops at once, not re-announced
        await fake.settle()
        assert fake.deps.last_stop == STOP_REFRESHES
        assert fake.refreshed == [MAIN_FILE]
        assert len(fake.notified) == 1

    def test_default_refresh_cap_lets_the_rounds_limit_fire_first(self) -> None:
        limits = Limits()
        # The probe plus every round, each with 3 files and one retry.
        assert limits.refresh_cap == 3 * (DEFAULT_ROUNDS + 1) * 2
        assert Limits(rounds=20).refresh_cap == 3 * 21 * 2
        assert Limits(refreshes_per_session=5).refresh_cap == 5

    async def test_closed_files_do_not_arm(self) -> None:
        fake = _Fake(open_files=[])
        fake.start()
        await asyncio.sleep(0.05)
        assert not fake.deps.running
        assert fake.refreshed == []


class TestRecord:
    async def test_budget_comes_from_the_proxys_own_record(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        record = tmp_path / "abc.deps.json"
        record.write_text(json.dumps({"schema": 1, "modules": [{"ga": f"g:{i}", "path": "/x"} for i in range(5)]}))
        fake = _Fake()
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.dependency_modules"):
            fake.deps.begin_session(record)
        # jdtls 1.61 does not keep added folders across a restart: the old record is not counted.
        assert fake.deps.used == 0
        assert "does not keep them" in caplog.text
        assert json.loads(record.read_text())["modules"] == []
        fake.start()
        await fake.settle()
        saved = json.loads(record.read_text())["modules"]
        assert [m["ga"] for m in saved] == ["g:mid", "g:owner"]
        assert saved[0]["path"] == str(MID)
        assert fake.deps.used == len(saved) == 2

    def test_record_write_leaves_no_temp_file_and_ignores_a_planted_name(self, tmp_path: Path) -> None:
        record = tmp_path / "abc.deps.json"
        victim = tmp_path / "victim"
        victim.write_text("keep")
        (tmp_path / "abc.deps.json.tmp").symlink_to(victim)  # v0.14.1's predictable temp name
        fake = _Fake()
        fake.deps.begin_session(record)
        assert json.loads(record.read_text()) == {"schema": 1, "modules": []}
        assert victim.read_text() == "keep"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["abc.deps.json", "abc.deps.json.tmp", "victim"]

    def test_unreadable_record_is_ignored(self, tmp_path: Path) -> None:
        record = tmp_path / "x.deps.json"
        record.write_text("{not json")
        fake = _Fake()
        fake.deps.begin_session(record)
        assert json.loads(record.read_text()) == {"schema": 1, "modules": []}
        fake.deps.begin_session(None)

    async def test_retired_folders_still_count(self) -> None:
        fake = _Fake(limits=Limits(rounds=1))
        fake.start()
        await fake.settle()
        assert fake.deps.take_covered_by(A) == [{"uri": f"file://{MID}", "name": "mid"}]
        assert fake.deps.registry == {}
        assert fake.deps.used == 1

    async def test_reset_forgets_session(self) -> None:
        fake = _Fake()
        fake.start()
        await fake.settle()
        fake.deps.reset()
        assert fake.deps.registry == {}
        assert fake.deps.used == 0
        assert fake.deps.rounds == 0
        assert fake.deps.last_stop is None

    async def test_summary_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        fake = _Fake()
        fake.start()
        await fake.settle()
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.dependency_modules"):
            fake.deps.log_summary()
        assert "rounds 2, imported 2/30, refreshes 3 (" in caplog.text
        assert "s), stops 1" in caplog.text
        assert "refreshes 3 (" in fake.notified[0]  # the refresh time is on the STOP line too

    async def test_round_log_names_the_imports(self, caplog: pytest.LogCaptureFixture) -> None:
        fake = _Fake()
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.dependency_modules"):
            fake.start()
            await fake.settle()
        rounds = [r.getMessage() for r in caplog.records if "dependency round" in r.getMessage()]
        assert len(rounds) == 2
        assert "imported 1 dependency module(s) (1/30 used): g:mid (groupA/mid)" in rounds[0]
        assert rounds[0].index("imported") < rounds[0].index("build idle") < rounds[0].index("refresh")
        assert "refresh errors" in rounds[0]
        assert "refresh clean" in rounds[1]
        assert any("stopped (clean)" in r.getMessage() for r in caplog.records)

    async def test_index_failure_is_contained(self, caplog: pytest.LogCaptureFixture) -> None:
        fake = _Fake()

        def broken(_root: Path) -> ReactorIndex:
            raise RuntimeError("boom")

        fake.deps._index_builder = broken
        with caplog.at_level(logging.WARNING, logger="java_functional_lsp.dependency_modules"):
            fake.start()
            await fake.settle()
        assert "reactor index of repo failed" in caplog.text
        assert fake.deps.last_stop == STOP_NO_CANDIDATES_NONE

    def test_note_publish_without_loop_is_a_no_op(self) -> None:
        fake = _Fake()
        fake.start()
        assert not fake.deps.running
        # Nothing was scheduled, so the module is not kept armed (a later publish can arm it).
        assert fake.deps._armed == set()
        assert fake.deps._pending_modules == []


class TestAggregatorsAndSymlinks:
    async def test_aggregators_are_never_candidates(self, tmp_path: Path) -> None:
        from java_functional_lsp.reactor import build_reactor_index

        agg = "<project><groupId>g</groupId><artifactId>{a}</artifactId>{d}<modules>{m}</modules></project>"
        dep = "<dependencies><dependency><groupId>g</groupId><artifactId>{a}</artifactId></dependency></dependencies>"
        root = tmp_path / "root"
        leaf_deps = dep.format(a="grp").replace("</dependencies>", "") + dep.format(a="leaf").replace(
            "<dependencies>", ""
        )
        for rel, text in {
            "": agg.format(a="root", d="", m="<module>grp</module><module>app</module>"),
            "grp": agg.format(a="grp", d="", m="<module>leaf</module>"),
            "grp/leaf": "<project><groupId>g</groupId><artifactId>leaf</artifactId></project>",
            "app": f"<project><groupId>g</groupId><artifactId>app</artifactId>{leaf_deps}</project>",
        }.items():
            (root / rel).mkdir(parents=True, exist_ok=True)
            (root / rel / "pom.xml").write_text(text)
        index = build_reactor_index(root)
        assert index.aggregators == {"g:root", "g:grp"}
        app = index.poms["g:app"]
        deps = DependencyModules(
            send_folders=AsyncMock(),
            covered_roots=list,
            reactor_root_for=lambda _d: root,
            to_uri=str,
            module_of=lambda _p: app,
            open_uris=lambda: [f"file://{app}/src/A.java"],
            index_builder=lambda _r: index,
        )
        deps.note_pom(app / "pom.xml", [Marker("g:grp"), Marker("g:leaf")])
        candidates, frontier, _ = await deps._candidates([f"file://{app}/src/A.java"])
        assert [ga for ga, _ in candidates] == ["g:leaf"]
        assert frontier == 1

    @pytest.mark.skipif(os.name == "nt", reason="symlinks")
    async def test_symlinked_covered_root_is_not_a_candidate(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        common = real / "groupA" / "common"
        it = real / "groupB" / "it"
        for d in (common, it):
            d.mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        index = ReactorIndex(real, {"g:common": common, "g:it": it})
        index.dependencies = {it: (Dependency("g:common"),)}
        covered: list[Path] = []
        deps = DependencyModules(
            send_folders=AsyncMock(),
            covered_roots=lambda: covered,
            reactor_root_for=lambda _d: real,
            to_uri=str,
            module_of=lambda _p: it,
            index_builder=lambda _r: index,
        )
        deps.note_pom(link / "groupB" / "it" / "pom.xml", [Marker("g:common")])
        uri = f"file://{link}/groupB/it/src/A.java"
        # Positive control: nothing covers it yet.
        assert [ga for ga, _ in (await deps._candidates([uri]))[0]] == ["g:common"]
        covered.append(link / "groupA" / "common")  # imported through the symlink
        assert (await deps._candidates([uri]))[0] == []

    @pytest.mark.skipif(os.name == "nt", reason="symlinks")
    def test_take_covered_by_matches_the_symlinked_spelling(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        (real / "groupA" / "common").mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        deps = DependencyModules(AsyncMock(), list, lambda _d: real, str)
        deps.registry["g:common"] = (real / "groupA" / "common").resolve()
        assert [r["name"] for r in deps.take_covered_by(link / "groupA")] == ["common"]


# --- proxy wiring ----------------------------------------------------------------------------


def _pom_diag(message: str, severity: int = 1) -> dict[str, Any]:
    return {"severity": severity, "message": message, "range": {}}


def _publish(proxy: JdtlsProxy, uri: str, diagnostics: list[Any]) -> None:
    proxy._handle_notification(
        {"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": diagnostics}}
    )


class TestProxyWiring:
    def test_every_pom_publish_feeds_markers(self) -> None:
        proxy = JdtlsProxy()
        uri = "file:///repo/groupB/it/pom.xml"
        msgs = [
            "Offline / Missing artifact com.example:common:jar:X-DEFAULT",
            "Missing artifact com.example:base:jar:tests:X-DEFAULT",
            "Non-resolvable parent POM",
        ]
        for _ in range(2):  # identical publishes still count as arrivals
            _publish(proxy, uri, [_pom_diag(m) for m in msgs])
        module = Path("/repo/groupB/it").resolve()
        deps = proxy.dependency_modules
        assert deps._pom_publishes[module] == 2
        assert deps._markers[module] == {Marker("com.example:common"), Marker("com.example:base", test_jar=True)}
        _publish(proxy, uri, [])
        assert module not in deps._markers

    def test_java_publish_reaches_the_controller(self) -> None:
        proxy = JdtlsProxy()
        seen: list[tuple[str, Any]] = []
        proxy.dependency_modules.note_publish = lambda u, d: seen.append((u, d))  # type: ignore[method-assign]
        _publish(proxy, "file:///repo/A.java", DEMAND)
        _publish(proxy, "file:///repo/pom.xml", [])
        assert seen == [("file:///repo/A.java", DEMAND)]

    def test_progress_report_reaches_build_idle(self) -> None:
        proxy = JdtlsProxy()
        proxy._handle_notification({"method": "language/progressReport", "params": _progress("b")})
        assert proxy.dependency_modules.idle.open_tasks == 1

    def test_classpath_updated_no_longer_refreshes(self) -> None:
        proxy = JdtlsProxy()
        proxy.send_request = AsyncMock()  # type: ignore[method-assign]
        proxy._handle_notification(
            {"method": "language/eventNotification", "params": {"eventType": 100, "data": "file:/repo/groupB/it/"}}
        )
        proxy.send_request.assert_not_called()

    async def test_refresh_sends_execute_command_and_reports_the_answer(self) -> None:
        proxy = JdtlsProxy()
        proxy._available = True
        proxy._request = AsyncMock(return_value=(True, None))  # type: ignore[method-assign]
        assert await proxy._refresh_file_diagnostics("file:///repo/A.java")
        method, params = proxy._request.await_args.args
        assert method == "workspace/executeCommand"
        assert params == {
            "command": "java.project.refreshDiagnostics",
            "arguments": ["file:///repo/A.java", "thisFile", False],
        }
        proxy._request = AsyncMock(return_value=(False, None))  # type: ignore[method-assign]
        assert not await proxy._refresh_file_diagnostics("file:///repo/A.java")

    async def test_refresh_skipped_when_unavailable(self) -> None:
        proxy = JdtlsProxy()
        proxy._request = AsyncMock()  # type: ignore[method-assign]
        assert not await proxy._refresh_file_diagnostics("file:///repo/A.java")
        proxy._request.assert_not_called()

    def test_callbacks_reach_the_controller(self) -> None:
        notes: list[str] = []
        proxy = JdtlsProxy(
            open_uris=lambda: ["file:///repo/it/A.java"], doc_digest=lambda _u: "d", notify_client=notes.append
        )
        deps = proxy.dependency_modules
        assert deps._open_list() == ["file:///repo/it/A.java"]
        assert deps._doc_digest("x") == "d"
        deps._notify("hi")
        assert notes == ["hi"]


@pytest.fixture
def two_groups(tmp_path: Path) -> dict[str, Path]:
    _find_maven_group_root.cache_clear()
    _find_repo_boundary.cache_clear()
    root = tmp_path / "root"
    agg = "<project><modules><module>x</module></modules></project>"
    leaf = "<project><artifactId>leaf</artifactId></project>"
    for rel, text in {"": agg, "groupA": agg, "groupB": agg, "groupA/common": leaf, "groupB/it": leaf}.items():
        d = root / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "pom.xml").write_text(text)
    src = root / "groupA" / "common" / "src" / "A.java"
    src.parent.mkdir(parents=True)
    src.write_text("class A {}")
    return {"root": root, "common": root / "groupA" / "common", "it": root / "groupB" / "it", "src": src}


class TestProxyGroupDedupe:
    async def test_group_expansion_removes_dependency_folders_in_same_event(self, two_groups: dict[str, Path]) -> None:
        proxy = JdtlsProxy()
        proxy._available = True
        proxy._original_root_uri = two_groups["root"].as_uri()
        proxy.send_notification = AsyncMock()  # type: ignore[method-assign]
        common = two_groups["common"].resolve()
        proxy.dependency_modules.registry["com.example:common"] = common
        await proxy.add_module_if_new(two_groups["src"].as_uri())
        method, params = proxy.send_notification.await_args.args
        assert method == "workspace/didChangeWorkspaceFolders"
        assert [a["name"] for a in params["event"]["added"]] == ["groupA"]
        assert [r["name"] for r in params["event"]["removed"]] == ["common"]
        assert proxy.dependency_modules.registry == {}

    async def test_expand_full_workspace_removes_covered_dependency_folders(self, two_groups: dict[str, Path]) -> None:
        proxy = JdtlsProxy()
        proxy._available = True
        proxy._original_root_uri = two_groups["root"].as_uri()
        proxy._initial_module_uri = two_groups["it"].as_uri()
        proxy.modules.mark_added(two_groups["it"].as_uri())
        proxy.send_notification = AsyncMock()  # type: ignore[method-assign]
        proxy.dependency_modules.registry["com.example:it2"] = (two_groups["root"] / "groupB" / "it2").resolve()
        proxy.dependency_modules.registry["com.example:common"] = two_groups["common"].resolve()
        await proxy.expand_full_workspace()
        params = proxy.send_notification.await_args.args[1]
        assert "it2" in [r["name"] for r in params["event"]["removed"]]
        assert "com.example:common" in proxy.dependency_modules.registry

    async def test_dependency_folders_are_not_in_group_registries(self, two_groups: dict[str, Path]) -> None:
        proxy = JdtlsProxy()
        proxy._available = True
        proxy.send_notification = AsyncMock()  # type: ignore[method-assign]
        await proxy.dependency_modules._import([("com.example:common", two_groups["common"])])
        proxy.send_notification.assert_awaited_once()
        assert proxy._expanded_groups == set()
        assert two_groups["common"].as_uri() not in proxy.modules.uris()
        assert "com.example:common" in proxy.dependency_modules.registry

    @pytest.mark.skipif(os.name == "nt", reason="symlinks")
    async def test_symlinked_client_root_still_retires_dependency_folders(
        self, two_groups: dict[str, Path], tmp_path: Path
    ) -> None:
        link = tmp_path / "link"
        link.symlink_to(two_groups["root"], target_is_directory=True)
        proxy = JdtlsProxy()
        proxy._available = True
        proxy._original_root_uri = link.as_uri()
        proxy.send_notification = AsyncMock()  # type: ignore[method-assign]
        proxy.dependency_modules.registry["com.example:common"] = two_groups["common"].resolve()
        await proxy.expand_full_workspace()
        params = proxy.send_notification.await_args.args[1]
        assert [r["name"] for r in params["event"]["removed"]] == ["common"]
        assert proxy.dependency_modules.registry == {}

    async def test_stop_resets_dependency_state(self) -> None:
        proxy = JdtlsProxy()
        proxy._available = True
        proxy.dependency_modules.registry["g:a"] = Path("/x")
        await proxy.stop()
        assert proxy.dependency_modules.registry == {}
