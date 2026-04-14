"""Tests for merkle snapshot integration with JdtlsProxy and server._apply_module_diff."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from java_functional_lsp.merkle import ModuleSnapshot, TreeDiff
from java_functional_lsp.proxy import (
    _compute_module_diff,
    _module_snapshot_path,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(files: dict[str, str]) -> ModuleSnapshot:
    h = hashlib.blake2b(digest_size=32)
    for p, fh in sorted(files.items()):
        h.update(f"{p}:{fh}\n".encode())
    return ModuleSnapshot(root_hash=h.hexdigest(), files=files)


def _make_diff(
    added: frozenset[str] | None = None,
    modified: frozenset[str] | None = None,
    removed: frozenset[str] | None = None,
) -> TreeDiff:
    return TreeDiff(
        added=added or frozenset(),
        modified=modified or frozenset(),
        removed=removed or frozenset(),
    )


# ---------------------------------------------------------------------------
# _module_snapshot_path
# ---------------------------------------------------------------------------


class TestModuleSnapshotPath:
    def test_returns_path_under_jdtls_snapshots_cache(self) -> None:
        path = _module_snapshot_path("file:///some/module")
        assert ".cache/jdtls-snapshots" in str(path)
        assert path.name == ".snapshot.json"

    def test_different_uris_produce_different_paths(self) -> None:
        p1 = _module_snapshot_path("file:///module/a")
        p2 = _module_snapshot_path("file:///module/b")
        assert p1 != p2

    def test_same_uri_produces_same_path(self) -> None:
        p1 = _module_snapshot_path("file:///module/stable")
        p2 = _module_snapshot_path("file:///module/stable")
        assert p1 == p2


# ---------------------------------------------------------------------------
# _compute_module_diff
# ---------------------------------------------------------------------------


class TestComputeModuleDiff:
    def test_returns_none_for_invalid_uri(self) -> None:
        result = _compute_module_diff("")
        assert result is None

    def test_returns_none_for_nonexistent_directory(self) -> None:
        result = _compute_module_diff("file:///nonexistent/path/that/does/not/exist")
        assert result is None

    def test_first_session_returns_none_diff_with_snapshot(self, tmp_path: Path) -> None:
        """No stored snapshot → diff is None, current snapshot is returned."""
        (tmp_path / "Foo.java").write_text("class Foo {}")
        from pygls.uris import from_fs_path

        module_uri = from_fs_path(str(tmp_path))
        assert module_uri is not None
        # Ensure no stored snapshot exists
        snap_path = _module_snapshot_path(module_uri)
        _module_snapshot_path.cache_clear()
        snap_path = _module_snapshot_path(module_uri)
        if snap_path.exists():
            snap_path.unlink()

        result = _compute_module_diff(module_uri)
        assert result is not None
        diff, snapshot = result
        assert diff is None
        assert "Foo.java" in snapshot.files

    def test_subsequent_session_detects_changes(self, tmp_path: Path) -> None:
        """Stored snapshot present → diff reflects filesystem changes."""
        from pygls.uris import from_fs_path

        foo = tmp_path / "Foo.java"
        foo.write_text("class Foo {}")
        module_uri = from_fs_path(str(tmp_path))
        assert module_uri is not None

        # First run: build and save baseline
        snap1 = ModuleSnapshot.build(tmp_path)
        assert snap1 is not None
        _module_snapshot_path.cache_clear()
        snap_path = _module_snapshot_path(module_uri)
        snap1.save(snap_path)

        # Modify a file
        foo.write_text("class Foo { int x; }")
        (tmp_path / "Bar.java").write_text("class Bar {}")

        result = _compute_module_diff(module_uri)
        assert result is not None
        diff, _ = result
        assert diff is not None
        assert "Foo.java" in diff.modified
        assert "Bar.java" in diff.added


# ---------------------------------------------------------------------------
# JdtlsProxy._kick_module_diff and await_module_diff
# ---------------------------------------------------------------------------


class TestKickModuleDiff:
    @pytest.mark.asyncio
    async def test_stores_result_before_removing_task(self) -> None:
        """Result must be in _module_diff_results before task is removed from _pending_diff_tasks."""
        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy(on_diagnostics=lambda *_: None)
        snapshot = _make_snapshot({"Foo.java": "h1"})

        with patch("java_functional_lsp.proxy._compute_module_diff", return_value=(None, snapshot)):
            task = asyncio.create_task(proxy._kick_module_diff("file:///mod"))
            await task

        assert "file:///mod" in proxy._module_diff_results
        assert "file:///mod" not in proxy._pending_diff_tasks

    @pytest.mark.asyncio
    async def test_none_result_not_stored(self) -> None:
        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy(on_diagnostics=lambda *_: None)

        with patch("java_functional_lsp.proxy._compute_module_diff", return_value=None):
            await proxy._kick_module_diff("file:///mod")

        assert "file:///mod" not in proxy._module_diff_results


class TestAwaitModuleDiff:
    @pytest.mark.asyncio
    async def test_awaits_in_flight_task(self) -> None:
        """await_module_diff waits for the task and result becomes available."""
        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy(on_diagnostics=lambda *_: None)
        snapshot = _make_snapshot({"Foo.java": "h1"})

        # Simulate a slow executor: patch to introduce a tiny delay
        async def _slow_kick(module_uri: str) -> None:
            await asyncio.sleep(0)  # yield once
            proxy._module_diff_results[module_uri] = (None, snapshot)
            proxy._pending_diff_tasks.pop(module_uri, None)

        task = asyncio.create_task(_slow_kick("file:///mod"))
        proxy._pending_diff_tasks["file:///mod"] = task  # type: ignore[assignment]

        await proxy.await_module_diff("file:///mod")
        assert "file:///mod" in proxy._module_diff_results

    @pytest.mark.asyncio
    async def test_noop_when_no_pending_task(self) -> None:
        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy(on_diagnostics=lambda *_: None)
        # Should not raise
        await proxy.await_module_diff("file:///no-such-module")

    @pytest.mark.asyncio
    async def test_swallows_cancelled_error(self) -> None:
        from java_functional_lsp.proxy import JdtlsProxy

        proxy = JdtlsProxy(on_diagnostics=lambda *_: None)

        async def _never() -> None:
            await asyncio.sleep(9999)

        task = asyncio.create_task(_never())
        proxy._pending_diff_tasks["file:///mod"] = task  # type: ignore[assignment]
        task.cancel()
        await asyncio.sleep(0)  # let cancellation propagate

        # Should not raise
        await proxy.await_module_diff("file:///mod")


# ---------------------------------------------------------------------------
# server._apply_module_diff
# ---------------------------------------------------------------------------


class _FakeProxy:
    """Minimal proxy stub for _apply_module_diff tests."""

    def __init__(self, data: tuple[TreeDiff | None, ModuleSnapshot] | None = None) -> None:
        self._data: dict[str, tuple[TreeDiff | None, ModuleSnapshot] | None] = {}
        if data is not None:
            self._data["file:///mod"] = data
        self.notifications: list[tuple[str, Any]] = []
        self._awaited = False

    def pop_module_data(self, module_uri: str) -> tuple[TreeDiff | None, ModuleSnapshot] | None:
        return self._data.pop(module_uri, None)

    async def await_module_diff(self, _: str) -> None:
        self._awaited = True

    async def send_notification(self, method: str, params: Any) -> None:
        self.notifications.append((method, params))


class TestApplyModuleDiff:
    @pytest.mark.asyncio
    async def test_noop_when_no_data(self) -> None:
        from java_functional_lsp.server import _apply_module_diff

        proxy = _FakeProxy()
        await _apply_module_diff(proxy, "file:///mod")  # type: ignore[arg-type]
        assert proxy.notifications == []

    @pytest.mark.asyncio
    async def test_awaits_task_on_race_then_processes(self) -> None:
        """If first pop returns None, await_module_diff is called and second pop is tried."""
        from java_functional_lsp.server import _apply_module_diff

        snapshot = _make_snapshot({"Foo.java": "h1"})
        proxy = _FakeProxy()

        call_count = 0

        def _pop(_: str) -> tuple[TreeDiff | None, ModuleSnapshot] | None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None
            return (None, snapshot)

        proxy.pop_module_data = _pop  # type: ignore[method-assign]

        with patch("java_functional_lsp.server._module_snapshot_path") as mock_path:
            mock_path.return_value = MagicMock()
            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value.run_in_executor = AsyncMock(return_value=None)
                await _apply_module_diff(proxy, "file:///mod")  # type: ignore[arg-type]

        assert proxy._awaited
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_first_session_saves_snapshot_without_notification(self) -> None:
        """diff=None (first session) → save snapshot, no workspace/didChangeWatchedFiles."""
        from java_functional_lsp.server import _apply_module_diff

        snapshot = _make_snapshot({"Foo.java": "h1"})
        proxy = _FakeProxy(data=(None, snapshot))

        with patch("java_functional_lsp.server._module_snapshot_path") as mock_path:
            mock_path.return_value = MagicMock()
            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value.run_in_executor = AsyncMock(return_value=None)
                await _apply_module_diff(proxy, "file:///mod")  # type: ignore[arg-type]

        assert proxy.notifications == []
        mock_loop.return_value.run_in_executor.assert_called_once()

    @pytest.mark.asyncio
    async def test_sends_correct_change_types(self) -> None:
        """Added → Created, modified → Changed, removed → Deleted."""
        from lsprotocol import types as lsp

        from java_functional_lsp.server import _apply_module_diff

        snapshot = _make_snapshot({"Foo.java": "h1"})
        diff = _make_diff(
            added=frozenset(["New.java"]),
            modified=frozenset(["Foo.java"]),
            removed=frozenset(["Old.java"]),
        )
        proxy = _FakeProxy(data=(diff, snapshot))

        with patch("java_functional_lsp.server._module_snapshot_path") as mock_path:
            mock_path.return_value = MagicMock()
            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value.run_in_executor = AsyncMock(return_value=None)
                with patch("pygls.uris.to_fs_path", return_value="/tmp/mod"):
                    with patch("pygls.uris.from_fs_path", side_effect=lambda p: f"file://{p}"):
                        await _apply_module_diff(proxy, "file:///mod")  # type: ignore[arg-type]

        assert len(proxy.notifications) == 1
        method, params = proxy.notifications[0]
        assert method == "workspace/didChangeWatchedFiles"
        changes = params["changes"]
        types_by_uri = {c["uri"].split("/")[-1]: c["type"] for c in changes}
        assert types_by_uri["New.java"] == lsp.FileChangeType.Created
        assert types_by_uri["Foo.java"] == lsp.FileChangeType.Changed
        assert types_by_uri["Old.java"] == lsp.FileChangeType.Deleted

    @pytest.mark.asyncio
    async def test_oserror_on_save_logged_not_raised(self) -> None:
        """OSError during snapshot save is logged and does not propagate."""
        from java_functional_lsp.server import _apply_module_diff

        snapshot = _make_snapshot({})
        diff = _make_diff()  # empty diff, no notification
        proxy = _FakeProxy(data=(diff, snapshot))

        with patch("java_functional_lsp.server._module_snapshot_path") as mock_path:
            mock_path.return_value = MagicMock()
            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value.run_in_executor = AsyncMock(side_effect=OSError("disk full"))
                # Should not raise
                await _apply_module_diff(proxy, "file:///mod")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _on_jdtls_diagnostics fires _apply_module_diff only on first READY transition
# ---------------------------------------------------------------------------


class TestOnJdtlsDiagnosticsReadyGuard:
    def test_fires_only_on_first_ready_transition(self) -> None:
        """_apply_module_diff should be scheduled exactly once per module."""
        from java_functional_lsp.server import server

        fired_count = 0

        def _fake_fire(coro: Any) -> None:
            nonlocal fired_count
            fired_count += 1
            # Don't actually schedule anything

            coro.close()

        with patch("java_functional_lsp.server._fire_and_forget", side_effect=_fake_fire):
            with patch("java_functional_lsp.server._analyze_and_publish"):
                with patch("java_functional_lsp.server._resolve_module_uri", return_value="file:///mod"):
                    # First call — module is not yet ready
                    server._proxy.modules._states.pop("file:///mod", None)
                    server._on_jdtls_diagnostics("file:///Foo.java", [])
                    first = fired_count
                    # Second call — module is already READY
                    server._on_jdtls_diagnostics("file:///Foo.java", [])
                    second = fired_count

        assert first == 1
        assert second == 1  # no additional fires
