"""Exception handling rules: detect throw statements and catch-rethrow patterns."""

from __future__ import annotations

from typing import Any

from .base import Diagnostic, find_nodes, has_sibling_annotation, severity_from_config

_MESSAGES = {
    "throw-statement": ("Avoid throwing exceptions. Use Either.left(error) or Try.of(() -> ...).toEither()."),
    "catch-rethrow": (
        "Avoid catching and rethrowing. Use Try.of(() -> ...).toEither() to convert exceptions to values."
    ),
}


def _is_in_bean_method(node: Any) -> bool:
    """Check if node is inside a method annotated with @Bean."""
    parent = node.parent
    while parent:
        if parent.type == "method_declaration":
            modifiers = next((c for c in parent.children if c.type == "modifiers"), None)
            if modifiers and has_sibling_annotation(modifiers, b"Bean"):
                return True
            return False
        parent = parent.parent
    return False


class ExceptionChecker:
    """Detects throw statements and catch-rethrow anti-patterns."""

    def analyze(self, tree: Any, source: bytes, config: dict[str, Any]) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []

        # Rule: throw-statement
        severity = severity_from_config(config, "throw-statement")
        if severity is not None:
            for node in find_nodes(tree.root_node, "throw_statement"):
                if _is_in_bean_method(node):
                    continue
                diagnostics.append(
                    Diagnostic(
                        line=node.start_point[0],
                        col=node.start_point[1],
                        end_line=node.end_point[0],
                        end_col=node.end_point[1],
                        severity=severity,
                        code="throw-statement",
                        message=_MESSAGES["throw-statement"],
                    )
                )

        # Rule: catch-rethrow
        severity = severity_from_config(config, "catch-rethrow")
        if severity is not None:
            for node in find_nodes(tree.root_node, "catch_clause"):
                if _is_in_bean_method(node):
                    continue
                body = node.child_by_field_name("body")
                if body is None:
                    continue
                statements = [c for c in body.named_children if c.type not in ("line_comment", "block_comment")]
                if len(statements) == 1 and statements[0].type == "throw_statement":
                    diagnostics.append(
                        Diagnostic(
                            line=node.start_point[0],
                            col=node.start_point[1],
                            end_line=node.end_point[0],
                            end_col=node.end_point[1],
                            severity=severity,
                            code="catch-rethrow",
                            message=_MESSAGES["catch-rethrow"],
                        )
                    )

        return diagnostics
