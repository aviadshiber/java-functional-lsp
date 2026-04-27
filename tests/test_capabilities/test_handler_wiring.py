"""Tests for HandlerWiring.

Asserts that `wire_eager` and `wire_lazy` register pygls feature handlers for
each entry's parent method AND every sub-method (e.g., callHierarchy/incomingCalls
rides along with the call_hierarchy parent capability).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import lsprotocol.types as lsp

from java_functional_lsp.capabilities.handler_wiring import HandlerWiring
from java_functional_lsp.capabilities.registry import REGISTRY, CapabilityEntry


class _FakeServer:
    """Minimal pygls stand-in that records `feature(method)(handler)` calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Callable[..., Any]]] = []

    def feature(self, method: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.calls.append((method, handler))
            return handler

        return decorator


def _by_suffix(suffix: str) -> CapabilityEntry:
    return next(e for e in REGISTRY if e.id_suffix == suffix)


def _noop(*args: Any, **kwargs: Any) -> None:
    _ = args, kwargs


def _make_handlers(methods: list[str]) -> dict[str, Callable[..., Any]]:
    return dict.fromkeys(methods, _noop)


def test_wire_eager_registers_parent_method() -> None:
    server = _FakeServer()
    entry = _by_suffix("hover")
    handlers = _make_handlers([entry.lsp_method])
    HandlerWiring(server, handlers).wire_eager(entries=(entry,))
    methods_registered = [call[0] for call in server.calls]
    assert methods_registered == [lsp.TEXT_DOCUMENT_HOVER]


def test_wire_eager_registers_sub_methods_with_parent() -> None:
    """call-hierarchy entry should wire prepare + incoming + outgoing together."""
    server = _FakeServer()
    entry = _by_suffix("call-hierarchy")
    handlers = _make_handlers([entry.lsp_method, *entry.sub_methods])
    HandlerWiring(server, handlers).wire_eager(entries=(entry,))
    methods_registered = {call[0] for call in server.calls}
    assert methods_registered == {
        lsp.TEXT_DOCUMENT_PREPARE_CALL_HIERARCHY,
        lsp.CALL_HIERARCHY_INCOMING_CALLS,
        lsp.CALL_HIERARCHY_OUTGOING_CALLS,
    }


def test_wire_lazy_uses_same_logic_as_eager() -> None:
    server_e = _FakeServer()
    server_l = _FakeServer()
    entry = _by_suffix("rename")
    handlers = _make_handlers([entry.lsp_method, *entry.sub_methods])
    HandlerWiring(server_e, handlers).wire_eager(entries=(entry,))
    HandlerWiring(server_l, handlers).wire_lazy(entries=(entry,))
    assert {c[0] for c in server_e.calls} == {c[0] for c in server_l.calls}


def test_missing_handler_is_silently_skipped() -> None:
    """Robustness: an entry with no registered handler should not crash."""
    server = _FakeServer()
    entry = _by_suffix("hover")
    HandlerWiring(server, handlers={}).wire_eager(entries=(entry,))
    assert server.calls == []


def test_partial_handlers_only_wires_present_ones() -> None:
    """If parent has a handler but a sub-method does not, only the parent gets wired."""
    server = _FakeServer()
    entry = _by_suffix("call-hierarchy")
    handlers = _make_handlers([entry.lsp_method])  # no sub-method handlers
    HandlerWiring(server, handlers).wire_eager(entries=(entry,))
    methods_registered = [call[0] for call in server.calls]
    assert methods_registered == [lsp.TEXT_DOCUMENT_PREPARE_CALL_HIERARCHY]


def test_wire_passes_actual_handler_function() -> None:
    server = _FakeServer()
    entry = _by_suffix("hover")

    def sentinel(*args: Any, **kwargs: Any) -> str:
        _ = args, kwargs
        return "sentinel-result"

    HandlerWiring(server, {entry.lsp_method: sentinel}).wire_eager(entries=(entry,))
    assert server.calls[0][1] is sentinel


class _PyglsLikeServer(_FakeServer):
    """Stand-in that mimics pygls' `server.protocol.fm._features` registry."""

    def __init__(self, prewired: list[str] | None = None) -> None:
        super().__init__()
        self.protocol = type("P", (), {"fm": type("F", (), {"_features": dict.fromkeys(prewired or [])})()})()

    def feature(self, method: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        decorator = super().feature(method)

        def wrapper(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.protocol.fm._features[method] = handler  # type: ignore[attr-defined]
            return decorator(handler)

        return wrapper


def test_wire_skips_already_registered_methods() -> None:
    """Idempotency guard: a method already in pygls' feature map is not re-registered."""
    entry = _by_suffix("hover")
    server = _PyglsLikeServer(prewired=[entry.lsp_method])
    HandlerWiring(server, _make_handlers([entry.lsp_method])).wire_eager(entries=(entry,))
    assert server.calls == []


def test_wire_succeeds_when_pygls_state_is_absent() -> None:
    """If `server.protocol.fm._features` isn't reachable, fall through to normal wiring."""
    entry = _by_suffix("hover")
    server = _FakeServer()  # plain fake — no protocol.fm
    HandlerWiring(server, _make_handlers([entry.lsp_method])).wire_eager(entries=(entry,))
    assert [c[0] for c in server.calls] == [entry.lsp_method]
