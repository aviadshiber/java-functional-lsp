"""Assemble the lsp.ServerCapabilities returned in InitializeResult.

Single responsibility: take the static-advertised entries from the negotiator and
build a `ServerCapabilities` object combining them with the always-static base
(text_document_sync + code_action_provider for our QuickFix custom diagnostics).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import lsprotocol.types as lsp

from .registry import CapabilityEntry

_TEXT_DOC_SYNC = lsp.TextDocumentSyncOptions(
    open_close=True,
    change=lsp.TextDocumentSyncKind.Full,
    save=lsp.SaveOptions(include_text=True),
)

_CODE_ACTION_OPTIONS = lsp.CodeActionOptions(code_action_kinds=[lsp.CodeActionKind.QuickFix])


class StaticCapabilityBuilder:
    """Builds the ServerCapabilities object for the InitializeResult.

    The base capabilities (text_document_sync, code_action_provider) are always
    advertised because they are fully owned by this server (custom diagnostics
    + QuickFix code actions) — they don't depend on jdtls warm-up.
    """

    def build(self, static_entries: Sequence[CapabilityEntry]) -> lsp.ServerCapabilities:
        kwargs: dict[str, Any] = {
            "text_document_sync": _TEXT_DOC_SYNC,
            "code_action_provider": _CODE_ACTION_OPTIONS,
        }
        for entry in static_entries:
            kwargs[entry.static_field] = entry.static_value_factory()
        return lsp.ServerCapabilities(**kwargs)
