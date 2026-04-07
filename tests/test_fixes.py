"""Tests for quick fix generators."""

from __future__ import annotations

from lsprotocol import types as lsp

from java_functional_lsp.fixes import ensure_import, fix_frozen_mutation, fix_null_check_to_monadic, fix_null_return


class TestEnsureImport:
    def test_adds_import_after_last_import(self) -> None:
        lines = [
            "package com.example;",
            "",
            "import java.util.List;",
            "import java.util.Map;",
            "",
            "class T {}",
        ]
        edit = ensure_import(lines, "io.vavr.collection.List")
        assert edit is not None
        assert edit.range.start.line == 4
        assert "import io.vavr.collection.List;" in edit.new_text

    def test_adds_import_after_package(self) -> None:
        lines = [
            "package com.example;",
            "",
            "class T {}",
        ]
        edit = ensure_import(lines, "io.vavr.collection.List")
        assert edit is not None
        assert edit.range.start.line == 2  # blank line after package

    def test_adds_import_at_top_if_no_package(self) -> None:
        lines = ["class T {}"]
        edit = ensure_import(lines, "io.vavr.collection.List")
        assert edit is not None
        assert edit.range.start.line == 0

    def test_skips_existing_import(self) -> None:
        lines = [
            "import io.vavr.collection.List;",
            "class T {}",
        ]
        edit = ensure_import(lines, "io.vavr.collection.List")
        assert edit is None


class TestFixFrozenMutation:
    def test_generates_workspace_edit(self) -> None:
        source = (
            "import java.util.List;\n"
            "\n"
            "class T {\n"
            "    void f() {\n"
            '        List<String> list = List.of("a", "b");\n'
            '        list.add("c");\n'
            "    }\n"
            "}\n"
        )
        diag_range = lsp.Range(
            start=lsp.Position(line=5, character=8),
            end=lsp.Position(line=5, character=22),
        )
        result = fix_frozen_mutation("file:///test.java", source, diag_range, {})
        assert result is not None
        assert isinstance(result, lsp.WorkspaceEdit)
        assert "file:///test.java" in (result.changes or {})
        edits = result.changes["file:///test.java"]
        # Should have at least an import edit and the mutation rewrite
        assert len(edits) >= 1

    def test_no_duplicate_import(self) -> None:
        source = (
            "import io.vavr.collection.List;\n"
            "\n"
            "class T {\n"
            "    void f() {\n"
            '        List<String> list = List.of("a", "b");\n'
            '        list.add("c");\n'
            "    }\n"
            "}\n"
        )
        diag_range = lsp.Range(
            start=lsp.Position(line=5, character=8),
            end=lsp.Position(line=5, character=22),
        )
        result = fix_frozen_mutation("file:///test.java", source, diag_range, {})
        if result is not None and result.changes:
            edits = result.changes.get("file:///test.java", [])
            import_edits = [e for e in edits if "import" in e.new_text]
            assert len(import_edits) == 0


class TestFixNullCheckToMonadic:
    def test_generates_option_rewrite(self) -> None:
        source = (
            "class T {\n"
            "    String f(User user) {\n"
            "        if (user != null) {\n"
            "            return user.getName();\n"
            "        }\n"
            "        return null;\n"
            "    }\n"
            "}\n"
        )
        diag_range = lsp.Range(
            start=lsp.Position(line=2, character=8),
            end=lsp.Position(line=4, character=9),
        )
        result = fix_null_check_to_monadic("file:///test.java", source, diag_range, {})
        assert result is not None
        assert isinstance(result, lsp.WorkspaceEdit)


class TestFixNullReturn:
    def test_replaces_null_with_option_none(self) -> None:
        source = "class T { String f() { return null; } }\n"
        diag_range = lsp.Range(
            start=lsp.Position(line=0, character=30),
            end=lsp.Position(line=0, character=34),
        )
        result = fix_null_return("file:///test.java", source, diag_range, {})
        assert result is not None
        assert isinstance(result, lsp.WorkspaceEdit)
        edits = result.changes["file:///test.java"]
        # Should have import + replacement
        assert len(edits) == 2
        texts = [e.new_text for e in edits]
        assert any("Option.none()" in t for t in texts)
        assert any("import io.vavr.control.Option;" in t for t in texts)

    def test_no_import_when_disabled(self) -> None:
        source = "class T { String f() { return null; } }\n"
        diag_range = lsp.Range(
            start=lsp.Position(line=0, character=30),
            end=lsp.Position(line=0, character=34),
        )
        result = fix_null_return("file:///test.java", source, diag_range, {"autoImportVavr": False})
        assert result is not None
        edits = result.changes["file:///test.java"]
        assert len(edits) == 1
        assert "Option.none()" in edits[0].new_text
