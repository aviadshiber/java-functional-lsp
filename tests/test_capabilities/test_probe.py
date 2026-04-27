"""Tests for ClientCapabilityProbe.

The probe must read `dynamicRegistration` correctly from:
- attrs-dataclass shapes (the normal pygls case),
- dict shapes (vendor extensions, raw JSON not converted by pygls),
- snake_case AND camelCase keys (depending on which side did the conversion).
And it must never raise on missing nodes — absence implies "no support".
"""

from __future__ import annotations

import lsprotocol.types as lsp

from java_functional_lsp.capabilities.probe import ClientCapabilityProbe


def _make_params(client_capabilities: lsp.ClientCapabilities) -> lsp.InitializeParams:
    return lsp.InitializeParams(capabilities=client_capabilities)


def test_dataclass_dynamic_registration_true() -> None:
    caps = lsp.ClientCapabilities(
        text_document=lsp.TextDocumentClientCapabilities(hover=lsp.HoverClientCapabilities(dynamic_registration=True))
    )
    probe = ClientCapabilityProbe(_make_params(caps))
    assert probe.supports_dynamic(("text_document", "hover")) is True


def test_dataclass_dynamic_registration_false() -> None:
    caps = lsp.ClientCapabilities(
        text_document=lsp.TextDocumentClientCapabilities(hover=lsp.HoverClientCapabilities(dynamic_registration=False))
    )
    probe = ClientCapabilityProbe(_make_params(caps))
    assert probe.supports_dynamic(("text_document", "hover")) is False


def test_dataclass_dynamic_registration_missing() -> None:
    """When `dynamic_registration` field exists but is None, treat as no support."""
    caps = lsp.ClientCapabilities(text_document=lsp.TextDocumentClientCapabilities(hover=lsp.HoverClientCapabilities()))
    probe = ClientCapabilityProbe(_make_params(caps))
    assert probe.supports_dynamic(("text_document", "hover")) is False


def test_missing_intermediate_node_returns_false() -> None:
    """If `text_document` is None or the feature itself is missing, return False."""
    caps = lsp.ClientCapabilities(text_document=None)
    probe = ClientCapabilityProbe(_make_params(caps))
    assert probe.supports_dynamic(("text_document", "hover")) is False


def test_feature_node_missing_returns_false() -> None:
    caps = lsp.ClientCapabilities(text_document=lsp.TextDocumentClientCapabilities())
    probe = ClientCapabilityProbe(_make_params(caps))
    assert probe.supports_dynamic(("text_document", "hover")) is False


def test_empty_path_returns_false() -> None:
    caps = lsp.ClientCapabilities()
    probe = ClientCapabilityProbe(_make_params(caps))
    assert probe.supports_dynamic(()) is False


def test_workspace_path() -> None:
    """Workspace symbol lives at a different root — verify path handling."""
    caps = lsp.ClientCapabilities(
        workspace=lsp.WorkspaceClientCapabilities(
            symbol=lsp.WorkspaceSymbolClientCapabilities(dynamic_registration=True)
        )
    )
    probe = ClientCapabilityProbe(_make_params(caps))
    assert probe.supports_dynamic(("workspace", "symbol")) is True


def test_dict_shape_snake_case() -> None:
    """A capabilities object whose nested fields are dicts (vendor extension shape)."""
    params = lsp.InitializeParams(capabilities=lsp.ClientCapabilities())
    # Replace `_caps` with a hand-built dict graph to simulate raw vendor extensions.
    probe = ClientCapabilityProbe(params)
    probe._caps = {  # type: ignore[attr-defined]
        "text_document": {"hover": {"dynamic_registration": True}}
    }
    assert probe.supports_dynamic(("text_document", "hover")) is True


def test_dict_shape_camel_case() -> None:
    params = lsp.InitializeParams(capabilities=lsp.ClientCapabilities())
    probe = ClientCapabilityProbe(params)
    probe._caps = {  # type: ignore[attr-defined]
        "textDocument": {"hover": {"dynamicRegistration": True}}
    }
    assert probe.supports_dynamic(("text_document", "hover")) is True


def test_dict_shape_camel_case_false() -> None:
    params = lsp.InitializeParams(capabilities=lsp.ClientCapabilities())
    probe = ClientCapabilityProbe(params)
    probe._caps = {  # type: ignore[attr-defined]
        "textDocument": {"hover": {"dynamicRegistration": False}}
    }
    assert probe.supports_dynamic(("text_document", "hover")) is False


def test_mixed_dataclass_and_dict() -> None:
    """Outer attrs object, inner dict (because vendor sent extra keys)."""
    caps = lsp.ClientCapabilities()
    caps.text_document = {"hover": {"dynamic_registration": True}}  # type: ignore[assignment]
    probe = ClientCapabilityProbe(_make_params(caps))
    assert probe.supports_dynamic(("text_document", "hover")) is True
