"""Tests for holding edit-triggered publishes until jdtls re-validates (#109)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
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
    on_did_open,
    on_did_save,
    on_initialize,
    server,
)

URI = "file:///mod/src/Foo.java"
OTHER_URI = "file:///mod/src/Other.java"
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

    def _release(self, state: _JdtlsFreshness, clock: FakeClock, after: float, key: str = "k") -> str:
        state.note_forwarded_change()
        state.mark_pending(key)
        clock.now += after
        return state.on_publish(key, lambda: "sig")

    def test_publish_after_min_age_releases(self) -> None:
        state, clock = self._state()
        assert self._release(state, clock, 0.5) == "released"
        assert not state.is_pending("k")

    def test_publish_right_after_change_is_too_early(self) -> None:
        state, clock = self._state()
        assert self._release(state, clock, 0.1) == "too_early"
        assert state.is_pending("k")

    def test_change_to_other_file_does_not_block_release(self) -> None:
        """A publish reflects this file's latest content once its own edit is old enough."""
        state, clock = self._state()
        state.note_forwarded_change()
        state.mark_pending("a")
        clock.now += 1.0
        state.note_forwarded_change()  # edit to another file
        clock.now += 0.1
        assert state.on_publish("a", lambda: "sig") == "released"

    def test_signature_is_computed_only_when_needed(self) -> None:
        state, _ = self._state()
        signature = MagicMock(return_value="sig")
        assert state.on_publish("k", signature) == "untracked"
        signature.assert_not_called()

    def test_untracked_uri(self) -> None:
        state, _ = self._state()
        assert state.on_publish("k", lambda: "sig") == "untracked"

    def test_no_forwarded_change_still_releases_after_min_age(self) -> None:
        state, clock = self._state()
        state.mark_pending("k")
        clock.now += 0.5
        assert state.on_publish("k", lambda: "sig") == "released"

    def test_deadline_extends_with_later_changes_but_caps_at_ceiling(self) -> None:
        state, clock = self._state()
        state.note_forwarded_change()
        state.mark_pending("a")
        start = clock.now
        assert state.deadline("a") == pytest.approx(start + state.deadline_after_change)
        for _ in range(20):
            clock.now += 1.0
            state.note_forwarded_change()
        assert state.deadline("a") == pytest.approx(start + state.ceiling)

    def test_ceiling_counts_from_the_files_latest_edit(self) -> None:
        state, clock = self._state()
        for _ in range(20):  # keeps typing in the same file
            state.note_forwarded_change()
            state.mark_pending("a")
            clock.now += 1.0
        assert state.deadline("a") == pytest.approx(clock.now - 1.0 + state.deadline_after_change)

    def test_constructor_timing_overrides(self) -> None:
        state = _JdtlsFreshness(clock=FakeClock(), fresh_min_age=0.1, deadline_after_change=1.0, ceiling=2.0)
        assert (state.fresh_min_age, state.deadline_after_change, state.ceiling) == (0.1, 1.0, 2.0)

    def test_production_defaults_match_readme(self) -> None:
        state = _JdtlsFreshness()
        assert (state.fresh_min_age, state.deadline_after_change, state.ceiling) == (0.4, 3.0, 10.0)

    def test_expire_only_after_deadline(self) -> None:
        state, clock = self._state()
        state.note_forwarded_change()
        state.mark_pending("k")
        clock.now += state.deadline_after_change - 0.01
        assert not state.expire("k")
        clock.now += 0.02
        assert state.expire("k")
        assert not state.is_pending("k")
        assert state.counts["timeout"] == 1

    def test_slow_jdtls_stretches_deadline(self) -> None:
        state, clock = self._state()
        for _ in range(10):
            assert self._release(state, clock, 4.0) == "released"
        state.note_forwarded_change()
        state.mark_pending("k")
        assert state.deadline("k") == pytest.approx(clock.now + 8.0, rel=0.05)

    def test_slow_release_does_not_reject_later_normal_publishes(self) -> None:
        state, clock = self._state()
        assert self._release(state, clock, 2.4) == "released"
        assert self._release(state, clock, 0.5) == "released"

    def test_late_correction_counted_once(self) -> None:
        state, clock = self._state()
        state.note_forwarded_change()
        state.mark_pending("k")
        clock.now += 0.5
        state.on_publish("k", lambda: "first")
        clock.now += 0.5
        assert state.on_publish("k", lambda: "first") == "untracked"
        assert state.on_publish("k", lambda: "second") == "late_correction"
        assert state.on_publish("k", lambda: "third") == "untracked"
        assert state.counts["late_correction"] == 1

    def test_new_edit_closes_late_correction_window(self) -> None:
        state, clock = self._state()
        self._release(state, clock, 0.5)
        state.mark_pending("k")  # next edit
        clock.now += 0.5
        assert state.on_publish("k", lambda: "other") == "released"
        assert state.counts["late_correction"] == 0

    def test_different_signature_after_window_is_not_late(self) -> None:
        state, clock = self._state()
        state.note_forwarded_change()
        state.mark_pending("k")
        clock.now += 0.5
        state.on_publish("k", lambda: "first")
        clock.now += state.LATE_CORRECTION_WINDOW + 1
        assert state.on_publish("k", lambda: "second") == "untracked"

    def test_reset_returns_pending_count(self) -> None:
        state, _ = self._state()
        state.note_forwarded_change()
        state.mark_pending("a")
        state.mark_pending("b")
        assert state.reset() == 2
        assert not state.is_pending("a")

    def test_summary_mentions_counts(self) -> None:
        state, clock = self._state()
        self._release(state, clock, 0.5)
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


