"""Tests for configuration and severity handling."""

from __future__ import annotations

from java_functional_lsp.analyzers.base import Severity, severity_from_config


class TestSeverityFromConfig:
    def test_default_warning(self) -> None:
        assert severity_from_config({}, "any-rule") == Severity.WARNING

    def test_explicit_error(self) -> None:
        config = {"rules": {"my-rule": "error"}}
        assert severity_from_config(config, "my-rule") == Severity.ERROR

    def test_explicit_info(self) -> None:
        config = {"rules": {"my-rule": "info"}}
        assert severity_from_config(config, "my-rule") == Severity.INFO

    def test_explicit_hint(self) -> None:
        config = {"rules": {"my-rule": "hint"}}
        assert severity_from_config(config, "my-rule") == Severity.HINT

    def test_off_returns_none(self) -> None:
        config = {"rules": {"my-rule": "off"}}
        assert severity_from_config(config, "my-rule") is None

    def test_unconfigured_rule_uses_default(self) -> None:
        config = {"rules": {"other-rule": "error"}}
        assert severity_from_config(config, "my-rule") == Severity.WARNING

    def test_custom_default(self) -> None:
        assert severity_from_config({}, "any-rule", Severity.INFO) == Severity.INFO
