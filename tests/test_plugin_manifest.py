"""Regression guard: shipped lspServers configs must not use fields old Claude Code rejects.

Claude Code 2.1.202 and earlier throw "<field> is not yet implemented" for
startupTimeout / shutdownTimeout / restartOnCrash / maxRestarts and drop the whole
server entry, so the Java LSP never starts ("No LSP server available for file
type: .java"). Newer versions accept them, so leaving them out works everywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REJECTED_BY_OLD_CLAUDE_CODE = ("startupTimeout", "shutdownTimeout", "restartOnCrash", "maxRestarts")


def test_plugin_manifest_lsp_servers_work_on_old_claude_code() -> None:
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    servers = manifest["lspServers"]
    assert servers, "plugin.json must declare at least one lspServers entry"
    for name, server in servers.items():
        present = sorted(set(server) & set(REJECTED_BY_OLD_CLAUDE_CODE))
        assert not present, f"lspServers[{name!r}] uses {present}; Claude Code <= 2.1.202 never starts it"


@pytest.mark.parametrize("doc", ["README.md", "SKILL.md"])
def test_documented_lsp_configs_work_on_old_claude_code(doc: str) -> None:
    text = (ROOT / doc).read_text()
    offenders = [field for field in REJECTED_BY_OLD_CLAUDE_CODE if f'"{field}":' in text]
    assert not offenders, f"{doc} shows lspServers config with {offenders}; copying it breaks Claude Code <= 2.1.202"
