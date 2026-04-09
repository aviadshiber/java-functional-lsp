"""Exception handling rules: detect throw statements and catch-rethrow patterns."""

from __future__ import annotations

from typing import Any

from .base import Diagnostic, DiagnosticData, Severity, find_nodes, has_sibling_annotation, severity_from_config

_IGNORED_CHILDREN = ("line_comment", "block_comment")

_MESSAGES = {
    "throw-statement": ("Avoid throwing exceptions. Use Either.left(error) or Try.of(() -> ...).toEither()."),
    "catch-rethrow": (
        "Avoid catching and rethrowing. Use Try.of(() -> ...).toEither() to convert exceptions to values."
    ),
    "try-catch-to-monadic": ("Imperative try/catch: use Try.of(() -> ...) for monadic error handling."),
}

_DATA = {
    "throw-statement": DiagnosticData(
        fix_type="USE_EITHER_OR_TRY",
        target_library="io.vavr.control.Either",
        rationale=(
            "Throwing exceptions breaks referential transparency."
            " Use Either.left(error) to represent failures as values."
        ),
    ),
    "catch-rethrow": DiagnosticData(
        fix_type="USE_TRY_TO_EITHER",
        target_library="io.vavr.control.Try",
        rationale=(
            "Catching and rethrowing adds noise. Use Try.of(() -> ...).toEither() to convert exceptions to values."
        ),
    ),
    "try-catch-to-monadic": DiagnosticData(
        fix_type="WRAP_IN_TRY",
        target_library="io.vavr.control.Try",
        rationale=(
            "try/catch mixes control flow with value handling."
            " Use Try.of(...) for composable, value-based failure handling."
        ),
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


def _matches_try_catch_monadic_shape(try_node: Any) -> bool:  # noqa: PLR0911
    """Verify a try_statement has a shape the code action can rewrite.

    Requirements:
    - No ``finally_clause`` child
    - Exactly one ``catch_clause`` child
    - Try body is a block with a single ``return_statement`` that has an expression
    - Catch body is a block whose last named child is a ``return_statement``,
      and any prior named children are ``expression_statement`` (for the logging pattern)

    Guard-clause returns are preferred for readability; noqa silences the
    "too many returns" rule since each branch fails a distinct shape check.
    """
    # Reject if finally present
    if any(c.type == "finally_clause" for c in try_node.children):
        return False

    # Exactly one catch clause
    catches = [c for c in try_node.children if c.type == "catch_clause"]
    if len(catches) != 1:
        return False

    # Try body must be a block with a single return_statement with an expression
    body = try_node.child_by_field_name("body")
    if body is None or body.type != "block":
        return False
    body_stmts = [c for c in body.named_children if c.type not in _IGNORED_CHILDREN]
    if len(body_stmts) != 1 or body_stmts[0].type != "return_statement":
        return False
    # Return must have an expression (not bare `return;`)
    ret_children = [c for c in body_stmts[0].named_children if c.type not in _IGNORED_CHILDREN]
    if not ret_children:
        return False

    # Catch body must end with a return_statement; prior stmts must be expression_statements
    catch_body = catches[0].child_by_field_name("body")
    if catch_body is None or catch_body.type != "block":
        return False
    catch_stmts = [c for c in catch_body.named_children if c.type not in _IGNORED_CHILDREN]
    if not catch_stmts or catch_stmts[-1].type != "return_statement":
        return False
    if any(s.type != "expression_statement" for s in catch_stmts[:-1]):
        return False
    # The catch return must have an expression
    catch_ret_children = [c for c in catch_stmts[-1].named_children if c.type not in _IGNORED_CHILDREN]
    if not catch_ret_children:
        return False

    return True


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
                        data=_DATA["throw-statement"],
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
                statements = [c for c in body.named_children if c.type not in _IGNORED_CHILDREN]
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
                            data=_DATA["catch-rethrow"],
                        )
                    )

        # Rule: try-catch-to-monadic
        severity = severity_from_config(config, "try-catch-to-monadic", default=Severity.HINT)
        if severity is not None:
            for try_node in find_nodes(tree.root_node, "try_statement"):
                if _is_in_bean_method(try_node):
                    continue
                if not _matches_try_catch_monadic_shape(try_node):
                    continue
                # Position the diagnostic on the `try` keyword only (narrow range).
                try_kw = next((c for c in try_node.children if c.type == "try"), try_node)
                diagnostics.append(
                    Diagnostic(
                        line=try_kw.start_point[0],
                        col=try_kw.start_point[1],
                        end_line=try_kw.end_point[0],
                        end_col=try_kw.end_point[1],
                        severity=severity,
                        code="try-catch-to-monadic",
                        message=_MESSAGES["try-catch-to-monadic"],
                        data=_DATA["try-catch-to-monadic"],
                    )
                )

        return diagnostics
