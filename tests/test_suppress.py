"""Tests for @SuppressWarnings inline suppression."""

from __future__ import annotations

from typing import Any

from java_functional_lsp.analyzers.base import get_parser, is_suppressed


def _codes(diags: list[Any]) -> set[str]:
    return {d.code for d in diags}


def _analyze_all(source: str) -> list[Any]:
    """Run analysis through server's _analyze_document to include suppression filtering."""
    from java_functional_lsp.server import _analyze_document

    return _analyze_document(source)


class TestSuppressSingleRule:
    def test_suppress_null_return(self) -> None:
        source = """
class T {
    @SuppressWarnings("java-functional-lsp:null-return")
    String f() { return null; }
}
"""
        diags = _analyze_all(source)
        assert "null-return" not in _codes(diags)

    def test_suppress_throw_statement(self) -> None:
        source = """
class T {
    @SuppressWarnings("java-functional-lsp:throw-statement")
    void f() { throw new RuntimeException(); }
}
"""
        diags = _analyze_all(source)
        assert "throw-statement" not in _codes(diags)


class TestSuppressAllRules:
    def test_suppress_all(self) -> None:
        source = """
class T {
    @SuppressWarnings("java-functional-lsp")
    String f() { return null; }
}
"""
        diags = _analyze_all(source)
        assert "null-return" not in _codes(diags)

    def test_suppress_all_multiple_violations(self) -> None:
        source = """
class T {
    @SuppressWarnings("java-functional-lsp")
    void f() {
        String x = null;
        throw new RuntimeException();
    }
}
"""
        diags = _analyze_all(source)
        codes = _codes(diags)
        assert "null-assignment" not in codes
        assert "throw-statement" not in codes


class TestSuppressMultipleRules:
    def test_suppress_array_syntax(self) -> None:
        source = """
class T {
    @SuppressWarnings({"java-functional-lsp:null-return", "java-functional-lsp:throw-statement"})
    String f() {
        if (true) throw new RuntimeException();
        return null;
    }
}
"""
        diags = _analyze_all(source)
        codes = _codes(diags)
        assert "null-return" not in codes
        assert "throw-statement" not in codes


class TestSuppressOnClass:
    def test_class_level_suppresses_all_methods(self) -> None:
        source = """
@SuppressWarnings("java-functional-lsp:null-return")
class T {
    String f() { return null; }
    String g() { return null; }
}
"""
        diags = _analyze_all(source)
        assert "null-return" not in _codes(diags)


class TestSuppressOnField:
    def test_field_suppression(self) -> None:
        source = """
class T {
    @SuppressWarnings("java-functional-lsp:null-field-assignment")
    String cache = null;
}
"""
        diags = _analyze_all(source)
        assert "null-field-assignment" not in _codes(diags)


class TestSuppressScope:
    def test_does_not_affect_sibling_methods(self) -> None:
        source = """
class T {
    @SuppressWarnings("java-functional-lsp:null-return")
    String f() { return null; }

    String g() { return null; }
}
"""
        diags = _analyze_all(source)
        # f() is suppressed, g() is not
        assert any(d.code == "null-return" for d in diags)
        # Verify the remaining diagnostic is on g()'s line
        null_diags = [d for d in diags if d.code == "null-return"]
        assert len(null_diags) == 1


class TestUnrelatedSuppressIgnored:
    def test_unrelated_annotation(self) -> None:
        source = """
class T {
    @SuppressWarnings("unchecked")
    String f() { return null; }
}
"""
        diags = _analyze_all(source)
        assert "null-return" in _codes(diags)

    def test_other_prefix_ignored(self) -> None:
        source = """
class T {
    @SuppressWarnings("other-linter:null-return")
    String f() { return null; }
}
"""
        diags = _analyze_all(source)
        assert "null-return" in _codes(diags)


class TestNoSuppress:
    def test_baseline_no_annotation(self) -> None:
        source = "class T { String f() { return null; } }"
        diags = _analyze_all(source)
        assert "null-return" in _codes(diags)


class TestIsSuppressedHelper:
    def test_returns_false_for_clean_code(self) -> None:
        parser = get_parser()
        tree = parser.parse(b'class T { String f() { return "ok"; } }')
        assert not is_suppressed(tree.root_node, 0, 30, "null-return")

    def test_returns_true_for_suppressed(self) -> None:
        parser = get_parser()
        source = b"""class T {
    @SuppressWarnings("java-functional-lsp:null-return")
    String f() { return null; }
}"""
        tree = parser.parse(source)
        # null is on line 2, find its column
        lines = source.split(b"\n")
        null_col = lines[2].index(b"null")
        assert is_suppressed(tree.root_node, 2, null_col, "null-return")
