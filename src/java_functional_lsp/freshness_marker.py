"""Per-file "diagnostics are final" markers for the Claude Code PostToolUse hook (#109).

Claude Code collects LSP diagnostics right after its PostToolUse hooks finish, without
waiting for the server. When the server publishes final diagnostics for a file (fresh
jdtls results merged in) it records the server pid and a hash of the content it analyzed;
``hooks/post_tool_lint.py`` waits until the hash matches the file on disk, so the fresh
set lands in the same tool result. A hash (not a time) means a publish for an older
version of the file can never satisfy the hook, and the pid lets the hook skip waiting
when the server that wrote the marker is gone.

The hook keeps its own copy of :func:`marker_path` and of the directory checks (it may run
without this package importable); tests keep the two in sync.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import stat
import tempfile
from pathlib import Path

#: Written at didOpen: never equals a content hash, so the hook waits for jdtls's first result.
OPENING = "opening"

_validated: set[Path] = set()


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # containers without a passwd entry: KeyError (<3.13) / OSError (3.13+)
        return str(os.getuid()) if hasattr(os, "getuid") else "user"


def marker_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"java-functional-lsp-{_user()}" / "fresh"


def marker_path(file_path: str) -> Path:
    digest = hashlib.sha256(os.path.realpath(file_path).encode()).hexdigest()[:32]
    return marker_dir() / digest


def content_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _ensure_private_dir(path: Path) -> bool:
    """Create *path* if needed; True only for a real directory we own with no group/other access."""
    path.mkdir(mode=0o700, exist_ok=True)
    st = os.lstat(path)  # lstat: a symlink is never S_ISDIR
    if not stat.S_ISDIR(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
        return False
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)
    return True


def write_marker(file_path: str, digest: str) -> None:
    """Record that final diagnostics for *file_path* were published for content *digest*. Never raises."""
    try:
        directory = marker_dir()
        if directory not in _validated:
            # Check each level before creating below it, so nothing lands behind a
            # symlink planted in a shared temp dir.
            if not (_ensure_private_dir(directory.parent) and _ensure_private_dir(directory)):
                return
            _validated.add(directory)
        target = marker_path(file_path)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(tmp, flags, 0o600), "w") as f:
            f.write(f"{os.getpid()} {digest}")
        os.replace(tmp, target)
    except Exception:
        _validated.clear()
