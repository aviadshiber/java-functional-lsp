"""Null safety rules: detect null literals in arguments, returns, and assignments."""

from __future__ import annotations

from typing import Any

from .base import Diagnostic, DiagnosticData, find_nodes, severity_from_config

_MESSAGES = {
    "null-literal-arg": "Avoid passing null as argument. Use Option.none(), a default value, or overload the method.",
    "null-return": "Avoid returning null. Use Option.of(), Option.none(), or Either<Error, T>.",
    "null-assignment": "Avoid assigning null to local variables. Use Option<T> to represent absence.",
    "null-field-assignment": "Avoid null field initializers. Use Option<T> with Option.none() for optional fields.",
}

_DATA = {
    "null-literal-arg": DiagnosticData(
        fix_type="WRAP_IN_OPTION_NONE",
        target_library="io.vavr.control.Option",
        rationale="Passing null propagates unsafe references. Use Option.none() to represent absence explicitly.",
        recommended_api="Option.none()",
        suggested_snippet="Option.none()",
    ),
    "null-return": DiagnosticData(
        fix_type="WRAP_IN_OPTION",
        target_library="io.vavr.control.Option",
        rationale=(
            "Returning null forces callers to perform null checks. Use Option to encode absence in the type system."
        ),
        recommended_api="Option.none() / Option.of(...)",
        suggested_snippet="return Option.none();",
    ),
    "null-assignment": DiagnosticData(
        fix_type="USE_OPTION_TYPE",
        target_library="io.vavr.control.Option",
        rationale="Local null assignment hides absence. Use Option<T> to make optionality explicit.",
        recommended_api="Option<T> = Option.none()",
    ),
    "null-field-assignment": DiagnosticData(
        fix_type="USE_OPTION_NONE",
        target_library="io.vavr.control.Option",
        rationale=(
            "Null field initializers propagate unsafe state. Use Option<T> with Option.none() for optional fields."
        ),
        recommended_api="Option<T> field = Option.none()",
    ),
}


def _build_null_assignment_data(declarator: Any, parent_decl: Any, is_field: bool) -> DiagnosticData:
    """Build a snippet using the real variable name and (where available) the declared type.

    ``parent_decl`` is the local_variable_declaration or field_declaration node — we read
    its ``type`` field to populate the generic parameter when it's a concrete type rather
    than the placeholder ``T``.
    """
    base = _DATA["null-field-assignment"] if is_field else _DATA["null-assignment"]
    name_node = declarator.child_by_field_name("name") if declarator is not None else None
    var_name = name_node.text.decode("utf-8") if name_node is not None and name_node.text else "value"
    type_node = parent_decl.child_by_field_name("type") if parent_decl is not None else None
    type_text = type_node.text.decode("utf-8") if type_node is not None and type_node.text else "T"
    if is_field:
        snippet = f"private final Option<{type_text}> {var_name} = Option.none();"
    else:
        snippet = f"Option<{type_text}> {var_name} = Option.none();"
    return DiagnosticData(
        fix_type=base.fix_type,
        target_library=base.target_library,
        rationale=base.rationale,
        recommended_api=base.recommended_api,
        suggested_snippet=snippet,
    )


class NullChecker:
    """Detects null literal usage in arguments, returns, and assignments."""

    def analyze(self, tree: Any, source: bytes, config: dict[str, Any]) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []

        for node in find_nodes(tree.root_node, "null_literal"):
            parent = node.parent
            if parent is None:
                continue

            rule_id = self._classify_null(node, parent)
            if rule_id is None:
                continue

            severity = severity_from_config(config, rule_id)
            if severity is None:
                continue

            # For variable_declarator-based rules, build the snippet from the real
            # variable name + declared type. The other two rules (-arg, -return) use
            # context-free snippets because the surrounding context shape varies too
            # much to template safely.
            if rule_id in ("null-assignment", "null-field-assignment"):
                data = _build_null_assignment_data(parent, parent.parent, is_field=rule_id == "null-field-assignment")
            else:
                data = _DATA[rule_id]

            diagnostics.append(
                Diagnostic(
                    line=node.start_point[0],
                    col=node.start_point[1],
                    end_line=node.end_point[0],
                    end_col=node.end_point[1],
                    severity=severity,
                    code=rule_id,
                    message=_MESSAGES[rule_id],
                    data=data,
                )
            )

        return diagnostics

    def _classify_null(self, node: Any, parent: Any) -> str | None:
        """Classify a null_literal by its context."""
        # null in argument list -> null-literal-arg
        if parent.type == "argument_list":
            return "null-literal-arg"

        # return null -> null-return
        if parent.type == "return_statement":
            return "null-return"

        # variable_declarator with null value
        if parent.type == "variable_declarator":
            grandparent = parent.parent
            if grandparent is None:
                return None
            if grandparent.type == "local_variable_declaration":
                return "null-assignment"
            if grandparent.type == "field_declaration":
                return "null-field-assignment"

        return None
