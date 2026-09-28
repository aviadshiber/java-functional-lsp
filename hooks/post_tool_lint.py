#!/usr/bin/env python3
"""PostToolUse hook: lint a .java file after Edit/Write and surface violations to Claude.

Reads the Claude Code PostToolUse JSON payload on stdin, runs java-functional-lsp
on the edited file, and emits diagnostics as ``hookSpecificOutput.additionalContext``
so the agent sees them in context and can fix them immediately (issue #70). Before
returning it waits (bounded) for the running language server to publish fresh jdtls
diagnostics for the file, so Claude Code attaches current ones to this edit (#109).

Failure-safe by design: every path exits 0 — a linter problem must never break the
editing session. Prefers the fast in-process import; falls back to the CLI when the
package is not importable under this interpreter.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TIMEOUT_SECONDS = 5  # hard cap; single-file analysis is typically <200ms
MAX_DIAGNOSTICS = 25  # keep additionalContext bounded
POLL_SECONDS = 0.05
DEFAULT_WAIT_SECONDS = 3.0
MAX_WAIT_SECONDS = 4.0
EXIT_MARGIN_SECONDS = 0.5  # finish well before SIGALRM so the lint output is printed
MARKER_MAX_BYTES = 128


def _fresh_wait_seconds() -> float:
    try:
        wait = float(os.environ.get("JAVA_FUNCTIONAL_LSP_HOOK_WAIT", DEFAULT_WAIT_SECONDS))
    except ValueError:
        return DEFAULT_WAIT_SECONDS
    return max(0.0, min(wait, MAX_WAIT_SECONDS))


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return str(os.getuid()) if hasattr(os, "getuid") else "user"


def _marker_path(file_path: str) -> Path:
    """Must match java_functional_lsp.freshness_marker.marker_path (a test checks)."""
    digest = hashlib.sha256(os.path.realpath(file_path).encode()).hexdigest()[:32]
    return Path(tempfile.gettempdir()) / f"java-functional-lsp-{_user()}" / "fresh" / digest


def _owned(st: os.stat_result) -> bool:
    return not hasattr(os, "getuid") or st.st_uid == os.getuid()


def _read_marker(marker: Path) -> tuple[int, str] | None:
    """(server pid, content digest), or None unless the marker and its dirs are private and ours."""
    try:
        for level in (marker.parent.parent, marker.parent):
            st = os.lstat(level)
            if not stat.S_ISDIR(st.st_mode) or not _owned(st) or st.st_mode & 0o077:
                return None
        fd = os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or not _owned(st):
                return None
            pid, digest = os.read(fd, MARKER_MAX_BYTES).decode().split()
        finally:
            os.close(fd)
        return int(pid), digest
    except (OSError, ValueError):
        return None


def _server_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_for_fresh_diagnostics(path: Path, budget: float) -> None:
    """Wait until the language server has published final diagnostics for this edit.

    Claude Code attaches LSP diagnostics right after PostToolUse hooks finish, but jdtls
    needs 0.4-2.4s to re-validate (#109). On each final publish the server records its pid
    and a hash of the content it analyzed; waiting until that hash matches the file puts
    fresh results in this tool result. No marker, or a dead server, means nobody will
    publish — don't wait.
    """
    marker = _marker_path(str(path))
    current = _read_marker(marker)
    if current is None or not _server_alive(current[0]):
        return
    want = hashlib.sha256(path.read_bytes()).hexdigest()
    deadline = time.monotonic() + min(_fresh_wait_seconds(), budget)
    while current is not None and current[1] != want and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        current = _read_marker(marker)


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
    started = time.monotonic()
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
    try:
        budget = TIMEOUT_SECONDS - EXIT_MARGIN_SECONDS - (time.monotonic() - started)
        _wait_for_fresh_diagnostics(path, budget=budget)
    except Exception:
        pass  # a marker problem must never suppress the lint output below
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
