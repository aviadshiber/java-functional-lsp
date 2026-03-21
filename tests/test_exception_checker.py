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
