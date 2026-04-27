"""Capability registry — single source of truth for jdtls-dependent LSP features.

Each entry pairs an LSP method with everything needed to advertise it both ways:
- statically in the InitializeResult.ServerCapabilities (for clients that
  ignore client/registerCapability, e.g., Claude Code)
- dynamically via client/registerCapability after jdtls warms up (for clients
  that respect dynamic registration, e.g., VS Code, IntelliJ-LSP4IJ — preserves
  PR #44 behavior of not suppressing IDE diagnostic tooltips).

The registry is data only. Decision logic lives in negotiator.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import lsprotocol.types as lsp

_JAVA_SELECTOR = [lsp.TextDocumentFilterLanguage(language="java")]


@dataclass(frozen=True)
class CapabilityEntry:
    """Metadata for one jdtls-dependent LSP capability.

    Attributes:
        id_suffix: Used to build the unique Registration.id for dynamic registration.
        lsp_method: The LSP method this capability gates (e.g., "textDocument/hover").
        static_field: Name of the kwarg on lsp.ServerCapabilities for static advertisement.
        client_cap_path: Path to the client capability that signals dynamic-registration
            support (e.g., ("text_document", "hover")). Compared against
            params.capabilities at initialize time.
        registration_options_factory: Builds the RegistrationOptions object used in
            client/registerCapability for the dynamic path.
        static_value_factory: Builds the value to assign to ServerCapabilities[static_field].
            Most are simply `True`; some are options objects (CompletionOptions etc.).
        sub_methods: LSP methods that share this capability's advertisement but need
            their own pygls handler (e.g., callHierarchy/incomingCalls). These are
            wired alongside lsp_method whenever the entry is activated (static or dynamic).
    """

    id_suffix: str
    lsp_method: str
    static_field: str
    client_cap_path: tuple[str, ...]
    registration_options_factory: Callable[[], Any]
    static_value_factory: Callable[[], Any]
    sub_methods: tuple[str, ...] = field(default_factory=tuple)


REGISTRY: tuple[CapabilityEntry, ...] = (
    CapabilityEntry(
        id_suffix="completion",
        lsp_method=lsp.TEXT_DOCUMENT_COMPLETION,
        static_field="completion_provider",
        client_cap_path=("text_document", "completion"),
        registration_options_factory=lambda: lsp.CompletionRegistrationOptions(
            document_selector=_JAVA_SELECTOR, trigger_characters=["."]
        ),
        static_value_factory=lambda: lsp.CompletionOptions(trigger_characters=["."]),
    ),
    CapabilityEntry(
        id_suffix="hover",
        lsp_method=lsp.TEXT_DOCUMENT_HOVER,
        static_field="hover_provider",
        client_cap_path=("text_document", "hover"),
        registration_options_factory=lambda: lsp.HoverRegistrationOptions(document_selector=_JAVA_SELECTOR),
        static_value_factory=lambda: True,
    ),
    CapabilityEntry(
        id_suffix="definition",
        lsp_method=lsp.TEXT_DOCUMENT_DEFINITION,
        static_field="definition_provider",
        client_cap_path=("text_document", "definition"),
        registration_options_factory=lambda: lsp.DefinitionRegistrationOptions(document_selector=_JAVA_SELECTOR),
        static_value_factory=lambda: True,
    ),
    CapabilityEntry(
        id_suffix="references",
        lsp_method=lsp.TEXT_DOCUMENT_REFERENCES,
        static_field="references_provider",
        client_cap_path=("text_document", "references"),
        registration_options_factory=lambda: lsp.ReferenceRegistrationOptions(document_selector=_JAVA_SELECTOR),
        static_value_factory=lambda: True,
    ),
    CapabilityEntry(
        id_suffix="document-symbol",
        lsp_method=lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL,
        static_field="document_symbol_provider",
        client_cap_path=("text_document", "document_symbol"),
        registration_options_factory=lambda: lsp.DocumentSymbolRegistrationOptions(document_selector=_JAVA_SELECTOR),
        static_value_factory=lambda: True,
    ),
    CapabilityEntry(
        id_suffix="call-hierarchy",
        lsp_method=lsp.TEXT_DOCUMENT_PREPARE_CALL_HIERARCHY,
        static_field="call_hierarchy_provider",
        client_cap_path=("text_document", "call_hierarchy"),
        registration_options_factory=lambda: lsp.CallHierarchyRegistrationOptions(document_selector=_JAVA_SELECTOR),
        static_value_factory=lambda: True,
        sub_methods=(lsp.CALL_HIERARCHY_INCOMING_CALLS, lsp.CALL_HIERARCHY_OUTGOING_CALLS),
    ),
    CapabilityEntry(
        id_suffix="signature-help",
        lsp_method=lsp.TEXT_DOCUMENT_SIGNATURE_HELP,
        static_field="signature_help_provider",
        client_cap_path=("text_document", "signature_help"),
        registration_options_factory=lambda: lsp.SignatureHelpRegistrationOptions(
            document_selector=_JAVA_SELECTOR,
            trigger_characters=["(", ","],
            retrigger_characters=[")"],
        ),
        static_value_factory=lambda: lsp.SignatureHelpOptions(
            trigger_characters=["(", ","], retrigger_characters=[")"]
        ),
    ),
    CapabilityEntry(
        id_suffix="implementation",
        lsp_method=lsp.TEXT_DOCUMENT_IMPLEMENTATION,
        static_field="implementation_provider",
        client_cap_path=("text_document", "implementation"),
        registration_options_factory=lambda: lsp.ImplementationRegistrationOptions(document_selector=_JAVA_SELECTOR),
        static_value_factory=lambda: True,
    ),
    CapabilityEntry(
        id_suffix="type-definition",
        lsp_method=lsp.TEXT_DOCUMENT_TYPE_DEFINITION,
        static_field="type_definition_provider",
        client_cap_path=("text_document", "type_definition"),
        registration_options_factory=lambda: lsp.TypeDefinitionRegistrationOptions(document_selector=_JAVA_SELECTOR),
        static_value_factory=lambda: True,
    ),
    CapabilityEntry(
        id_suffix="declaration",
        lsp_method=lsp.TEXT_DOCUMENT_DECLARATION,
        static_field="declaration_provider",
        client_cap_path=("text_document", "declaration"),
        registration_options_factory=lambda: lsp.DeclarationRegistrationOptions(document_selector=_JAVA_SELECTOR),
        static_value_factory=lambda: True,
    ),
    CapabilityEntry(
        id_suffix="document-highlight",
        lsp_method=lsp.TEXT_DOCUMENT_DOCUMENT_HIGHLIGHT,
        static_field="document_highlight_provider",
        client_cap_path=("text_document", "document_highlight"),
        registration_options_factory=lambda: lsp.DocumentHighlightRegistrationOptions(document_selector=_JAVA_SELECTOR),
        static_value_factory=lambda: True,
    ),
    CapabilityEntry(
        id_suffix="rename",
        lsp_method=lsp.TEXT_DOCUMENT_RENAME,
        static_field="rename_provider",
        client_cap_path=("text_document", "rename"),
        registration_options_factory=lambda: lsp.RenameRegistrationOptions(
            document_selector=_JAVA_SELECTOR, prepare_provider=True
        ),
        static_value_factory=lambda: lsp.RenameOptions(prepare_provider=True),
        sub_methods=(lsp.TEXT_DOCUMENT_PREPARE_RENAME,),
    ),
    CapabilityEntry(
        id_suffix="type-hierarchy",
        lsp_method=lsp.TEXT_DOCUMENT_PREPARE_TYPE_HIERARCHY,
        static_field="type_hierarchy_provider",
        client_cap_path=("text_document", "type_hierarchy"),
        registration_options_factory=lambda: lsp.TypeHierarchyRegistrationOptions(document_selector=_JAVA_SELECTOR),
        static_value_factory=lambda: True,
        sub_methods=(lsp.TYPE_HIERARCHY_SUPERTYPES, lsp.TYPE_HIERARCHY_SUBTYPES),
    ),
    CapabilityEntry(
        id_suffix="workspace-symbol",
        lsp_method=lsp.WORKSPACE_SYMBOL,
        static_field="workspace_symbol_provider",
        client_cap_path=("workspace", "symbol"),
        registration_options_factory=lsp.WorkspaceSymbolRegistrationOptions,
        static_value_factory=lambda: True,
    ),
)


def all_methods(entries: tuple[CapabilityEntry, ...] = REGISTRY) -> tuple[str, ...]:
    """Return every LSP method (parent + sub) reachable from `entries`."""
    methods: list[str] = []
    for entry in entries:
        methods.append(entry.lsp_method)
        methods.extend(entry.sub_methods)
    return tuple(methods)