def _put_document(uri: str) -> None:
    server.workspace.put_text_document(lsp.TextDocumentItem(uri=uri, language_id="java", version=1, text=SOURCE))
    server._record_opened(uri)


@pytest.fixture
async def live(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[MagicMock]:
    """A server whose jdtls looks live, with a ready module, fast debounce and a captured publisher.

    Deadlines are long so that release-path tests never depend on wall-clock speed;
    timeout tests shorten them explicitly.
    """
    created_workspace = server.protocol._workspace is None
    if created_workspace:
        server.protocol._workspace = Workspace(root_uri=MODULE, sync_kind=lsp.TextDocumentSyncKind.Full)
    _put_document(URI)
    fresh = _JdtlsFreshness(fresh_min_age=0.05, deadline_after_change=30.0, ceiling=60.0)
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
        tasks = [srv_mod._pending.pop(uri) for uri in (URI, OTHER_URI) if uri in srv_mod._pending]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, *list(srv_mod._bg_tasks), return_exceptions=True)
    server._proxy._diagnostics_cache.clear()
    server._proxy.modules.clear()
    for uri in (URI, OTHER_URI):
        server._session_opened_uris.pop(srv_mod._normalize_uri(uri), None)
        server.workspace.remove_text_document(uri)
    if created_workspace:
        server.protocol._workspace = None


def _published(publisher: MagicMock) -> list[lsp.PublishDiagnosticsParams]:
    return [call.args[0] for call in publisher.call_args_list]


def _messages(params: lsp.PublishDiagnosticsParams, source: str) -> list[str]:
    return [d.message for d in params.diagnostics if d.source == source]


async def _until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached in time"
        await asyncio.sleep(0.005)


async def _edit(uri: str = URI) -> None:
    await on_did_change(
        lsp.DidChangeTextDocumentParams(
            text_document=lsp.VersionedTextDocumentIdentifier(uri=uri, version=2),
            content_changes=[lsp.TextDocumentContentChangeWholeDocument(text=SOURCE)],
        )
    )


async def _past_debounce() -> None:
    await asyncio.sleep(0.03)


