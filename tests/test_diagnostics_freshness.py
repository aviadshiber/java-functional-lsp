"""Tests for holding edit-triggered publishes until jdtls re-validates (#109)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from lsprotocol import types as lsp
from pygls.workspace import Workspace

from java_functional_lsp import server as srv_mod
from java_functional_lsp.proxy import JdtlsProxy
from java_functional_lsp.server import (
    _HOLD_ALL,
    _HOLD_CUSTOM_FIRST,
    _HOLD_OFF,
    _JdtlsFreshness,
    _resolve_hold_mode,
    on_did_change,
    on_did_close,
    on_did_save,
    server,
)

URI = "file:///mod/src/Foo.java"
MODULE = "file:///mod"
SOURCE = "class Foo { String f() { return null; } }"


def _java_diag(message: str, line: int = 0) -> dict[str, Any]:
    return {
        "range": {"start": {"line": line, "character": 0}, "end": {"line": line, "character": 3}},
        "message": message,
        "severity": 1,
        "source": "Java",
    }


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


# --- _JdtlsFreshness (pure state, fake clock) ---


class TestFreshnessState:
    def _state(self) -> tuple[_JdtlsFreshness, FakeClock]:
        clock = FakeClock()
        return _JdtlsFreshness(clock=clock), clock

    def test_publish_after_min_age_releases(self) -> None:
        state, clock = self._state()
        state.note_forwarded_change()
        state.mark_pending("k")
        clock.now += 0.5
        assert state.on_publish("k", "sig") == "released"
        assert not state.is_pending("k")

    def test_publish_right_after_change_is_too_early(self) -> None:
        state, clock = self._state()
        state.note_forwarded_change()
        state.mark_pending("k")
        clock.now += 0.1
        assert state.on_publish("k", "stale") == "too_early"
        assert state.is_pending("k")

    def test_change_to_other_file_makes_publish_too_early(self) -> None:
        """jdtls has one validation job; any file's change restarts it."""
        state, clock = self._state()
        state.note_forwarded_change()
        state.mark_pending("a")
        clock.now += 1.0
        state.note_forwarded_change()  # edit to another file
        clock.now += 0.1
        assert state.on_publish("a", "sig") == "too_early"

    def test_untracked_uri(self) -> None:
        state, _ = self._state()
        assert state.on_publish("k", "sig") == "untracked"

    def test_deadline_extends_with_later_changes_but_caps_at_ceiling(self) -> None:
        state, clock = self._state()
        state.note_forwarded_change()
        state.mark_pending("a")
        start = clock.now
        assert state.deadline("a") == pytest.approx(start + state.DEADLINE_AFTER_CHANGE)
        for _ in range(20):
            clock.now += 1.0
            state.note_forwarded_change()
        assert state.deadline("a") == pytest.approx(start + state.CEILING)

    def test_expire_only_after_deadline(self) -> None:
        state, clock = self._state()
        state.note_forwarded_change()
        state.mark_pending("k")
        clock.now += state.DEADLINE_AFTER_CHANGE - 0.01
        assert not state.expire("k")
        clock.now += 0.02
        assert state.expire("k")
        assert not state.is_pending("k")
        assert state.counts["timeout"] == 1

    def test_ewma_raises_min_age_and_deadline(self) -> None:
        state, clock = self._state()
        for _ in range(10):
            state.note_forwarded_change()
            state.mark_pending("k")
            clock.now += 4.0
            assert state.on_publish("k", "sig") == "released"
        assert state.fresh_min_age() == pytest.approx(2.0, rel=0.05)
        state.note_forwarded_change()
        state.mark_pending("k")
        assert state.deadline("k") == pytest.approx(clock.now + 8.0, rel=0.05)

    def test_late_correction_counted_once(self) -> None:
        state, clock = self._state()
        state.note_forwarded_change()
        state.mark_pending("k")
        clock.now += 0.5
        state.on_publish("k", "first")
        clock.now += 0.5
        assert state.on_publish("k", "first") == "untracked"
        assert state.on_publish("k", "second") == "late_correction"
        assert state.on_publish("k", "third") == "untracked"
        assert state.counts["late_correction"] == 1

    def test_different_signature_after_window_is_not_late(self) -> None:
        state, clock = self._state()
        state.note_forwarded_change()
        state.mark_pending("k")
        clock.now += 0.5
        state.on_publish("k", "first")
        clock.now += state.LATE_CORRECTION_WINDOW + 1
        assert state.on_publish("k", "second") == "untracked"

    def test_reset_returns_pending_count(self) -> None:
        state, _ = self._state()
        state.note_forwarded_change()
        state.mark_pending("a")
        state.mark_pending("b")
        assert state.reset() == 2
        assert not state.is_pending("a")

    def test_summary_mentions_counts(self) -> None:
        state, clock = self._state()
        state.note_forwarded_change()
        state.mark_pending("k")
        clock.now += 0.5
        state.on_publish("k", "s")
        assert "released=1" in state.summary()


