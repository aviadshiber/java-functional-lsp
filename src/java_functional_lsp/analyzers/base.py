"""Base analyzer class and diagnostic types."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol, cast

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
    source: str = "java-functional-lsp"


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


def find_nodes(root: Node, type_name: str) -> Generator[Node, None, None]:
    """Find all descendant nodes of a given type using TreeCursor for performance."""
    cursor = root.walk()
    visited_children = False
    while True:
        if not visited_children:
            current = cast(Node, cursor.node)
            if current.type == type_name:
                yield current
            if not cursor.goto_first_child():
                visited_children = True
        elif cursor.goto_next_sibling():
            visited_children = False
        elif not cursor.goto_parent():
            break


def find_nodes_multi(root: Node, type_names: set[str]) -> Generator[Node, None, None]:
    """Find all descendant nodes matching any of the given types using TreeCursor."""
    cursor = root.walk()
    visited_children = False
    while True:
        if not visited_children:
            current = cast(Node, cursor.node)
            if current.type in type_names:
                yield current
            if not cursor.goto_first_child():
                visited_children = True
        elif cursor.goto_next_sibling():
            visited_children = False
        elif not cursor.goto_parent():
            break


def collect_nodes_by_type(root: Node, type_names: set[str]) -> dict[str, list[Node]]:
    """Walk tree once, bucket nodes by type. Avoids multiple full traversals."""
    buckets: dict[str, list[Node]] = {t: [] for t in type_names}
    cursor = root.walk()
    visited_children = False
    while True:
        if not visited_children:
            current = cast(Node, cursor.node)
            if current.type in buckets:
                buckets[current.type].append(current)
            if not cursor.goto_first_child():
                visited_children = True
        elif cursor.goto_next_sibling():
            visited_children = False
        elif not cursor.goto_parent():
            break
    return buckets


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
