"""Sanity-check the registry against lsprotocol's actual ServerCapabilities shape.

Cheap in CPU, expensive when broken: a typo in a static_field name would
silently produce a malformed initialize response the IDE rejects without
explanation. This guard tests the dataclass contract, not the data values.
"""

from __future__ import annotations

import attrs
import lsprotocol.types as lsp
import pytest

from java_functional_lsp.capabilities.registry import REGISTRY, all_methods


def _server_capability_field_names() -> set[str]:
    return {f.name for f in attrs.fields(lsp.ServerCapabilities)}


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda e: e.id_suffix)
def test_static_field_exists_on_server_capabilities(entry: object) -> None:
    valid_fields = _server_capability_field_names()
    assert entry.static_field in valid_fields, (  # type: ignore[attr-defined]
        f"{entry.id_suffix}: static_field {entry.static_field!r} "  # type: ignore[attr-defined]
        f"is not a valid lsp.ServerCapabilities kwarg"
    )


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda e: e.id_suffix)
def test_client_cap_path_is_non_empty(entry: object) -> None:
    assert entry.client_cap_path, f"{entry.id_suffix}: client_cap_path must be non-empty"  # type: ignore[attr-defined]


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda e: e.id_suffix)
def test_id_suffix_is_unique(entry: object) -> None:
    suffixes = [e.id_suffix for e in REGISTRY]
    assert suffixes.count(entry.id_suffix) == 1, (  # type: ignore[attr-defined]
        f"id_suffix {entry.id_suffix!r} appears more than once"  # type: ignore[attr-defined]
    )


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda e: e.id_suffix)
def test_factories_produce_values(entry: object) -> None:
    static_value = entry.static_value_factory()  # type: ignore[attr-defined]
    assert static_value is not None
    reg_options = entry.registration_options_factory()  # type: ignore[attr-defined]
    assert reg_options is not None


def test_all_methods_includes_parents_and_subs() -> None:
    methods = all_methods()
    parents = {e.lsp_method for e in REGISTRY}
    subs = {sub for e in REGISTRY for sub in e.sub_methods}
    assert parents.issubset(set(methods))
    assert subs.issubset(set(methods))


def test_all_methods_returns_no_duplicates() -> None:
    methods = all_methods()
    assert len(methods) == len(set(methods))
