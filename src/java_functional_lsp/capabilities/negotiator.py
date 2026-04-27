"""Decide which capabilities to advertise statically vs dynamically.

Given the registry and a probe, split entries into two groups:
- dynamic: client advertised dynamicRegistration support → register via
  client/registerCapability after jdtls warms up. Preserves PR #44 behavior
  (don't claim hover before jdtls is ready, so the IDE keeps showing its
  own diagnostic tooltips).
- static: client did not → advertise in the InitializeResult so clients that
  ignore client/registerCapability (like Claude Code) still route correctly.

This module is pure logic — no I/O, no mutation of pygls state. That keeps it
trivially unit-testable with hand-built probes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .probe import ClientCapabilityProbe
from .registry import REGISTRY, CapabilityEntry


@dataclass(frozen=True)
class NegotiationResult:
    """Output of CapabilityNegotiator.negotiate.

    static: entries to advertise in InitializeResult.ServerCapabilities.
    dynamic: entries to register later via client/registerCapability.
    """

    static: tuple[CapabilityEntry, ...]
    dynamic: tuple[CapabilityEntry, ...]


class CapabilityNegotiator:
    """Splits a capability registry into static/dynamic subsets per client probe."""

    def __init__(
        self,
        probe: ClientCapabilityProbe,
        registry: Sequence[CapabilityEntry] = REGISTRY,
    ) -> None:
        self._registry = tuple(registry)
        self._probe = probe

    def negotiate(self) -> NegotiationResult:
        static: list[CapabilityEntry] = []
        dynamic: list[CapabilityEntry] = []
        for entry in self._registry:
            target = dynamic if self._probe.supports_dynamic(entry.client_cap_path) else static
            target.append(entry)
        return NegotiationResult(static=tuple(static), dynamic=tuple(dynamic))
