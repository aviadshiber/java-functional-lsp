#!/usr/bin/env python3
"""PostToolUse hook: lint a .java file after Edit/Write and surface violations to Claude.

Reads the Claude Code PostToolUse JSON payload on stdin, runs java-functional-lsp
on the edited file, and emits diagnostics as ``hookSpecificOutput.additionalContext``
so the agent sees them in context and can fix them immediately (issue #70).

Failure-safe by design: every path exits 0 — a linter problem must never break the
editing session. Prefers the fast in-process import; falls back to the CLI when the
package is not importable under this interpreter.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

TIMEOUT_SECONDS = 5  # hard cap; single-file analysis is typically <200ms
MAX_DIAGNOSTICS = 25  # keep additionalContext bounded


def _lint_in_process(path: Path) -> list[str] | None:
    """Lint via direct import. Returns None if the package isn't importable here."""
    try:
        from java_functional_lsp.analyzers.base import is_excluded
        from java_functional_lsp.cli import check_file, format_diagnostic, load_config
    except ImportError:
        return None
    config = load_config(path)
    if is_excluded(path.as_posix(), config.get("excludes", [])):
        return []
    return [format_diagnostic(path, d) for d in check_file(path, config)]


def _lint_via_cli(path: Path) -> list[str]:
    """Fallback: shell out to the installed CLI (exit 1 + stdout lines on violations)."""
    proc = subprocess.run(
        ["java-functional-lsp", "check", str(path)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,  # exit 1 just means violations were found
    )
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def main() -> None:
    hook_input = json.load(sys.stdin)
    file_path = (hook_input.get("tool_input") or {}).get("file_path", "")
    if not file_path.endswith(".java"):
        return  # silent no-op
    path = Path(file_path)
    if not path.is_file():
        return  # tool call may have failed or the file was deleted

    lines = _lint_in_process(path)
    if lines is None:
        lines = _lint_via_cli(path)
    if not lines:
        return  # clean file: stay silent, no per-edit context noise

    if len(lines) > MAX_DIAGNOSTICS:
        lines = [*lines[:MAX_DIAGNOSTICS], f"... and {len(lines) - MAX_DIAGNOSTICS} more"]
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    "java-functional-lsp found violations in the file you just edited:\n"
                    + "\n".join(lines)
                    + "\nFix each violation now with your next Edit. Do not explain or list them."
                ),
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    if hasattr(signal, "SIGALRM"):  # POSIX hard runtime cap
        signal.signal(signal.SIGALRM, lambda *_: sys.exit(0))
        signal.alarm(TIMEOUT_SECONDS)
    try:
        main()
    except Exception:
        sys.exit(0)  # hooks must never break the session
