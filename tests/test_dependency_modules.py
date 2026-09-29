"""Tests for importing missing in-repo Maven modules and refreshing open files (#110 part B)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from java_functional_lsp.dependency_modules import (
    DEFAULT_BUDGET,
    ENV_BUDGET,
    MAX_BUDGET,
    ClasspathRefresher,
    DependencyModules,
    path_from_uri,
    resolve_budget,
)
from java_functional_lsp.proxy import JdtlsProxy, _find_maven_group_root, _find_repo_boundary
from java_functional_lsp.reactor import ReactorIndex


class TestBudget:
    def test_default(self) -> None:
        assert resolve_budget({}, {}) == DEFAULT_BUDGET == 60

    def test_env(self) -> None:
        assert resolve_budget({}, {ENV_BUDGET: "7"}) == 7

    def test_config(self) -> None:
        assert resolve_budget({"jdtls": {"dependencyModules": 9}}, {}) == 9

    def test_env_wins_over_config(self) -> None:
        assert resolve_budget({"jdtls": {"dependencyModules": 9}}, {ENV_BUDGET: "3"}) == 3

    def test_zero_disables(self) -> None:
        assert resolve_budget({}, {ENV_BUDGET: "0"}) == 0

    def test_clamped_to_max(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            assert resolve_budget({}, {ENV_BUDGET: "5000"}) == MAX_BUDGET == 200
        assert "above the maximum" in caplog.text

    def test_negative_is_zero(self) -> None:
        assert resolve_budget({}, {ENV_BUDGET: "-4"}) == 0

    @pytest.mark.parametrize("raw", ["lots", "1.5"])
    def test_invalid_env_uses_default(self, raw: str, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            assert resolve_budget({}, {ENV_BUDGET: raw}) == DEFAULT_BUDGET
        assert "invalid" in caplog.text

    @pytest.mark.parametrize("raw", [True, [1], {"max": 1}])
    def test_invalid_config_uses_default(self, raw: Any) -> None:
        assert resolve_budget({"jdtls": {"dependencyModules": raw}}, {}) == DEFAULT_BUDGET

    def test_reads_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_BUDGET, "11")
        assert resolve_budget() == 11


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


ROOT = Path("/repo")
COMMON = ROOT / "groupA" / "common"
UTIL = ROOT / "groupA" / "util"
IT = ROOT / "groupB" / "it"


def _index() -> ReactorIndex:
    return ReactorIndex(
        ROOT,
        {"com.example:common": COMMON, "com.example:util": UTIL, "com.example:it": IT},
    )


class _Harness:
    def __init__(self, *, budget: int = DEFAULT_BUDGET, covered: list[Path] | None = None) -> None:
        self.sent: list[tuple[list[dict[str, str]], list[dict[str, str]]]] = []
        self.covered = covered if covered is not None else [ROOT / "groupB"]
        self.builds = 0

        async def send(added: list[dict[str, str]], removed: list[dict[str, str]]) -> None:
            self.sent.append((added, removed))

        def build(_root: Path) -> ReactorIndex:
            self.builds += 1
            return _index()

        self.deps = DependencyModules(
            send_folders=send,
            covered_roots=lambda: self.covered,
            reactor_root_for=lambda _d: ROOT,
            to_uri=lambda p: f"file://{p}",
            budget=budget,
            debounce=0.01,
            index_builder=build,
        )

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0.03)


class TestDependencyModules:
    async def test_batches_candidates_into_one_add(self) -> None:
        h = _Harness()
        h.deps.note_missing(IT / "pom.xml", ["com.example:common"])
        h.deps.note_missing(IT / "pom.xml", ["com.example:util", "com.example:common"])
        await h.settle()
        assert len(h.sent) == 1
        added, removed = h.sent[0]
        assert {a["uri"] for a in added} == {f"file://{COMMON}", f"file://{UTIL}"}
        assert removed == []
        assert h.deps.used == 2
        assert h.builds == 1  # single flight

    async def test_external_and_covered_artifacts_are_ignored(self) -> None:
        h = _Harness()
        h.deps.note_missing(IT / "pom.xml", ["org.external:lib", "com.example:it"])  # it: under groupB
        await h.settle()
        assert h.sent == []
        assert h.deps.used == 0

    async def test_already_added_is_not_re_added(self) -> None:
        h = _Harness()
        h.deps.note_missing(IT / "pom.xml", ["com.example:common"])
        await h.settle()
        h.deps.note_missing(IT / "pom.xml", ["com.example:common"])
        await h.settle()
        assert len(h.sent) == 1

    async def test_disabled_does_nothing(self) -> None:
        h = _Harness(budget=0)
        h.deps.note_missing(IT / "pom.xml", ["com.example:common"])
        await h.settle()
        assert h.sent == []
        assert h.builds == 0

    async def test_budget_exhaustion_warns_with_unresolved_gas(self, caplog: pytest.LogCaptureFixture) -> None:
        h = _Harness(budget=1)
        with caplog.at_level(logging.WARNING, logger="java_functional_lsp.dependency_modules"):
            h.deps.note_missing(IT / "pom.xml", ["com.example:common", "com.example:util"])
            await h.settle()
            h.deps.note_missing(IT / "pom.xml", ["com.example:util"])
            await h.settle()
        assert len(h.sent) == 1
        assert len(h.sent[0][0]) == 1
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1  # the same GA is reported once
        assert "com.example:util" in warnings[0]
        assert ENV_BUDGET in warnings[0]

    async def test_group_expansion_retires_covered_folders(self) -> None:
        h = _Harness()
        h.deps.note_missing(IT / "pom.xml", ["com.example:common"])
        await h.settle()
        removed = h.deps.take_covered_by(ROOT / "groupA")
        assert removed == [{"uri": f"file://{COMMON}", "name": "common"}]
        assert h.deps.registry == {}
        # Never re-added, even if the covering folder is not reported as covered.
        h.covered = []
        h.deps.note_missing(IT / "pom.xml", ["com.example:common"])
        await h.settle()
        assert len(h.sent) == 1
        assert h.deps.used == 1  # removal does not refund the budget

    async def test_group_expansion_drops_pending_candidates(self) -> None:
        h = _Harness()
        h.deps.debounce = 0.2
        h.deps.note_missing(IT / "pom.xml", ["com.example:common"])
        await asyncio.sleep(0.05)  # indexed and pending, not flushed yet
        assert h.deps.take_covered_by(ROOT / "groupA") == []
        await asyncio.sleep(0.3)
        assert h.sent == []

    async def test_group_expanded_before_flush_skips_candidate(self) -> None:
        h = _Harness()
        h.deps.debounce = 0.1
        h.deps.note_missing(IT / "pom.xml", ["com.example:common"])
        await asyncio.sleep(0.03)
        h.covered.append(ROOT / "groupA")
        await asyncio.sleep(0.2)
        assert h.sent == []

    async def test_index_failure_is_contained(self, caplog: pytest.LogCaptureFixture) -> None:
        def boom(_root: Path) -> ReactorIndex:
            raise OSError("disk")

        deps = DependencyModules(
            send_folders=AsyncMock(),
            covered_roots=list,
            reactor_root_for=lambda _d: ROOT,
            to_uri=str,
            debounce=0,
            index_builder=boom,
        )
        with caplog.at_level(logging.WARNING):
            deps.note_missing(IT / "pom.xml", ["com.example:common"])
            await asyncio.sleep(0.05)
        assert "reactor index" in caplog.text

    async def test_reset_forgets_session(self) -> None:
        h = _Harness()
        h.deps.note_missing(IT / "pom.xml", ["com.example:common"])
        await h.settle()
        h.deps.reset()
        assert h.deps.used == 0
        assert h.deps.registry == {}
        h.deps.note_missing(IT / "pom.xml", ["com.example:common"])
        await h.settle()
        assert len(h.sent) == 2
        assert h.builds == 2

    def test_note_missing_without_loop_is_a_no_op(self) -> None:
        _Harness().deps.note_missing(IT / "pom.xml", ["com.example:common"])


class TestClasspathRefresher:
    async def test_refreshes_open_java_files_under_project_once(self) -> None:
        sent: list[str] = []
        opened = [
            f"file://{IT}/src/test/java/A.java",
            f"file://{IT}/src/test/java/B.java",
            f"file://{IT}/pom.xml",
            f"file://{COMMON}/src/main/java/C.java",
        ]

        async def refresh(uri: str) -> None:
            sent.append(uri)

        r = ClasspathRefresher(lambda: opened, refresh, debounce=0.02)
        r.note_updated(IT)
        r.note_updated(IT)  # debounced
        await asyncio.sleep(0.1)
        assert sent == opened[:2]

    async def test_projects_are_debounced_separately(self) -> None:
        sent: list[str] = []

        async def refresh(uri: str) -> None:
            sent.append(uri)

        opened = [f"file://{IT}/A.java", f"file://{COMMON}/C.java"]
        r = ClasspathRefresher(lambda: opened, refresh, debounce=0.02)
        r.note_updated(IT)
        r.note_updated(COMMON)
        await asyncio.sleep(0.1)
        assert sorted(sent) == sorted(opened)

    async def test_open_uris_failure_is_contained(self) -> None:
        def broken() -> list[str]:
            raise RuntimeError("no workspace")

        send = AsyncMock()
        r = ClasspathRefresher(broken, send, debounce=0)
        r.note_updated(IT)
        await asyncio.sleep(0.02)
        send.assert_not_called()

    async def test_reset_cancels_pending(self) -> None:
        send = AsyncMock()
        r = ClasspathRefresher(lambda: [f"file://{IT}/A.java"], send, debounce=0.05)
        r.note_updated(IT)
        r.reset()
        await asyncio.sleep(0.1)
        send.assert_not_called()


# --- proxy wiring ---------------------------------------------------------------------------


def _pom_diag(message: str, severity: int = 1) -> dict[str, Any]:
    return {"severity": severity, "message": message, "range": {}}


class TestProxyWiring:
    def test_classpath_updated_event_triggers_refresh(self) -> None:
        proxy = JdtlsProxy()
        proxy._classpath_refresher = MagicMock()
        proxy._handle_notification(
            {"method": "language/eventNotification", "params": {"eventType": 100, "data": "file:/repo/groupB/it/"}}
        )
        proxy._classpath_refresher.note_updated.assert_called_once_with(Path("/repo/groupB/it"))

    @pytest.mark.parametrize("params", [{"eventType": 200, "data": ["file:/x/"]}, {"eventType": 100, "data": 3}, []])
    def test_other_events_are_ignored(self, params: Any) -> None:
        proxy = JdtlsProxy()
        proxy._classpath_refresher = MagicMock()
        proxy._handle_notification({"method": "language/eventNotification", "params": params})
        proxy._classpath_refresher.note_updated.assert_not_called()

    def test_missing_artifact_on_pom_queues_import_and_clearing_refreshes(self) -> None:
        proxy = JdtlsProxy()
        proxy.dependency_modules = MagicMock()
        proxy._classpath_refresher = MagicMock()
        uri = "file:///repo/groupB/it/pom.xml"
        msg = "Offline / Missing artifact com.example:common:jar:X-DEFAULT"
        proxy._handle_notification(
            {"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": [_pom_diag(msg)]}}
        )
        proxy.dependency_modules.note_missing.assert_called_once_with(
            Path("/repo/groupB/it/pom.xml"), ["com.example:common"]
        )
        proxy._classpath_refresher.note_updated.assert_not_called()
        proxy._handle_notification(
            {"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": []}}
        )
        proxy._classpath_refresher.note_updated.assert_called_once_with(Path("/repo/groupB/it"))

    def test_other_pom_errors_do_not_trigger(self) -> None:
        proxy = JdtlsProxy()
        proxy.dependency_modules = MagicMock()
        proxy._classpath_refresher = MagicMock()
        uri = "file:///repo/it/pom.xml"
        for diags in ([_pom_diag("Non-resolvable parent POM")], []):
            proxy._handle_notification(
                {"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": diags}}
            )
        proxy.dependency_modules.note_missing.assert_not_called()
        proxy._classpath_refresher.note_updated.assert_not_called()

    async def test_refresh_sends_execute_command(self) -> None:
        proxy = JdtlsProxy()
        proxy._available = True
        proxy.send_request = AsyncMock(return_value=None)  # type: ignore[method-assign]
        await proxy._refresh_file_diagnostics("file:///repo/A.java")
        proxy.send_request.assert_awaited_once()
        method, params = proxy.send_request.await_args.args
        assert method == "workspace/executeCommand"
        assert params == {
            "command": "java.project.refreshDiagnostics",
            "arguments": ["file:///repo/A.java", "thisFile", False],
        }

    async def test_refresh_skipped_when_unavailable(self) -> None:
        proxy = JdtlsProxy()
        proxy.send_request = AsyncMock()  # type: ignore[method-assign]
        await proxy._refresh_file_diagnostics("file:///repo/A.java")
        proxy.send_request.assert_not_called()

    async def test_open_uris_callback_reaches_refresher(self) -> None:
        proxy = JdtlsProxy(open_uris=lambda: ["file:///repo/it/A.java"])
        assert proxy._classpath_refresher.open_files_under(Path("/repo/it")) == ["file:///repo/it/A.java"]


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
        # A dependency folder inside groupB (e.g. added while the group expansion was pending).
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
        proxy.modules.mark_added(two_groups["it"].as_uri())
        deps = proxy.dependency_modules
        deps.debounce = 0
        deps._index_builder = lambda _r: ReactorIndex(two_groups["root"], {"com.example:common": two_groups["common"]})
        deps.note_missing(two_groups["it"] / "pom.xml", ["com.example:common"])
        for _ in range(5):
            await asyncio.sleep(0.02)
        proxy.send_notification.assert_awaited_once()
        assert proxy._expanded_groups == set()
        assert two_groups["common"].as_uri() not in proxy.modules.uris()
        assert "com.example:common" in deps.registry

    async def test_stop_resets_dependency_state(self) -> None:
        proxy = JdtlsProxy()
        proxy._available = True
        proxy.dependency_modules.registry["g:a"] = Path("/x")
        await proxy.stop()
        assert proxy.dependency_modules.registry == {}
