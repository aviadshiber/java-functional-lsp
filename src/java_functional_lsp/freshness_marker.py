"""Per-file "diagnostics are final" markers for the Claude Code PostToolUse hook (#109).

Claude Code collects LSP diagnostics right after its PostToolUse hooks finish, without
waiting for the server. The server records, per file, when it last published a final
diagnostic set (fresh jdtls results merged in); ``hooks/post_tool_lint.py`` waits until
that time is newer than the edited file so the fresh set lands in the same tool result.

Markers hold only a timestamp and are named by a hash of the path. The hook keeps its own
copy of :func:`marker_path` (it may run without this package importable); a test keeps
the two in sync.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import tempfile
import time
from pathlib import Path


def marker_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"java-functional-lsp-{getpass.getuser()}" / "fresh"


def marker_path(file_path: str) -> Path:
    digest = hashlib.sha256(os.path.realpath(file_path).encode()).hexdigest()[:32]
    return marker_dir() / digest


def _private_dir(path: Path) -> bool:
    """True if *path* is a real directory owned by us (not a symlink planted in a shared tmp)."""
    try:
        st = path.lstat()
    except OSError:
        return False
    owned = not hasattr(os, "getuid") or st.st_uid == os.getuid()
    return path.is_dir() and not path.is_symlink() and owned


def write_marker(file_path: str) -> None:
    """Record that final diagnostics for *file_path* were published now. Never raises."""
    directory = marker_dir()
    try:
        # Check each level before creating below it, so nothing is created through a
        # symlink planted in a shared temp dir.
        for level in (directory.parent, directory):
            level.mkdir(mode=0o700, exist_ok=True)
            if not _private_dir(level):
                return
        target = marker_path(file_path)
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(repr(time.time()))
        os.replace(tmp, target)
    except OSError:
        return
