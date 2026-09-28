"""Tests for the freshness markers shared by the server and the PostToolUse hook (#109)."""

from __future__ import annotations

import getpass
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from java_functional_lsp import freshness_marker

HOOK = Path(__file__).parent.parent / "hooks" / "post_tool_lint.py"
CLEAN_JAVA = "public class Clean {}\n"
DEAD_PID = 2**22 + 12345  # above the hard pid limit on Linux (PID_MAX_LIMIT 2**22) and macOS (99999)


def _load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("post_tool_lint", HOOK)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def private_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    freshness_marker._validated.clear()
    yield tmp_path
    freshness_marker._validated.clear()


def _digest_of(path: Path) -> str:
    return freshness_marker.content_digest(path.read_bytes())


def _stamp(java: Path, digest: str, pid: int | None = None) -> None:
    """Write a marker the way the server does (tempfile.tempdir must point at the test dir)."""
    if pid is None:
        freshness_marker.write_marker(str(java), digest)
        return
    marker = freshness_marker.marker_path(str(java))
    marker.write_text(f"{pid} {digest}")


def _run_hook(file_path: Path, tmpdir: Path, wait: str = "3") -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_input": {"file_path": str(file_path)}}),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "TMPDIR": str(tmpdir), "JAVA_FUNCTIONAL_LSP_HOOK_WAIT": wait},
    )
    return proc, time.monotonic() - started


