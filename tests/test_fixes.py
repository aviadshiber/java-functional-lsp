"""Tests for quick fix generators."""

from __future__ import annotations

from lsprotocol import types as lsp

from java_functional_lsp.fixes import (
    ensure_import,
    fix_frozen_mutation,
    fix_imperative_option_unwrap,
    fix_mutable_dto,
    fix_null_check_to_monadic,
    fix_null_return,
    fix_try_catch_to_monadic,
    get_fix_registry_keys,
)


def _range(start_line: int, start_char: int, end_line: int, end_char: int) -> lsp.Range:
    """Convenience constructor for lsp.Range."""
    return lsp.Range(
        start=lsp.Position(line=start_line, character=start_char),
        end=lsp.Position(line=end_line, character=end_char),
    )


def _offset_at(lines: list[str], line: int, char: int) -> int:
    """Absolute character offset of (line, char) in a `splitlines(keepends=True)` list."""
    return sum(len(lines[i]) for i in range(min(line, len(lines)))) + char


def _apply_edits(source: str, edits: list[lsp.TextEdit]) -> str:
    """Apply a list of TextEdits to ``source`` and return the resulting string.

    Mirrors the LSP-client semantics for non-overlapping edits: applied in reverse-document
    order so earlier-line offsets aren't invalidated by later replacements. Lets tests assert
    on the final source text rather than just inspecting individual TextEdit shapes, which
    catches off-by-one range bugs that edit-level assertions miss.
    """
    lines = source.splitlines(keepends=True)
    # Sort edits by start position descending so applying one doesn't shift the others.
    sorted_edits = sorted(
        edits,
        key=lambda e: (e.range.start.line, e.range.start.character),
        reverse=True,
    )
    for edit in sorted_edits:
        start_off = _offset_at(lines, edit.range.start.line, edit.range.start.character)
        end_off = _offset_at(lines, edit.range.end.line, edit.range.end.character)
        joined = "".join(lines)
        new_source = joined[:start_off] + edit.new_text + joined[end_off:]
        # Re-split so subsequent edits' offset() computations operate on the current line-array.
        lines = new_source.splitlines(keepends=True)
    return "".join(lines)


