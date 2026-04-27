"""Tests for StaticCapabilityBuilder.

The builder always emits text_document_sync + code_action_provider, then layers
in the negotiated static entries. Each entry maps to a specific kwarg on
lsp.ServerCapabilities, so the test asserts both presence and value identity.
"""

from __future__ import annotations

import lsprotocol.types as lsp

from java_functional_lsp.capabilities.registry import REGISTRY, CapabilityEntry
from java_functional_lsp.capabilities.static_builder import StaticCapabilityBuilder


def _by_suffix(suffix: str) -> CapabilityEntry:
    return next(e for e in REGISTRY if e.id_suffix == suffix)


def test_base_capabilities_always_present() -> None:
    caps = StaticCapabilityBuilder().build(static_entries=())
    assert caps.text_document_sync is not None
    assert caps.code_action_provider is not None


def test_text_document_sync_shape() -> None:
    caps = StaticCapabilityBuilder().build(static_entries=())
    sync = caps.text_document_sync
    assert isinstance(sync, lsp.TextDocumentSyncOptions)
    assert sync.open_close is True
    assert sync.change == lsp.TextDocumentSyncKind.Full
    assert isinstance(sync.save, lsp.SaveOptions)
    assert sync.save.include_text is True


def test_code_action_options_advertise_quickfix() -> None:
    caps = StaticCapabilityBuilder().build(static_entries=())
    code_action = caps.code_action_provider
    assert isinstance(code_action, lsp.CodeActionOptions)
    assert code_action.code_action_kinds == [lsp.CodeActionKind.QuickFix]


def test_static_entry_sets_simple_boolean_field() -> None:
    caps = StaticCapabilityBuilder().build(static_entries=(_by_suffix("hover"),))
    assert caps.hover_provider is True


def test_static_entry_sets_options_object() -> None:
    caps = StaticCapabilityBuilder().build(static_entries=(_by_suffix("completion"),))
    assert isinstance(caps.completion_provider, lsp.CompletionOptions)
    assert caps.completion_provider.trigger_characters == ["."]


def test_static_entry_signature_help_options() -> None:
    caps = StaticCapabilityBuilder().build(static_entries=(_by_suffix("signature-help"),))
    assert isinstance(caps.signature_help_provider, lsp.SignatureHelpOptions)
    assert caps.signature_help_provider.trigger_characters == ["(", ","]
    assert caps.signature_help_provider.retrigger_characters == [")"]


def test_static_entry_rename_options() -> None:
    caps = StaticCapabilityBuilder().build(static_entries=(_by_suffix("rename"),))
    assert isinstance(caps.rename_provider, lsp.RenameOptions)
    assert caps.rename_provider.prepare_provider is True


def test_full_registry_static() -> None:
    """When *every* entry is static, all per-feature kwargs should be set."""
    caps = StaticCapabilityBuilder().build(static_entries=REGISTRY)
    for entry in REGISTRY:
        assert getattr(caps, entry.static_field) is not None, (
            f"{entry.id_suffix}: static_field {entry.static_field} should be set"
        )


def test_empty_entries_does_not_set_extra_fields() -> None:
    caps = StaticCapabilityBuilder().build(static_entries=())
    for entry in REGISTRY:
        assert getattr(caps, entry.static_field, None) is None