class TestHoldMode:
    def test_claude_code_gets_custom_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JAVA_FUNCTIONAL_LSP_DIAG_HOLD", raising=False)
        assert _resolve_hold_mode("Claude Code") == _HOLD_CUSTOM_FIRST

    def test_other_clients_get_hold_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JAVA_FUNCTIONAL_LSP_DIAG_HOLD", raising=False)
        assert _resolve_hold_mode("Visual Studio Code") == _HOLD_ALL

    @pytest.mark.parametrize("value", ["off", "OFF ", "hold-all", "custom-first"])
    def test_env_override(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("JAVA_FUNCTIONAL_LSP_DIAG_HOLD", value)
        assert _resolve_hold_mode("Claude Code") == value.strip().lower()

    def test_unknown_env_value_falls_back(self, monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
        monkeypatch.setenv("JAVA_FUNCTIONAL_LSP_DIAG_HOLD", "bogus")
        with caplog.at_level(logging.WARNING, logger="java_functional_lsp.server"):
            assert _resolve_hold_mode("Neovim") == _HOLD_ALL
        assert "bogus" in caplog.text


# --- Wiring: handlers + hold task against a mocked live jdtls ---


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """A server whose jdtls looks live, with a ready module, fast timings and a captured publisher."""
    if server.protocol._workspace is None:
        server.protocol._workspace = Workspace(root_uri="file:///mod", sync_kind=lsp.TextDocumentSyncKind.Full)
    server.workspace.put_text_document(lsp.TextDocumentItem(uri=URI, language_id="java", version=1, text=SOURCE))
    server._record_opened(URI)
    fresh = _JdtlsFreshness()
    fresh.FRESH_MIN_AGE = 0.05
    fresh.DEADLINE_AFTER_CHANGE = 0.4
    fresh.CEILING = 1.0
    monkeypatch.setattr(srv_mod, "_freshness", fresh)
    monkeypatch.setattr(srv_mod, "_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(srv_mod, "_resolve_module_uri", MagicMock(return_value=MODULE))
    monkeypatch.setattr(server, "_skip_jdtls", False)
    monkeypatch.setattr(server, "_hold_mode", _HOLD_ALL)
    monkeypatch.setattr(server._proxy, "_available", True)
    monkeypatch.setattr(server._proxy, "send_notification", AsyncMock())
    server._proxy.modules.mark_ready(MODULE)
    server._proxy._diagnostics_cache.clear()
    publisher = MagicMock()
    with patch.object(server, "text_document_publish_diagnostics", publisher):
        yield publisher
    for task in list(srv_mod._pending.values()):
        task.cancel()
    srv_mod._pending.clear()
    srv_mod._hold_events.clear()
    server._proxy._diagnostics_cache.clear()
    server._proxy.modules.clear()
    server._session_opened_uris.pop(URI, None)
    server.workspace.remove_text_document(URI)


def _published(publisher: MagicMock) -> list[list[lsp.Diagnostic]]:
    return [call.args[0].diagnostics for call in publisher.call_args_list]


def _messages(diags: list[lsp.Diagnostic], source: str) -> list[str]:
    return [d.message for d in diags if d.source == source]


async def _edit() -> None:
    await on_did_change(
        lsp.DidChangeTextDocumentParams(
            text_document=lsp.VersionedTextDocumentIdentifier(uri=URI, version=2),
            content_changes=[lsp.TextDocumentContentChangeWholeDocument(text=SOURCE)],
        )
    )


def _jdtls_publishes(diags: list[dict[str, Any]], uri: str = URI) -> None:
    server._proxy._handle_notification(
        {"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": diags}}
    )


class TestHoldWiring:
    async def test_hold_all_publishes_only_fresh_jdtls(self, live: MagicMock) -> None:
        _jdtls_publishes([_java_diag("Bar cannot be resolved to a type")])  # pre-edit state
        live.reset_mock()
        await _edit()
        await asyncio.sleep(0.03)
        assert live.call_count == 0, "stale merge must not be published at the debounce"
        await asyncio.sleep(0.05)
        _jdtls_publishes([])  # jdtls re-validated: import fixed
        await asyncio.sleep(0.02)
        published = _published(live)
        assert len(published) == 1
        assert _messages(published[0], "Java") == []
        assert _messages(published[0], "java-functional-lsp")

    async def test_custom_first_omits_stale_java_then_merges(self, live: MagicMock) -> None:
        server._hold_mode = _HOLD_CUSTOM_FIRST
        _jdtls_publishes([_java_diag("Bar cannot be resolved to a type")])
        live.reset_mock()
        await _edit()
        await asyncio.sleep(0.03)
        first = _published(live)
        assert len(first) == 1
        assert _messages(first[0], "Java") == [], "stale jdtls set must not ride on the custom-first publish"
        assert _messages(first[0], "java-functional-lsp")
        await asyncio.sleep(0.05)
        _jdtls_publishes([_java_diag("The constructor Foo(int) is undefined", 1)])
        await asyncio.sleep(0.02)
        final = _published(live)[-1]
        assert _messages(final, "Java") == ["The constructor Foo(int) is undefined"]

    async def test_too_early_publish_keeps_holding(self, live: MagicMock) -> None:
        await _edit()
        _jdtls_publishes([_java_diag("stale in-flight result")])  # arrives right after the change
        await asyncio.sleep(0.03)
        assert live.call_count == 0
        await asyncio.sleep(0.05)
        _jdtls_publishes([])
        await asyncio.sleep(0.02)
        assert _messages(_published(live)[-1], "Java") == []

    async def test_rapid_edits_never_publish_superseded_jdtls_set(self, live: MagicMock) -> None:
        _jdtls_publishes([_java_diag("Bar cannot be resolved to a type")])
        live.reset_mock()
        await _edit()
        await asyncio.sleep(0.06)
        await _edit()  # N+1 before jdtls answered N
        _jdtls_publishes([_java_diag("Bar cannot be resolved to a type")])  # in-flight result for N
        await asyncio.sleep(0.06)
        _jdtls_publishes([])  # result for N+1
        await asyncio.sleep(0.02)
        published = _published(live)
        assert published, "the fresh result must be published"
        assert all(_messages(p, "Java") == [] for p in published)

    async def test_timeout_publishes_cached(self, live: MagicMock) -> None:
        _jdtls_publishes([_java_diag("cached")])
        live.reset_mock()
        await _edit()
        await asyncio.sleep(0.2)
        assert live.call_count == 0
        await asyncio.sleep(0.35)
        assert _messages(_published(live)[-1], "Java") == ["cached"]
        assert srv_mod._freshness.counts["timeout"] == 1

    async def test_save_while_pending_does_not_publish(self, live: MagicMock) -> None:
        await _edit()
        live.reset_mock()
        await on_did_save(lsp.DidSaveTextDocumentParams(text_document=lsp.TextDocumentIdentifier(uri=URI)))
        assert live.call_count == 0

    async def test_save_when_not_pending_publishes(self, live: MagicMock) -> None:
        await on_did_save(lsp.DidSaveTextDocumentParams(text_document=lsp.TextDocumentIdentifier(uri=URI)))
        assert live.call_count == 1

    async def test_module_not_ready_uses_debounce_path(self, live: MagicMock) -> None:
        _jdtls_publishes([_java_diag("cached")])
        server._proxy.modules.clear()  # after the publish, which would mark the module READY again
        live.reset_mock()
        await _edit()
        await asyncio.sleep(0.03)
        assert live.call_count == 1
        assert srv_mod._freshness.counts["ineligible"] == 1

    async def test_diagnostic_filter_uri_is_not_held(self, live: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            server, "_config", {"jdtls": {"settings": {"java": {"diagnostic": {"filter": ["*/src/*.java"]}}}}}
        )
        live.reset_mock()
        await _edit()
        await asyncio.sleep(0.03)
        assert live.call_count == 1

    async def test_hold_off_keeps_old_behavior(self, live: MagicMock) -> None:
        server._hold_mode = _HOLD_OFF
        _jdtls_publishes([_java_diag("cached")])
        live.reset_mock()
        await _edit()
        await asyncio.sleep(0.03)
        assert _messages(_published(live)[-1], "Java") == ["cached"]

    async def test_jdtls_unavailable_uses_debounce_path(self, live: MagicMock) -> None:
        server._proxy._available = False
        await _edit()
        await asyncio.sleep(0.03)
        assert live.call_count == 1
        assert not srv_mod._freshness.is_pending(URI)

    async def test_jdtls_stop_releases_hold(self, live: MagicMock) -> None:
        _jdtls_publishes([_java_diag("from dead jdtls")])
        live.reset_mock()
        await _edit()
        await asyncio.sleep(0.03)
        server._proxy._mark_stopped()
        await asyncio.sleep(0.02)
        published = _published(live)
        assert len(published) == 1
        assert _messages(published[0], "Java") == []

    async def test_close_while_pending_cancels_hold(self, live: MagicMock) -> None:
        await _edit()
        await asyncio.sleep(0.03)
        await on_did_close(lsp.DidCloseTextDocumentParams(text_document=lsp.TextDocumentIdentifier(uri=URI)))
        live.reset_mock()
        await asyncio.sleep(0.5)
        assert live.call_count == 0
        assert not srv_mod._freshness.is_pending(URI)

    async def test_jdtls_uri_encoding_publishes_under_client_uri(self, live: MagicMock) -> None:
        client_uri = "file:///mod/src/My Foo.java"
        server.workspace.put_text_document(
            lsp.TextDocumentItem(uri=client_uri, language_id="java", version=1, text=SOURCE)
        )
        server._record_opened(client_uri)
        try:
            _jdtls_publishes([_java_diag("err")], uri="file:///mod/src/My%20Foo.java")
            assert live.call_args.args[0].uri == client_uri
            assert server._proxy.get_cached_diagnostics(client_uri)[0]["message"] == "err"
        finally:
            server._session_opened_uris.pop(srv_mod._normalize_uri(client_uri), None)
            server.workspace.remove_text_document(client_uri)


# --- Proxy additions ---


class TestProxyFreshnessSupport:
    def test_stop_clears_cache_and_notifies(self) -> None:
        stopped = MagicMock()
        proxy = JdtlsProxy(on_stopped=stopped)
        proxy._diagnostics_cache["file:///a.java"] = [_java_diag("x")]
        proxy._mark_stopped()
        assert proxy._diagnostics_cache == {}
        assert not proxy.is_available
        stopped.assert_called_once()

    async def test_reader_eof_marks_stopped(self) -> None:
        stopped = MagicMock()
        proxy = JdtlsProxy(on_stopped=stopped)
        reader = asyncio.StreamReader()
        reader.feed_eof()
        await proxy._reader_loop(reader)
        stopped.assert_called_once()

    def test_dropped_requests_counted_by_method(self, caplog: Any) -> None:
        proxy = JdtlsProxy()
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.proxy"):
            for request_id in (1, 2):
                proxy._dispatch_message(
                    {"id": request_id, "method": "workspace/configuration", "params": {"items": [{"section": "x"}]}}
                )
        assert proxy._dropped_request_counts == {"workspace/configuration": 2}
        assert caplog.text.count("workspace/configuration dropped") == 1
        assert "section" not in caplog.text
