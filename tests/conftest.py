"""Shared fixtures for java-functional-lsp tests."""

from __future__ import annotations

from typing import Any

import pytest

from java_functional_lsp.analyzers.base import get_parser


@pytest.fixture(autouse=True)
def isolate_diagnostics_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the server singleton's hold mode and freshness state from leaking between tests.

    on_initialize switches the mode to hold-all/custom-first; later in-process tests
    would otherwise hold their publishes.
    """
    from java_functional_lsp import server as srv_mod

    monkeypatch.setattr(srv_mod.server, "_hold_mode", srv_mod._HOLD_OFF)
    monkeypatch.setattr(srv_mod.server, "_agent_host", False)
    monkeypatch.setattr(srv_mod, "_freshness", srv_mod._JdtlsFreshness())
    monkeypatch.setattr(srv_mod, "_hold_events", {})


@pytest.fixture
def parser():  # type: ignore[no-untyped-def]
    """Return a reusable tree-sitter Java parser."""
    return get_parser()


@pytest.fixture
def empty_config() -> dict[str, Any]:
    """Return empty config (all rules enabled at default severity)."""
    return {}


def parse_and_analyze(analyzer: Any, source: bytes, config: dict[str, Any] | None = None) -> list[Any]:
    """Helper to parse Java source and run an analyzer."""
    p = get_parser()
    tree = p.parse(source)
    return analyzer.analyze(tree, source, config or {})
