"""Functional semantic analysis rules: frozen mutation, null-check monadic flow, purity extraction."""

from __future__ import annotations

from typing import Any

from .base import (
    Diagnostic,
    DiagnosticData,
    find_nodes,
    find_nodes_multi,
    severity_from_config,
)

_MESSAGES = {
    "frozen-mutation": (
        "Runtime Exception Risk: Mutating a frozen structure (e.g. List.of()). "
        "Use io.vavr.collection.List for safe persistent immutability."
    ),
    "null-check-to-monadic": ("Imperative null handling: Consider monadic flow with Option.of().map().getOrNull()."),
    "impure-method": (
        "Hidden side-effect: Method mixes pure logic with IO/state mutations. "
        "Extract pure logic to a separate method; wrap side-effects in Try."
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
    ),
    "null-check-to-monadic": DiagnosticData(
        fix_type="WRAP_IN_OPTION_MAP",
        target_library="io.vavr.control.Option",
        rationale=(
            "Imperative null checks create nested branching. "
            "Use Option.of().map() for composable, null-safe monadic flow."
        ),
    ),
    "impure-method": DiagnosticData(
        fix_type="EXTRACT_PURE_LOGIC",
        target_library="io.vavr.control.Try",
        rationale=(
            "Mixing pure logic with side-effects breaks referential transparency. "
            "Extract pure logic; wrap IO/state mutations in Try."
        ),
    ),
}

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


class FunctionalChecker:
    """Detects frozen mutation traps, imperative null checks, and impure methods."""

    def analyze(self, tree: Any, source: bytes, config: dict[str, Any]) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []

        self._check_frozen_mutation(tree, diagnostics, config)
        self._check_null_check_to_monadic(tree, diagnostics, config)
        self._check_impure_method(tree, diagnostics, config)

        return diagnostics

    def _check_frozen_mutation(self, tree: Any, diagnostics: list[Diagnostic], config: dict[str, Any]) -> None:
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
                if self._is_frozen_init(value_node):
                    frozen_vars.add(name_node.text or b"")

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
                            data=_DATA["frozen-mutation"],
                        )
                    )

    def _is_frozen_init(self, value_node: Any) -> bool:
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

    def _check_null_check_to_monadic(self, tree: Any, diagnostics: list[Diagnostic], config: dict[str, Any]) -> None:
        """Detect if (x != null) { return x.something(); } patterns."""
        severity = severity_from_config(config, "null-check-to-monadic")
        if severity is None:
            return

        for if_node in find_nodes(tree.root_node, "if_statement"):
            condition = if_node.child_by_field_name("condition")
            if condition is None:
                continue

            # Look for binary expression: x != null or null != x
            checked_var = self._extract_null_check_var(condition)
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
            # Must be a return statement or expression statement referencing the checked var
            if stmt.type not in ("return_statement", "expression_statement"):
                continue

            # Verify the statement references the checked variable
            if not self._references_var(stmt, checked_var):
                continue

            diagnostics.append(
                Diagnostic(
                    line=if_node.start_point[0],
                    col=if_node.start_point[1],
                    end_line=if_node.end_point[0],
                    end_col=if_node.end_point[1],
                    severity=severity,
                    code="null-check-to-monadic",
                    message=_MESSAGES["null-check-to-monadic"],
                    data=_DATA["null-check-to-monadic"],
                )
            )

    def _extract_null_check_var(self, condition: Any) -> bytes | None:
        """Extract the variable name from a `x != null` or `null != x` condition."""
        # The condition might be wrapped in parenthesized_expression
        node = condition
        if node.type == "parenthesized_expression" and node.named_child_count == 1:
            node = node.named_children[0]

        if node.type != "binary_expression":
            return None

        # Determine the operator — tree-sitter may store it as named field or unnamed child
        op_text = self._get_binary_operator(node)
        if op_text != b"!=":
            return None

        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None:
            return None

        # x != null or null != x
        var_node = left if right.type == "null_literal" else (right if left.type == "null_literal" else None)
        if var_node is not None and var_node.type == "identifier":
            val: bytes | None = var_node.text
            return val
        return None

    @staticmethod
    def _get_binary_operator(node: Any) -> bytes | None:
        """Extract the operator text from a binary_expression node."""
        operator = node.child_by_field_name("operator")
        if operator is not None and hasattr(operator, "text") and operator.text:
            result: bytes = operator.text
            return result
        # Fallback: check unnamed children
        for child in node.children:
            if child.type in ("!=", "=="):
                op: bytes = child.type.encode() if isinstance(child.type, str) else child.type
                return op
        return None

    def _references_var(self, node: Any, var_name: bytes) -> bool:
        """Check if a node (or descendants) references a given variable name."""
        if node.type == "identifier" and node.text == var_name:
            return True
        # Check method_invocation receiver
        if node.type == "method_invocation":
            obj = node.child_by_field_name("object")
            if obj is not None and obj.type == "identifier" and obj.text == var_name:
                return True
        for child in node.named_children:
            if self._references_var(child, var_name):
                return True
        return False

    def _check_impure_method(self, tree: Any, diagnostics: list[Diagnostic], config: dict[str, Any]) -> None:
        """Detect methods mixing pure logic with side-effects."""
        severity = severity_from_config(config, "impure-method")
        if severity is None:
            return

        strict = config.get("strictPurity", False)
        if strict:
            severity_to_use = severity_from_config(config, "impure-method", default=severity) or severity
        else:
            severity_to_use = severity

        for method in find_nodes(tree.root_node, "method_declaration"):
            body = method.child_by_field_name("body")
            if body is None:
                continue

            has_side_effect = False
            has_pure_logic = False

            statements = [c for c in body.named_children if c.type not in ("line_comment", "block_comment")]
            if len(statements) < 2:  # noqa: PLR2004
                continue  # Need at least 2 statements to have a mix

            for stmt in statements:
                if self._is_side_effect_statement(stmt):
                    has_side_effect = True
                else:
                    has_pure_logic = True

            if has_side_effect and has_pure_logic:
                name_node = method.child_by_field_name("name")
                if name_node is None:
                    continue
                diagnostics.append(
                    Diagnostic(
                        line=name_node.start_point[0],
                        col=name_node.start_point[1],
                        end_line=name_node.end_point[0],
                        end_col=name_node.end_point[1],
                        severity=severity_to_use,
                        code="impure-method",
                        message=_MESSAGES["impure-method"],
                        data=_DATA["impure-method"],
                    )
                )

    def _is_side_effect_statement(self, stmt: Any) -> bool:
        """Check if a statement contains side-effect calls."""
        for invocation in find_nodes(stmt, "method_invocation"):
            obj_node = invocation.child_by_field_name("object")
            method_name = invocation.child_by_field_name("name")
            if method_name is None:
                continue

            # Direct side-effect methods (e.g., System.out.println)
            if obj_node is not None:
                # Check for System.out.println / System.err.println
                if obj_node.type == "field_access":
                    receiver = obj_node.child_by_field_name("object")
                    if receiver is not None and receiver.text in _SIDE_EFFECT_RECEIVERS:
                        return True
                # Check for logger.info, log.debug, etc.
                if obj_node.type == "identifier" and obj_node.text in _SIDE_EFFECT_RECEIVERS:
                    if method_name.text in _SIDE_EFFECT_METHODS:
                        return True

            # Standalone side-effect method names
            if method_name.text in _SIDE_EFFECT_METHODS and obj_node is None:
                return True

        # throw statements are side-effects
        for _ in find_nodes(stmt, "throw_statement"):
            return True

        return False
