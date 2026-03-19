"""Base analyzer class and diagnostic types."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol

import tree_sitter_java as tsjava
from tree_sitter import Language, Node, Parser


class Severity(IntEnum):
    ERROR = 1
    WARNING = 2
    INFO = 3
    HINT = 4


@dataclass(frozen=True)
class Diagnostic:
    line: int  # 0-based
    col: int
    end_line: int
    end_col: int
    severity: Severity
    code: str  # rule ID
    message: str
    source: str = "deeperdive-java-linter"


class Analyzer(Protocol):
    """Protocol for all analyzers."""

    def analyze(self, tree: Any, source: bytes, config: dict[str, Any]) -> list[Diagnostic]:
        """Analyze a parsed tree and return diagnostics."""
        ...


_parser: Parser | None = None
_language: Language | None = None


def get_parser() -> Parser:
    """Get or create a reusable tree-sitter Java parser."""
    global _parser, _language
    if _parser is None:
        _language = Language(tsjava.language())
        _parser = Parser(_language)
    return _parser


def get_language() -> Language:
    """Get the Java language for queries."""
    global _language
    if _language is None:
        get_parser()
    assert _language is not None
    return _language


def find_nodes(node: Node, type_name: str) -> Generator[Node, None, None]:
    """Recursively find all descendant nodes of a given type."""
    if node.type == type_name:
        yield node
    for child in node.children:
        yield from find_nodes(child, type_name)


def find_nodes_multi(node: Node, type_names: set[str]) -> Generator[Node, None, None]:
    """Recursively find all descendant nodes matching any of the given types."""
    if node.type in type_names:
        yield node
    for child in node.children:
        yield from find_nodes_multi(child, type_names)


def find_ancestor(node: Node, type_name: str) -> Node | None:
    """Walk up the tree to find the nearest ancestor of a given type."""
    parent = node.parent
    while parent:
        if parent.type == type_name:
            return parent
        parent = parent.parent
    return None


def has_ancestor(node: Node, type_names: set[str]) -> bool:
    """Check if any ancestor matches one of the given types."""
    parent = node.parent
    while parent:
        if parent.type in type_names:
            return True
        parent = parent.parent
    return False


def severity_from_config(config: dict[str, Any], rule_id: str, default: Severity = Severity.WARNING) -> Severity | None:
    """Get severity for a rule from config. Returns None if rule is disabled."""
    rules: dict[str, str] = config.get("rules", {})
    level = rules.get(rule_id)
    if level is None:
        return default
    if level == "off":
        return None
    return {
        "error": Severity.ERROR,
        "warning": Severity.WARNING,
        "info": Severity.INFO,
        "hint": Severity.HINT,
    }.get(level, default)
