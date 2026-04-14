"""Tests for merkle.ModuleSnapshot and TreeDiff."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from java_functional_lsp.merkle import (
    _BUILD_FILES,
    ModuleSnapshot,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write(path: Path, content: str = "class Foo {}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# ModuleSnapshot.build
# ---------------------------------------------------------------------------


class TestModuleSnapshotBuild:
    def test_empty_module(self, tmp_path: Path) -> None:
        snap = ModuleSnapshot.build(tmp_path)
        assert snap is not None
        assert snap.files == {}

    def test_tracks_java_files(self, tmp_path: Path) -> None:
        write(tmp_path / "src" / "Foo.java", "class Foo {}")
        write(tmp_path / "src" / "Bar.java", "class Bar {}")
        snap = ModuleSnapshot.build(tmp_path)
        assert snap is not None
        assert "src/Foo.java" in snap.files
        assert "src/Bar.java" in snap.files

    def test_tracks_build_files(self, tmp_path: Path) -> None:
        write(tmp_path / "pom.xml", "<project/>")
        write(tmp_path / "build.gradle", "apply plugin: 'java'")
        snap = ModuleSnapshot.build(tmp_path)
        assert snap is not None
        assert "pom.xml" in snap.files
        assert "build.gradle" in snap.files

    def test_ignores_non_tracked_files(self, tmp_path: Path) -> None:
        write(tmp_path / "README.md", "# readme")
        write(tmp_path / "src" / "Foo.class", b"\xca\xfe\xba\xbe".decode("latin-1"))
        snap = ModuleSnapshot.build(tmp_path)
        assert snap is not None
        assert all(f.endswith(".java") or Path(f).name in _BUILD_FILES for f in snap.files)

    def test_skips_target_directory(self, tmp_path: Path) -> None:
        write(tmp_path / "src" / "Main.java")
        write(tmp_path / "target" / "classes" / "Main.java")
        snap = ModuleSnapshot.build(tmp_path)
        assert snap is not None
        assert "src/Main.java" in snap.files
        assert not any("target" in f for f in snap.files)

    def test_skips_git_directory(self, tmp_path: Path) -> None:
        write(tmp_path / ".git" / "hooks" / "pre-commit.java")
        write(tmp_path / "src" / "App.java")
        snap = ModuleSnapshot.build(tmp_path)
        assert snap is not None
        assert "src/App.java" in snap.files
        assert not any(".git" in f for f in snap.files)

    def test_skips_node_modules(self, tmp_path: Path) -> None:
        write(tmp_path / "node_modules" / "pkg" / "Foo.java")
        write(tmp_path / "src" / "App.java")
        snap = ModuleSnapshot.build(tmp_path)
        assert snap is not None
        assert not any("node_modules" in f for f in snap.files)

    def test_skips_symlinks(self, tmp_path: Path) -> None:
        real = write(tmp_path / "Real.java")
        link = tmp_path / "Link.java"
        link.symlink_to(real)
        snap = ModuleSnapshot.build(tmp_path)
        assert snap is not None
        assert "Real.java" in snap.files
        assert "Link.java" not in snap.files

    def test_root_hash_stable(self, tmp_path: Path) -> None:
        write(tmp_path / "Foo.java")
        snap1 = ModuleSnapshot.build(tmp_path)
        snap2 = ModuleSnapshot.build(tmp_path)
        assert snap1 is not None
        assert snap2 is not None
        assert snap1.root_hash == snap2.root_hash

    def test_root_hash_changes_on_content_change(self, tmp_path: Path) -> None:
        f = write(tmp_path / "Foo.java", "class Foo {}")
        snap1 = ModuleSnapshot.build(tmp_path)
        f.write_text("class Foo { int x; }")
        snap2 = ModuleSnapshot.build(tmp_path)
        assert snap1 is not None
        assert snap2 is not None
        assert snap1.root_hash != snap2.root_hash

    def test_returns_none_above_file_count_limit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Patch the limit to 2 to avoid creating thousands of files.
        import java_functional_lsp.merkle as merkle_mod

        monkeypatch.setattr(merkle_mod, "_MAX_FILES", 2)
        for i in range(3):
            write(tmp_path / f"Cls{i}.java")
        result = ModuleSnapshot.build(tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# TreeDiff
# ---------------------------------------------------------------------------


class TestTreeDiff:
    def _snap(self, files: dict[str, str]) -> ModuleSnapshot:
        root_input = "\n".join(f"{p}:{h}" for p, h in sorted(files.items()))
        import hashlib

        root_hash = hashlib.blake2b(root_input.encode(), digest_size=32).hexdigest()
        return ModuleSnapshot(root_hash=root_hash, files=files)

    def test_empty_diff_when_identical(self) -> None:
        snap = self._snap({"Foo.java": "aaa"})
        diff = snap.diff(snap)
        assert diff.is_empty

    def test_added_file(self) -> None:
        old = self._snap({"Foo.java": "aaa"})
        new = self._snap({"Foo.java": "aaa", "Bar.java": "bbb"})
        diff = old.diff(new)
        assert "Bar.java" in diff.added
        assert diff.modified == frozenset()
        assert diff.removed == frozenset()

    def test_modified_file(self) -> None:
        old = self._snap({"Foo.java": "aaa"})
        new = self._snap({"Foo.java": "bbb"})
        diff = old.diff(new)
        assert "Foo.java" in diff.modified
        assert diff.added == frozenset()
        assert diff.removed == frozenset()

    def test_removed_file(self) -> None:
        old = self._snap({"Foo.java": "aaa", "Bar.java": "bbb"})
        new = self._snap({"Foo.java": "aaa"})
        diff = old.diff(new)
        assert "Bar.java" in diff.removed
        assert diff.added == frozenset()
        assert diff.modified == frozenset()

    def test_all_changed_excludes_removed(self) -> None:
        old = self._snap({"Foo.java": "aaa", "Bar.java": "bbb"})
        new = self._snap({"Baz.java": "ccc"})
        diff = old.diff(new)
        assert "Baz.java" in diff.all_changed
        assert "Bar.java" not in diff.all_changed  # removed, not added/modified

    def test_has_build_file_changes_pom(self) -> None:
        old = self._snap({"pom.xml": "v1"})
        new = self._snap({"pom.xml": "v2"})
        diff = old.diff(new)
        assert diff.has_build_file_changes

    def test_has_build_file_changes_gradle(self) -> None:
        old = self._snap({"build.gradle": "v1"})
        new = self._snap({"build.gradle": "v2"})
        diff = old.diff(new)
        assert diff.has_build_file_changes

    def test_has_no_build_file_changes_for_java(self) -> None:
        old = self._snap({"Foo.java": "v1"})
        new = self._snap({"Foo.java": "v2"})
        diff = old.diff(new)
        assert not diff.has_build_file_changes


# ---------------------------------------------------------------------------
# Save / load roundtrip
# ---------------------------------------------------------------------------


class TestModuleSnapshotPersistence:
    def test_roundtrip(self, tmp_path: Path) -> None:
        snap = ModuleSnapshot(root_hash="abc123", files={"Foo.java": "hash1", "pom.xml": "hash2"})
        p = tmp_path / "snap" / ".snapshot.json"
        snap.save(p)
        loaded = ModuleSnapshot.load(p)
        assert loaded is not None
        assert loaded.root_hash == snap.root_hash
        assert loaded.files == snap.files

    def test_atomic_write_creates_parent(self, tmp_path: Path) -> None:
        snap = ModuleSnapshot(root_hash="x", files={})
        p = tmp_path / "a" / "b" / "c" / ".snapshot.json"
        snap.save(p)
        assert p.exists()

    def test_parent_created_with_restricted_permissions(self, tmp_path: Path) -> None:
        snap = ModuleSnapshot(root_hash="x", files={})
        snap_dir = tmp_path / "private"
        p = snap_dir / ".snapshot.json"
        snap.save(p)
        mode = oct(os.stat(snap_dir).st_mode)[-3:]
        assert mode == "700"

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        result = ModuleSnapshot.load(tmp_path / "nonexistent.json")
        assert result is None

    def test_load_corrupted_json_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{not valid json")
        assert ModuleSnapshot.load(p) is None

    def test_load_rejects_path_traversal(self, tmp_path: Path) -> None:
        p = tmp_path / "snap.json"
        data = json.dumps({"root": "x", "files": {"../secret": "hash"}})
        p.write_text(data)
        assert ModuleSnapshot.load(p) is None

    def test_load_rejects_absolute_path_key(self, tmp_path: Path) -> None:
        p = tmp_path / "snap.json"
        data = json.dumps({"root": "x", "files": {"/etc/passwd": "hash"}})
        p.write_text(data)
        assert ModuleSnapshot.load(p) is None

    def test_load_rejects_null_byte_in_key(self, tmp_path: Path) -> None:
        p = tmp_path / "snap.json"
        data = json.dumps({"root": "x", "files": {"foo\x00bar": "hash"}})
        p.write_text(data)
        assert ModuleSnapshot.load(p) is None

    def test_load_rejects_wrong_types(self, tmp_path: Path) -> None:
        p = tmp_path / "snap.json"
        p.write_text(json.dumps({"root": 123, "files": {}}))
        assert ModuleSnapshot.load(p) is None

    def test_load_rejects_oversized_snapshot(self, tmp_path: Path) -> None:
        from java_functional_lsp.merkle import _MAX_SNAPSHOT_FILES

        p = tmp_path / "snap.json"
        big = {f"File{i}.java": "h" for i in range(_MAX_SNAPSHOT_FILES + 1)}
        p.write_text(json.dumps({"root": "x", "files": big}))
        assert ModuleSnapshot.load(p) is None


# ---------------------------------------------------------------------------
# Integration: build + diff + save + load
# ---------------------------------------------------------------------------


class TestModuleSnapshotIntegration:
    def test_build_diff_save_load(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "Foo.java").write_text("class Foo {}")
        (src / "pom.xml").write_text("<project/>")

        snap_path = tmp_path / ".snapshot.json"
        snap1 = ModuleSnapshot.build(tmp_path)
        assert snap1 is not None
        snap1.save(snap_path)

        # Modify one file, add another
        (src / "Foo.java").write_text("class Foo { int x; }")
        (src / "Bar.java").write_text("class Bar {}")

        snap2 = ModuleSnapshot.build(tmp_path)
        assert snap2 is not None

        loaded1 = ModuleSnapshot.load(snap_path)
        assert loaded1 is not None

        diff = loaded1.diff(snap2)
        assert "src/Foo.java" in diff.modified
        assert "src/Bar.java" in diff.added
        assert diff.removed == frozenset()
        assert not diff.is_empty
