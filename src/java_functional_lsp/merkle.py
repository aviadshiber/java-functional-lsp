"""Module snapshot tracking for incremental file-change detection between sessions.

Each Java module's source files are hashed into a ``ModuleSnapshot``.
On the next plugin session the stored snapshot is compared against the
current one; only files that actually changed are forwarded to jdtls via
``workspace/didChangeWatchedFiles``, letting it do an incremental rebuild
instead of a full cold-start re-index.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Files and directories tracked / skipped during snapshot.
_BUILD_FILES: frozenset[str] = frozenset({"pom.xml", "build.gradle", "build.gradle.kts"})
_SKIP_DIRS: frozenset[str] = frozenset({"target", ".git", "node_modules", ".gradle", "build", ".idea", ".mvn"})

# Safety bound: modules larger than this are skipped to prevent multi-second
# executor stalls (e.g., when jdtls is scoped to the monorepo root).
_MAX_FILES = 20_000

# Upper bound on entries in a persisted snapshot (sanity / DoS guard).
_MAX_SNAPSHOT_FILES = 100_000


def _blake2b_hex(data: bytes) -> str:
    """Return a 64-char hex digest of *data* using BLAKE2b (256-bit output)."""
    return hashlib.blake2b(data, digest_size=32).hexdigest()


@dataclass(frozen=True)
class TreeDiff:
    """Set-based diff between two ``ModuleSnapshot`` instances."""

    added: frozenset[str]
    modified: frozenset[str]
    removed: frozenset[str]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.removed)

    @property
    def has_build_file_changes(self) -> bool:
        """True if any build file (pom.xml, build.gradle, …) changed."""
        all_changed = self.added | self.modified | self.removed
        return any(Path(p).name in _BUILD_FILES for p in all_changed)

    @property
    def all_changed(self) -> frozenset[str]:
        """Union of added and modified relative paths (files present on disk)."""
        return self.added | self.modified


@dataclass
class ModuleSnapshot:
    """Snapshot of a Java module's tracked source files.

    ``root_hash`` is a BLAKE2b hash of all ``(relative_path, content_hash)``
    pairs sorted by path.  Two snapshots with the same ``root_hash`` are
    identical; this enables an O(1) equality check before the O(N) per-file
    diff.

    ``files`` maps each tracked relative path to the BLAKE2b hash of its
    content at snapshot time.
    """

    root_hash: str
    files: dict[str, str]  # relative path → content hash

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def build(cls, module_root: Path) -> ModuleSnapshot | None:
        """Scan *module_root* and return a snapshot, or ``None`` if too large.

        Tracked files: ``*.java`` + ``pom.xml`` / ``build.gradle`` /
        ``build.gradle.kts``.  Directories in ``_SKIP_DIRS`` (``target``,
        ``.git``, ``node_modules``, …) are pruned from the walk entirely.
        Symlinks are skipped.  Paths that resolve outside *module_root* are
        rejected (defense-in-depth against directory traversal).
        """
        files: dict[str, str] = {}

        for file_path in _iter_tracked_files(module_root):
            if len(files) >= _MAX_FILES:
                logger.warning(
                    "merkle: module %s has ≥%d tracked files — skipping snapshot",
                    module_root,
                    _MAX_FILES,
                )
                return None
            rel = str(file_path.relative_to(module_root))
            try:
                content = file_path.read_bytes()
                files[rel] = _blake2b_hex(content)
            except OSError as exc:
                logger.debug("merkle: could not read %s: %s", file_path, exc)

        root_input = "\n".join(f"{p}:{h}" for p, h in sorted(files.items()))
        root_hash = hashlib.blake2b(root_input.encode(), digest_size=32).hexdigest()
        return cls(root_hash=root_hash, files=files)

    # ------------------------------------------------------------------
    # Comparison
    # ------------------------------------------------------------------

    def diff(self, newer: ModuleSnapshot) -> TreeDiff:
        """Return what changed between *self* (old) and *newer* (current)."""
        old = self.files
        new = newer.files
        added = frozenset(new) - frozenset(old)
        removed = frozenset(old) - frozenset(new)
        modified = frozenset(p for p in old.keys() & new.keys() if old[p] != new[p])
        return TreeDiff(added=added, modified=modified, removed=removed)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Atomically write snapshot to *path* (write to temp then rename).

        The parent directory is created with mode ``0o700`` (owner-only) on
        first use to prevent other local users from tampering with the cache.
        """
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps({"root": self.root_hash, "files": self.files}, separators=(",", ":"))
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            try:
                os.write(fd, payload.encode())
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: Path) -> ModuleSnapshot | None:
        """Load a snapshot from *path*.

        Returns ``None`` on missing file, JSON parse error, or invalid
        content (path traversal attempts, oversized entries, wrong types).
        Never raises.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            root_hash = data["root"]
            files = data["files"]
            if not isinstance(root_hash, str) or not isinstance(files, dict):
                return None
            if len(files) > _MAX_SNAPSHOT_FILES:
                return None
            # Validate each entry: relative, no traversal, no nulls, str values.
            valid = all(
                isinstance(k, str)
                and isinstance(v, str)
                and "\x00" not in k
                and not Path(k).is_absolute()
                and ".." not in Path(k).parts
                for k, v in files.items()
            )
            return cls(root_hash=root_hash, files=files) if valid else None
        except (OSError, KeyError, json.JSONDecodeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _iter_tracked_files(module_root: Path) -> Iterator[Path]:
    """Yield ``.java`` and build files under *module_root*.

    Uses ``os.walk`` with ``followlinks=False`` and prunes ``_SKIP_DIRS``
    in-place so entire subtrees (``target/``, ``.git/``, …) are never
    entered.  Symlink files are skipped individually.  Any path that resolves
    outside *module_root* after symlink expansion is rejected.
    """
    module_resolved = module_root.resolve()

    for dirpath_str, dirnames, filenames in os.walk(str(module_root), followlinks=False):
        # Prune skip dirs in-place to stop os.walk from descending into them.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

        dir_path = Path(dirpath_str)
        for fname in filenames:
            if not (fname.endswith(".java") or fname in _BUILD_FILES):
                continue
            file_path = dir_path / fname
            if file_path.is_symlink():
                continue
            # Reject any path whose resolved form escapes the module root.
            try:
                file_path.resolve().relative_to(module_resolved)
            except ValueError:
                continue
            yield file_path
