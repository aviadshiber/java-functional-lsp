"""Tests for Spring configuration rules."""

from __future__ import annotations

from java_functional_lsp.analyzers.spring_checker import SpringChecker
from tests.conftest import parse_and_analyze


class TestFieldInjection:
    def test_detects_autowired_field(self) -> None:
        source = b"class T { @Autowired private Foo foo; }"
        diags = parse_and_analyze(SpringChecker(), source)
        codes = [d.code for d in diags]
        assert "field-injection" in codes

    def test_ignores_non_autowired_field(self) -> None:
        source = b"class T { @Value private String name; }"
        diags = parse_and_analyze(SpringChecker(), source)
        assert not any(d.code == "field-injection" for d in diags)


class TestSpringCheckerData:
    def test_field_injection_has_data_field(self) -> None:
        source = b"class T { @Autowired private Foo foo; }"
        diags = parse_and_analyze(SpringChecker(), source)
        fi_diags = [d for d in diags if d.code == "field-injection"]
        assert len(fi_diags) == 1
        assert fi_diags[0].data is not None
        assert fi_diags[0].data.fix_type == "USE_CONSTRUCTOR_INJECTION"
        assert fi_diags[0].data.target_library == "lombok.Value"

    def test_component_annotation_has_data_field(self) -> None:
        source = b"@Service class Foo { }"
        diags = parse_and_analyze(SpringChecker(), source)
        comp_diags = [d for d in diags if d.code == "component-annotation"]
        assert len(comp_diags) == 1
        assert comp_diags[0].data is not None
        assert comp_diags[0].data.fix_type == "USE_CONFIGURATION_BEAN"


class TestComponentAnnotation:
    def test_detects_service(self) -> None:
        source = b"@Service class Foo { }"
        diags = parse_and_analyze(SpringChecker(), source)
        codes = [d.code for d in diags]
        assert "component-annotation" in codes

    def test_detects_component(self) -> None:
        source = b"@Component class Foo { }"
        diags = parse_and_analyze(SpringChecker(), source)
        codes = [d.code for d in diags]
        assert "component-annotation" in codes

    def test_detects_repository(self) -> None:
        source = b"@Repository class Foo { }"
        diags = parse_and_analyze(SpringChecker(), source)
        codes = [d.code for d in diags]
        assert "component-annotation" in codes

    def test_ignores_configuration(self) -> None:
        source = b"@Configuration class Foo { }"
        diags = parse_and_analyze(SpringChecker(), source)
        assert not any(d.code == "component-annotation" for d in diags)