class TestFixRegistryConsistency:
    def test_fix_registry_keys_match_server_titles(self) -> None:
        """Every rule with a fix generator must have a title registered in server._FIX_TITLES."""
        from java_functional_lsp.server import _FIX_TITLES

        registry_keys = get_fix_registry_keys()
        title_keys = set(_FIX_TITLES.keys())
        assert registry_keys == title_keys, f"Mismatch: registry has {registry_keys}, titles has {title_keys}"

    def test_fix_titles_only_reference_known_rules(self) -> None:
        """Issue #74 review: every key in `_FIX_TITLES` must be a rule code some analyzer
        actually emits. Catches typos like `fronzen-mutation` that would otherwise silently
        never appear in client code-action menus."""
        from java_functional_lsp.analyzers import KNOWN_RULES
        from java_functional_lsp.server import _FIX_TITLES

        unknown = set(_FIX_TITLES) - KNOWN_RULES
        assert not unknown, f"_FIX_TITLES references unknown rules: {sorted(unknown)}"

    def test_impure_method_is_in_known_rules(self) -> None:
        """KNOWN_RULES should expose the wire-visible `impure-method` code, not the internal
        `_DATA` keys (`impure-method-io`, `impure-method-throw`)."""
        from java_functional_lsp.analyzers import KNOWN_RULES

        assert "impure-method" in KNOWN_RULES
        assert "impure-method-io" not in KNOWN_RULES
        assert "impure-method-throw" not in KNOWN_RULES

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

    def test_chained_two_level_identity(self) -> None:
        """Two-level chain -> Option.of().orElse().getOrElse()."""
        source = (
            "class T {\n"
            "    int f(String key) {\n"
            "        Integer val = map.get(key);\n"
            "        if (val != null) {\n"
            "            return val;\n"
            "        } else {\n"
            "            val = fallback.get(key);\n"
            "            if (val != null) {\n"
            "                return val;\n"
            "            }\n"
            "        }\n"
            "        return defaultVal;\n"
            "    }\n"
            "}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(3, 8, 10, 9), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = [e for e in edits if "Option.of(" in e.new_text]
        assert len(rewrite) == 1
        text = rewrite[0].new_text
        assert "Option.of(map.get(key))" in text
        assert ".orElse(() -> Option.of(fallback.get(key)))" in text
        assert ".getOrElse(defaultVal)" in text

    def test_chained_three_level_identity(self) -> None:
        """Three-level chain -> two .orElse() calls."""
        source = (
            "class T {\n"
            "    int f(String key) {\n"
            "        Integer val = m1.get(key);\n"
            "        if (val != null) {\n"
            "            return val;\n"
            "        } else {\n"
            "            val = m2.get(key);\n"
            "            if (val != null) {\n"
            "                return val;\n"
            "            } else {\n"
            "                val = m3.get(key);\n"
            "                if (val != null) {\n"
            "                    return val;\n"
            "                }\n"
            "            }\n"
            "        }\n"
            "        return defaultVal;\n"
            "    }\n"
            "}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(3, 8, 15, 9), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = [e for e in edits if "Option.of(" in e.new_text]
        assert len(rewrite) == 1
        text = rewrite[0].new_text
        assert text.count(".orElse(") == 2

    def test_chained_lazy_default(self) -> None:
        """Method call default -> .getOrElse(() -> compute())."""
        source = (
            "class T {\n"
            "    int f(String key) {\n"
            "        Integer val = map.get(key);\n"
            "        if (val != null) {\n"
            "            return val;\n"
            "        } else {\n"
            "            val = fallback.get(key);\n"
            "            if (val != null) {\n"
            "                return val;\n"
            "            }\n"
            "        }\n"
            "        return computeDefault();\n"
            "    }\n"
            "}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(3, 8, 10, 9), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = [e for e in edits if "Option.of(" in e.new_text]
        assert len(rewrite) == 1
        assert ".getOrElse(() -> computeDefault())" in rewrite[0].new_text

    def test_chained_null_default_returns_none(self) -> None:
        """Null default -> no code action (return type change unsafe)."""
        source = (
            "class T {\n"
            "    Integer f(String key) {\n"
            "        Integer val = map.get(key);\n"
            "        if (val != null) {\n"
            "            return val;\n"
            "        } else {\n"
            "            val = fallback.get(key);\n"
            "            if (val != null) {\n"
            "                return val;\n"
            "            }\n"
            "        }\n"
            "        return null;\n"
            "    }\n"
            "}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(3, 8, 10, 9), {})
        assert result is None

    def test_chained_non_identity_returns_none(self) -> None:
        """Non-identity return in chain -> no code action."""
        source = (
            "class T {\n"
            "    String f(String key) {\n"
            "        String val = map.get(key);\n"
            "        if (val != null) {\n"
            "            return val.trim();\n"
            "        } else {\n"
            "            val = fallback.get(key);\n"
            "            if (val != null) {\n"
            "                return val;\n"
            "            }\n"
            "        }\n"
            "        return defaultVal;\n"
            "    }\n"
            "}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(3, 8, 10, 9), {})
        assert result is None

    def test_chained_extra_statements_returns_none(self) -> None:
        """Side effects in else -> no code action."""
        source = (
            "class T {\n"
            "    int f(String key) {\n"
            "        Integer val = map.get(key);\n"
            "        if (val != null) {\n"
            "            return val;\n"
            "        } else {\n"
            "            logger.debug(key);\n"
            "            val = fallback.get(key);\n"
            "            if (val != null) {\n"
            "                return val;\n"
            "            }\n"
            "        }\n"
            "        return defaultVal;\n"
            "    }\n"
            "}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(3, 8, 11, 9), {})
        assert result is None

    def test_chained_no_default_return_returns_none(self) -> None:
        """No return after if/else -> no code action."""
        source = (
            "class T {\n"
            "    void f(String key) {\n"
            "        Integer val = map.get(key);\n"
            "        if (val != null) {\n"
            "            return val;\n"
            "        } else {\n"
            "            val = fallback.get(key);\n"
            "            if (val != null) {\n"
            "                return val;\n"
            "            }\n"
            "        }\n"
            "        doSomething();\n"
            "    }\n"
            "}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(3, 8, 10, 9), {})
        assert result is None

    def test_chained_declaration_not_adjacent_returns_none(self) -> None:
        """Declaration separated from if by other statements -> no code action."""
        source = (
            "class T {\n"
            "    int f(String key) {\n"
            "        Integer val = map.get(key);\n"
            "        log(val);\n"
            "        if (val != null) {\n"
            "            return val;\n"
            "        } else {\n"
            "            val = fallback.get(key);\n"
            "            if (val != null) {\n"
            "                return val;\n"
            "            }\n"
            "        }\n"
            "        return defaultVal;\n"
            "    }\n"
            "}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(4, 8, 11, 9), {})
        assert result is None

    def test_chained_complex_fallback_parenthesized(self) -> None:
        """Ternary in fallback expression -> parenthesized in output."""
        source = (
            "class T {\n"
            "    int f(String key) {\n"
            "        Integer val = map.get(key);\n"
            "        if (val != null) {\n"
            "            return val;\n"
            "        } else {\n"
            "            val = useBackup ? backup.get(key) : other.get(key);\n"
            "            if (val != null) {\n"
            "                return val;\n"
            "            }\n"
            "        }\n"
            "        return defaultVal;\n"
            "    }\n"
            "}"
        )
        result = fix_null_check_to_monadic("file:///test.java", source, _range(3, 8, 10, 9), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = [e for e in edits if "Option.of(" in e.new_text]
        assert len(rewrite) == 1
        # Ternary should be parenthesized
        assert "(useBackup ? backup.get(key) : other.get(key))" in rewrite[0].new_text


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


class TestFixTryCatchToMonadic:
    # The canonical test source places the `try` keyword at line 2, cols 8-11
    # (under a top-level class declaration with a single-method body).
    _TRY_RANGE = _range(2, 8, 2, 11)

    def test_pattern1_eager_literal(self) -> None:
        """String literal default → eager .getOrElse("default") with no lambda."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (IOException e) {\n"
            '            return "default";\n'
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = next(e for e in edits if "Try.of" in e.new_text)
        assert "Try.of(() -> risky())" in rewrite.new_text
        assert '.getOrElse("default")' in rewrite.new_text
        # Eager — no lambda wrapping on the default
        assert '() -> "default"' not in rewrite.new_text

    def test_pattern1_eager_identifier(self) -> None:
        """Bare identifier → eager .getOrElse(identifier)."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (IOException e) {\n"
            "            return fallback;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = next(e for e in edits if "Try.of" in e.new_text)
        assert ".getOrElse(fallback)" in rewrite.new_text
        assert "() -> fallback" not in rewrite.new_text

    def test_pattern1_lazy_method_call(self) -> None:
        """Method-call default → lazy .getOrElse(() -> computeDefault())."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (IOException e) {\n"
            "            return computeDefault();\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = next(e for e in edits if "Try.of" in e.new_text)
        assert ".getOrElse(() -> computeDefault())" in rewrite.new_text

    def test_pattern2_logging_onfailure(self) -> None:
        """Logging + return → Try.of().onFailure(e -> log).getOrElse(default)."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (IOException e) {\n"
            '            logger.warn("failed", e);\n'
            '            return "default";\n'
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = next(e for e in edits if "Try.of" in e.new_text)
        assert '.onFailure(e -> logger.warn("failed", e))' in rewrite.new_text
        assert '.getOrElse("default")' in rewrite.new_text

    def test_pattern3_recover_with_exception_var(self) -> None:
        """Recovery uses exception var → .recover(Type.class, e -> expr).get()."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (IOException e) {\n"
            "            return fallback(e);\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = next(e for e in edits if "Try.of" in e.new_text)
        assert ".recover(IOException.class, e -> fallback(e))" in rewrite.new_text
        assert ".get()" in rewrite.new_text
        # Must not use getOrElse in Pattern 3
        assert ".getOrElse(" not in rewrite.new_text

    def test_pattern3_generic_exception_type(self) -> None:
        """Generic Exception type works the same as specific types."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (Exception ex) {\n"
            "            return handle(ex);\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = next(e for e in edits if "Try.of" in e.new_text)
        assert ".recover(Exception.class, ex -> handle(ex))" in rewrite.new_text

    def test_imports_vavr_try(self) -> None:
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (Exception e) {\n"
            "            return fallback;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        assert any("import io.vavr.control.Try;" in e.new_text for e in edits)

    def test_no_import_when_disabled(self) -> None:
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (Exception e) {\n"
            "            return fallback;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {"autoImportVavr": False})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        assert not any("import" in e.new_text for e in edits)

    def test_no_duplicate_import(self) -> None:
        source = (
            "import io.vavr.control.Try;\n"
            "\n"
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (Exception e) {\n"
            "            return fallback;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        # try keyword is on line 4 now (import + blank + class + method)
        result = fix_try_catch_to_monadic("file:///test.java", source, _range(4, 8, 4, 11), {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        assert not any("import" in e.new_text for e in edits)

    def test_multi_statement_try_returns_none(self) -> None:
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            String x = risky();\n"
            "            return x.trim();\n"
            "        } catch (Exception e) { return fallback; }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is None

    def test_finally_returns_none(self) -> None:
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (Exception e) {\n"
            "            return fallback;\n"
            "        } finally {\n"
            "            cleanup();\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is None

    def test_multi_catch_returns_none(self) -> None:
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (IOException e) {\n"
            "            return a;\n"
            "        } catch (SQLException e) {\n"
            "            return b;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is None

    def test_unknown_prior_statement_returns_none(self) -> None:
        """Prior statement that isn't a recognized side-effect call → no auto-fix."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (Exception e) {\n"
            "            cache.put(key, fallback);\n"
            "            return fallback;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is None

    def test_replaces_full_try_statement(self) -> None:
        """The fix should replace the entire try_statement range with `return <chain>;`."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (Exception e) {\n"
            "            return fallback;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = next(e for e in edits if "Try.of" in e.new_text)
        # Replacement text should start with "return " and end with ";"
        assert rewrite.new_text.startswith("return ")
        assert rewrite.new_text.endswith(";")
        # The range should start at the `try` keyword (line 2, col 8)
        assert rewrite.range.start.line == 2
        assert rewrite.range.start.character == 8
        # The range should end at the closing brace of the try/catch block on line 6, col 9.
        # Source has try { ... } on line 2 through line 6 (closing brace column 9).
        assert rewrite.range.end.line == 6
        assert rewrite.range.end.character == 9

    def test_exact_equality_on_new_text(self) -> None:
        """Pin the full rewrite output for one canonical Pattern 1 case."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (IOException e) {\n"
            '            return "default";\n'
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = next(e for e in edits if "Try.of" in e.new_text)
        assert rewrite.new_text == 'return Try.of(() -> risky()).getOrElse("default");'

    def test_pattern2_with_non_e_variable_name(self) -> None:
        """Pattern 2 must use the actual catch variable name, not hardcoded `e`."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (IOException ex) {\n"
            '            logger.warn("failed", ex);\n'
            '            return "default";\n'
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = next(e for e in edits if "Try.of" in e.new_text)
        assert '.onFailure(ex -> logger.warn("failed", ex))' in rewrite.new_text
        assert '.getOrElse("default")' in rewrite.new_text

    def test_bare_return_in_catch_returns_none(self) -> None:
        """A catch body ending with bare `return;` (no expression) is not rewritable."""
        source = (
            "class T {\n"
            "    void f() {\n"
            "        try {\n"
            "            return;\n"
            "        } catch (Exception e) {\n"
            "            return;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is None

    def test_pattern2_multiple_prior_statements_returns_none(self) -> None:
        """Two logger calls before the return exceed Pattern 2 scope → no fix."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (Exception e) {\n"
            '            logger.warn("first", e);\n'
            '            logger.warn("second", e);\n'
            '            return "default";\n'
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is None

    def test_pattern2_plus_pattern3_hybrid_returns_none(self) -> None:
        """Logging + exception-dependent recovery is explicitly rejected (untested hybrid)."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (IOException e) {\n"
            '            logger.warn("failed", e);\n'
            "            return fallback(e);\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is None

    def test_ancestor_walk_finds_try_from_inside(self) -> None:
        """A diagnostic range pointing inside the try body still resolves to the try_statement."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try {\n"
            "            return risky();\n"
            "        } catch (Exception e) {\n"
            "            return fallback;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        # Point the range at `risky` inside the return statement (line 3, cols 19-24).
        inner_range = _range(3, 19, 3, 24)
        result = fix_try_catch_to_monadic("file:///test.java", source, inner_range, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        rewrite = next(e for e in edits if "Try.of" in e.new_text)
        assert "Try.of(() -> risky()).getOrElse(fallback)" in rewrite.new_text

    def test_import_edit_position_is_top_of_file(self) -> None:
        """The Try import should be inserted at line 0 when there's no package or existing import."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try { return risky(); }\n"
            "        catch (Exception e) { return fallback; }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///test.java"]
        import_edit = next(e for e in edits if "import io.vavr.control.Try;" in e.new_text)
        assert import_edit.range.start.line == 0
        assert import_edit.range.start.character == 0

    def test_union_catch_returns_none(self) -> None:
        """Multi-catch (A | B e) is explicitly rejected by the fix."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try { return risky(); }\n"
            "        catch (IOException | SQLException e) { return fallback; }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is None

    def test_try_with_resources_returns_none(self) -> None:
        """try-with-resources must not be rewritten — closing the resource would be lost."""
        source = (
            "class T {\n"
            "    String f() {\n"
            "        try (java.io.InputStream is = open()) {\n"
            "            return parse(is);\n"
            "        } catch (java.io.IOException e) {\n"
            '            return "empty";\n'
            "        }\n"
            "    }\n"
            "}\n"
        )
        result = fix_try_catch_to_monadic("file:///test.java", source, self._TRY_RANGE, {})
        assert result is None

    def test_strip_trailing_semicolon_helper(self) -> None:
        """The trailing-semicolon helper strips exactly one semicolon, not greedily."""
        from java_functional_lsp.fixes import _strip_trailing_semicolon

        assert _strip_trailing_semicolon('logger.warn("x", e);') == 'logger.warn("x", e)'
        # removesuffix strips at most one, not all
        assert _strip_trailing_semicolon('logger.warn("x", e);;') == 'logger.warn("x", e);'
        # Trailing whitespace is handled
        assert _strip_trailing_semicolon('logger.warn("x", e);  ') == 'logger.warn("x", e)'
        # No trailing semicolon → no change
        assert _strip_trailing_semicolon('logger.warn("x", e)') == 'logger.warn("x", e)'


class TestFixImperativeOptionUnwrap:
    """Issue #74 #3: quick-fix for if(opt.isDefined()) return opt.get(); else ..."""

    def test_rewrites_if_return_get_else_return(self) -> None:
        source = (
            "import io.vavr.control.Option;\n"
            "\n"
            "class T {\n"
            "    String f(Option<String> opt) {\n"
            "        if (opt.isDefined()) {\n"
            "            return opt.get();\n"
            "        } else {\n"
            '            return "default";\n'
            "        }\n"
            "    }\n"
            "}\n"
        )
        # The diagnostic spans the whole if-statement (mutation_checker emits start..end).
        diag_range = _range(4, 8, 8, 9)
        result = fix_imperative_option_unwrap("file:///T.java", source, diag_range, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///T.java"]
        assert len(edits) == 1
        # The rewrite uses the real var name `opt`.
        assert "opt.map(it -> it)" in edits[0].new_text
        assert '.getOrElse("default")' in edits[0].new_text

    def test_lazy_else_uses_supplier(self) -> None:
        source = (
            "class T {\n"
            "    String f(Option<String> opt) {\n"
            "        if (opt.isDefined()) {\n"
            "            return opt.get();\n"
            "        } else {\n"
            "            return computeDefault();\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        diag_range = _range(2, 8, 6, 9)
        result = fix_imperative_option_unwrap("file:///T.java", source, diag_range, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///T.java"]
        assert ".getOrElse(() -> computeDefault())" in edits[0].new_text

    def test_complex_consequence_bails(self) -> None:
        """If the if-body has multiple statements, no fix should be produced."""
        source = (
            "class T {\n"
            "    String f(Option<String> opt) {\n"
            "        if (opt.isDefined()) {\n"
            "            log(opt.get());\n"
            "            return opt.get();\n"
            "        } else {\n"
            '            return "default";\n'
            "        }\n"
            "    }\n"
            "}\n"
        )
        diag_range = _range(2, 8, 7, 9)
        result = fix_imperative_option_unwrap("file:///T.java", source, diag_range, {})
        assert result is None

    def test_no_else_branch_bails(self) -> None:
        """Issue #74 review (blocker): without an else, rewriting to `opt.map(it -> it)` returns
        Option<T> instead of the method's declared T — must NOT produce an edit.
        """
        source = (
            "class T {\n"
            "    String f(Option<String> opt) {\n"
            "        if (opt.isDefined()) {\n"
            "            return opt.get();\n"
            "        }\n"
            '        return "default";\n'
            "    }\n"
            "}\n"
        )
        diag_range = _range(2, 8, 4, 9)
        result = fix_imperative_option_unwrap("file:///T.java", source, diag_range, {})
        assert result is None, "fix must bail when there's no else branch (type-mismatch risk)"

    def test_post_edit_source_is_valid_java(self) -> None:
        """Issue #74 review (test quality): assert on the post-edit source, not just edits.

        Catches range off-by-one bugs: an edit replacing the wrong span would still pass an
        "edits exist" check but produce broken output.
        """
        source = (
            "class T {\n"
            "    String f(Option<String> opt) {\n"
            "        if (opt.isDefined()) {\n"
            "            return opt.get();\n"
            "        } else {\n"
            '            return "default";\n'
            "        }\n"
            "    }\n"
            "}\n"
        )
        diag_range = _range(2, 8, 6, 9)
        result = fix_imperative_option_unwrap("file:///T.java", source, diag_range, {})
        assert result is not None
        assert result.changes is not None
        edited = _apply_edits(source, result.changes["file:///T.java"])
        # The full if/else is gone, replaced by a single return chain.
        assert "if (opt.isDefined())" not in edited
        assert 'return opt.map(it -> it).getOrElse("default");' in edited
        # And the surrounding method braces are still intact.
        assert "class T {" in edited
        assert "String f(Option<String> opt) {" in edited


class TestFixMutableDto:
    """Issue #74 #3: quick-fix that swaps @Data for @Value (only — @Setter is rejected)."""

    def test_skips_setter_annotation(self) -> None:
        """Issue #74 review: blanket @Setter -> @Value silently strips setters and makes class
        final, breaking callers. The fix must only rewrite @Data."""
        source = "@Setter\npublic class UserDto {\n    private String name;\n}\n"
        diag_range = _range(0, 0, 0, 7)
        result = fix_mutable_dto("file:///UserDto.java", source, diag_range, {})
        assert result is None

    def test_skips_when_configuration_properties_sibling(self) -> None:
        """Issue #74 review: @ConfigurationProperties classes need @ConstructorBinding, not @Value.
        The diagnostic message already says so — the fix must respect it."""
        source = (
            '@Data\n@ConfigurationProperties(prefix = "x")\npublic class AppConfig {\n    private String name;\n}\n'
        )
        diag_range = _range(0, 0, 0, 5)
        result = fix_mutable_dto("file:///AppConfig.java", source, diag_range, {})
        assert result is None

    def test_rewrites_data_to_value(self) -> None:
        source = "import lombok.Data;\n\n@Data\npublic class UserDto {\n    private String name;\n}\n"
        diag_range = _range(2, 0, 2, 5)
        result = fix_mutable_dto("file:///UserDto.java", source, diag_range, {})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///UserDto.java"]
        # One edit replaces "Data" with "Value"; another adds the lombok.Value import.
        replacement_edits = [e for e in edits if e.new_text == "Value"]
        assert len(replacement_edits) == 1
        import_edits = [e for e in edits if "import lombok.Value" in e.new_text]
        assert len(import_edits) == 1

    def test_skips_when_conflicting_annotation_present(self) -> None:
        """@Value doesn't combine with @NoArgsConstructor — bail."""
        source = "@Data\n@NoArgsConstructor\npublic class UserDto {\n    private String name;\n}\n"
        diag_range = _range(0, 0, 0, 5)
        result = fix_mutable_dto("file:///UserDto.java", source, diag_range, {})
        assert result is None

    def test_no_import_when_lombok_disabled(self) -> None:
        """`autoImportLombok=False` should suppress the lombok.Value import edit."""
        source = "@Data\npublic class UserDto { private String name; }\n"
        diag_range = _range(0, 0, 0, 5)
        result = fix_mutable_dto("file:///UserDto.java", source, diag_range, {"autoImportLombok": False})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///UserDto.java"]
        # Only the annotation rewrite, no import.
        assert all("import" not in e.new_text for e in edits)

    def test_autovavr_disabled_does_not_affect_lombok_import(self) -> None:
        """Issue #74 review: `autoImportVavr=False` must NOT block the Lombok import,
        since the user might want Vavr off but still want Lombok auto-imported.
        """
        source = "@Data\npublic class UserDto { private String name; }\n"
        diag_range = _range(0, 0, 0, 5)
        result = fix_mutable_dto("file:///UserDto.java", source, diag_range, {"autoImportVavr": False})
        assert result is not None
        assert result.changes is not None
        edits = result.changes["file:///UserDto.java"]
        # The Lombok import should still appear.
        assert any("import lombok.Value" in e.new_text for e in edits)

    def test_post_edit_source_replaces_data_token_only(self) -> None:
        """Issue #74 review (test quality): assert the @Data token is replaced exactly,
        without disturbing surrounding code."""
        source = "import lombok.Data;\n\n@Data\npublic class UserDto {\n    private String name;\n}\n"
        diag_range = _range(2, 0, 2, 5)
        result = fix_mutable_dto("file:///UserDto.java", source, diag_range, {})
        assert result is not None
        assert result.changes is not None
        edited = _apply_edits(source, result.changes["file:///UserDto.java"])
        assert "@Value" in edited
        assert "@Data" not in edited
        # The class body and import below were not touched.
        assert "public class UserDto {" in edited
        assert "    private String name;" in edited
        # And the lombok.Value import was added.
        assert "import lombok.Value;" in edited


class TestFieldInjectionNoQuickFix:
    """field-injection has no registered quick-fix (synthesising a constructor is unsafe
    without class-wide analysis). The diagnostic + suggested_snippet remain available for AI agents.
    """

    def test_field_injection_not_in_registry(self) -> None:
        assert "field-injection" not in get_fix_registry_keys()
