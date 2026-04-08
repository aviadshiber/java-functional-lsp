"""Tests for functional semantic analysis rules."""

from __future__ import annotations

from java_functional_lsp.analyzers.functional_checker import FunctionalChecker
from tests.conftest import parse_and_analyze


class TestFrozenMutation:
    def test_detects_list_of_add(self) -> None:
        source = b"""
        class T {
            void f() {
                List<String> list = List.of("a", "b");
                list.add("c");
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        codes = [d.code for d in diags]
        assert "frozen-mutation" in codes

    def test_detects_set_of_add(self) -> None:
        source = b"""
        class T {
            void f() {
                Set<String> s = Set.of("a");
                s.add("b");
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        codes = [d.code for d in diags]
        assert "frozen-mutation" in codes

    def test_detects_map_of_put(self) -> None:
        source = b"""
        class T {
            void f() {
                Map<String, Integer> m = Map.of("a", 1);
                m.put("b", 2);
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        codes = [d.code for d in diags]
        assert "frozen-mutation" in codes

    def test_detects_collections_unmodifiable_sort(self) -> None:
        source = b"""
        class T {
            void f() {
                List<String> list = Collections.unmodifiableList(other);
                list.sort(Comparator.naturalOrder());
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        codes = [d.code for d in diags]
        assert "frozen-mutation" in codes

    def test_detects_list_copy_of_add(self) -> None:
        source = b"""
        class T {
            void f(List<String> other) {
                List<String> frozen = List.copyOf(other);
                frozen.add("x");
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        codes = [d.code for d in diags]
        assert "frozen-mutation" in codes

    def test_ignores_normal_arraylist(self) -> None:
        source = b"""
        class T {
            void f() {
                List<String> list = new ArrayList<>();
                list.add("c");
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert not any(d.code == "frozen-mutation" for d in diags)

    def test_ignores_unfrozen_variable_mutation(self) -> None:
        source = b"""
        class T {
            void f() {
                List<String> frozen = List.of("a");
                List<String> mutable = new ArrayList<>();
                mutable.add("b");
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        frozen_diags = [d for d in diags if d.code == "frozen-mutation"]
        assert len(frozen_diags) == 0

    def test_has_data_field(self) -> None:
        source = b"""
        class T {
            void f() {
                List<String> list = List.of("a");
                list.add("b");
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        frozen_diags = [d for d in diags if d.code == "frozen-mutation"]
        assert len(frozen_diags) == 1
        assert frozen_diags[0].data is not None
        assert frozen_diags[0].data.fix_type == "REPLACE_WITH_VAVR_LIST"
        assert frozen_diags[0].data.target_library == "io.vavr.collection.List"

    def test_disabled_by_config(self) -> None:
        source = b"""
        class T {
            void f() {
                List<String> list = List.of("a");
                list.add("b");
            }
        }
        """
        config = {"rules": {"frozen-mutation": "off"}}
        diags = parse_and_analyze(FunctionalChecker(), source, config)
        assert not any(d.code == "frozen-mutation" for d in diags)

    def test_detects_immutable_list_of_add(self) -> None:
        """Guava ImmutableList.of() then .add() should trigger."""
        source = b"""
        class T {
            void f() {
                List<String> list = ImmutableList.of("a", "b");
                list.add("c");
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert any(d.code == "frozen-mutation" for d in diags)

    def test_detects_immutable_set_of_add(self) -> None:
        """Guava ImmutableSet.of() then .add() should trigger."""
        source = b"""
        class T {
            void f() {
                Set<String> s = ImmutableSet.of("a");
                s.add("b");
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert any(d.code == "frozen-mutation" for d in diags)

    def test_detects_immutable_map_of_put(self) -> None:
        """Guava ImmutableMap.of() then .put() should trigger."""
        source = b"""
        class T {
            void f() {
                Map<String, Integer> m = ImmutableMap.of("a", 1);
                m.put("b", 2);
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert any(d.code == "frozen-mutation" for d in diags)


class TestNullCheckToMonadic:
    def test_detects_reversed_null_check(self) -> None:
        """null != x (reversed operand order) should also trigger."""
        source = b"""
        class T {
            String f(User user) {
                if (null != user) {
                    return user.getName();
                }
                return null;
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        codes = [d.code for d in diags]
        assert "null-check-to-monadic" in codes

    def test_detects_simple_null_check(self) -> None:
        source = b"""
        class T {
            String f(User user) {
                if (user != null) {
                    return user.getName();
                }
                return null;
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        codes = [d.code for d in diags]
        assert "null-check-to-monadic" in codes

    def test_ignores_null_check_with_expression_statement(self) -> None:
        """expression_statement bodies are not yet fixable — diagnostic should not fire."""
        source = b"""
        class T {
            void f(User user) {
                if (user != null) {
                    process(user);
                }
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert not any(d.code == "null-check-to-monadic" for d in diags)

    def test_ignores_complex_if_body(self) -> None:
        """Multi-statement if-body should NOT trigger."""
        source = b"""
        class T {
            String f(User user) {
                if (user != null) {
                    log(user);
                    return user.getName();
                }
                return null;
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert not any(d.code == "null-check-to-monadic" for d in diags)

    def test_ignores_non_null_check(self) -> None:
        """Condition not involving null should not trigger."""
        source = b"""
        class T {
            String f(User user) {
                if (user.isActive()) {
                    return user.getName();
                }
                return null;
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert not any(d.code == "null-check-to-monadic" for d in diags)

    def test_ignores_eq_null_check(self) -> None:
        """== null (not !=) should not trigger."""
        source = b"""
        class T {
            String f(User user) {
                if (user == null) {
                    return "default";
                }
                return null;
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert not any(d.code == "null-check-to-monadic" for d in diags)

    def test_has_data_field(self) -> None:
        source = b"""
        class T {
            String f(User user) {
                if (user != null) {
                    return user.getName();
                }
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        null_diags = [d for d in diags if d.code == "null-check-to-monadic"]
        assert len(null_diags) == 1
        assert null_diags[0].data is not None
        assert null_diags[0].data.fix_type == "WRAP_IN_OPTION_MAP"

    def test_disabled_by_config(self) -> None:
        source = b"""
        class T {
            String f(User user) {
                if (user != null) { return user.getName(); }
            }
        }
        """
        config = {"rules": {"null-check-to-monadic": "off"}}
        diags = parse_and_analyze(FunctionalChecker(), source, config)
        assert not any(d.code == "null-check-to-monadic" for d in diags)

    def test_detects_identity_return(self) -> None:
        """if (x != null) { return x; } should still trigger — rewrite skips .map()."""
        source = b"""
        class T {
            String f(User user) {
                if (user != null) {
                    return user;
                }
                return null;
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert any(d.code == "null-check-to-monadic" for d in diags)

    def test_detects_simple_else_branch(self) -> None:
        """Simple else with single return should trigger."""
        source = b"""
        class T {
            String f(User user) {
                if (user != null) {
                    return user.getName();
                } else {
                    return "unknown";
                }
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert any(d.code == "null-check-to-monadic" for d in diags)

    def test_detects_complex_else_branch(self) -> None:
        """Complex else should still trigger diagnostic (no code action, but agents use it)."""
        source = b"""
        class T {
            String f(String key) {
                String val = map.get(key);
                if (val != null) {
                    return val;
                } else {
                    val = fallback.get(key);
                    if (val != null) {
                        return val;
                    }
                }
                return defaultVal;
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert any(d.code == "null-check-to-monadic" for d in diags)


class TestImpureMethod:
    def test_detects_mixed_method(self) -> None:
        source = b"""
        class T {
            String f(String input) {
                String result = input.trim();
                System.out.println(result);
                return result;
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        codes = [d.code for d in diags]
        assert "impure-method" in codes

    def test_detects_logger_with_logic(self) -> None:
        source = b"""
        class T {
            int calculate(int x) {
                int result = x * 2 + 1;
                logger.info("result: " + result);
                return result;
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        codes = [d.code for d in diags]
        assert "impure-method" in codes

    def test_detects_throw_with_pure_logic(self) -> None:
        """throw statement in a method with pure logic should trigger impure-method."""
        source = b"""
        class T {
            String process(String input) {
                String result = input.trim();
                if (result.isEmpty()) {
                    throw new IllegalArgumentException("empty input");
                }
                return result;
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        codes = [d.code for d in diags]
        assert "impure-method" in codes

    def test_ignores_pure_method(self) -> None:
        source = b"""
        class T {
            int add(int a, int b) {
                return a + b;
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert not any(d.code == "impure-method" for d in diags)

    def test_ignores_single_statement_method(self) -> None:
        """Single-statement methods can't have a mix of pure/impure."""
        source = b"""
        class T {
            void f() {
                System.out.println("hello");
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert not any(d.code == "impure-method" for d in diags)

    def test_has_data_field(self) -> None:
        source = b"""
        class T {
            String f(String input) {
                String result = input.trim();
                System.out.println(result);
                return result;
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        impure_diags = [d for d in diags if d.code == "impure-method"]
        assert len(impure_diags) == 1
        assert impure_diags[0].data is not None
        assert impure_diags[0].data.fix_type == "EXTRACT_PURE_LOGIC"

    def test_strict_purity_uses_warning_severity(self) -> None:
        """strictPurity: true should elevate impure-method to WARNING."""
        source = b"""
        class T {
            String f(String input) {
                String result = input.trim();
                System.out.println(result);
                return result;
            }
        }
        """
        from java_functional_lsp.analyzers.base import Severity

        config = {"strictPurity": True}
        diags = parse_and_analyze(FunctionalChecker(), source, config)
        impure_diags = [d for d in diags if d.code == "impure-method"]
        assert len(impure_diags) == 1
        assert impure_diags[0].severity == Severity.WARNING

    def test_default_uses_hint_severity(self) -> None:
        """Default config should use HINT severity for impure-method."""
        source = b"""
        class T {
            String f(String input) {
                String result = input.trim();
                System.out.println(result);
                return result;
            }
        }
        """
        from java_functional_lsp.analyzers.base import Severity

        diags = parse_and_analyze(FunctionalChecker(), source)
        impure_diags = [d for d in diags if d.code == "impure-method"]
        assert len(impure_diags) == 1
        assert impure_diags[0].severity == Severity.HINT

    def test_disabled_by_config(self) -> None:
        source = b"""
        class T {
            String f(String input) {
                String result = input.trim();
                System.out.println(result);
                return result;
            }
        }
        """
        config = {"rules": {"impure-method": "off"}}
        diags = parse_and_analyze(FunctionalChecker(), source, config)
        assert not any(d.code == "impure-method" for d in diags)
