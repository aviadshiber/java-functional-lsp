"""Null safety rules: detect null literals in arguments, returns, and assignments."""

from .base import Diagnostic, find_nodes, severity_from_config


_MESSAGES = {
    "null-literal-arg": "Avoid passing null as argument. Use Option.none(), a default value, or overload the method.",
    "null-return": "Avoid returning null. Use Option.of(), Option.none(), or Either<Error, T>.",
    "null-assignment": "Avoid assigning null to local variables. Use Option<T> to represent absence.",
    "null-field-assignment": "Avoid null field initializers. Use Option<T> with Option.none() for optional fields.",
}


class NullChecker:
    """Detects null literal usage in arguments, returns, and assignments."""

    def analyze(self, tree, source: bytes, config: dict) -> list[Diagnostic]:
        diagnostics = []

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

            diagnostics.append(Diagnostic(
                line=node.start_point[0],
                col=node.start_point[1],
                end_line=node.end_point[0],
                end_col=node.end_point[1],
                severity=severity,
                code=rule_id,
                message=_MESSAGES[rule_id],
            ))

        return diagnostics

    def _classify_null(self, node, parent) -> str | None:
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
