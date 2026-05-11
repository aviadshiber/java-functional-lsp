"""Java code quality analyzers using tree-sitter.

This module exposes ``KNOWN_RULES`` — the union of every rule code emitted by any analyzer
in this package. The server uses it as a guardrail: ``_FIX_TITLES`` keys must be a subset of
``KNOWN_RULES`` so a typo in a fix registration is caught at import time rather than as a
silently-missing code action at runtime.
"""

from __future__ import annotations

from .exception_checker import _MESSAGES as _EXCEPTION_MESSAGES
from .functional_checker import _MESSAGES as _FUNCTIONAL_MESSAGES
from .mutation_checker import _MESSAGES as _MUTATION_MESSAGES
from .null_checker import _MESSAGES as _NULL_MESSAGES
from .spring_checker import _MESSAGES as _SPRING_MESSAGES

# ``impure-method-io`` / ``impure-method-throw`` are internal data-payload keys; the rule
# code emitted on the wire is ``impure-method`` regardless of variant, so we register only
# the wire-visible name here.
_IMPURE_METHOD_INTERNAL_KEYS = {"impure-method-io", "impure-method-throw"}

_ALL_MESSAGE_KEYS: set[str] = (
    set(_FUNCTIONAL_MESSAGES.keys())
    | set(_MUTATION_MESSAGES.keys())
    | set(_EXCEPTION_MESSAGES.keys())
    | set(_SPRING_MESSAGES.keys())
    | set(_NULL_MESSAGES.keys())
)
KNOWN_RULES: frozenset[str] = frozenset((_ALL_MESSAGE_KEYS - _IMPURE_METHOD_INTERNAL_KEYS) | {"impure-method"})
