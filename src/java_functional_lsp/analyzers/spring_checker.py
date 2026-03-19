"""Spring configuration rules: detect field injection and component annotations."""

from .base import Diagnostic, find_nodes, severity_from_config


_MESSAGES = {
    "field-injection": "Avoid @Autowired field injection. Use constructor injection with @Value (Lombok) classes.",
    "component-annotation": "Avoid @Component/@Service/@Repository. Use @Configuration classes with @Bean methods instead.",
}

_BAD_ANNOTATIONS = {b"Component", b"Service", b"Repository"}


class SpringChecker:
    """Detects Spring anti-patterns: field injection and component scanning annotations."""

    def analyze(self, tree, source: bytes, config: dict) -> list[Diagnostic]:
        diagnostics = []

        self._check_field_injection(tree, diagnostics, config)
        self._check_component_annotation(tree, diagnostics, config)

        return diagnostics

    def _check_field_injection(self, tree, diagnostics: list, config: dict):
        """Detect @Autowired on field declarations."""
        severity = severity_from_config(config, "field-injection")
        if severity is None:
            return

        for node in find_nodes(tree.root_node, "marker_annotation"):
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.text != b"Autowired":
                continue
            # Check it's on a field declaration
            if (node.parent and node.parent.type == "modifiers"
                    and node.parent.parent and node.parent.parent.type == "field_declaration"):
                diagnostics.append(Diagnostic(
                    line=name_node.start_point[0],
                    col=name_node.start_point[1],
                    end_line=name_node.end_point[0],
                    end_col=name_node.end_point[1],
                    severity=severity,
                    code="field-injection",
                    message=_MESSAGES["field-injection"],
                ))

    def _check_component_annotation(self, tree, diagnostics: list, config: dict):
        """Detect @Component, @Service, @Repository on classes."""
        severity = severity_from_config(config, "component-annotation")
        if severity is None:
            return

        for node in find_nodes(tree.root_node, "marker_annotation"):
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.text not in _BAD_ANNOTATIONS:
                continue
            # Check it's on a class declaration
            if (node.parent and node.parent.type == "modifiers"
                    and node.parent.parent and node.parent.parent.type == "class_declaration"):
                diagnostics.append(Diagnostic(
                    line=name_node.start_point[0],
                    col=name_node.start_point[1],
                    end_line=name_node.end_point[0],
                    end_col=name_node.end_point[1],
                    severity=severity,
                    code="component-annotation",
                    message=_MESSAGES["component-annotation"],
                ))
