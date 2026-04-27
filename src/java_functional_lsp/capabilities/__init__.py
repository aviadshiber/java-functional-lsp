"""Capability negotiation — splits jdtls features into static-advertised vs
dynamically-registered based on what the LSP client claims to support."""

from __future__ import annotations

from .handler_wiring import HandlerWiring
from .negotiator import CapabilityNegotiator, NegotiationResult
from .probe import ClientCapabilityProbe
from .registry import REGISTRY, CapabilityEntry, all_methods
from .static_builder import StaticCapabilityBuilder

__all__ = [
    "REGISTRY",
    "CapabilityEntry",
    "CapabilityNegotiator",
    "ClientCapabilityProbe",
    "HandlerWiring",
    "NegotiationResult",
    "StaticCapabilityBuilder",
    "all_methods",
]
