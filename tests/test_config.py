"""Tests for configuration and severity handling."""

from __future__ import annotations

from java_functional_lsp.analyzers.base import Severity, severity_from_config, source_level_from_config


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


class TestSourceLevelFromConfig:
    def test_missing_defaults_to_8(self) -> None:
        assert source_level_from_config({}) == 8

    def test_int_source_level(self) -> None:
        assert source_level_from_config({"sourceLevel": 17}) == 17

    def test_string_source_level(self) -> None:
        assert source_level_from_config({"sourceLevel": "17"}) == 17

    def test_old_style_1_8_normalizes_to_8(self) -> None:
        assert source_level_from_config({"sourceLevel": "1.8"}) == 8

    def test_java_version_alias(self) -> None:
        assert source_level_from_config({"javaVersion": 21}) == 21

    def test_source_level_wins_over_java_version(self) -> None:
        assert source_level_from_config({"sourceLevel": 17, "javaVersion": 8}) == 17

    def test_unparseable_string_falls_back_to_default(self) -> None:
        assert source_level_from_config({"sourceLevel": "not-a-version"}) == 8

    def test_custom_default(self) -> None:
        assert source_level_from_config({}, default=11) == 11
