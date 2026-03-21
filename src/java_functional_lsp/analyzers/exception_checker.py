"""Exception handling rules: detect throw statements and catch-rethrow patterns."""

from __future__ import annotations

from typing import Any

from .base import Diagnostic, find_nodes, severity_from_config

_MESSAGES = {
    "throw-statement": ("Avoid throwing exceptions. Use Either.left(error) or Try.of(() -> ...).toEither()."),
    "catch-rethrow": (
        "Avoid catching and rethrowing. Use Try.of(() -> ...).toEither() to convert exceptions to values."
    ),
}


class ExceptionChecker:
    """Detects throw statements and catch-rethrow anti-patterns."""

    def analyze(self, tree: Any, source: bytes, config: dict[str, Any]) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []

        # Rule: throw-statement
        severity = severity_from_config(config, "throw-statement")
        if severity is not None:
            for node in find_nodes(tree.root_node, "throw_statement"):
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
                body = node.child_by_field_name("body")
                if body is None:
                    continue
                # Check if the block has exactly one named statement and it's a throw
                statements = [c for c in body.named_children if c.type != "comment"]
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
