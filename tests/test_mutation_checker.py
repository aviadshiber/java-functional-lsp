"""Tests for mutation and imperative pattern rules."""

from __future__ import annotations

from java_functional_lsp.analyzers.mutation_checker import MutationChecker
from tests.conftest import parse_and_analyze


class TestMutableVariable:
    def test_detects_reassignment(self) -> None:
        source = b"class T { void f() { int x = 1; x = 2; } }"
        diags = parse_and_analyze(MutationChecker(), source)
        codes = [d.code for d in diags]
        assert "mutable-variable" in codes

    def test_ignores_initial_assignment(self) -> None:
        source = b"class T { void f() { final int x = 1; } }"
        diags = parse_and_analyze(MutationChecker(), source)
        assert not any(d.code == "mutable-variable" for d in diags)


class TestImperativeLoop:
    def test_detects_for_each(self) -> None:
        source = b"class T { void f() { for (String s : list) { process(s); } } }"
        diags = parse_and_analyze(MutationChecker(), source)
        codes = [d.code for d in diags]
        assert "imperative-loop" in codes

    def test_detects_while(self) -> None:
        source = b"class T { void f() { while (iter.hasNext()) { process(iter.next()); } } }"
        diags = parse_and_analyze(MutationChecker(), source)
        codes = [d.code for d in diags]
        assert "imperative-loop" in codes

    def test_skips_main_method(self) -> None:
        source = (
            b"class T { public static void main(String[] args) { for (String a : args) { System.out.println(a); } } }"
        )
        diags = parse_and_analyze(MutationChecker(), source)
        assert not any(d.code == "imperative-loop" for d in diags)


class TestMutableDto:
    def test_detects_data_annotation(self) -> None:
        source = b"@Data class Foo { private String name; }"
        diags = parse_and_analyze(MutationChecker(), source)
        codes = [d.code for d in diags]
        assert "mutable-dto" in codes

    def test_detects_setter_annotation(self) -> None:
        source = b"@Setter class Foo { private String name; }"
        diags = parse_and_analyze(MutationChecker(), source)
        codes = [d.code for d in diags]
        assert "mutable-dto" in codes

    def test_ignores_value_annotation(self) -> None:
        source = b"@Value class Foo { String name; }"
        diags = parse_and_analyze(MutationChecker(), source)
        assert not any(d.code == "mutable-dto" for d in diags)


class TestImperativeOptionUnwrap:
    def test_detects_is_defined_get(self) -> None:
        source = b"""
        class T {
            void f() {
                if (opt.isDefined()) { return opt.get(); }
            }
        }
        """
        diags = parse_and_analyze(MutationChecker(), source)
        codes = [d.code for d in diags]
        assert "imperative-option-unwrap" in codes

    def test_detects_is_present_get(self) -> None:
        source = b"""
        class T {
            void f() {
                if (opt.isPresent()) { return opt.get(); }
            }
        }
        """
        diags = parse_and_analyze(MutationChecker(), source)
        codes = [d.code for d in diags]
        assert "imperative-option-unwrap" in codes

    def test_ignores_no_get_in_body(self) -> None:
        source = b"""
        class T {
            void f() {
                if (opt.isDefined()) { doSomething(); }
            }
        }
        """
        diags = parse_and_analyze(MutationChecker(), source)
        assert not any(d.code == "imperative-option-unwrap" for d in diags)

    def test_ignores_unrelated_get(self) -> None:
        """Different object's .get() should not trigger the rule."""
        source = b"""
        class T {
            void f() {
                if (opt.isDefined()) { other.get(); }
            }
        }
        """
        diags = parse_and_analyze(MutationChecker(), source)
        assert not any(d.code == "imperative-option-unwrap" for d in diags)


class TestConstructorAssignment:
    def test_ignores_this_field_in_constructor(self) -> None:
        source = b"class T { final int x; T(int x) { this.x = x; } }"
        diags = parse_and_analyze(MutationChecker(), source)
        assert not any(d.code == "mutable-variable" for d in diags)

    def test_ignores_computed_field_in_constructor(self) -> None:
        """this.x = computeValue() in constructor should not be flagged."""
        source = b"class T { final int x; T() { this.x = compute(); } }"
        diags = parse_and_analyze(MutationChecker(), source)
        assert not any(d.code == "mutable-variable" for d in diags)

    def test_detects_reassignment_in_method(self) -> None:
        """this.x = ... in a regular method IS a mutation."""
        source = b"class T { int x; void f() { this.x = 42; } }"
        diags = parse_and_analyze(MutationChecker(), source)
        codes = [d.code for d in diags]
        assert "mutable-variable" in codes
