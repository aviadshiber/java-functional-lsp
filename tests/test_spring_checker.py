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
