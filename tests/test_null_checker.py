"""Tests for null safety rules."""

from __future__ import annotations

from java_functional_lsp.analyzers.null_checker import NullChecker
from tests.conftest import parse_and_analyze


class TestNullLiteralArg:
    def test_detects_null_in_method_call(self) -> None:
        source = b"class T { void f() { foo(null); } }"
        diags = parse_and_analyze(NullChecker(), source)
        codes = [d.code for d in diags]
        assert "null-literal-arg" in codes

    def test_ignores_non_null_args(self) -> None:
        source = b'class T { void f() { foo("bar"); } }'
        diags = parse_and_analyze(NullChecker(), source)
        assert not any(d.code == "null-literal-arg" for d in diags)


class TestNullReturn:
    def test_detects_return_null(self) -> None:
        source = b"class T { String f() { return null; } }"
        diags = parse_and_analyze(NullChecker(), source)
        codes = [d.code for d in diags]
        assert "null-return" in codes

    def test_ignores_return_value(self) -> None:
        source = b'class T { String f() { return "ok"; } }'
        diags = parse_and_analyze(NullChecker(), source)
        assert not any(d.code == "null-return" for d in diags)


class TestNullAssignment:
    def test_detects_local_null_assignment(self) -> None:
        source = b"class T { void f() { String x = null; } }"
        diags = parse_and_analyze(NullChecker(), source)
        codes = [d.code for d in diags]
        assert "null-assignment" in codes

    def test_detects_field_null_assignment(self) -> None:
        source = b"class T { private String name = null; }"
        diags = parse_and_analyze(NullChecker(), source)
        codes = [d.code for d in diags]
        assert "null-field-assignment" in codes


class TestNullCheckerData:
    def test_null_return_has_data_field(self) -> None:
        source = b"class T { String f() { return null; } }"
        diags = parse_and_analyze(NullChecker(), source)
        null_diags = [d for d in diags if d.code == "null-return"]
        assert len(null_diags) == 1
        assert null_diags[0].data is not None
        assert null_diags[0].data.fix_type == "WRAP_IN_OPTION"
        assert null_diags[0].data.target_library == "io.vavr.control.Option"

    def test_null_literal_arg_has_data_field(self) -> None:
        source = b"class T { void f() { foo(null); } }"
        diags = parse_and_analyze(NullChecker(), source)
        arg_diags = [d for d in diags if d.code == "null-literal-arg"]
        assert len(arg_diags) == 1
        assert arg_diags[0].data is not None
        assert arg_diags[0].data.fix_type == "WRAP_IN_OPTION_NONE"
        assert arg_diags[0].data.target_library == "io.vavr.control.Option"


class TestNullConfig:
    def test_disabled_rule_produces_no_diagnostics(self) -> None:
        source = b"class T { void f() { foo(null); return null; } }"
        config = {"rules": {"null-literal-arg": "off", "null-return": "off"}}
        diags = parse_and_analyze(NullChecker(), source, config)
        assert len(diags) == 0

    def test_severity_override(self) -> None:
        source = b"class T { void f() { foo(null); } }"
        config = {"rules": {"null-literal-arg": "error"}}
        diags = parse_and_analyze(NullChecker(), source, config)
        assert diags[0].severity == 1  # ERROR = 1
