"""Tests for exception handling rules."""

from __future__ import annotations

from java_functional_lsp.analyzers.exception_checker import ExceptionChecker
from tests.conftest import parse_and_analyze


class TestThrowStatement:
    def test_detects_throw(self) -> None:
        source = b"class T { void f() { throw new RuntimeException(); } }"
        diags = parse_and_analyze(ExceptionChecker(), source)
        codes = [d.code for d in diags]
        assert "throw-statement" in codes

    def test_ignores_no_throw(self) -> None:
        source = b"class T { void f() { System.out.println(); } }"
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert not any(d.code == "throw-statement" for d in diags)


class TestCatchRethrow:
    def test_detects_catch_rethrow(self) -> None:
        source = b"""
        class T {
            void f() {
                try { foo(); }
                catch (Exception e) { throw new RuntimeException(e); }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        codes = [d.code for d in diags]
        assert "catch-rethrow" in codes

    def test_catch_with_comment_and_throw_still_flagged(self) -> None:
        """A catch with only a comment + throw is still a rethrow — comments are ignored."""
        source = b"""
        class T {
            void f() {
                try { foo(); }
                catch (Exception e) {
                    // log the error
                    throw new RuntimeException(e);
                }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert any(d.code == "catch-rethrow" for d in diags)

    def test_ignores_catch_with_logic(self) -> None:
        source = b"""
        class T {
            void f() {
                try { foo(); }
                catch (Exception e) { log.error(e); return; }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert not any(d.code == "catch-rethrow" for d in diags)


class TestExceptionCheckerData:
    def test_throw_statement_has_data_field(self) -> None:
        source = b"class T { void f() { throw new RuntimeException(); } }"
        diags = parse_and_analyze(ExceptionChecker(), source)
        throw_diags = [d for d in diags if d.code == "throw-statement"]
        assert len(throw_diags) == 1
        assert throw_diags[0].data is not None
        assert throw_diags[0].data.fix_type == "USE_EITHER_OR_TRY"
        assert throw_diags[0].data.target_library == "io.vavr.control.Either"

    def test_catch_rethrow_has_data_field(self) -> None:
        source = b"""
        class T {
            void f() {
                try { foo(); }
                catch (Exception e) { throw new RuntimeException(e); }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        rethrow_diags = [d for d in diags if d.code == "catch-rethrow"]
        assert len(rethrow_diags) == 1
        assert rethrow_diags[0].data is not None
        assert rethrow_diags[0].data.fix_type == "USE_TRY_TO_EITHER"
        assert rethrow_diags[0].data.target_library == "io.vavr.control.Try"


class TestBeanSuppression:
    def test_ignores_throw_in_bean_method(self) -> None:
        source = b"""
        class Config {
            @Bean
            DataSource dataSource() {
                if (url == null) {
                    throw new IllegalStateException("url required");
                }
                return new DataSource(url);
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert not any(d.code == "throw-statement" for d in diags)

    def test_flags_throw_in_regular_method(self) -> None:
        source = b"""
        class Service {
            void process() {
                throw new RuntimeException("error");
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert any(d.code == "throw-statement" for d in diags)

    def test_ignores_catch_rethrow_in_bean_method(self) -> None:
        source = b"""
        class Config {
            @Bean
            DataSource dataSource() {
                try { return connect(); }
                catch (Exception e) { throw new RuntimeException(e); }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert not any(d.code == "catch-rethrow" for d in diags)
        assert not any(d.code == "throw-statement" for d in diags)


class TestTryCatchToMonadic:
    def test_detects_simple_try_return(self) -> None:
        source = b"""
        class T {
            String f() {
                try { return risky(); }
                catch (IOException e) { return "default"; }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert any(d.code == "try-catch-to-monadic" for d in diags)

    def test_detects_logging_then_return(self) -> None:
        source = b"""
        class T {
            String f() {
                try { return risky(); }
                catch (IOException e) {
                    logger.warn("failed", e);
                    return "fallback";
                }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert any(d.code == "try-catch-to-monadic" for d in diags)

    def test_detects_recover_pattern(self) -> None:
        source = b"""
        class T {
            String f() {
                try { return risky(); }
                catch (IOException e) { return fallback(e); }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert any(d.code == "try-catch-to-monadic" for d in diags)

    def test_diagnostic_on_try_keyword(self) -> None:
        """Diagnostic range should cover only the `try` keyword (3 chars)."""
        source = b"""
        class T {
            String f() {
                try { return risky(); }
                catch (Exception e) { return "x"; }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        d = next(x for x in diags if x.code == "try-catch-to-monadic")
        # Narrow range: only the `try` keyword
        assert d.line == d.end_line
        assert d.end_col - d.col == 3  # len("try")

    def test_ignores_try_with_finally(self) -> None:
        source = b"""
        class T {
            String f() {
                try { return risky(); }
                catch (Exception e) { return "x"; }
                finally { cleanup(); }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert not any(d.code == "try-catch-to-monadic" for d in diags)

    def test_ignores_multi_catch(self) -> None:
        source = b"""
        class T {
            String f() {
                try { return risky(); }
                catch (IOException e) { return "io"; }
                catch (SQLException e) { return "sql"; }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert not any(d.code == "try-catch-to-monadic" for d in diags)

    def test_ignores_multi_statement_try_body(self) -> None:
        source = b"""
        class T {
            String f() {
                try {
                    String x = risky();
                    return x.trim();
                } catch (Exception e) { return "x"; }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert not any(d.code == "try-catch-to-monadic" for d in diags)

    def test_ignores_try_without_return(self) -> None:
        source = b"""
        class T {
            void f() {
                try { risky(); }
                catch (Exception e) { log(e); }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert not any(d.code == "try-catch-to-monadic" for d in diags)

    def test_severity_is_hint_by_default(self) -> None:
        from java_functional_lsp.analyzers.base import Severity

        source = b"""
        class T {
            String f() {
                try { return risky(); }
                catch (Exception e) { return "x"; }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        d = next(x for x in diags if x.code == "try-catch-to-monadic")
        assert d.severity == Severity.HINT

    def test_suppressed_in_bean_method(self) -> None:
        source = b"""
        class Config {
            @Bean
            DataSource dataSource() {
                try { return connect(); }
                catch (Exception e) { return fallback; }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert not any(d.code == "try-catch-to-monadic" for d in diags)

    def test_has_diagnostic_data(self) -> None:
        source = b"""
        class T {
            String f() {
                try { return risky(); }
                catch (Exception e) { return "x"; }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        d = next(x for x in diags if x.code == "try-catch-to-monadic")
        assert d.data is not None
        assert d.data.fix_type == "WRAP_IN_TRY"
        assert d.data.target_library == "io.vavr.control.Try"

    def test_disabled_by_config(self) -> None:
        source = b"""
        class T {
            String f() {
                try { return risky(); }
                catch (Exception e) { return "x"; }
            }
        }
        """
        config = {"rules": {"try-catch-to-monadic": "off"}}
        diags = parse_and_analyze(ExceptionChecker(), source, config)
        assert not any(d.code == "try-catch-to-monadic" for d in diags)

    def test_ignores_union_catch_type(self) -> None:
        """Multi-catch (A | B e) is not supported — no diagnostic should be emitted."""
        source = b"""
        class T {
            String f() {
                try { return risky(); }
                catch (IOException | SQLException e) { return "x"; }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert not any(d.code == "try-catch-to-monadic" for d in diags)

    def test_ignores_try_with_resources(self) -> None:
        """try-with-resources must not be rewritten — closing the resource would be lost."""
        source = b"""
        class T {
            String f() {
                try (java.io.InputStream is = open()) {
                    return parse(is);
                } catch (java.io.IOException e) {
                    return "empty";
                }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        assert not any(d.code == "try-catch-to-monadic" for d in diags)

    def test_catch_with_throw_only_flags_rethrow_not_monadic(self) -> None:
        """A catch whose sole statement is a throw should fire catch-rethrow, NOT try-catch-to-monadic."""
        source = b"""
        class T {
            String f() {
                try { return risky(); }
                catch (Exception e) { throw new RuntimeException(e); }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        codes = [d.code for d in diags]
        # catch-rethrow fires (the existing rule)
        assert "catch-rethrow" in codes
        # but try-catch-to-monadic does NOT — the catch body is a throw, not a return
        assert "try-catch-to-monadic" not in codes

    def test_detects_two_sequential_try_catches(self) -> None:
        """Two independent try/catch blocks in the same class produce two diagnostics."""
        source = b"""
        class T {
            String f() {
                try { return a(); } catch (Exception e) { return "a-default"; }
            }
            String g() {
                try { return b(); } catch (Exception e) { return "b-default"; }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        monadic = [d for d in diags if d.code == "try-catch-to-monadic"]
        assert len(monadic) == 2

    def test_no_duplicate_diagnostic_for_single_try(self) -> None:
        """Regression guard: a single matching try/catch produces exactly one diagnostic."""
        source = b"""
        class T {
            String f() {
                try { return risky(); }
                catch (Exception e) { return "x"; }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        monadic = [d for d in diags if d.code == "try-catch-to-monadic"]
        assert len(monadic) == 1

    def test_nested_try_catch_outer_only(self) -> None:
        """Outer try/catch that contains an inner non-matching block still matches on the outer."""
        source = b"""
        class T {
            String f() {
                try {
                    return inner();
                } catch (Exception e) {
                    return "outer";
                }
            }
        }
        """
        diags = parse_and_analyze(ExceptionChecker(), source)
        monadic = [d for d in diags if d.code == "try-catch-to-monadic"]
        # The outer try matches the shape exactly once.
        assert len(monadic) == 1

    def test_severity_via_config_warning(self) -> None:
        """Non-default severity level propagates through the config."""
        from java_functional_lsp.analyzers.base import Severity

        source = b"""
        class T {
            String f() {
                try { return risky(); }
                catch (Exception e) { return "x"; }
            }
        }
        """
        config = {"rules": {"try-catch-to-monadic": "warning"}}
        diags = parse_and_analyze(ExceptionChecker(), source, config)
        d = next(x for x in diags if x.code == "try-catch-to-monadic")
        assert d.severity == Severity.WARNING
