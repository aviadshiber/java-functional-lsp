"""Exception handling rules: detect throw statements and catch-rethrow patterns."""

from __future__ import annotations

from typing import Any

from .base import (
    IGNORED_CHILDREN,
    Diagnostic,
    DiagnosticData,
    Severity,
    collect_nodes_by_type,
    has_sibling_annotation,
    severity_from_config,
)

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
        recommended_api="Either.left(...) / Try.failure(...)",
    ),
    "catch-rethrow": DiagnosticData(
        fix_type="USE_TRY_TO_EITHER",
        target_library="io.vavr.control.Try",
        rationale=(
            "Catching and rethrowing adds noise. Use Try.of(() -> ...).toEither() to convert exceptions to values."
        ),
        recommended_api="Try.of(() -> ...).toEither()",
    ),
    "try-catch-to-monadic": DiagnosticData(
        fix_type="WRAP_IN_TRY",
        target_library="io.vavr.control.Try",
        rationale=(
            "try/catch mixes control flow with value handling."
            " Use Try.of(...) for composable, value-based failure handling."
        ),
        recommended_api="Try.of(() -> ...).getOrElse(...) / .recover(Type.class, e -> ...).get()",
    ),
}


def _build_throw_statement_data(throw_node: Any) -> DiagnosticData:
    """Build a DiagnosticData with a concrete Either.left/Try.failure snippet.

    Reads the throw expression text from the AST so the snippet preserves the
    real exception construction (e.g. ``new IllegalArgumentException("x")``).
    """
    base = _DATA["throw-statement"]
    expr_text = "error"
    # throw_statement has a single expression child (the exception being thrown).
    for child in throw_node.named_children:
        if child.type not in ("line_comment", "block_comment"):
            if child.text:
                expr_text = child.text.decode("utf-8")
            break
    snippet = f"return Either.left({expr_text});  // or: return Try.failure({expr_text});"
    return DiagnosticData(
        fix_type=base.fix_type,
        target_library=base.target_library,
        rationale=base.rationale,
        recommended_api=base.recommended_api,
        suggested_snippet=snippet,
    )


def _extract_return_expr(block_node: Any) -> str | None:
    """Return the text of the (last) return-statement expression in a block, or None."""
    if block_node is None:
        return None
    stmts = [c for c in block_node.named_children if c.type not in IGNORED_CHILDREN]
    if not stmts or stmts[-1].type != "return_statement":
        return None
    ret_children = [c for c in stmts[-1].named_children if c.type not in IGNORED_CHILDREN]
    if not ret_children or not ret_children[0].text:
        return None
    decoded: str = ret_children[0].text.decode("utf-8")
    return decoded


def _build_try_catch_to_monadic_data(try_node: Any) -> DiagnosticData:
    """Build a DiagnosticData with a Try.of(...).getOrElse(...) snippet drawn from the AST.

    Best-effort: when the try/catch shape isn't a clean single-return-each pair, the
    base data is returned without a snippet rather than synthesising garbage.
    """
    base = _DATA["try-catch-to-monadic"]
    body = try_node.child_by_field_name("body")
    catches = [c for c in try_node.children if c.type == "catch_clause"]
    if body is None or not catches:
        return base

    try_expr = _extract_return_expr(body)
    catch_expr = _extract_return_expr(catches[0].child_by_field_name("body"))
    if try_expr is None or catch_expr is None:
        return base

    snippet = f"return Try.of(() -> {try_expr}).getOrElse({catch_expr});"
    return DiagnosticData(
        fix_type=base.fix_type,
        target_library=base.target_library,
        rationale=base.rationale,
        recommended_api=base.recommended_api,
        suggested_snippet=snippet,
    )


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


