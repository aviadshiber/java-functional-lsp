"""Wire jdtls passthrough handlers into pygls' feature dispatcher.

When `server.feature(method)(handler)` is called, pygls registers the handler so
incoming requests for that method reach our code. The same call also influences
pygls' auto-advertised capabilities, but we're not relying on that — we own the
ServerCapabilities object explicitly via StaticCapabilityBuilder.

`wire_eager` and `wire_lazy` share their body. Different names exist so the
call-site intent (during `on_initialize` vs after jdtls warm-up) is self-evident.
That readability is the SRP win.

Idempotency: pygls' FeatureManager raises FeatureAlreadyRegisteredError if a
method is registered twice. Tests use a module-level `server` singleton across
many initialize cycles, so we skip already-registered methods rather than
re-raising. In production the same guard is harmless — the second call is just
a no-op.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .registry import CapabilityEntry


class HandlerWiring:
    """Registers pygls feature handlers for capability entries."""

    def __init__(self, server: Any, handlers: Mapping[str, Callable[..., Any]]) -> None:
        self._server = server
        self._handlers = handlers

    def wire_eager(self, entries: Sequence[CapabilityEntry]) -> None:
        """Wire handlers immediately (call from on_initialize for static features)."""
        self._wire(entries)

    def wire_lazy(self, entries: Sequence[CapabilityEntry]) -> None:
        """Wire handlers after jdtls warm-up (call before client/registerCapability)."""
        self._wire(entries)

    def _wire(self, entries: Sequence[CapabilityEntry]) -> None:
        for entry in entries:
            for method in self._methods_for(entry):
                handler = self._handlers.get(method)
                if handler is None:
                    continue
                if self._already_registered(method):
                    continue
                self._server.feature(method)(handler)

    def _already_registered(self, method: str) -> bool:
        """Check whether pygls already has a handler bound for `method`.

        We probe `server.protocol.fm._features` (pygls FeatureManager). If the
        attribute path isn't there (custom server stand-ins in tests), assume
        not-registered and let the caller proceed.
        """
        protocol = getattr(self._server, "protocol", None)
        fm = getattr(protocol, "fm", None) if protocol is not None else None
        features = getattr(fm, "_features", None) if fm is not None else None
        if features is None:
            return False
        return method in features

    @staticmethod
    def _methods_for(entry: CapabilityEntry) -> tuple[str, ...]:
        """Return parent method + any sub-methods that ride along with it."""
        return (entry.lsp_method, *entry.sub_methods)
