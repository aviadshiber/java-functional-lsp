"""Shared fixtures for java-functional-lsp tests."""

from __future__ import annotations

from typing import Any

import pytest

from java_functional_lsp.analyzers.base import get_parser


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
