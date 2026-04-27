"""Tests for CapabilityNegotiator.

Pure decision logic — given a probe and a registry, the negotiator partitions
entries into (static, dynamic). We construct fake probes (no real LSP) so
the test asserts the pure function in isolation.
"""

from __future__ import annotations

from collections.abc import Iterable

from java_functional_lsp.capabilities.negotiator import CapabilityNegotiator, NegotiationResult
from java_functional_lsp.capabilities.registry import REGISTRY, CapabilityEntry


class _FakeProbe:
    """Probe stub: returns True only for the paths we declare as 'dynamic-supported'."""

    def __init__(self, dynamic_paths: Iterable[tuple[str, ...]]) -> None:
        self._set = set(dynamic_paths)

    def supports_dynamic(self, path: tuple[str, ...]) -> bool:
        return path in self._set


def _entries_for(suffixes: Iterable[str]) -> tuple[CapabilityEntry, ...]:
    by_suffix = {e.id_suffix: e for e in REGISTRY}
    return tuple(by_suffix[s] for s in suffixes)


def test_all_static_when_probe_returns_false_everywhere() -> None:
    probe = _FakeProbe(dynamic_paths=())
    result = CapabilityNegotiator(probe).negotiate()
    assert isinstance(result, NegotiationResult)
    assert set(result.static) == set(REGISTRY)
    assert result.dynamic == ()


def test_all_dynamic_when_probe_returns_true_everywhere() -> None:
    all_paths = [e.client_cap_path for e in REGISTRY]
    probe = _FakeProbe(dynamic_paths=all_paths)
    result = CapabilityNegotiator(probe).negotiate()
    assert result.static == ()
    assert set(result.dynamic) == set(REGISTRY)


def test_partial_split() -> None:
    """Hover supports dynamic; everything else is static — verifies per-feature split."""
    hover_path = next(e.client_cap_path for e in REGISTRY if e.id_suffix == "hover")
    probe = _FakeProbe(dynamic_paths=[hover_path])
    result = CapabilityNegotiator(probe).negotiate()
    dynamic_suffixes = {e.id_suffix for e in result.dynamic}
    static_suffixes = {e.id_suffix for e in result.static}
    assert dynamic_suffixes == {"hover"}
    assert "hover" not in static_suffixes
    assert len(static_suffixes) == len(REGISTRY) - 1


def test_static_and_dynamic_are_disjoint() -> None:
    probe = _FakeProbe(dynamic_paths=[("text_document", "hover"), ("workspace", "symbol")])
    result = CapabilityNegotiator(probe).negotiate()
    assert set(result.static).isdisjoint(set(result.dynamic))


def test_custom_registry_is_respected() -> None:
    """Passing an explicit (smaller) registry should only consider those entries."""
    subset = _entries_for(["hover", "definition"])
    probe = _FakeProbe(dynamic_paths=[("text_document", "hover")])
    result = CapabilityNegotiator(probe, registry=subset).negotiate()
    assert {e.id_suffix for e in result.dynamic} == {"hover"}
    assert {e.id_suffix for e in result.static} == {"definition"}


def test_result_uses_tuples_for_immutability() -> None:
    probe = _FakeProbe(dynamic_paths=())
    result = CapabilityNegotiator(probe).negotiate()
    assert isinstance(result.static, tuple)
    assert isinstance(result.dynamic, tuple)
