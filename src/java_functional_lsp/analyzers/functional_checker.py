"""Functional semantic analysis rules: frozen mutation, null-check monadic flow, purity extraction."""

from __future__ import annotations

import re
from typing import Any, Literal

from tree_sitter import Node, Tree

from .base import (
    Diagnostic,
    DiagnosticData,
    Severity,
    extract_null_check_var,
    find_nodes,
    find_nodes_multi,
    severity_from_config,
)

_MESSAGES = {
    "frozen-mutation": (
        "Runtime Exception Risk: Mutating a frozen structure (e.g. List.of()). "
        "Use io.vavr.collection.List for safe persistent immutability."
    ),
    "null-check-to-monadic": ("Imperative null handling: Consider monadic flow with Option.of().map().getOrElse()."),
    "impure-method-io": (
        "Hidden side-effect: Method mixes pure logic with IO/state mutations. "
        "Extract pure logic to a separate method; wrap side-effects in Try."
    ),
    "impure-method-throw": (
        "Hidden side-effect: Method mixes pure logic with exceptions. "
        "Extract pure logic; return Either.left(...) or Try.failure(...) instead of throwing."
    ),
}

_DATA = {
    "frozen-mutation": DiagnosticData(
        fix_type="REPLACE_WITH_VAVR_LIST",
        target_library="io.vavr.collection.List",
        rationale=(
            "Runtime mutation of List.of() causes UnsupportedOperationException. "
            "Use Vavr for safe, persistent immutability."
        ),
        recommended_api=".append / .appendAll / .update / .remove (returns a new persistent collection)",
    ),
    "null-check-to-monadic": DiagnosticData(
        fix_type="WRAP_IN_OPTION_MAP",
        target_library="io.vavr.control.Option",
        rationale=(
            "Imperative null checks create nested branching. "
            "Use Option.of().map() for composable, null-safe monadic flow."
        ),
        recommended_api="Option.of(...).map(...).getOrElse(...)",
    ),
    "impure-method-io": DiagnosticData(
        fix_type="EXTRACT_PURE_LOGIC",
        target_library="io.vavr.control.Try",
        rationale=(
            "Mixing pure logic with IO/state mutations breaks referential transparency. "
            "Extract pure logic; wrap IO/state mutations in Try."
        ),
        recommended_api="Try.of(() -> sideEffect()).onFailure(...)",
    ),
    "impure-method-throw": DiagnosticData(
        fix_type="EXTRACT_PURE_LOGIC",
        target_library="io.vavr.control.Either",
        rationale=(
            "Mixing pure logic with exceptions breaks referential transparency. "
            "Extract pure logic; return Either.left(...) or Try.failure(...) instead of throwing."
        ),
        recommended_api="Either.left(...) / Try.of(() -> ...)",
    ),
}


def _build_null_check_to_monadic_data(
    var_name: bytes, consequence: Node | None, alternative: Node | None, if_node: Node | None = None
) -> DiagnosticData:
    """Build an Option snippet using the real var name + real return expressions.

    Inspects the if-then return expression (the body of ``if (x != null) return X.something();``)
    and rewrites references to ``var_name`` as ``it`` in a ``.map(it -> ...)`` lambda. The else
    branch's return expression — either nested in the if as an alternative or following the if
    as a fallthrough — becomes the ``.getOrElse(...)`` argument. Falls back to the base data
    (no snippet) when the then-branch isn't a single return.
    """
    base = _DATA["null-check-to-monadic"]
    if not var_name:
        return base
    var = var_name.decode("utf-8")

    then_expr = _single_return_expr_text(consequence)
    if then_expr is None:
        return base
    # Rewrite references to the checked variable as `it` in the lambda body. Use a word-boundary
    # regex so a short var name like `s` doesn't match `s.toString()` inside another identifier.
    # Skip the map when the body is exactly the variable itself (identity case).
    if then_expr == var:
        chain_body = f"Option.of({var})"
    else:
        lambda_body = re.sub(rf"\b{re.escape(var)}\b", "it", then_expr)
        chain_body = f"Option.of({var}).map(it -> {lambda_body})"

    # Prefer the nested else-branch; otherwise look at the statement immediately following the if
    # (a common fallthrough pattern: `if (x != null) return ...; return fallback;`).
    else_expr = _single_return_expr_text(alternative)
    if else_expr is None and if_node is not None:
        else_expr = _next_statement_return_expr(if_node)

    if else_expr is None or else_expr == "null":
        # No else (or else returns null) — return Option<T> directly; don't fabricate a default.
        snippet = f"return {chain_body};"
    else:
        snippet = f"return {chain_body}.getOrElse({else_expr});"

    return DiagnosticData(
        fix_type=base.fix_type,
        target_library=base.target_library,
        rationale=base.rationale,
        recommended_api=base.recommended_api,
        suggested_snippet=snippet,
    )


