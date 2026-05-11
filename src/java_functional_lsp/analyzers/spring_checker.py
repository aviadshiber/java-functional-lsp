"""Spring configuration rules: detect field injection and component annotations."""

from __future__ import annotations

from typing import Any

from .base import Diagnostic, DiagnosticData, find_nodes, severity_from_config

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
        suggested_snippet=(
            "@Configuration\n"
            "public class FooConfig {\n"
            "    @Bean\n"
            "    public Foo foo() { return new Foo(); }\n"
            "}"
        ),
    ),
}


def _build_field_injection_data(field_decl: Any) -> DiagnosticData:
    """Build a DiagnosticData with a concrete constructor-injection snippet.

    Reads the field's type and name from the AST so the snippet shows the user
    exactly which constructor parameter to add.
    """
    base = _DATA["field-injection"]
    type_node = field_decl.child_by_field_name("type")
    type_text = type_node.text.decode("utf-8") if type_node is not None and type_node.text else "Foo"
    field_name = "foo"
    for declarator in field_decl.children:
        if declarator.type == "variable_declarator":
            name_node = declarator.child_by_field_name("name")
            if name_node is not None and name_node.text:
                field_name = name_node.text.decode("utf-8")
                break
    snippet = (
        f"private final {type_text} {field_name};\n"
        f"// constructor:\n"
        f"public Foo(final {type_text} {field_name}) {{ this.{field_name} = {field_name}; }}"
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
                        data=_DATA["component-annotation"],
                    )
                )