def _matches_try_catch_monadic_shape(try_node: Any) -> bool:  # noqa: PLR0911, PLR0912
    """Verify a try_statement has a shape the try-catch-to-monadic fix can rewrite.

    Requirements:
    - No ``resource_specification`` child (try-with-resources would lose auto-close semantics)
    - No ``finally_clause`` child
    - Exactly one ``catch_clause`` child
    - Catch clause must use a single exception type (no union-catch ``A | B e``)
    - Try body is a block with a single ``return_statement`` that has an expression
    - Catch body is a block whose last named child is a ``return_statement`` with an expression,
      and any prior named children are ``expression_statement`` (for the Pattern 2 logging case)

    Guard-clause returns are preferred for readability; noqa silences the
    "too many returns" rule since each branch fails a distinct shape check.

    Note: This analyzer-side check must stay in sync with
    ``_validate_and_extract_try_catch_parts`` in fixes.py — any shape the
    analyzer accepts must also be rewritable by the fix, otherwise users
    see a diagnostic with no working code action.
    """
    # Single pass over try_node.children: detect finally, resource_specification, and catches
    has_finally = False
    has_resources = False
    catches: list[Any] = []
    for c in try_node.children:
        if c.type == "finally_clause":
            has_finally = True
        elif c.type == "resource_specification":
            has_resources = True
        elif c.type == "catch_clause":
            catches.append(c)

    if has_finally or has_resources:
        return False
    if len(catches) != 1:
        return False

    # Reject union-catch (A | B e). Tree-sitter encodes the type in catch_formal_parameter
    # as either a single type node or a catch_type node containing multiple type_identifier children.
    catch = catches[0]
    param = next((c for c in catch.children if c.type == "catch_formal_parameter"), None)
    if param is None:
        return False
    catch_type_node = next((c for c in param.children if c.type == "catch_type"), None)
    if catch_type_node is not None and catch_type_node.text and b"|" in catch_type_node.text:
        return False

    # Try body must be a block with a single return_statement with an expression
    body = try_node.child_by_field_name("body")
    if body is None or body.type != "block":
        return False
    body_stmts = [c for c in body.named_children if c.type not in IGNORED_CHILDREN]
    if len(body_stmts) != 1 or body_stmts[0].type != "return_statement":
        return False
    # Return must have an expression (not bare `return;`)
    ret_children = [c for c in body_stmts[0].named_children if c.type not in IGNORED_CHILDREN]
    if not ret_children:
        return False

    # Catch body must end with a return_statement; prior stmts must be expression_statements
    catch_body = catch.child_by_field_name("body")
    if catch_body is None or catch_body.type != "block":
        return False
    catch_stmts = [c for c in catch_body.named_children if c.type not in IGNORED_CHILDREN]
    if not catch_stmts or catch_stmts[-1].type != "return_statement":
        return False
    if any(s.type != "expression_statement" for s in catch_stmts[:-1]):
        return False
    # The catch return must have an expression (not bare `return;`)
    catch_ret_children = [c for c in catch_stmts[-1].named_children if c.type not in IGNORED_CHILDREN]
    if not catch_ret_children:
        return False

    return True


class ExceptionChecker:
    """Detects throw statements and catch-rethrow anti-patterns."""

    def analyze(self, tree: Any, source: bytes, config: dict[str, Any]) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []

        # Single tree walk collecting all three node types we care about.
        # Previously this method did three separate find_nodes walks, which is 3x the traversal
        # cost on every analysis pass (LSP re-runs on every document change).
        buckets = collect_nodes_by_type(tree.root_node, {"throw_statement", "catch_clause", "try_statement"})

        # Rule: throw-statement
        severity = severity_from_config(config, "throw-statement")
        if severity is not None:
            for node in buckets["throw_statement"]:
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
                        data=_build_throw_statement_data(node),
                    )
                )

        # Rule: catch-rethrow
        severity = severity_from_config(config, "catch-rethrow")
        if severity is not None:
            for node in buckets["catch_clause"]:
                if _is_in_bean_method(node):
                    continue
                body = node.child_by_field_name("body")
                if body is None:
                    continue
                statements = [c for c in body.named_children if c.type not in IGNORED_CHILDREN]
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
            for try_node in buckets["try_statement"]:
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
                        data=_build_try_catch_to_monadic_data(try_node),
                    )
                )

        return diagnostics