def _next_statement_return_expr(if_node: Node) -> str | None:
    """If the statement immediately after ``if_node`` (in its enclosing block) is a single
    `return <expr>;`, return that expression's text. Otherwise None."""
    parent = if_node.parent
    if parent is None or parent.type != "block":
        return None
    siblings = [c for c in parent.named_children if c.type not in ("line_comment", "block_comment")]
    try:
        idx = siblings.index(if_node)
    except ValueError:
        return None
    if idx + 1 >= len(siblings):
        return None
    next_stmt = siblings[idx + 1]
    return _single_return_expr_text(next_stmt)


def _single_return_expr_text(block_or_stmt: Node | None) -> str | None:
    """Return the text of a single ``return <expr>;`` statement in a block (or the statement
    itself). Returns None for anything else (multiple statements, no return, bare return)."""
    if block_or_stmt is None:
        return None
    if block_or_stmt.type == "block":
        stmts = [c for c in block_or_stmt.named_children if c.type not in ("line_comment", "block_comment")]
    else:
        stmts = [block_or_stmt]
    if len(stmts) != 1 or stmts[0].type != "return_statement":
        return None
    ret_children = [c for c in stmts[0].named_children if c.type not in ("line_comment", "block_comment")]
    if not ret_children or not ret_children[0].text:
        return None
    decoded: str = ret_children[0].text.decode("utf-8")
    return decoded


def _build_frozen_mutation_data(method_name: bytes, var_name: bytes) -> DiagnosticData:
    """Build a DiagnosticData carrying a concrete Vavr-persistent snippet using real names."""
    base = _DATA["frozen-mutation"]
    var = var_name.decode("utf-8") if var_name else "items"
    vavr_method_map = {
        b"add": "append",
        b"addAll": "appendAll",
        b"remove": "remove",
        b"set": "update",
        b"sort": "sorted",
    }
    vavr_method = vavr_method_map.get(method_name, "append") if method_name else "append"
    snippet = f"{var} = {var}.{vavr_method}(...);  // returns a new persistent collection"
    return DiagnosticData(
        fix_type=base.fix_type,
        target_library=base.target_library,
        rationale=base.rationale,
        recommended_api=base.recommended_api,
        suggested_snippet=snippet,
    )


# Factory methods that produce frozen (unmodifiable) collections
_FROZEN_FACTORIES = {
    b"of",  # List.of(), Set.of(), Map.of()
    b"copyOf",  # List.copyOf(), Set.copyOf()
}

# Qualifier types that produce frozen collections when calling _FROZEN_FACTORIES
_FROZEN_QUALIFIERS = {
    b"List",
    b"Set",
    b"Map",
    # Guava immutable collections (same frozen semantics as List.of / Set.of)
    b"ImmutableList",
    b"ImmutableSet",
    b"ImmutableMap",
    b"ImmutableSortedSet",
    b"ImmutableSortedMap",
    b"ImmutableMultiset",
}

# Collections utility methods that produce frozen wrappers
_COLLECTIONS_FROZEN_METHODS = {
    b"unmodifiableList",
    b"unmodifiableSet",
    b"unmodifiableMap",
    b"unmodifiableSortedSet",
    b"unmodifiableSortedMap",
    b"emptyList",
    b"emptySet",
    b"emptyMap",
    b"singletonList",
    b"singleton",
    b"singletonMap",
}

# Mutation methods that will throw on frozen collections
_MUTATION_METHODS = {
    b"add",
    b"addAll",
    b"remove",
    b"removeAll",
    b"set",
    b"put",
    b"putAll",
    b"clear",
    b"sort",
    b"replaceAll",
    b"retainAll",
}

# Side-effect indicators for purity analysis
_SIDE_EFFECT_RECEIVERS = {b"System", b"logger", b"log", b"LOG"}
_SIDE_EFFECT_METHODS = {
    b"println",
    b"print",
    b"printf",
    b"write",
    b"flush",
    b"close",
    b"send",
    b"save",
    b"delete",
    b"insert",
    b"update",
    b"execute",
    b"info",
    b"warn",
    b"error",
    b"debug",
    b"trace",
}

_METHOD_SCOPES = {"method_declaration", "constructor_declaration", "lambda_expression"}