async def _past_min_age() -> None:
    await asyncio.sleep(0.08)


def _jdtls_publishes(diags: list[dict[str, Any]], uri: str = URI) -> None:
    server._proxy._handle_notification(
        {"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": diags}}
    )


def _short_deadlines() -> None:
    srv_mod._freshness.deadline_after_change = 0.3
    srv_mod._freshness.ceiling = 1.0


class TestHoldWiring:
    async def test_hold_all_publishes_only_fresh_jdtls(self, live: MagicMock) -> None:
        _jdtls_publishes([_java_diag("Bar cannot be resolved to a type")])  # pre-edit state
        live.reset_mock()
        await _edit()
        await _past_debounce()
        assert live.call_count == 0, "stale merge must not be published at the debounce"
        await _past_min_age()
        _jdtls_publishes([])  # jdtls re-validated: import fixed
        await _until(lambda: live.call_count == 1)
        final = _published(live)[0]
        assert _messages(final, "Java") == []
        assert _messages(final, "java-functional-lsp")
        assert srv_mod._freshness.counts["released"] == 1

    async def test_custom_first_omits_stale_java_then_merges(self, live: MagicMock) -> None:
        server._hold_mode = _HOLD_CUSTOM_FIRST
        _jdtls_publishes([_java_diag("Bar cannot be resolved to a type")])
        live.reset_mock()
        await _edit()
        await _until(lambda: live.call_count == 1)
        first = _published(live)[0]
        assert _messages(first, "Java") == [], "stale jdtls set must not ride on the custom-first publish"
        assert _messages(first, "java-functional-lsp")
        await _past_min_age()
        _jdtls_publishes([_java_diag("The constructor Foo(int) is undefined", 1)])
        await _until(lambda: live.call_count == 2)
        assert _messages(_published(live)[1], "Java") == ["The constructor Foo(int) is undefined"]

    async def test_custom_first_timeout_falls_back_to_cache(self, live: MagicMock) -> None:
        server._hold_mode = _HOLD_CUSTOM_FIRST
        _short_deadlines()
        _jdtls_publishes([_java_diag("cached")])
        live.reset_mock()
        await _edit()
        await _until(lambda: live.call_count == 2, timeout=3.0)
        custom_only, fallback = _published(live)
        assert _messages(custom_only, "Java") == []
        assert _messages(fallback, "Java") == ["cached"]
        assert srv_mod._freshness.counts["timeout"] == 1

    async def test_too_early_publish_keeps_holding(self, live: MagicMock) -> None:
        await _edit()
        _jdtls_publishes([_java_diag("stale in-flight result")])  # arrives right after the change
        await _past_debounce()
        assert live.call_count == 0
        await _past_min_age()
        _jdtls_publishes([])
        await _until(lambda: live.call_count == 1)
        assert _messages(_published(live)[0], "Java") == []

    async def test_rapid_edits_never_publish_superseded_jdtls_set(self, live: MagicMock) -> None:
        _jdtls_publishes([_java_diag("Bar cannot be resolved to a type")])
        live.reset_mock()
        await _edit()
        await _past_min_age()
        await _edit()  # N+1 before jdtls answered N
        _jdtls_publishes([_java_diag("Bar cannot be resolved to a type")])  # in-flight result for N
        await _past_min_age()
        _jdtls_publishes([])  # result for N+1
        await _until(lambda: live.call_count == 1)
        await _past_debounce()
        published = _published(live)
        assert len(published) == 1
        assert _messages(published[0], "Java") == []
        assert srv_mod._freshness.counts["too_early"] == 1

    async def test_edit_to_other_file_does_not_delay_release(self, live: MagicMock) -> None:
        _put_document(OTHER_URI)
        await _edit(URI)
        await _past_min_age()
        await _edit(OTHER_URI)
        _jdtls_publishes([_java_diag("fresh for Foo")], uri=URI)  # right after Other's change
        await _until(lambda: live.call_count == 1)
        published = _published(live)[0]
        assert published.uri == URI
        assert _messages(published, "Java") == ["fresh for Foo"]
        assert srv_mod._freshness.is_pending(srv_mod._normalize_uri(OTHER_URI))
        assert srv_mod._freshness.counts["too_early"] == 0

    async def test_timeout_publishes_cached(self, live: MagicMock) -> None:
        _short_deadlines()
        _jdtls_publishes([_java_diag("cached")])
        live.reset_mock()
        await _edit()
        await _past_debounce()
        assert live.call_count == 0
        await _until(lambda: live.call_count == 1, timeout=2.0)
        assert _messages(_published(live)[0], "Java") == ["cached"]
        assert srv_mod._freshness.counts["timeout"] == 1

    async def test_save_while_pending_defers_to_hold(self, live: MagicMock) -> None:
        await _edit()
        live.reset_mock()
        await on_did_save(lsp.DidSaveTextDocumentParams(text_document=lsp.TextDocumentIdentifier(uri=URI)))
        assert live.call_count == 0
        sent = server._proxy.send_notification
        assert isinstance(sent, AsyncMock)
        forwarded = [call.args[0] for call in sent.call_args_list]
        assert "textDocument/didSave" in forwarded
        await _past_min_age()
        _jdtls_publishes([])
        await _until(lambda: live.call_count == 1)
        assert _messages(_published(live)[0], "Java") == []

    async def test_save_when_not_pending_publishes(self, live: MagicMock) -> None:
        await on_did_save(lsp.DidSaveTextDocumentParams(text_document=lsp.TextDocumentIdentifier(uri=URI)))
        assert live.call_count == 1

    async def test_late_correction_republishes(self, live: MagicMock) -> None:
        await _edit()
        await _past_min_age()
        _jdtls_publishes([_java_diag("first")])
        await _until(lambda: live.call_count == 1)
        _jdtls_publishes([_java_diag("corrected")])
        assert live.call_count == 2
        assert _messages(_published(live)[1], "Java") == ["corrected"]
        assert srv_mod._freshness.counts["late_correction"] == 1

    async def test_did_open_while_pending_skips_stale_jdtls_and_hold_still_releases(self, live: MagicMock) -> None:
        _jdtls_publishes([_java_diag("stale")])
        live.reset_mock()
        await _edit()
        with patch.object(server._proxy, "add_module_if_new", AsyncMock()):
            await on_did_open(
                lsp.DidOpenTextDocumentParams(
                    text_document=lsp.TextDocumentItem(uri=URI, language_id="java", version=3, text=SOURCE)
                )
            )
        assert live.call_count == 1
        assert _messages(_published(live)[0], "Java") == []
        assert _messages(_published(live)[0], "java-functional-lsp")
        await _past_min_age()
        _jdtls_publishes([_java_diag("fresh")])
        await _until(lambda: live.call_count == 2)
        assert _messages(_published(live)[1], "Java") == ["fresh"]

    async def test_module_not_ready_uses_debounce_path(self, live: MagicMock) -> None:
        _jdtls_publishes([_java_diag("cached")])
        server._proxy.modules.clear()  # after the publish, which would mark the module READY again
        live.reset_mock()
        await _edit()
        await _until(lambda: live.call_count == 1)
        assert _messages(_published(live)[0], "Java") == ["cached"]
        assert srv_mod._freshness.counts["ineligible"] == 1

    @pytest.mark.parametrize("patterns", [["**/generated/*.java"], [42]])
    async def test_non_matching_diagnostic_filter_still_holds(
        self, live: MagicMock, monkeypatch: pytest.MonkeyPatch, patterns: list[Any]
    ) -> None:
        monkeypatch.setattr(server, "_config", {"jdtls": {"settings": {"java": {"diagnostic": {"filter": patterns}}}}})
        await _edit()
        assert srv_mod._freshness.is_pending(URI)
        assert live.call_count == 0

    async def test_diagnostic_filter_uri_is_not_held(self, live: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            server, "_config", {"jdtls": {"settings": {"java": {"diagnostic": {"filter": ["*/src/*.java"]}}}}}
        )
        live.reset_mock()
        await _edit()
        assert not srv_mod._freshness.is_pending(URI)
        assert srv_mod._freshness.counts["ineligible"] == 1
        await _until(lambda: live.call_count == 1)

    async def test_unopened_file_is_not_held(self, live: MagicMock) -> None:
        server._session_opened_uris.pop(URI, None)
        await _edit()
        assert not srv_mod._freshness.is_pending(URI)
        await _until(lambda: live.call_count == 1)

    async def test_hold_off_keeps_old_behavior(self, live: MagicMock) -> None:
        server._hold_mode = _HOLD_OFF
        _jdtls_publishes([_java_diag("cached")])
        live.reset_mock()
        await _edit()
        await _until(lambda: live.call_count == 1)
        assert _messages(_published(live)[0], "Java") == ["cached"]

    async def test_jdtls_unavailable_uses_debounce_path(self, live: MagicMock) -> None:
        server._proxy._available = False
        await _edit()
        assert not srv_mod._freshness.is_pending(URI)
        await _until(lambda: live.call_count == 1)

    async def test_jdtls_stop_releases_hold(self, live: MagicMock) -> None:
        _jdtls_publishes([_java_diag("from dead jdtls")])
        live.reset_mock()
        await _edit()
        await _past_debounce()
        server._proxy._mark_stopped()
        assert server._proxy._diagnostics_cache == {}
        await _until(lambda: live.call_count == 1)
        assert _messages(_published(live)[0], "Java") == []

    async def test_reinitialize_releases_hold(self, live: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        for attr in (
            "_config",
            "_init_params",
            "_user_suppress_patterns",
            "_dynamic_features",
            "_init_generation",
            "_skip_jdtls_registration",
        ):
            monkeypatch.setattr(server, attr, getattr(server, attr))
        monkeypatch.setattr(srv_mod, "_jdtls_capabilities_registered", srv_mod._jdtls_capabilities_registered)
        monkeypatch.setattr(srv_mod, "HandlerWiring", MagicMock())
        monkeypatch.delenv("JAVA_FUNCTIONAL_LSP_DIAG_HOLD", raising=False)
        await _edit()
        await _past_debounce()
        with patch.object(srv_mod, "_load_config", return_value={}):
            on_initialize(
                lsp.InitializeParams(
                    capabilities=lsp.ClientCapabilities(),
                    root_uri=MODULE,
                    client_info=lsp.ClientInfo(name="Claude Code"),
                )
            )
        assert not srv_mod._freshness.is_pending(URI)
        assert server._hold_mode == _HOLD_CUSTOM_FIRST
        await _until(lambda: live.call_count == 1)

    async def test_close_while_pending_cancels_hold(self, live: MagicMock) -> None:
        _short_deadlines()
        await _edit()
        await _past_debounce()
        hold_task = srv_mod._pending[URI]
        await on_did_close(lsp.DidCloseTextDocumentParams(text_document=lsp.TextDocumentIdentifier(uri=URI)))
        live.reset_mock()
        await asyncio.sleep(0.4)  # past the (short) deadline: an uncancelled hold would have published
        assert live.call_count == 0
        assert hold_task.cancelled()
        assert not srv_mod._freshness.is_pending(URI)
        assert URI not in srv_mod._hold_events

    async def test_superseded_hold_task_does_not_publish_or_steal_event(self, live: MagicMock) -> None:
        srv_mod._freshness.note_forwarded_change()
        srv_mod._freshness.mark_pending(URI)
        replacement = asyncio.create_task(asyncio.sleep(10))
        srv_mod._pending[URI] = replacement  # the live task for URI
        stray = asyncio.create_task(srv_mod._hold_until_fresh(URI, URI))
        await _past_min_age()
        _jdtls_publishes([])
        await asyncio.wait_for(stray, timeout=2.0)
        assert live.call_count == 0
        assert URI in srv_mod._hold_events, "the superseded task must not delete the live task's event"

    async def test_custom_first_publish_failure_does_not_strand_file(self, live: MagicMock) -> None:
        server._hold_mode = _HOLD_CUSTOM_FIRST
        with patch.object(srv_mod, "_run_analysis", side_effect=RuntimeError("boom")):
            await _edit()
            await _past_debounce()
        assert not srv_mod._freshness.is_pending(URI)
        _jdtls_publishes([])
        assert live.call_count == 1

    async def test_encoded_jdtls_uri_releases_hold_and_publishes_client_uri(self, live: MagicMock) -> None:
        client_uri = "file:///mod/src/My Foo.java"
        _put_document(client_uri)
        try:
            await _edit(client_uri)
            await _past_min_age()
            _jdtls_publishes([_java_diag("err")], uri="file:///mod/src/My%20Foo.java")
            await _until(lambda: live.call_count == 1)
            assert _published(live)[0].uri == client_uri
            assert _messages(_published(live)[0], "Java") == ["err"]
            assert server._proxy.get_cached_diagnostics(client_uri)[0]["message"] == "err"
        finally:
            task = srv_mod._pending.pop(client_uri, None)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
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

    async def test_reader_eof_marks_stopped_once(self) -> None:
        stopped = MagicMock()
        proxy = JdtlsProxy(on_stopped=stopped)
        proxy._available = True
        reader = asyncio.StreamReader()
        reader.feed_eof()
        await proxy._reader_loop(reader)
        proxy._mark_stopped()  # stop() after the EOF must not notify again
        stopped.assert_called_once()

    async def test_reader_error_marks_stopped(self) -> None:
        stopped = MagicMock()
        proxy = JdtlsProxy(on_stopped=stopped)
        proxy._available = True
        with patch("java_functional_lsp.proxy.read_message", AsyncMock(side_effect=ValueError("bad frame"))):
            await proxy._reader_loop(asyncio.StreamReader())
        stopped.assert_called_once()
        assert not proxy.is_available

    def test_dropped_request_method_is_truncated_and_capped(self) -> None:
        proxy = JdtlsProxy()
        proxy._note_dropped_request("x" * 500)
        for i in range(60):
            proxy._note_dropped_request(f"m{i}")
        assert "x" * 100 in proxy._dropped_request_counts
        assert len(proxy._dropped_request_counts) <= 51
        assert proxy._dropped_request_counts["<other>"] == 11

    def test_dropped_requests_counted_by_method(self, caplog: Any) -> None:
        proxy = JdtlsProxy()
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.proxy"):
            for request_id in (1, 2):
                proxy._dispatch_message(
                    {"id": request_id, "method": "workspace/configuration", "params": {"items": [{"section": "x"}]}}
                )
        assert proxy._dropped_request_counts == {"workspace/configuration": 2}
        assert caplog.text.count("'workspace/configuration' dropped") == 1
        assert "section" not in caplog.text


class TestLogLevel:
    @pytest.mark.parametrize(("value", "expected"), [("DEBUG", logging.DEBUG), (" warning ", logging.WARNING)])
    def test_valid_values(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: int) -> None:
        monkeypatch.setenv("JAVA_FUNCTIONAL_LSP_LOG_LEVEL", value)
        assert srv_mod._log_level_from_env() == expected

    @pytest.mark.parametrize("value", ["verbose", "", "basicConfig"])
    def test_invalid_values_fall_back_to_info(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("JAVA_FUNCTIONAL_LSP_LOG_LEVEL", value)
        assert srv_mod._log_level_from_env() == logging.INFO
