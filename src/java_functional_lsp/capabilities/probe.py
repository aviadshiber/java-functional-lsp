"""Read-only inspection of the client's declared capabilities.

Pygls hands `on_initialize` a `lsp.InitializeParams` whose `capabilities` field is
an attrs-based `ClientCapabilities` object. We walk a path like
`("text_document", "hover")` and return whether the client opted in to LSP
`dynamicRegistration` for that feature.

We intentionally accept both attrs-dataclass shapes and dicts at every level
because some clients send vendor-specific extensions as raw dicts that pygls
leaves untouched. Returning `False` (rather than raising) on missing nodes
matches the LSP spec's default: absence of `dynamicRegistration` means the
client does not advertise support.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import lsprotocol.types as lsp


class ClientCapabilityProbe:
    """Wraps `params.capabilities` and answers per-feature support questions."""

    def __init__(self, params: lsp.InitializeParams) -> None:
        self._caps: Any = params.capabilities

    def supports_dynamic(self, path: tuple[str, ...]) -> bool:
        """Return True if the client claims dynamicRegistration support for `path`.

        `path` walks into ClientCapabilities — e.g., ("text_document", "hover")
        means `params.capabilities.text_document.hover.dynamic_registration`.
        Empty path or a missing intermediate node yields False (no support).
        """
        if not path:
            return False
        node: Any = self._caps
        for key in path:
            node = _walk(node, key)
            if node is None:
                return False
        return _read_dynamic_registration(node)


def _walk(node: Any, key: str) -> Any:
    """Get a child of `node` by `key`, supporting both attrs/dataclass and dict shapes."""
    if node is None:
        return None
    if isinstance(node, Mapping):
        # Try snake_case first, then the camelCase the wire protocol uses.
        if key in node:
            return node[key]
        return node.get(_snake_to_camel(key))
    return getattr(node, key, None)


def _read_dynamic_registration(node: Any) -> bool:
    """Return whether `node.dynamicRegistration` is truthy."""
    if node is None:
        return False
    if isinstance(node, Mapping):
        return bool(node.get("dynamic_registration") or node.get("dynamicRegistration"))
    return bool(getattr(node, "dynamic_registration", None))


def _snake_to_camel(key: str) -> str:
    """`text_document` -> `textDocument`. Idempotent for already-camel names."""
    head, *tail = key.split("_")
    return head + "".join(part.capitalize() for part in tail)
