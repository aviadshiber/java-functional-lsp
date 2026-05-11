"""Spring configuration rules: detect field injection and component annotations."""

from __future__ import annotations

from typing import Any

from .base import Diagnostic, DiagnosticData, find_ancestor, find_nodes, severity_from_config

_MESSAGES = {
    "field-injection": "Avoid @Autowired field injection. Use constructor injection with @Value (Lombok) classes.",
    "component-annotation": "Avoid @Component/@Service/@Repository. Use @Configuration + @Bean instead.",
}

_DATA = {
    "field-injection": DiagnosticData(
        fix_type="USE_CONSTRUCTOR_INJECTION",
        target_library="lombok.Value",
        rationale=(
            "Field injection hides dependencies and prevents immutability. Use constructor injection with @Value."
        ),
        recommended_api="constructor injection + @Value (or @RequiredArgsConstructor on final fields)",
    ),
    "component-annotation": DiagnosticData(
        fix_type="USE_CONFIGURATION_BEAN",
        target_library="org.springframework.context.annotation.Configuration",
        rationale=(
            "Component scanning reduces explicit wiring control."
            " Use @Configuration + @Bean for explicit dependency graphs."
        ),
        recommended_api="@Configuration + @Bean",
    ),
}


def _enclosing_class_name(node: Any) -> str | None:
    """Return the simple name of the nearest enclosing class_declaration, or None."""
    class_decl = find_ancestor(node, "class_declaration")
    if class_decl is None:
        return None
    name_node = class_decl.child_by_field_name("name")
    if name_node is None or not name_node.text:
        return None
    decoded: str = name_node.text.decode("utf-8")
    return decoded


def _build_field_injection_data(field_decl: Any) -> DiagnosticData:
    """Build a DiagnosticData with a concrete constructor-injection snippet.

    Reads the field's type, name, and enclosing class name from the AST. Bails to the
    base (no snippet) for multi-declarator fields like ``private Bar a, b;`` — those
    need either listing all names or refusing to rewrite, and we keep behavior
    conservative for now.
    """
    base = _DATA["field-injection"]
    declarators = [c for c in field_decl.children if c.type == "variable_declarator"]
    if len(declarators) != 1:
        return base  # multi-declarator: ambiguous — skip the snippet.

    type_node = field_decl.child_by_field_name("type")
    if type_node is None or not type_node.text:
        return base
    name_node = declarators[0].child_by_field_name("name")
    if name_node is None or not name_node.text:
        return base

    type_text = type_node.text.decode("utf-8")
    field_name = name_node.text.decode("utf-8")
    class_name = _enclosing_class_name(field_decl) or "MyClass"

    snippet = (
        f"private final {type_text} {field_name};\n"
        f"// constructor:\n"
        f"public {class_name}(final {type_text} {field_name}) {{ this.{field_name} = {field_name}; }}"
    )
    return DiagnosticData(
        fix_type=base.fix_type,
        target_library=base.target_library,
        rationale=base.rationale,
        recommended_api=base.recommended_api,
        suggested_snippet=snippet,
    )


def _build_component_annotation_data(class_decl: Any) -> DiagnosticData:
    """Build a DiagnosticData with a @Configuration+@Bean snippet using the real class name."""
    base = _DATA["component-annotation"]
    name_node = class_decl.child_by_field_name("name") if class_decl is not None else None
    if name_node is None or not name_node.text:
        return base
    class_name = name_node.text.decode("utf-8")
    bean_name = class_name[:1].lower() + class_name[1:] if class_name else "bean"
    snippet = (
        f"@Configuration\n"
        f"public class {class_name}Config {{\n"
        f"    @Bean\n"
        f"    public {class_name} {bean_name}() {{ return new {class_name}(); }}\n"
        f"}}"
    )
    return DiagnosticData(
        fix_type=base.fix_type,
        target_library=base.target_library,
        rationale=base.rationale,
        recommended_api=base.recommended_api,
        suggested_snippet=snippet,
    )


_BAD_ANNOTATIONS = {b"Component", b"Service", b"Repository"}


class SpringChecker:
    """Detects Spring anti-patterns: field injection and component scanning annotations."""

    def analyze(self, tree: Any, source: bytes, config: dict[str, Any]) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []

        self._check_field_injection(tree, diagnostics, config)
        self._check_component_annotation(tree, diagnostics, config)

        return diagnostics

    def _check_field_injection(self, tree: Any, diagnostics: list[Diagnostic], config: dict[str, Any]) -> None:
        """Detect @Autowired on field declarations."""
        severity = severity_from_config(config, "field-injection")
        if severity is None:
            return

        for node in find_nodes(tree.root_node, "marker_annotation"):
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.text != b"Autowired":
                continue
            # Check it's on a field declaration
            if (
                node.parent
                and node.parent.type == "modifiers"
                and node.parent.parent
                and node.parent.parent.type == "field_declaration"
            ):
                diagnostics.append(
                    Diagnostic(
                        line=name_node.start_point[0],
                        col=name_node.start_point[1],
                        end_line=name_node.end_point[0],
                        end_col=name_node.end_point[1],
                        severity=severity,
                        code="field-injection",
                        message=_MESSAGES["field-injection"],
                        data=_build_field_injection_data(node.parent.parent),
                    )
                )

    def _check_component_annotation(self, tree: Any, diagnostics: list[Diagnostic], config: dict[str, Any]) -> None:
        """Detect @Component, @Service, @Repository on classes."""
        severity = severity_from_config(config, "component-annotation")
        if severity is None:
            return

        for node in find_nodes(tree.root_node, "marker_annotation"):
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.text not in _BAD_ANNOTATIONS:
                continue
            # Check it's on a class declaration
            if (
                node.parent
                and node.parent.type == "modifiers"
                and node.parent.parent
                and node.parent.parent.type == "class_declaration"
            ):
                diagnostics.append(
                    Diagnostic(
                        line=name_node.start_point[0],
                        col=name_node.start_point[1],
                        end_line=name_node.end_point[0],
                        end_col=name_node.end_point[1],
                        severity=severity,
                        code="component-annotation",
                        message=_MESSAGES["component-annotation"],
                        data=_build_component_annotation_data(node.parent.parent),
                    )
                )
