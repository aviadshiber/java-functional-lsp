"""Tests for quick fix generators."""

from __future__ import annotations

from lsprotocol import types as lsp

from java_functional_lsp.fixes import (
    ensure_import,
    fix_frozen_mutation,
    fix_null_check_to_monadic,
    fix_null_return,
    get_fix_registry_keys,
)


def _range(start_line: int, start_char: int, end_line: int, end_char: int) -> lsp.Range:
    """Convenience constructor for lsp.Range."""
    return lsp.Range(
        start=lsp.Position(line=start_line, character=start_char),
        end=lsp.Position(line=end_line, character=end_char),
    )


class TestFixRegistryConsistency:
    def test_fix_registry_keys_match_server_titles(self) -> None:
        """Every rule with a fix generator must have a title registered in server._FIX_TITLES."""
        from java_functional_lsp.server import _FIX_TITLES

        registry_keys = get_fix_registry_keys()
        title_keys = set(_FIX_TITLES.keys())
        assert registry_keys == title_keys, f"Mismatch: registry has {registry_keys}, titles has {title_keys}"

    def test_ensure_import_rejects_invalid_path(self) -> None:
        """ensure_import should return None for paths that contain invalid characters."""
        lines: list[str] = ["class T {}"]
        # Paths with spaces, semicolons, or other invalid chars should be rejected
        assert ensure_import(lines, "io.vavr; DROP TABLE") is None
        assert ensure_import(lines, "io.vavr collection.List") is None
        assert ensure_import(lines, "") is None

    def test_ensure_import_accepts_valid_path(self) -> None:
        """ensure_import should accept valid Java FQN paths."""
        lines: list[str] = ["class T {}"]
        edit = ensure_import(lines, "io.vavr.collection.List")
        assert edit is not None

    def test_import_insert_position_no_blank_line_after_package(self) -> None:
        """When there's no blank line after package, position should not exceed file length."""
        from java_functional_lsp.fixes import _find_import_insert_position

        lines = ["package com.example;", "class T {}"]
        pos = _find_import_insert_position(lines)
        assert pos <= len(lines)


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
        assert result.changes is not None
        assert "file:///test.java" in result.changes
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
        assert result is not None
        assert result.changes is not None
        edits = result.changes.get("file:///test.java", [])
        import_edits = [e for e in edits if "import" in e.new_text]
        assert len(import_edits) == 0

    def test_no_import_when_disabled(self) -> None:
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
        result = fix_frozen_mutation("file:///test.java", source, diag_range, {"autoImportVavr": False})
        assert result is not None
        assert result.changes is not None
        edits = result.changes.get("file:///test.java", [])
        import_edits = [e for e in edits if "import" in e.new_text]
        assert len(import_edits) == 0

    def test_imports_vavr_set_for_set_collection(self) -> None:
        source = (
            "import java.util.Set;\n"
            "\n"
            "class T {\n"
            "    void f() {\n"
            '        Set<String> s = Set.of("a");\n'
            '        s.add("b");\n'
            "    }\n"
            "}\n"
        )
        diag_range = lsp.Range(
            start=lsp.Position(line=5, character=8),
            end=lsp.Position(line=5, character=18),
        )
        result = fix_frozen_mutation("file:///test.java", source, diag_range, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        import_edits = [e for e in edits if "import" in e.new_text]
        assert any("io.vavr.collection.Set" in e.new_text for e in import_edits)


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

    def test_no_import_when_disabled(self) -> None:
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
        result = fix_null_check_to_monadic("file:///test.java", source, diag_range, {"autoImportVavr": False})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        import_edits = [e for e in edits if "import" in e.new_text]
        assert len(import_edits) == 0

    def test_uses_non_conflicting_lambda_param(self) -> None:
        """Lambda param should use 'it' to avoid shadowing the outer variable."""
        source = (
            "class T {\n    String f(User user) {\n        if (user != null) {\n"
            "            return user.getName();\n        }\n        return null;\n    }\n}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(2, 8, 4, 9), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = [e for e in edits if "Option" in e.new_text and "map" in e.new_text]
        assert len(rewrite) == 1
        assert ".map(it -> it.getName())" in rewrite[0].new_text
        assert ".map(user" not in rewrite[0].new_text

    def test_identity_return_returns_option(self) -> None:
        """Identity return (return x) should generate Option.of(x) with no .map() and no .getOrNull()."""
        source = (
            "class T {\n    String f(User user) {\n        if (user != null) {\n"
            "            return user;\n        }\n        return null;\n    }\n}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(2, 8, 4, 9), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = [e for e in edits if "Option.of(" in e.new_text]
        assert len(rewrite) == 1
        assert "Option.of(user)" in rewrite[0].new_text
        assert ".map(" not in rewrite[0].new_text
        assert ".getOrNull()" not in rewrite[0].new_text

    def test_null_fallback_returns_option(self) -> None:
        """When fallback is return null, generate Option.of() without .getOrNull()."""
        source = (
            "class T {\n    String f(User user) {\n        if (user != null) {\n"
            "            return user.getName();\n        }\n        return null;\n    }\n}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(2, 8, 4, 9), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = [e for e in edits if "Option" in e.new_text and "map" in e.new_text]
        assert len(rewrite) == 1
        assert ".getOrNull()" not in rewrite[0].new_text
        assert rewrite[0].new_text.rstrip().endswith(";")

    def test_simple_else_uses_get_or_else(self) -> None:
        """Simple else with literal value should use .getOrElse()."""
        source = (
            "class T {\n    String f(User user) {\n        if (user != null) {\n"
            '            return user.getName();\n        } else {\n            return "unknown";\n'
            "        }\n    }\n}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(2, 8, 6, 9), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = [e for e in edits if "Option.of(" in e.new_text]
        assert len(rewrite) == 1
        assert '.getOrElse("unknown")' in rewrite[0].new_text

    def test_lazy_else_uses_supplier(self) -> None:
        """Else with method call should use lazy .getOrElse(() -> ...)."""
        source = (
            "class T {\n    String f(User user) {\n        if (user != null) {\n"
            "            return user.getName();\n        } else {\n"
            "            return computeDefault();\n        }\n    }\n}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(2, 8, 6, 9), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = [e for e in edits if "Option.of(" in e.new_text]
        assert len(rewrite) == 1
        assert ".getOrElse(() -> computeDefault())" in rewrite[0].new_text

    def test_complex_else_returns_none(self) -> None:
        """Complex else (multi-statement) should return None — no code action."""
        source = (
            "class T {\n    String f(String key) {\n        String val = map.get(key);\n"
            "        if (val != null) {\n            return val;\n        } else {\n"
            "            val = fallback.get(key);\n            return val;\n        }\n    }\n}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(3, 8, 8, 9), {})
        assert result is None

    def test_eager_identifier_in_else(self) -> None:
        """Else returning an identifier should use eager .getOrElse()."""
        source = (
            "class T {\n    String f(User user) {\n        if (user != null) {\n"
            "            return user.getName();\n        } else {\n"
            "            return fallbackName;\n        }\n    }\n}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(2, 8, 6, 9), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = [e for e in edits if "Option.of(" in e.new_text]
        assert len(rewrite) == 1
        assert ".getOrElse(fallbackName)" in rewrite[0].new_text
        assert "() ->" not in rewrite[0].new_text

    def test_eager_field_access_in_else(self) -> None:
        """Else returning a field access should use eager .getOrElse()."""
        source = (
            "class T {\n    String f(User user) {\n        if (user != null) {\n"
            "            return user.getName();\n        } else {\n"
            "            return Config.DEFAULT_VALUE;\n        }\n    }\n}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(2, 8, 6, 9), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = [e for e in edits if "Option.of(" in e.new_text]
        assert len(rewrite) == 1
        assert ".getOrElse(Config.DEFAULT_VALUE)" in rewrite[0].new_text
        assert "() ->" not in rewrite[0].new_text

    def test_short_var_name_word_boundary(self) -> None:
        """Short var names should not mangle substrings via regex replace."""
        source = (
            "class T {\n    String f(String s) {\n        if (s != null) {\n"
            "            return s.toString();\n        }\n        return null;\n    }\n}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(2, 8, 4, 9), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = [e for e in edits if "Option" in e.new_text and "map" in e.new_text]
        assert len(rewrite) == 1
        assert ".map(it -> it.toString())" in rewrite[0].new_text

    def test_empty_else_returns_none(self) -> None:
        """Empty else body should return None — no code action."""
        source = (
            "class T {\n    String f(String x) {\n        if (x != null) {\n"
            "            return x;\n        } else {\n        }\n    }\n}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(2, 8, 5, 9), {})
        assert result is None

    def test_else_returning_null_returns_bare_option(self) -> None:
        """Else { return null; } should produce bare Option (no .getOrElse)."""
        source = (
            "class T {\n    String f(User user) {\n        if (user != null) {\n"
            "            return user.getName();\n        } else {\n"
            "            return null;\n        }\n    }\n}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(2, 8, 6, 9), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = [e for e in edits if "Option" in e.new_text and "map" in e.new_text]
        assert len(rewrite) == 1
        assert ".getOrElse" not in rewrite[0].new_text
        assert ".getOrNull()" not in rewrite[0].new_text

    def test_identity_with_else_uses_get_or_else(self) -> None:
        """Identity return with else value should use Option.of(x).getOrElse(val) — no .map()."""
        source = (
            "class T {\n    String f(String x) {\n        if (x != null) {\n"
            "            return x;\n        } else {\n            return defaultVal;\n"
            "        }\n    }\n}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(2, 8, 6, 9), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = [e for e in edits if "Option.of(" in e.new_text]
        assert len(rewrite) == 1
        assert "Option.of(x)" in rewrite[0].new_text
        assert ".map(" not in rewrite[0].new_text
        assert ".getOrElse(defaultVal)" in rewrite[0].new_text


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
        assert result.changes is not None
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
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        assert len(edits) == 1
        assert "Option.none()" in edits[0].new_text