class TestMarker:
    def test_hook_and_server_agree_on_marker_path(self, private_tmp: Path) -> None:
        java = private_tmp / "A.java"
        assert _load_hook()._marker_path(str(java)) == freshness_marker.marker_path(str(java))

    def test_user_fallback_matches_when_getpass_fails(self, private_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_user() -> str:
            raise KeyError("no passwd entry")

        monkeypatch.setattr(getpass, "getuser", no_user)
        java = private_tmp / "A.java"
        assert _load_hook()._marker_path(str(java)) == freshness_marker.marker_path(str(java))
        assert str(os.getuid()) in str(freshness_marker.marker_dir())

    def test_write_marker_records_pid_and_digest_privately(self, private_tmp: Path) -> None:
        java = private_tmp / "A.java"
        freshness_marker.write_marker(str(java), "abc123")
        marker = freshness_marker.marker_path(str(java))
        assert marker.read_text() == f"{os.getpid()} abc123"
        assert marker.stat().st_mode & 0o777 == 0o600
        assert marker.parent.stat().st_mode & 0o777 == 0o700
        assert _load_hook()._read_marker(marker) == (os.getpid(), "abc123")

    @pytest.mark.parametrize("level", ["base", "fresh"])
    def test_symlinked_marker_dir_is_refused(self, private_tmp: Path, level: str) -> None:
        elsewhere = private_tmp / "elsewhere"
        elsewhere.mkdir()
        directory = freshness_marker.marker_dir()
        if level == "base":
            directory.parent.symlink_to(elsewhere)
        else:
            directory.parent.mkdir(mode=0o700)
            directory.symlink_to(elsewhere)
        freshness_marker.write_marker(str(private_tmp / "A.java"), "abc")
        assert not any(elsewhere.rglob("*"))

    def test_loose_permissions_are_tightened(self, private_tmp: Path) -> None:
        base = freshness_marker.marker_dir().parent
        base.mkdir(mode=0o755)
        base.chmod(0o755)
        freshness_marker.write_marker(str(private_tmp / "A.java"), "abc")
        assert base.stat().st_mode & 0o777 == 0o700

    def test_write_marker_never_raises(self, private_tmp: Path) -> None:
        freshness_marker.marker_dir().parent.write_text("not a directory")
        freshness_marker.write_marker(str(private_tmp / "A.java"), "abc")
        assert not freshness_marker.marker_dir().exists()

    @pytest.mark.parametrize("content", ["", "garbage", "1 2 3", "notapid abc", "0 abc", "-1 abc"])
    def test_hook_rejects_malformed_marker(self, private_tmp: Path, content: str) -> None:
        java = private_tmp / "A.java"
        freshness_marker.write_marker(str(java), "abc")
        marker = freshness_marker.marker_path(str(java))
        marker.write_text(content)
        assert _load_hook()._read_marker(marker) is None

    def test_hook_rejects_loose_marker_dir(self, private_tmp: Path) -> None:
        java = private_tmp / "A.java"
        freshness_marker.write_marker(str(java), "abc")
        freshness_marker.marker_dir().chmod(0o777)
        assert _load_hook()._read_marker(freshness_marker.marker_path(str(java))) is None

    def test_hook_does_not_follow_marker_symlink(self, private_tmp: Path) -> None:
        java = private_tmp / "A.java"
        freshness_marker.write_marker(str(java), "abc")
        marker = freshness_marker.marker_path(str(java))
        target = private_tmp / "planted"
        target.write_text(f"{os.getpid()} abc")
        marker.unlink()
        marker.symlink_to(target)
        assert _load_hook()._read_marker(marker) is None


class TestHookWait:
    """_wait_for_fresh_diagnostics in-process: real time, small budgets."""

    def _java(self, root: Path, text: str = CLEAN_JAVA) -> Path:
        java = root / "Clean.java"
        java.write_text(text)
        return java

    def _timed_wait(self, java: Path, budget: float, monkeypatch: pytest.MonkeyPatch, wait: str = "4") -> float:
        monkeypatch.setenv("JAVA_FUNCTIONAL_LSP_HOOK_WAIT", wait)
        started = time.monotonic()
        _load_hook()._wait_for_fresh_diagnostics(java, budget=budget)
        return time.monotonic() - started

    def test_returns_when_digest_matches(self, private_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        java = self._java(private_tmp)
        _stamp(java, "previous-version")
        timer = threading.Timer(0.3, lambda: _stamp(java, _digest_of(java)))
        timer.start()
        try:
            elapsed = self._timed_wait(java, budget=3.0, monkeypatch=monkeypatch)
        finally:
            timer.cancel()
        assert 0.25 <= elapsed < 2.0

    def test_already_matching_returns_at_once(self, private_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        java = self._java(private_tmp)
        _stamp(java, _digest_of(java))
        assert self._timed_wait(java, budget=3.0, monkeypatch=monkeypatch) < 0.2

    def test_no_marker_means_no_wait(self, private_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._timed_wait(self._java(private_tmp), budget=3.0, monkeypatch=monkeypatch) < 0.2

    def test_dead_server_means_no_wait(self, private_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        java = self._java(private_tmp)
        _stamp(java, "previous-version")  # creates the private dirs
        _stamp(java, "previous-version", pid=DEAD_PID)
        assert self._timed_wait(java, budget=3.0, monkeypatch=monkeypatch) < 0.2

    def test_opening_placeholder_waits(self, private_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        java = self._java(private_tmp)
        _stamp(java, freshness_marker.OPENING)
        elapsed = self._timed_wait(java, budget=0.3, monkeypatch=monkeypatch)
        assert 0.25 <= elapsed < 1.0

    @pytest.mark.parametrize(("budget", "wait", "low", "high"), [(0.3, "4", 0.25, 1.0), (4.0, "0.3", 0.25, 1.0)])
    def test_wait_bounded_by_budget_and_setting(
        self, private_tmp: Path, monkeypatch: pytest.MonkeyPatch, budget: float, wait: str, low: float, high: float
    ) -> None:
        java = self._java(private_tmp)
        _stamp(java, "previous-version")
        assert low <= self._timed_wait(java, budget=budget, monkeypatch=monkeypatch, wait=wait) < high

    @pytest.mark.parametrize("budget", [0.0, -1.0])
    def test_exhausted_budget_returns_immediately(
        self, private_tmp: Path, monkeypatch: pytest.MonkeyPatch, budget: float
    ) -> None:
        java = self._java(private_tmp)
        _stamp(java, "previous-version")
        assert self._timed_wait(java, budget=budget, monkeypatch=monkeypatch) < 0.2

    @pytest.mark.parametrize(("value", "expected"), [("1.5", 1.5), ("-1", 0.0), ("99", 4.0), ("soon", 3.0)])
    def test_wait_seconds_parsing(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: float) -> None:
        monkeypatch.setenv("JAVA_FUNCTIONAL_LSP_HOOK_WAIT", value)
        assert _load_hook()._fresh_wait_seconds() == expected


class TestHookSubprocess:
    """Smoke tests of the hook as Claude Code runs it."""

    def test_waits_for_matching_digest(self, private_tmp: Path) -> None:
        java = private_tmp / "Clean.java"
        java.write_text(CLEAN_JAVA)
        _stamp(java, "previous-version")
        timer = threading.Timer(1.0, lambda: _stamp(java, _digest_of(java)))
        timer.start()
        try:
            proc, elapsed = _run_hook(java, private_tmp)
        finally:
            timer.cancel()
        assert proc.returncode == 0
        assert 0.95 <= elapsed < 3.0

    def test_violations_still_reported_after_wait(self, private_tmp: Path) -> None:
        java = private_tmp / "Bad.java"
        java.write_text("public class Bad { String f() { return null; } }\n")
        _stamp(java, "previous-version")
        proc, _ = _run_hook(java, private_tmp, wait="0.3")
        assert proc.returncode == 0
        out: dict[str, Any] = json.loads(proc.stdout)
        assert "null-return" in out["hookSpecificOutput"]["additionalContext"]

    def test_broken_marker_dir_never_suppresses_lint_output(self, private_tmp: Path) -> None:
        java = private_tmp / "Bad.java"
        java.write_text("public class Bad { String f() { return null; } }\n")
        freshness_marker.marker_dir().parent.write_text("not a directory")
        proc, _ = _run_hook(java, private_tmp)
        assert proc.returncode == 0
        assert "null-return" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
