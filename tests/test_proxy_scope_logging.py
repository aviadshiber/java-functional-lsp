"""Tests for #110 part A: repository-bounded group scope (F2) and jdtls log forwarding (F3)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from java_functional_lsp.proxy import (
    _JDTLS_LOG_MAX_CHARS,
    _JDTLS_LOG_RATE,
    _JDTLS_LOG_WINDOW_SEC,
    JdtlsProxy,
    _find_maven_group_root,
    _find_repo_boundary,
    _JdtlsLogForwarder,
    _sanitize_jdtls_log,
)

_AGGREGATOR = "<project><modules><module>x</module></modules></project>"
_LEAF = "<project><artifactId>leaf</artifactId></project>"


@pytest.fixture(autouse=True)
def _clear_scope_caches() -> Iterator[None]:
    _find_maven_group_root.cache_clear()
    _find_repo_boundary.cache_clear()
    yield
    _find_maven_group_root.cache_clear()
    _find_repo_boundary.cache_clear()


def _has_git_ancestor(path: Path) -> bool:
    return any((p / ".git").exists() for p in [path, *path.parents])


@pytest.fixture
def git_tree(tmp_path: Path) -> dict[str, Path]:
    """``git/repo`` (.git, aggregator pom) with ``group/mod``, ``top-leaf`` and ``group/sub/leaf``."""
    assert not _has_git_ancestor(tmp_path), "tmp_path must not sit inside a git repository"
    git_dir = tmp_path / "git"
    repo = git_dir / "repo"
    group = repo / "group"
    mod = group / "mod"
    top_leaf = repo / "top-leaf"
    for d in (mod, top_leaf):
        d.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "pom.xml").write_text(_AGGREGATOR)
    (group / "pom.xml").write_text(_AGGREGATOR)
    (mod / "pom.xml").write_text(_LEAF)
    (top_leaf / "pom.xml").write_text(_LEAF)
    return {"git": git_dir, "repo": repo, "group": group, "mod": mod, "top_leaf": top_leaf}


class TestRepoBoundary:
    def test_boundary_is_nearest_git_dir(self, git_tree: dict[str, Path]) -> None:
        assert _find_repo_boundary(git_tree["mod"].resolve()) == git_tree["repo"].resolve()

    def test_boundary_accepts_git_file_worktree(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        mod = wt / "mod"
        mod.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
        (wt / "pom.xml").write_text(_AGGREGATOR)
        (mod / "pom.xml").write_text(_LEAF)
        assert _find_repo_boundary(mod.resolve()) == wt.resolve()

    def test_boundary_without_git_is_topmost_consecutive_pom(self, tmp_path: Path) -> None:
        assert not _has_git_ancestor(tmp_path)
        reactor = tmp_path / "outer" / "reactor"
        mod = reactor / "group" / "mod"
        mod.mkdir(parents=True)
        for d in (reactor, reactor / "group", mod):
            (d / "pom.xml").write_text(_AGGREGATOR)
        # tmp_path/outer has no pom.xml → chain stops at reactor.
        assert _find_repo_boundary(mod.resolve()) == reactor.resolve()


class TestGroupRootBoundary:
    def test_client_root_above_repo_does_not_scope_to_reactor(self, git_tree: dict[str, Path]) -> None:
        """Regression (#110 M2): client root ~/git made a top-level module scope to the whole reactor."""
        assert _find_maven_group_root(git_tree["top_leaf"], git_tree["git"]) == git_tree["top_leaf"].resolve()

    def test_client_root_above_repo_still_finds_group(self, git_tree: dict[str, Path]) -> None:
        assert _find_maven_group_root(git_tree["mod"], git_tree["git"]) == git_tree["group"].resolve()

    def test_client_root_at_repo(self, git_tree: dict[str, Path]) -> None:
        assert _find_maven_group_root(git_tree["mod"], git_tree["repo"]) == git_tree["group"].resolve()
        assert _find_maven_group_root(git_tree["top_leaf"], git_tree["repo"]) == git_tree["top_leaf"].resolve()

    def test_client_root_below_repo_stays_the_bound(self, git_tree: dict[str, Path]) -> None:
        # Client root = group: group's own pom must not be a candidate → module scopes to itself.
        assert _find_maven_group_root(git_tree["mod"], git_tree["group"]) == git_tree["mod"].resolve()

    def test_module_is_repo_root(self, git_tree: dict[str, Path]) -> None:
        assert _find_maven_group_root(git_tree["repo"], git_tree["git"]) == git_tree["repo"].resolve()

    def test_module_directly_under_reactor_without_git(self, tmp_path: Path) -> None:
        assert not _has_git_ancestor(tmp_path)
        reactor = tmp_path / "ws" / "reactor"
        leaf = reactor / "leaf"
        leaf.mkdir(parents=True)
        (reactor / "pom.xml").write_text(_AGGREGATOR)
        (leaf / "pom.xml").write_text(_LEAF)
        # Client root (tmp_path) is above the reactor; the reactor pom must not be used.
        assert _find_maven_group_root(leaf, tmp_path) == leaf.resolve()

    def test_nested_group_without_git(self, tmp_path: Path) -> None:
        reactor = tmp_path / "reactor"
        group = reactor / "group"
        mod = group / "mod"
        mod.mkdir(parents=True)
        (reactor / "pom.xml").write_text(_AGGREGATOR)
        (group / "pom.xml").write_text(_AGGREGATOR)
        (mod / "pom.xml").write_text(_LEAF)
        assert _find_maven_group_root(mod, tmp_path) == group.resolve()

    def test_symlinked_paths_resolve_to_real_scope(self, git_tree: dict[str, Path], tmp_path: Path) -> None:
        link_root = tmp_path / "link-to-git"
        link_root.symlink_to(git_tree["git"], target_is_directory=True)
        via_link_mod = link_root / "repo" / "top-leaf"
        assert _find_maven_group_root(via_link_mod, link_root) == git_tree["top_leaf"].resolve()
        assert _find_maven_group_root(link_root / "repo" / "group" / "mod", git_tree["git"]) == (
            git_tree["group"].resolve()
        )

    async def test_expand_logs_scope_once_at_info(
        self, git_tree: dict[str, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        proxy = JdtlsProxy()
        proxy._available = True
        proxy._original_root_uri = git_tree["git"].as_uri()
        proxy._initial_module_uri = git_tree["top_leaf"].as_uri()
        proxy.modules.mark_added(git_tree["top_leaf"].as_uri())
        proxy.send_notification = AsyncMock()  # type: ignore[assignment]
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.proxy"):
            await proxy.expand_full_workspace()
        lines = [r.getMessage() for r in caplog.records if "group scope" in r.getMessage()]
        assert lines == [
            "jdtls: group scope: client root .../git, repository boundary .../repo, group root .../top-leaf"
        ]
        proxy.send_notification.assert_not_called()  # type: ignore[attr-defined]


class TestSanitize:
    def test_redacts_url_userinfo(self) -> None:
        out = _sanitize_jdtls_log("fetch https://bob:hunter2@repo.example.com/x failed")
        assert "hunter2" not in out
        assert "bob" not in out
        assert "https://***@repo.example.com/x" in out

    @pytest.mark.parametrize("name", ["token", "access_token", "apiKey", "X-Amz-Signature", "password", "auth", "sig"])
    def test_redacts_secret_query_params(self, name: str) -> None:
        out = _sanitize_jdtls_log(f"GET https://h/p?a=1&{name}=S3CRET&b=2")
        assert "S3CRET" not in out
        assert "a=1" in out
        assert "b=2" in out

    def test_redacts_authorization_and_bearer(self) -> None:
        out = _sanitize_jdtls_log("Authorization: Basic dXNlcjpwYXNz and Bearer abc.def.ghi")
        assert "dXNlcjpwYXNz" not in out
        assert "abc.def.ghi" not in out
        assert "Authorization: Basic ***" in out
        assert "Bearer ***" in out

    def test_escapes_newlines_and_strips_control_chars(self) -> None:
        out = _sanitize_jdtls_log("line1\r\nline2\x1b[31mred\x00\x85\x9b end")
        assert out == "line1\\r\\nline2[31mred end"

    def test_truncates(self) -> None:
        out = _sanitize_jdtls_log("x" * 10_000)
        assert out.startswith("x" * _JDTLS_LOG_MAX_CHARS)
        assert out.endswith("...(truncated)")
        assert len(out) == _JDTLS_LOG_MAX_CHARS + len("...(truncated)")

    def test_redacts_before_truncating(self) -> None:
        text = "x" * (_JDTLS_LOG_MAX_CHARS - 20) + "https://u:topsecretpassword@h/"
        assert "topsecret" not in _sanitize_jdtls_log(text)


class TestSanitizeIsLinear:
    """Hostile jdtls text (it can echo pom content) must not stall the reader loop."""

    @pytest.mark.parametrize(
        "text",
        ["?token" * 3000, "a" * 16384, "?a" * 8000, "x://" * 4000, "&" + "k" * 16000],
    )
    def test_pathological_input_is_fast(self, text: str) -> None:
        import time

        started = time.monotonic()
        _sanitize_jdtls_log(text)
        assert time.monotonic() - started < 0.5

    def test_non_secret_params_are_kept(self) -> None:
        assert _sanitize_jdtls_log("https://h/p?version=1&token=abc") == "https://h/p?version=1&token=***"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _jdtls_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "java_functional_lsp.proxy"]


class TestLogForwarding:
    def _notify(self, proxy: JdtlsProxy, method: str, params: object) -> None:
        proxy._dispatch_message({"jsonrpc": "2.0", "method": method, "params": params})

    def test_log_message_levels(self, caplog: pytest.LogCaptureFixture) -> None:
        proxy = JdtlsProxy()
        with caplog.at_level(logging.DEBUG, logger="java_functional_lsp.proxy"):
            self._notify(proxy, "window/logMessage", {"type": 1, "message": "boom"})
            self._notify(proxy, "window/logMessage", {"type": 2, "message": "careful"})
            self._notify(proxy, "window/logMessage", {"type": 3, "message": "fyi"})
            self._notify(proxy, "window/logMessage", {"message": "no type"})
        got = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "java_functional_lsp.proxy"]
        assert got == [
            (logging.WARNING, "jdtls[log:1]: boom"),
            (logging.INFO, "jdtls[log:2]: careful"),
            (logging.DEBUG, "jdtls[log:3]: fyi"),
            (logging.DEBUG, "jdtls[log:other]: no type"),
        ]

    def test_language_status_levels(self, caplog: pytest.LogCaptureFixture) -> None:
        proxy = JdtlsProxy()
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.proxy"):
            self._notify(proxy, "language/status", {"type": "Starting", "message": "Init..."})
            self._notify(proxy, "language/status", {"type": "Error", "message": "import failed"})
        assert _jdtls_lines(caplog) == ["jdtls[status:Error]: import failed"]

    def test_unknown_kinds_are_folded(self, caplog: pytest.LogCaptureFixture) -> None:
        proxy = JdtlsProxy()
        with caplog.at_level(logging.DEBUG, logger="java_functional_lsp.proxy"):
            self._notify(proxy, "window/logMessage", {"type": "1\nFAKE LINE", "message": "a"})
            self._notify(proxy, "language/status", {"type": "Err\x1b[31m", "message": "b"})
        assert _jdtls_lines(caplog) == ["jdtls[log:other]: a", "jdtls[status:other]: b"]
        assert set(proxy._log_forwarder._buckets) == {"log:other", "status:other"}

    def test_format_characters_are_logged_literally(self, caplog: pytest.LogCaptureFixture) -> None:
        proxy = JdtlsProxy()
        with caplog.at_level(logging.WARNING, logger="java_functional_lsp.proxy"):
            self._notify(proxy, "window/logMessage", {"type": 1, "message": "bad %s %(x)s %d"})
        assert _jdtls_lines(caplog) == ["jdtls[log:1]: bad %s %(x)s %d"]

    def test_malformed_params_do_not_raise(self) -> None:
        proxy = JdtlsProxy()
        self._notify(proxy, "window/logMessage", None)
        self._notify(proxy, "window/logMessage", {"type": "1", "message": 42})
        self._notify(proxy, "language/status", ["not", "a", "dict"])

    def test_dedupe_and_suppressed_report(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = _Clock()
        fwd = _JdtlsLogForwarder(clock=clock)
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.proxy"):
            for _ in range(5):
                fwd.forward(logging.WARNING, "log:1", "same")
            clock.now += _JDTLS_LOG_WINDOW_SEC + 1
            fwd.forward(logging.WARNING, "log:1", "next")
        assert _jdtls_lines(caplog) == [
            "jdtls[log:1]: same",
            "jdtls: suppressed 4 jdtls log messages",
            "jdtls[log:1]: next",
        ]

    def test_token_bucket_per_kind(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = _Clock()
        fwd = _JdtlsLogForwarder(clock=clock)
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.proxy"):
            for i in range(_JDTLS_LOG_RATE + 10):
                fwd.forward(logging.WARNING, "log:1", f"msg {i}")
            fwd.forward(logging.INFO, "log:2", "other kind still flows")
            fwd.flush()
        lines = _jdtls_lines(caplog)
        assert sum(1 for m in lines if m.startswith("jdtls[log:1]")) == _JDTLS_LOG_RATE
        assert "jdtls[log:2]: other kind still flows" in lines
        assert lines[-1] == "jdtls: suppressed 10 jdtls log messages"

    def test_bucket_refills_over_time(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = _Clock()
        fwd = _JdtlsLogForwarder(clock=clock)
        with caplog.at_level(logging.WARNING, logger="java_functional_lsp.proxy"):
            for i in range(_JDTLS_LOG_RATE):
                fwd.forward(logging.WARNING, "log:1", f"a{i}")
            fwd.forward(logging.WARNING, "log:1", "dropped")
            clock.now += _JDTLS_LOG_WINDOW_SEC / _JDTLS_LOG_RATE  # one token back
            fwd.forward(logging.WARNING, "log:1", "allowed")
        lines = _jdtls_lines(caplog)
        assert "jdtls[log:1]: dropped" not in lines
        assert lines[-1] == "jdtls[log:1]: allowed"

    def test_sanitizes_forwarded_text(self, caplog: pytest.LogCaptureFixture) -> None:
        proxy = JdtlsProxy()
        with caplog.at_level(logging.WARNING, logger="java_functional_lsp.proxy"):
            self._notify(proxy, "window/logMessage", {"type": 1, "message": "GET https://u:pw@h/r?token=abc\n\x1b[2Jx"})
        assert _jdtls_lines(caplog) == ["jdtls[log:1]: GET https://***@h/r?token=***\\n[2Jx"]


class TestPomDiagnosticsSummary:
    def _publish(self, proxy: JdtlsProxy, uri: str, diagnostics: list[dict[str, object]]) -> None:
        proxy._dispatch_message(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": diagnostics},
            }
        )

    def test_logs_only_on_change_and_keeps_publishing(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        published: list[tuple[str, list[object]]] = []
        proxy = JdtlsProxy(on_diagnostics=lambda u, d: published.append((u, d)))
        proxy._original_root_uri = tmp_path.as_uri()
        pom_uri = (tmp_path / "groupB" / "it" / "pom.xml").as_uri()
        missing = [
            {"severity": 1, "message": f"Offline / Missing artifact com.example:c{i}:jar:X-DEFAULT"} for i in range(5)
        ]
        warn = [{"severity": 2, "message": "just a warning"}]
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.proxy"):
            self._publish(proxy, pom_uri, warn)  # no errors, unseen → nothing logged
            self._publish(proxy, pom_uri, missing)
            self._publish(proxy, pom_uri, missing + warn)  # same Error set → nothing
            self._publish(proxy, pom_uri, [])
            self._publish(proxy, pom_uri, [])
        rel = str(Path("groupB") / "it" / "pom.xml")
        assert _jdtls_lines(caplog) == [
            f"jdtls: pom.xml errors changed for {rel}: 5 error(s): "
            "Offline / Missing artifact com.example:c0:jar:X-DEFAULT; "
            "Offline / Missing artifact com.example:c1:jar:X-DEFAULT; "
            "Offline / Missing artifact com.example:c2:jar:X-DEFAULT (+2 more)",
            f"jdtls: pom.xml errors changed for {rel}: 0 error(s)",
        ]
        # Publishing to the client is unchanged: every notification is forwarded as-is.
        assert [d for _, d in published] == [warn, missing, missing + warn, [], []]
        assert proxy.get_cached_diagnostics(pom_uri) == []

    def test_java_files_are_not_summarized(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        proxy = JdtlsProxy()
        uri = (tmp_path / "A.java").as_uri()
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.proxy"):
            self._publish(proxy, uri, [{"severity": 1, "message": "x cannot be resolved"}])
        assert _jdtls_lines(caplog) == []

    def test_pom_outside_client_root_is_redacted(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        proxy = JdtlsProxy()
        proxy._original_root_uri = (tmp_path / "elsewhere").as_uri()
        uri = (tmp_path / "secret-user" / "pom.xml").as_uri()
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.proxy"):
            self._publish(proxy, uri, [{"severity": 1, "message": "Missing artifact g:a:jar:1\n\x1b[0m"}])
        assert _jdtls_lines(caplog) == [
            "jdtls: pom.xml errors changed for .../pom.xml: 1 error(s): Missing artifact g:a:jar:1\\n[0m"
        ]
