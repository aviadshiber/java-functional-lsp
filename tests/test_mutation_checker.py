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

    def test_config_properties_suggests_constructor_binding(self) -> None:
        source = b"@ConfigurationProperties @Setter class Props { String name; }"
        diags = parse_and_analyze(MutationChecker(), source)
        dto_diags = [d for d in diags if d.code == "mutable-dto"]
        assert len(dto_diags) == 1
        assert "@ConstructorBinding" in dto_diags[0].message

    def test_regular_setter_suggests_value(self) -> None:
        source = b"@Setter class Foo { String name; }"
        diags = parse_and_analyze(MutationChecker(), source)
        dto_diags = [d for d in diags if d.code == "mutable-dto"]
        assert len(dto_diags) == 1
        assert "@Value" in dto_diags[0].message


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


class TestMutationCheckerData:
    def test_mutable_variable_has_data_field(self) -> None:
        source = b"class T { void f() { int x = 1; x = 2; } }"
        diags = parse_and_analyze(MutationChecker(), source)
        mut_diags = [d for d in diags if d.code == "mutable-variable"]
        assert len(mut_diags) >= 1
        assert mut_diags[0].data is not None
        assert mut_diags[0].data.fix_type == "USE_FINAL_TRANSFORMS"
        assert mut_diags[0].data.target_library == "io.vavr.collection.List"

    def test_mutable_variable_recommended_api_omits_final(self) -> None:
        """Issue #74 review: `recommended_api` is an API hint paired with `target_library`; the
        `final` language modifier doesn't belong there. The rationale field mentions it instead."""
        source = b"class T { void f() { int x = 1; x = 2; } }"
        diags = parse_and_analyze(MutationChecker(), source)
        mut = next(d for d in diags if d.code == "mutable-variable")
        assert mut.data is not None
        assert mut.data.recommended_api is not None
        assert "final" not in mut.data.recommended_api
        # But the rationale still mentions final.
        assert "final" in mut.data.rationale

    def test_imperative_loop_has_data_field(self) -> None:
        source = b"class T { void f() { for (String s : list) { process(s); } } }"
        diags = parse_and_analyze(MutationChecker(), source)
        loop_diags = [d for d in diags if d.code == "imperative-loop"]
        assert len(loop_diags) == 1
        assert loop_diags[0].data is not None
        assert loop_diags[0].data.fix_type == "USE_FUNCTIONAL_TRANSFORMS"

    def test_mutable_dto_has_data_field(self) -> None:
        source = b"@Data class Foo { private String name; }"
        diags = parse_and_analyze(MutationChecker(), source)
        dto_diags = [d for d in diags if d.code == "mutable-dto"]
        assert len(dto_diags) == 1
        assert dto_diags[0].data is not None
        assert dto_diags[0].data.fix_type == "USE_VALUE_ANNOTATION"
        assert dto_diags[0].data.target_library == "lombok.Value"

    def test_imperative_option_unwrap_has_snippet_with_real_var_name(self) -> None:
        """Issue #74 #2: snippet uses the AST variable name, not a placeholder."""
        source = b"""
        class T {
            String f(Option<String> myOpt) {
                if (myOpt.isDefined()) {
                    return myOpt.get();
                } else {
                    return "fallback";
                }
            }
        }
        """
        diags = parse_and_analyze(MutationChecker(), source)
        unwraps = [d for d in diags if d.code == "imperative-option-unwrap"]
        assert len(unwraps) == 1
        unwrap = unwraps[0]
        assert unwrap.data is not None
        assert unwrap.data.recommended_api is not None
        assert "ifPresent" in unwrap.data.recommended_api  # warns about Vavr-vs-Optional confusion
        snippet = unwrap.data.suggested_snippet
        assert snippet is not None
        assert "myOpt" in snippet  # real variable name from AST
        assert '"fallback"' in snippet  # real default value from AST

    def test_mutable_dto_has_recommended_api(self) -> None:
        """Issue #74 #1: mutable-dto carries the @Value recommendation."""
        source = b"@Data class Foo { private String name; }"
        diags = parse_and_analyze(MutationChecker(), source)
        dto = next(d for d in diags if d.code == "mutable-dto")
        assert dto.data is not None
        assert dto.data.recommended_api == "@Value"

    def test_mutable_dto_snippet_uses_real_class_name(self) -> None:
        """Issue #74 review: snippet must reference the real class name, not the placeholder `Foo`
        with `{ ... }` body that would never compile if pasted."""
        source = b"@Data class UserDto { private String name; }"
        diags = parse_and_analyze(MutationChecker(), source)
        dto = next(d for d in diags if d.code == "mutable-dto")
        assert dto.data is not None
        snippet = dto.data.suggested_snippet
        assert snippet is not None
        assert "class UserDto" in snippet
        # No `{ ... }` placeholder body (that's not valid Java).
        assert "{ ... }" not in snippet


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

    def test_detects_other_object_field_in_constructor(self) -> None:
        """other.field = x in a constructor IS a mutation and should be flagged."""
        source = b"class T { T() { other.field = 42; } }"
        diags = parse_and_analyze(MutationChecker(), source)
        assert any(d.code == "mutable-variable" for d in diags)

    def test_detects_reassignment_in_method(self) -> None:
        """this.x = ... in a regular method IS a mutation."""
        source = b"class T { int x; void f() { this.x = 42; } }"
        diags = parse_and_analyze(MutationChecker(), source)
        codes = [d.code for d in diags]
        assert "mutable-variable" in codes
