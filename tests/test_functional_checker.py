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

    def test_snippet_spells_out_vavr_type_migration(self) -> None:
        """Issue #74 review: the snippet must call out that the *variable's type* needs to be
        migrated to io.vavr.collection.List — pasting the assignment alone won't compile if the
        variable is still a java.util.List."""
        source = b"""
        class T {
            void f() {
                List<String> list = List.of("a");
                list.add("b");
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        diag = next(d for d in diags if d.code == "frozen-mutation")
        assert diag.data is not None
        snippet = diag.data.suggested_snippet
        assert snippet is not None
        assert "io.vavr.collection.List" in snippet
        assert "list" in snippet
        assert ".append(...)" in snippet

    def test_snippet_uses_word_boundary_for_short_var_name(self) -> None:
        """Issue #74 review: the `var = var.append(...)` snippet is safe to suggest only when
        the receiver is a plain identifier — chained LHS would produce invalid Java. With a
        plain identifier, the snippet should populate normally."""
        source = b"""
        class T {
            void f() {
                List<String> xs = List.of("a");
                xs.add("b");
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        diag = next(d for d in diags if d.code == "frozen-mutation")
        assert diag.data is not None
        snippet = diag.data.suggested_snippet
        assert snippet is not None
        assert "xs = xs.append(...)" in snippet

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

    def test_detects_immutable_sorted_set_of_add(self) -> None:
        """Guava ImmutableSortedSet.of() then .add() should trigger."""
        source = b"""
        class T {
            void f() {
                Set<String> s = ImmutableSortedSet.of("a");
                s.add("b");
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

    def test_snippet_uses_real_return_expression(self) -> None:
        """Issue #74 review: snippet must build the lambda body from the real return expression,
        not the degenerate `Option.of(x).map(v -> v).getOrElse(null)`."""
        source = b"""
        class T {
            String f(User user) {
                if (user != null) {
                    return user.getName();
                }
                return "guest";
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        diag = next(d for d in diags if d.code == "null-check-to-monadic")
        assert diag.data is not None
        snippet = diag.data.suggested_snippet
        assert snippet is not None
        # Lambda body should be the real method call, with `user` rewritten as `it`.
        assert "Option.of(user).map(it -> it.getName())" in snippet
        # Default should be the real fallback, not always-null.
        assert '.getOrElse("guest")' in snippet
        assert ".getOrElse(null)" not in snippet
        # Must NOT contain the old degenerate identity-map shape.
        assert ".map(v -> v)" not in snippet

    def test_snippet_omits_default_when_else_returns_null(self) -> None:
        """When the else-branch returns null, the snippet should leave the Option monadic
        (no `.getOrElse(null)` — which would defeat the rule)."""
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
        diag = next(d for d in diags if d.code == "null-check-to-monadic")
        assert diag.data is not None
        snippet = diag.data.suggested_snippet
        assert snippet is not None
        assert ".getOrElse(null)" not in snippet

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
                    log(key);
                    return fallback.get(key);
                }
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert any(d.code == "null-check-to-monadic" for d in diags)

    def test_inner_chained_if_suppressed(self) -> None:
        """Inner if in a chain should NOT produce a separate diagnostic."""
        source = b"""
    class T {
        int f(String key) {
            Integer val = map.get(key);
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
        null_check_diags = [d for d in diags if d.code == "null-check-to-monadic"]
        # Should be exactly 1 (outer if), not 2
        assert len(null_check_diags) == 1


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
        # Issue #74 review: fix_type now distinguishes IO vs throw variants. The default test
        # source uses an IO side-effect (`System.out.println`), so the variant suffix is `_IO`.
        assert impure_diags[0].data.fix_type == "EXTRACT_PURE_LOGIC_IO"

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

    def test_io_case_uses_try_data(self) -> None:
        """Issue #74 #4: impure-method with IO side-effect carries Try-targeted data."""
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
        impure = [d for d in diags if d.code == "impure-method"]
        assert len(impure) == 1
        assert impure[0].data is not None
        assert impure[0].data.target_library == "io.vavr.control.Try"
        assert "Try.of" in (impure[0].data.recommended_api or "")
        assert "IO/state" in impure[0].message
        # Issue #74 review: fix_type discriminates the variant so agents can filter without
        # parsing target_library or message text.
        assert impure[0].data.fix_type == "EXTRACT_PURE_LOGIC_IO"

    def test_throw_case_uses_either_data(self) -> None:
        """Issue #74 #4: impure-method with throw side-effect carries Either-targeted data + distinct message."""
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
        impure = [d for d in diags if d.code == "impure-method"]
        assert len(impure) == 1
        assert impure[0].data is not None
        assert impure[0].data.target_library == "io.vavr.control.Either"
        assert "Either.left" in (impure[0].data.recommended_api or "")
        assert "exceptions" in impure[0].message
        # Issue #74 review: fix_type discriminates the variant from the IO case.
        assert impure[0].data.fix_type == "EXTRACT_PURE_LOGIC_THROW"

    def test_points_at_offending_statement_not_method_decl(self) -> None:
        """Issue #74 #5: diagnostic range covers the offending side-effect, not the method name."""
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
        impure = [d for d in diags if d.code == "impure-method"]
        assert len(impure) == 1
        # The method declaration is on line 2 (0-indexed: line 2 = `String f(...) {`).
        # The println is on line 4. Range must land on the println line, not the method name.
        diag_line = impure[0].line
        method_decl_line = source.split(b"\n").index(b"            String f(String input) {")
        assert diag_line != method_decl_line, (
            f"Diagnostic should point at the side-effect line, not the method declaration "
            f"(diag_line={diag_line}, method_decl_line={method_decl_line})"
        )


class TestOptionMapNullable:
    """Issue #69: Option.map() producing Some(null) before a chained value-consuming call."""

    def test_detects_map_get_followed_by_filter(self) -> None:
        source = b"""
        class T {
            Option<String> author(Map<String, String> metadata) {
                return Option.of(metadata)
                    .map(m -> m.get("author"))
                    .filter(s -> !s.trim().isEmpty());
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        codes = [d.code for d in diags]
        assert "option-map-nullable" in codes

    def test_detects_map_get_followed_by_map(self) -> None:
        source = b"""
        class T {
            Option<String> f(Map<String, String> m0) {
                return Option.of(m0).map(m -> m.get("k")).map(s -> s.trim());
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert "option-map-nullable" in [d.code for d in diags]

    def test_detects_qualified_option_root(self) -> None:
        source = b"""
        class T {
            void f(Map<String, String> m0) {
                io.vavr.control.Option.of(m0).map(m -> m.get("k")).forEach(s -> use(s));
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert "option-map-nullable" in [d.code for d in diags]

    def test_data_payload_has_flatmap_snippet_with_real_names(self) -> None:
        source = b"""
        class T {
            Option<String> f(Map<String, String> metadata) {
                return Option.of(metadata)
                    .map(m -> m.get("author"))
                    .filter(s -> !s.isEmpty());
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        diag = next(d for d in diags if d.code == "option-map-nullable")
        assert diag.data is not None
        assert diag.data.fix_type == "USE_FLATMAP_OPTION_OF"
        assert diag.data.target_library == "io.vavr.control.Option"
        assert diag.data.suggested_snippet == '.flatMap(m -> Option.of(m.get("author")))'

    def test_range_starts_at_map_not_chain_root(self) -> None:
        source = b"""
        class T {
            Option<String> f(Map<String, String> m0) {
                return Option.of(m0).map(m -> m.get("k")).filter(s -> !s.isEmpty());
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        diag = next(d for d in diags if d.code == "option-map-nullable")
        line = source.split(b"\n")[diag.line]
        assert line[diag.col :].startswith(b"map("), "range should start at the `map` token"

    def test_no_warn_flatmap_version(self) -> None:
        source = b"""
        class T {
            Option<String> f(Map<String, String> metadata) {
                return Option.of(metadata)
                    .flatMap(m -> Option.of(m.get("author")))
                    .filter(s -> !s.isEmpty());
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert "option-map-nullable" not in [d.code for d in diags]

    def test_no_warn_non_nullable_lambda(self) -> None:
        source = b"""
        class T {
            Option<String> f(String s0) {
                return Option.of(s0).map(s -> s + "!").filter(s -> !s.isEmpty());
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert "option-map-nullable" not in [d.code for d in diags]

    def test_no_warn_java_util_optional(self) -> None:
        source = b"""
        class T {
            Optional<String> f(Map<String, String> m0) {
                return Optional.ofNullable(m0).map(m -> m.get("k")).filter(s -> !s.isEmpty());
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert "option-map-nullable" not in [d.code for d in diags]

    def test_no_warn_terminal_map(self) -> None:
        source = b"""
        class T {
            Option<String> f(Map<String, String> m0) {
                return Option.of(m0).map(m -> m.get("k"));
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert "option-map-nullable" not in [d.code for d in diags]

    def test_no_warn_zero_arg_get(self) -> None:
        source = b"""
        class T {
            Option<String> f(Supplier<String> s0) {
                return Option.of(s0).map(s -> s.get()).filter(v -> !v.isEmpty());
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert "option-map-nullable" not in [d.code for d in diags]

    def test_no_warn_list_index_get(self) -> None:
        source = b"""
        class T {
            Option<String> f(List<String> xs0) {
                return Option.of(xs0).map(xs -> xs.get(0)).filter(v -> !v.isEmpty());
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert "option-map-nullable" not in [d.code for d in diags]

    def test_no_warn_get_or_else_follower(self) -> None:
        source = b"""
        class T {
            String f(Map<String, String> m0) {
                return Option.of(m0).map(m -> m.get("k")).getOrElse("x");
            }
        }
        """
        diags = parse_and_analyze(FunctionalChecker(), source)
        assert "option-map-nullable" not in [d.code for d in diags]

    def test_rule_off_in_config(self) -> None:
        source = b"""
        class T {
            Option<String> f(Map<String, String> m0) {
                return Option.of(m0).map(m -> m.get("k")).filter(s -> !s.isEmpty());
            }
        }
        """
        config = {"rules": {"option-map-nullable": "off"}}
        diags = parse_and_analyze(FunctionalChecker(), source, config)
        assert "option-map-nullable" not in [d.code for d in diags]
