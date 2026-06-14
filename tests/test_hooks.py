"""Tests for the PostToolUse lint hook (hooks/post_tool_lint.py, issue #70).

The hook is exercised as a subprocess — faithful to how Claude Code invokes it —
piping a PostToolUse JSON payload to stdin and asserting on stdout/exit code.
Every case must exit 0: the hook is failure-safe by contract.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

HOOK = Path(__file__).parent.parent / "hooks" / "post_tool_lint.py"


def run_hook(payload: Any) -> subprocess.CompletedProcess[str]:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=raw,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,  # the hook's exit code is itself under test
    )


def test_java_file_with_violation_emits_diagnostics(tmp_path: Path) -> None:
    f = tmp_path / "Test.java"
    f.write_text("class T { String f() { return null; } }")
    proc = run_hook(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(f), "old_string": "x", "new_string": "y"},
            "tool_response": {},
        }
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "null-return" in ctx
    assert str(f) in ctx


def test_non_java_file_is_silent_noop(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("hello")
    proc = run_hook({"tool_name": "Write", "tool_input": {"file_path": str(f)}})
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_clean_java_file_is_silent(tmp_path: Path) -> None:
    f = tmp_path / "Clean.java"
    f.write_text("final class Clean { static int add(int a, int b) { return a + b; } }")
    proc = run_hook({"tool_name": "Edit", "tool_input": {"file_path": str(f)}})
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_missing_file_path_key_is_silent() -> None:
    proc = run_hook({"tool_name": "Edit", "tool_input": {}})
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_malformed_stdin_exits_zero() -> None:
    proc = run_hook("not json")
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_nonexistent_file_exits_zero(tmp_path: Path) -> None:
    proc = run_hook({"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "Gone.java")}})
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_excluded_file_is_silent(tmp_path: Path) -> None:
    (tmp_path / ".java-functional-lsp.json").write_text(json.dumps({"excludes": ["**/generated/**"]}))
    gen = tmp_path / "generated"
    gen.mkdir()
    f = gen / "Gen.java"
    f.write_text("class T { String f() { return null; } }")
    proc = run_hook({"tool_name": "Write", "tool_input": {"file_path": str(f)}})
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_diagnostics_are_capped(tmp_path: Path) -> None:
    methods = "\n".join(f"String f{i}() {{ return null; }}" for i in range(30))
    f = tmp_path / "Many.java"
    f.write_text(f"class T {{\n{methods}\n}}")
    proc = run_hook({"tool_name": "Edit", "tool_input": {"file_path": str(f)}})
    assert proc.returncode == 0
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "... and" in ctx
    assert ctx.count("null-return") <= 25
