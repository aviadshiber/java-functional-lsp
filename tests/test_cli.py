"""Tests for CLI check mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from java_functional_lsp.cli import check_file, format_diagnostic, load_config


class TestCheckFile:
    def test_finds_null_return(self, tmp_path: Path) -> None:
        java_file = tmp_path / "Test.java"
        java_file.write_text("class T { String f() { return null; } }")
        diags = check_file(java_file, {})
        codes = [d.code for d in diags]
        assert "null-return" in codes

    def test_clean_file(self, tmp_path: Path) -> None:
        java_file = tmp_path / "Clean.java"
        java_file.write_text('class T { String f() { return "ok"; } }')
        diags = check_file(java_file, {})
        assert len(diags) == 0

    def test_respects_config(self, tmp_path: Path) -> None:
        java_file = tmp_path / "Test.java"
        java_file.write_text("class T { String f() { return null; } }")
        config: dict[str, Any] = {"rules": {"null-return": "off"}}
        diags = check_file(java_file, config)
        assert not any(d.code == "null-return" for d in diags)


class TestFormatDiagnostic:
    def test_format(self, tmp_path: Path) -> None:
        java_file = tmp_path / "Test.java"
        java_file.write_text("class T { String f() { return null; } }")
        diags = check_file(java_file, {})
        line = format_diagnostic(java_file, diags[0])
        assert "null-return" in line
        assert "[W]" in line


class TestLoadConfig:
    def test_finds_config(self, tmp_path: Path) -> None:
        config_file = tmp_path / ".java-functional-lsp.json"
        config_file.write_text('{"rules": {"null-return": "off"}}')
        java_file = tmp_path / "src" / "Test.java"
        java_file.parent.mkdir()
        java_file.write_text("")
        config = load_config(java_file)
        assert config["rules"]["null-return"] == "off"

    def test_missing_config(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "Test.java")
        assert config == {}
