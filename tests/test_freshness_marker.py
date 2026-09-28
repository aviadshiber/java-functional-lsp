"""Tests for the freshness markers shared by the server and the PostToolUse hook (#109)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from java_functional_lsp import freshness_marker

HOOK = Path(__file__).parent.parent / "hooks" / "post_tool_lint.py"
CLEAN_JAVA = "public class Clean {}\n"


def _load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("post_tool_lint", HOOK)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def private_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _run_hook(file_path: Path, tmpdir: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], float]:
    payload = json.dumps({"tool_input": {"file_path": str(file_path)}})
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "TMPDIR": str(tmpdir), **env},
    )
    return proc, time.monotonic() - started


class TestMarker:
    def test_hook_and_server_agree_on_marker_path(self, private_tmp: Path) -> None:
        java = private_tmp / "A.java"
        assert _load_hook()._marker_path(str(java)) == freshness_marker.marker_path(str(java))

    def test_write_marker_records_time_in_private_dir(self, private_tmp: Path) -> None:
        java = private_tmp / "A.java"
        before = time.time()
        freshness_marker.write_marker(str(java))
        marker = freshness_marker.marker_path(str(java))
        assert float(marker.read_text()) >= before
        assert oct(marker.parent.stat().st_mode & 0o777) == oct(0o700)
        assert str(java) not in marker.read_text()

    def test_symlinked_marker_dir_is_refused(self, private_tmp: Path) -> None:
        elsewhere = private_tmp / "elsewhere"
        elsewhere.mkdir()
        base = freshness_marker.marker_dir().parent
        base.symlink_to(elsewhere)
        freshness_marker.write_marker(str(private_tmp / "A.java"))
        assert not any(elsewhere.rglob("*"))

    def test_write_marker_never_raises(self, private_tmp: Path) -> None:
        freshness_marker.marker_dir().parent.write_text("not a directory")
        freshness_marker.write_marker(str(private_tmp / "A.java"))


class TestHookWaits:
    def _java(self, root: Path) -> Path:
        java = root / "Clean.java"
        java.write_text(CLEAN_JAVA)
        return java

    def _stamp(self, java: Path, at: float, tmpdir: Path) -> None:
        marker = _load_hook_marker(java, tmpdir)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(repr(at))

    def test_waits_until_server_publishes_after_the_edit(self, tmp_path: Path) -> None:
        java = self._java(tmp_path)
        self._stamp(java, java.stat().st_mtime - 5, tmp_path)  # previous edit's publish
        timer = threading.Timer(0.5, lambda: self._stamp(java, time.time(), tmp_path))
        timer.start()
        try:
            proc, elapsed = _run_hook(java, tmp_path)
        finally:
            timer.cancel()
        assert proc.returncode == 0
        assert 0.45 <= elapsed < 3.0

    def test_no_marker_means_no_wait(self, tmp_path: Path) -> None:
        java = self._java(tmp_path)
        proc, elapsed = _run_hook(java, tmp_path, JAVA_FUNCTIONAL_LSP_HOOK_WAIT="3")
        assert proc.returncode == 0
        assert elapsed < 2.5

    def test_already_fresh_marker_returns_at_once(self, tmp_path: Path) -> None:
        java = self._java(tmp_path)
        self._stamp(java, time.time() + 1, tmp_path)
        _, elapsed = _run_hook(java, tmp_path, JAVA_FUNCTIONAL_LSP_HOOK_WAIT="3")
        assert elapsed < 2.5

    def test_wait_is_bounded(self, tmp_path: Path) -> None:
        java = self._java(tmp_path)
        self._stamp(java, java.stat().st_mtime - 5, tmp_path)
        proc, elapsed = _run_hook(java, tmp_path, JAVA_FUNCTIONAL_LSP_HOOK_WAIT="0.6")
        assert proc.returncode == 0
        assert 0.55 <= elapsed < 3.0

    @pytest.mark.parametrize(("value", "expected"), [("1.5", 1.5), ("-1", 0.0), ("99", 4.0), ("soon", 3.0)])
    def test_wait_seconds_parsing(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: float) -> None:
        monkeypatch.setenv("JAVA_FUNCTIONAL_LSP_HOOK_WAIT", value)
        assert _load_hook()._fresh_wait_seconds() == expected

    def test_violations_still_reported_after_wait(self, tmp_path: Path) -> None:
        java = tmp_path / "Bad.java"
        java.write_text("public class Bad { String f() { return null; } }\n")
        self._stamp(java, java.stat().st_mtime - 5, tmp_path)
        proc, _ = _run_hook(java, tmp_path, JAVA_FUNCTIONAL_LSP_HOOK_WAIT="0.3")
        out: dict[str, Any] = json.loads(proc.stdout)
        assert "null-return" in out["hookSpecificOutput"]["additionalContext"]


def _load_hook_marker(java: Path, tmpdir: Path) -> Path:
    """The marker path as the hook subprocess (TMPDIR=tmpdir) computes it."""
    saved = tempfile.tempdir
    tempfile.tempdir = str(tmpdir)
    try:
        path: Path = _load_hook()._marker_path(str(java))
        return path
    finally:
        tempfile.tempdir = saved
