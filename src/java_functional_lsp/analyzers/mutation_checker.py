"""Mutation and imperative pattern rules: detect mutable variables, loops, and imperative unwrapping."""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from .base import (
    IGNORED_CHILDREN,
    Diagnostic,
    DiagnosticData,
    find_nodes,
    find_nodes_multi,
    has_ancestor,
    has_sibling_annotation,
    severity_from_config,
)

_MESSAGES = {
    "mutable-variable": "Avoid reassigning variables. Use final + functional transforms (map, flatMap, fold).",
    "imperative-loop": "Replace imperative loop with .map(), .filter(), .flatMap(), or .foldLeft().",
    "mutable-dto": "Use @Value instead of @Data/@Setter for immutable DTOs.",
    "imperative-option-unwrap": "Avoid imperative unwrapping (isDefined/get). Use map(), flatMap(), or fold().",
}

_DATA = {
    "mutable-variable": DiagnosticData(
        fix_type="USE_FINAL_TRANSFORMS",
        target_library="io.vavr.collection.List",
        rationale=(
            "Reassigning variables creates temporal coupling. "
            "Declare the variable `final` and replace reassignment with functional transforms."
        ),
        # API hint only — the `final` modifier is a language feature, not an API; mentioning it
        # in `rationale` is appropriate but it doesn't belong in `recommended_api`.
        recommended_api=".map / .filter / .flatMap / .foldLeft",
    ),
    "imperative-loop": DiagnosticData(
        fix_type="USE_FUNCTIONAL_TRANSFORMS",
        target_library="io.vavr.collection.List",
        rationale=(
            "Imperative loops hide intent."
            " Use .map(), .filter(), .flatMap(), or .foldLeft() for declarative transforms."
        ),
        recommended_api=".map / .filter / .flatMap / .foldLeft",
    ),
    "mutable-dto": DiagnosticData(
        fix_type="USE_VALUE_ANNOTATION",
        target_library="lombok.Value",
        rationale="Mutable DTOs allow uncontrolled state changes. Use @Value for immutable data classes.",
        recommended_api="@Value",
    ),
    "imperative-option-unwrap": DiagnosticData(
        fix_type="USE_MAP_FLATMAP",
        target_library="io.vavr.control.Option",
        rationale="Imperative isDefined/get is error-prone. Use map(), flatMap(), or fold() for safe monadic access.",
        recommended_api="map / flatMap / fold / forEach (NOT ifPresent — Vavr Option uses forEach)",
    ),
}


def _build_mutable_dto_data(class_decl: Any) -> DiagnosticData:
    """Build a DiagnosticData with a @Value snippet using the real class name."""
    base = _DATA["mutable-dto"]
    name_node = class_decl.child_by_field_name("name") if class_decl is not None else None
    if name_node is None or not name_node.text:
        return base
    class_name = name_node.text.decode("utf-8")
    snippet = f"@Value\npublic class {class_name} {{ /* fields become final */ }}"
    return dataclasses.replace(base, suggested_snippet=snippet)


def _single_return_stmt(branch: Any) -> Any | None:
    """Return the lone return_statement of a branch (block or bare statement), else None."""
    if branch is None:
        return None
    if branch.type == "block":
        stmts = [c for c in branch.named_children if c.type not in IGNORED_CHILDREN]
    else:
        stmts = [branch]
    if len(stmts) != 1 or stmts[0].type != "return_statement":
        return None
    return stmts[0]


def _return_expr_text(return_stmt: Any) -> str | None:
    """Return the expression text of a `return <expr>;` statement, else None."""
    children = [c for c in return_stmt.named_children if c.type not in IGNORED_CHILDREN]
    if not children or not children[0].text:
        return None
    text: str = children[0].text.decode("utf-8")
    return text


def _build_imperative_option_unwrap_data(obj_name: bytes, consequence: Any, else_branch: Any) -> DiagnosticData:
    """Build a DiagnosticData carrying a concrete snippet for imperative Option unwrap.

    Uses the real variable name from the AST. Snippet shape depends on whether the
    if-body is a return (use map+getOrElse) or a side-effect statement (use forEach).
    """
    base = _DATA["imperative-option-unwrap"]
    var = obj_name.decode("utf-8") if obj_name else "opt"

    # `return opt.get();` shape — map/getOrElse fits. Otherwise (statement-style
    # consumer), forEach is the right hint.
    then_return = _single_return_stmt(consequence)
    if then_return is None:
        return dataclasses.replace(base, suggested_snippet=f"{var}.forEach(value -> {{ /* use value */ }});")

    default_text = "default"
    else_return = _single_return_stmt(else_branch)
    if else_return is not None:
        default_text = _return_expr_text(else_return) or default_text

    # Derive the lambda body from the real then-branch expression (issue #74 #2):
    # `return opt.get().toUpperCase();` becomes `.map(value -> value.toUpperCase())`.
    # Mirrors the rewrite in fixes.fix_imperative_option_unwrap — duplicated rather
    # than imported because analyzers must not depend on fixes (fixes already imports
    # from analyzers; the reverse edge would create a cycle).
    lambda_body = "value"
    ret_text = _return_expr_text(then_return)
    if ret_text is not None and ret_text != f"{var}.get()":
        rewritten = re.sub(rf"\b{re.escape(var)}\.get\(\)", "value", ret_text)
        # Keep the identity placeholder when no `var.get()` was found to rewrite —
        # the shape isn't one we can synthesise safely.
        if rewritten != ret_text:
            lambda_body = rewritten

    snippet = f"return {var}.map(value -> {lambda_body}).getOrElse({default_text});"
    return dataclasses.replace(base, suggested_snippet=snippet)


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
                    modifiers = node.parent
                    grandparent = modifiers.parent
                    if grandparent and grandparent.type == "class_declaration":
                        if has_sibling_annotation(modifiers, b"ConfigurationProperties"):
                            message = (
                                "Use @ConstructorBinding instead of @Data/@Setter for @ConfigurationProperties classes."
                            )
                        else:
                            message = _MESSAGES["mutable-dto"]
                        diagnostics.append(
                            Diagnostic(
                                line=name_node.start_point[0],
                                col=name_node.start_point[1],
                                end_line=name_node.end_point[0],
                                end_col=name_node.end_point[1],
                                severity=severity,
                                code="mutable-dto",
                                message=message,
                                data=_build_mutable_dto_data(grandparent),
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
                        data=_DATA["imperative-loop"],
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
                    alternative = if_node.child_by_field_name("alternative")
                    diagnostics.append(
                        Diagnostic(
                            line=if_node.start_point[0],
                            col=if_node.start_point[1],
                            end_line=if_node.end_point[0],
                            end_col=if_node.end_point[1],
                            severity=severity,
                            code="imperative-option-unwrap",
                            message=_MESSAGES["imperative-option-unwrap"],
                            data=_build_imperative_option_unwrap_data(obj_name, consequence, alternative),
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

            # Skip this.field = ... in constructors (field initialization, not reassignment)
            left = node.child_by_field_name("left")
            if left and left.type == "field_access" and has_ancestor(node, {"constructor_declaration"}):
                receiver = left.child_by_field_name("object")
                if receiver and receiver.type == "this":
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
                    data=_DATA["mutable-variable"],
                )
            )