def is_side_effect_invocation(invocation: Node) -> bool:
    """Check if a method_invocation node is a side-effect call.

    Recognizes patterns like:
    - ``System.out.println(...)`` / ``System.err.println(...)``
    - ``logger.info(...)`` / ``log.debug(...)`` / ``LOG.warn(...)``
    - Bare side-effect method names (e.g. ``println(...)``)
    """
    obj_node = invocation.child_by_field_name("object")
    method_name = invocation.child_by_field_name("name")
    if method_name is None:
        return False

    if obj_node is not None:
        # System.out.println / System.err.println
        if obj_node.type == "field_access":
            receiver = obj_node.child_by_field_name("object")
            if receiver is not None and receiver.text in _SIDE_EFFECT_RECEIVERS:
                return True
        # logger.info, log.debug, etc.
        if obj_node.type == "identifier" and obj_node.text in _SIDE_EFFECT_RECEIVERS:
            if method_name.text in _SIDE_EFFECT_METHODS:
                return True

    # Standalone side-effect method names
    if method_name.text in _SIDE_EFFECT_METHODS and obj_node is None:
        return True

    return False


class FunctionalChecker:
    """Detects frozen mutation traps, imperative null checks, and impure methods."""

    def analyze(self, tree: Tree, source: bytes, config: dict[str, Any]) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []

        self._check_frozen_mutation(tree, diagnostics, config)
        self._check_null_check_to_monadic(tree, diagnostics, config)
        self._check_impure_method(tree, diagnostics, config)

        return diagnostics

    def _check_frozen_mutation(self, tree: Tree, diagnostics: list[Diagnostic], config: dict[str, Any]) -> None:
        """Detect mutation calls on frozen collections (List.of(), Collections.unmodifiable*, etc.)."""
        severity = severity_from_config(config, "frozen-mutation")
        if severity is None:
            return

        # Scan method bodies for frozen variable declarations and subsequent mutations
        for method in find_nodes_multi(tree.root_node, _METHOD_SCOPES):
            frozen_vars: set[bytes] = set()

            # Pass 1: Find variables assigned from frozen factories
            for decl in find_nodes(method, "variable_declarator"):
                name_node = decl.child_by_field_name("name")
                value_node = decl.child_by_field_name("value")
                if name_node is None or value_node is None:
                    continue
                if self._is_frozen_init(value_node) and name_node.text:
                    frozen_vars.add(name_node.text)

            if not frozen_vars:
                continue

            # Pass 2: Find mutation method calls on frozen variables
            for invocation in find_nodes(method, "method_invocation"):
                method_name = invocation.child_by_field_name("name")
                obj_node = invocation.child_by_field_name("object")
                if method_name is None or obj_node is None:
                    continue
                if method_name.text in _MUTATION_METHODS and obj_node.text in frozen_vars:
                    diagnostics.append(
                        Diagnostic(
                            line=invocation.start_point[0],
                            col=invocation.start_point[1],
                            end_line=invocation.end_point[0],
                            end_col=invocation.end_point[1],
                            severity=severity,
                            code="frozen-mutation",
                            message=_MESSAGES["frozen-mutation"],
                            data=_build_frozen_mutation_data(
                                method_name.text or b"",
                                obj_node.text or b"",
                            ),
                        )
                    )

    def _is_frozen_init(self, value_node: Node) -> bool:
        """Check if a value node is a frozen collection factory call."""
        if value_node.type != "method_invocation":
            return False
        name_node = value_node.child_by_field_name("name")
        obj_node = value_node.child_by_field_name("object")
        if name_node is None or obj_node is None:
            return False
        # List.of(), Set.of(), Map.of()
        if obj_node.text in _FROZEN_QUALIFIERS and name_node.text in _FROZEN_FACTORIES:
            return True
        # Collections.unmodifiableList(), etc.
        if obj_node.text == b"Collections" and name_node.text in _COLLECTIONS_FROZEN_METHODS:
            return True
        return False

    def _check_null_check_to_monadic(self, tree: Tree, diagnostics: list[Diagnostic], config: dict[str, Any]) -> None:
        """Detect if (x != null) { return x.something(); } patterns."""
        severity = severity_from_config(config, "null-check-to-monadic")
        if severity is None:
            return

        for if_node in find_nodes(tree.root_node, "if_statement"):
            condition = if_node.child_by_field_name("condition")
            if condition is None:
                continue

            # Look for binary expression: x != null or null != x
            checked_var = extract_null_check_var(condition)
            if checked_var is None:
                continue

            # Check that the if-body is a simple single-statement block using the checked var
            consequence = if_node.child_by_field_name("consequence")
            if consequence is None:
                continue

            statements = [c for c in consequence.named_children if c.type not in ("line_comment", "block_comment")]
            if len(statements) != 1:
                continue

            stmt = statements[0]
            # Must be a return_statement: expression_statement case is not yet fixable
            # (no automatic rewrite is available for side-effect-only bodies).
            if stmt.type != "return_statement":
                continue

            # Verify the statement references the checked variable
            if not self._references_var(stmt, checked_var):
                continue

            # Suppress inner if_nodes that are part of an outer chained null-check.
            # Walk: if_node → parent block → check if that block is the "alternative"
            # of an outer if_statement that null-checks the same variable.
            parent_block = if_node.parent
            if parent_block is not None and parent_block.type == "block":
                outer_if = parent_block.parent
                if outer_if is not None and outer_if.type == "if_statement":
                    alt_node = outer_if.child_by_field_name("alternative")
                    if alt_node is not None and alt_node == parent_block:
                        outer_condition = outer_if.child_by_field_name("condition")
                        if outer_condition is not None and extract_null_check_var(outer_condition) == checked_var:
                            continue  # outer if already carries the diagnostic

            diagnostics.append(
                Diagnostic(
                    line=if_node.start_point[0],
                    col=if_node.start_point[1],
                    end_line=if_node.end_point[0],
                    end_col=if_node.end_point[1],
                    severity=severity,
                    code="null-check-to-monadic",
                    message=_MESSAGES["null-check-to-monadic"],
                    data=_build_null_check_to_monadic_data(
                        checked_var,
                        consequence,
                        if_node.child_by_field_name("alternative"),
                        if_node,
                    ),
                )
            )

    def _references_var(self, node: Node, var_name: bytes) -> bool:
        """Check if a node (or descendants) references a given variable name.

        Uses TreeCursor for traversal to avoid Python object allocation per child
        (tree-sitter best practice for performance on large ASTs).
        """
        cursor = node.walk()
        visited_children = False
        while True:
            if not visited_children:
                current: Node | None = cursor.node
                if current is not None:
                    if current.type == "identifier" and current.text == var_name:
                        return True
                    # Check method_invocation receiver explicitly
                    if current.type == "method_invocation":
                        obj = current.child_by_field_name("object")
                        if obj is not None and obj.type == "identifier" and obj.text == var_name:
                            return True
                if not cursor.goto_first_child():
                    visited_children = True
            elif cursor.goto_next_sibling():
                visited_children = False
            elif not cursor.goto_parent():
                break
        return False

    def _check_impure_method(self, tree: Tree, diagnostics: list[Diagnostic], config: dict[str, Any]) -> None:
        """Detect methods mixing pure logic with side-effects.

        Points the diagnostic at the first offending statement (throw or IO call) rather
        than the method declaration, so the user immediately sees what to extract.
        The message + data payload differ based on whether the side-effect is a throw
        or an IO call — agents need to suggest the right Vavr type (Either vs Try).
        """
        default = Severity.WARNING if config.get("strictPurity", False) else Severity.HINT
        severity = severity_from_config(config, "impure-method", default=default)
        if severity is None:
            return

        for method in find_nodes(tree.root_node, "method_declaration"):
            body = method.child_by_field_name("body")
            if body is None:
                continue

            statements = [c for c in body.named_children if c.type not in ("line_comment", "block_comment")]
            if len(statements) < 2:  # noqa: PLR2004
                continue  # Need at least 2 statements to have a mix

            offender: Node | None = None
            offender_kind: Literal["throw", "io"] | None = None
            has_pure_logic = False

            for stmt in statements:
                kind, node = self._classify_side_effect(stmt)
                if kind is None:
                    has_pure_logic = True
                elif offender is None:
                    offender = node
                    offender_kind = kind

            if offender is None or not has_pure_logic:
                continue

            name_node = method.child_by_field_name("name")
            # Range = the offending statement itself, not the method declaration.
            range_node = offender if offender is not None else name_node
            if range_node is None:
                continue

            if offender_kind == "throw":
                message = _MESSAGES["impure-method-throw"]
                data = _DATA["impure-method-throw"]
            else:
                message = _MESSAGES["impure-method-io"]
                data = _DATA["impure-method-io"]

            diagnostics.append(
                Diagnostic(
                    line=range_node.start_point[0],
                    col=range_node.start_point[1],
                    end_line=range_node.end_point[0],
                    end_col=range_node.end_point[1],
                    severity=severity,
                    code="impure-method",
                    message=message,
                    data=data,
                )
            )

    def _classify_side_effect(self, stmt: Node) -> tuple[Literal["throw", "io"] | None, Node | None]:
        """Find the first side-effect node within a statement subtree.

        Returns (kind, node) where kind is "throw", "io", or None (if the statement
        contains no side-effects). The node is the offending throw_statement or
        method_invocation — used to position the diagnostic. The Literal annotation
        catches typos in the discriminator at type-check time.

        Single TreeCursor traversal to detect both throws and IO calls in one pass.
        """
        cursor = stmt.walk()
        visited_children = False
        while True:
            if not visited_children:
                current: Node | None = cursor.node
                if current is not None:
                    if current.type == "throw_statement":
                        return "throw", current
                    if current.type == "method_invocation" and is_side_effect_invocation(current):
                        return "io", current
                if not cursor.goto_first_child():
                    visited_children = True
            elif cursor.goto_next_sibling():
                visited_children = False
            elif not cursor.goto_parent():
                break
        return None, None
