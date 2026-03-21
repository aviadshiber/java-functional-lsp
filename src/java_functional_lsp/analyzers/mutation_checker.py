"""Mutation and imperative pattern rules: detect mutable variables, loops, and imperative unwrapping."""

from __future__ import annotations

from typing import Any

from .base import Diagnostic, find_nodes, find_nodes_multi, has_ancestor, severity_from_config

_MESSAGES = {
    "mutable-variable": "Avoid reassigning variables. Use final + functional transforms (map, flatMap, fold).",
    "imperative-loop": "Replace imperative loop with .map(), .filter(), .flatMap(), or .foldLeft().",
    "mutable-dto": "Use @Value instead of @Data/@Setter for immutable DTOs.",
    "imperative-option-unwrap": "Avoid imperative unwrapping (isDefined/get). Use map(), flatMap(), or fold().",
}

_LOOP_TYPES = {"enhanced_for_statement", "for_statement", "while_statement"}
_METHOD_TYPES = {"method_declaration", "constructor_declaration", "lambda_expression"}
_CHECK_METHODS = {b"isDefined", b"isEmpty", b"isPresent", b"isNone"}


class MutationChecker:
    """Detects mutable variables, imperative loops, and imperative unwrapping patterns."""

    def analyze(self, tree: Any, source: bytes, config: dict[str, Any]) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []

        self._check_mutable_dto(tree, diagnostics, config)
        self._check_imperative_loops(tree, diagnostics, config)
        self._check_imperative_option_unwrap(tree, diagnostics, config)
        self._check_mutable_variables(tree, diagnostics, config)

        return diagnostics

    def _check_mutable_dto(self, tree: Any, diagnostics: list[Diagnostic], config: dict[str, Any]) -> None:
        """Detect @Data or @Setter annotations on classes."""
        severity = severity_from_config(config, "mutable-dto")
        if severity is None:
            return

        for node in find_nodes(tree.root_node, "marker_annotation"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            ann_text = name_node.text
            if ann_text in (b"Data", b"Setter"):
                # Verify it's on a class declaration
                if node.parent and node.parent.type == "modifiers":
                    grandparent = node.parent.parent
                    if grandparent and grandparent.type == "class_declaration":
                        diagnostics.append(
                            Diagnostic(
                                line=name_node.start_point[0],
                                col=name_node.start_point[1],
                                end_line=name_node.end_point[0],
                                end_col=name_node.end_point[1],
                                severity=severity,
                                code="mutable-dto",
                                message=_MESSAGES["mutable-dto"],
                            )
                        )

    def _check_imperative_loops(self, tree: Any, diagnostics: list[Diagnostic], config: dict[str, Any]) -> None:
        """Detect for/while loops that could be functional operations."""
        severity = severity_from_config(config, "imperative-loop")
        if severity is None:
            return

        for node in find_nodes_multi(tree.root_node, _LOOP_TYPES):
            # Skip loops inside main methods
            parent = node.parent
            while parent:
                if parent.type == "method_declaration":
                    method_name_node = parent.child_by_field_name("name")
                    if method_name_node and method_name_node.text == b"main":
                        break
                parent = parent.parent
            else:
                # Highlight just the keyword (for/while)
                keyword = node.type.split("_")[0]  # "for" or "while" or "enhanced"
                if keyword == "enhanced":
                    keyword = "for"
                diagnostics.append(
                    Diagnostic(
                        line=node.start_point[0],
                        col=node.start_point[1],
                        end_line=node.start_point[0],
                        end_col=node.start_point[1] + len(keyword),
                        severity=severity,
                        code="imperative-loop",
                        message=_MESSAGES["imperative-loop"],
                    )
                )

    def _check_imperative_option_unwrap(self, tree: Any, diagnostics: list[Diagnostic], config: dict[str, Any]) -> None:
        """Detect if(opt.isDefined()) { opt.get() } patterns."""
        severity = severity_from_config(config, "imperative-option-unwrap")
        if severity is None:
            return

        for if_node in find_nodes(tree.root_node, "if_statement"):
            condition = if_node.child_by_field_name("condition")
            if condition is None:
                continue

            # Look for method_invocation in condition
            for invocation in find_nodes(condition, "method_invocation"):
                name_node = invocation.child_by_field_name("name")
                obj_node = invocation.child_by_field_name("object")
                if name_node is None or obj_node is None:
                    continue
                if name_node.text not in _CHECK_METHODS:
                    continue

                # Check if the if-body contains .get() on the same object (AST-based)
                obj_name = obj_node.text
                consequence = if_node.child_by_field_name("consequence")
                if consequence is None or obj_name is None:
                    continue
                found_get = False
                for call in find_nodes(consequence, "method_invocation"):
                    call_name = call.child_by_field_name("name")
                    call_obj = call.child_by_field_name("object")
                    if call_name and call_name.text == b"get" and call_obj and call_obj.text == obj_name:
                        found_get = True
                        break
                if found_get:
                    diagnostics.append(
                        Diagnostic(
                            line=if_node.start_point[0],
                            col=if_node.start_point[1],
                            end_line=if_node.end_point[0],
                            end_col=if_node.end_point[1],
                            severity=severity,
                            code="imperative-option-unwrap",
                            message=_MESSAGES["imperative-option-unwrap"],
                        )
                    )
                break  # Only check first invocation in condition

    def _check_mutable_variables(self, tree: Any, diagnostics: list[Diagnostic], config: dict[str, Any]) -> None:
        """Detect local variables that are reassigned (non-final, mutated)."""
        severity = severity_from_config(config, "mutable-variable")
        if severity is None:
            return

        for node in find_nodes(tree.root_node, "assignment_expression"):
            # Only flag reassignments inside method bodies
            if not has_ancestor(node, _METHOD_TYPES):
                continue

            # Skip field_access assignments in constructors (field initialization, not reassignment)
            left = node.child_by_field_name("left")
            if left and left.type == "field_access" and has_ancestor(node, {"constructor_declaration"}):
                continue

            diagnostics.append(
                Diagnostic(
                    line=node.start_point[0],
                    col=node.start_point[1],
                    end_line=node.end_point[0],
                    end_col=node.end_point[1],
                    severity=severity,
                    code="mutable-variable",
                    message=_MESSAGES["mutable-variable"],
                )
            )
