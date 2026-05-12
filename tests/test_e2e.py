"""End-to-end tests: start the LSP server over stdio, send real JSON-RPC messages, verify diagnostics."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from java_functional_lsp.capabilities.registry import REGISTRY


def _encode_lsp(obj: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC message with Content-Length header."""
    body = json.dumps(obj).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _read_lsp(proc: subprocess.Popen[bytes]) -> dict[str, Any] | None:
    """Read one LSP message from the server's stdout."""
    assert proc.stdout is not None
    headers: dict[str, str] = {}
    while True:
        line = proc.stdout.readline()
        if not line:
            return None
        line_str = line.decode("ascii").strip()
        if line_str == "":
            break
        key, _, value = line_str.partition(": ")
        headers[key] = value
    length = int(headers.get("Content-Length", "0"))
    if length == 0:
        return None
    body = proc.stdout.read(length)
    return json.loads(body)


def _send(proc: subprocess.Popen[bytes], msg: dict[str, Any]) -> None:
    """Send an LSP message to the server."""
    assert proc.stdin is not None
    proc.stdin.write(_encode_lsp(msg))
    proc.stdin.flush()


def _read_until_method(proc: subprocess.Popen[bytes], method: str, max_messages: int = 20) -> dict[str, Any] | None:
    """Read messages until one with the given method appears."""
    for _ in range(max_messages):
        msg = _read_lsp(proc)
        if msg is None:
            return None
        if msg.get("method") == method:
            return msg
    return None


@pytest.fixture
def server():
    """Start the LSP server as a subprocess, shut down cleanly after test."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "java_functional_lsp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    yield proc
    # Graceful LSP shutdown: send shutdown request, then exit notification
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 9999, "method": "shutdown", "params": None})
        _read_lsp(proc)  # consume shutdown response
        _send(proc, {"jsonrpc": "2.0", "method": "exit", "params": None})
        proc.wait(timeout=5)
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _initialize(proc: subprocess.Popen[bytes], root_uri: str = "file:///tmp") -> dict[str, Any] | None:
    """Send initialize + initialized, return the initialize response."""
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "processId": None,
                "capabilities": {},
                "rootUri": root_uri,
            },
        },
    )
    response = _read_lsp(proc)
    _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
    return response


def _did_open(proc: subprocess.Popen[bytes], uri: str, text: str, version: int = 1) -> None:
    """Send textDocument/didOpen notification."""
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": uri,
                    "languageId": "java",
                    "version": version,
                    "text": text,
                }
            },
        },
    )


def _did_change(proc: subprocess.Popen[bytes], uri: str, text: str, version: int = 2) -> None:
    """Send textDocument/didChange notification with full content."""
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "method": "textDocument/didChange",
            "params": {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            },
        },
    )


def _did_close(proc: subprocess.Popen[bytes], uri: str) -> None:
    """Send textDocument/didClose notification."""
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "method": "textDocument/didClose",
            "params": {"textDocument": {"uri": uri}},
        },
    )


def _did_save(proc: subprocess.Popen[bytes], uri: str) -> None:
    """Send textDocument/didSave notification."""
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "method": "textDocument/didSave",
            "params": {"textDocument": {"uri": uri}},
        },
    )


def _wait_diagnostics(proc: subprocess.Popen[bytes]) -> dict[str, Any] | None:
    """Wait for a publishDiagnostics notification."""
    return _read_until_method(proc, "textDocument/publishDiagnostics")


# Per-test timeout prevents CI from hanging if the server crashes mid-test
pytestmark = pytest.mark.timeout(15)


class TestE2EInitialize:
    def test_server_capabilities(self, server: subprocess.Popen[bytes]) -> None:
        """Server should advertise text document sync capability."""
        response = _initialize(server)
        assert response is not None
        result = response.get("result", {})
        caps = result.get("capabilities", {})
        assert "textDocumentSync" in caps

    def test_code_action_capability(self, server: subprocess.Popen[bytes]) -> None:
        """Server should advertise code action capability."""
        response = _initialize(server)
        assert response is not None
        result = response.get("result", {})
        caps = result.get("capabilities", {})
        assert "codeActionProvider" in caps

    def test_server_name(self, server: subprocess.Popen[bytes]) -> None:
        """Server should identify itself."""
        response = _initialize(server)
        assert response is not None
        info = response["result"].get("serverInfo", {})
        assert "java-functional" in info.get("name", "").lower()


def _snake_field_to_wire(field: str) -> str:
    """Convert a ServerCapabilities snake_case field name to the camelCase JSON key.

    Mirrors lsprotocol's converter, which renames `hover_provider` → `hoverProvider`
    on serialization. Keeping this local (vs importing the converter) avoids
    coupling tests to lsprotocol internals.
    """
    head, *tail = field.split("_")
    return head + "".join(part.capitalize() for part in tail)


# Derived from REGISTRY (single source of truth) so adding a new CapabilityEntry
# automatically extends the regression assertion in
# `test_initialize_with_empty_caps_advertises_all_jdtls_features_statically`.
# A hand-maintained mirror would silently let new features regress for
# Claude Code-style clients that ignore client/registerCapability.
_JDTLS_PROVIDER_KEYS: frozenset[str] = frozenset(_snake_field_to_wire(entry.static_field) for entry in REGISTRY)


def _initialize_with_caps(
    proc: subprocess.Popen[bytes], capabilities: dict[str, Any], root_uri: str = "file:///tmp"
) -> dict[str, Any]:
    """Send `initialize` + `initialized` with arbitrary client capabilities, return the raw response."""
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"processId": None, "capabilities": capabilities, "rootUri": root_uri},
        },
    )
    response = _read_lsp(proc)
    assert response is not None, "server returned no initialize response"
    _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
    return response


class TestE2ECapabilityNegotiation:
    """Regression coverage for per-feature static/dynamic capability negotiation.

    Why these go through stdio + pygls (not just `on_initialize()` directly):

    Pure-Python tests of `on_initialize` (test_server.py:782, :815) construct
    `lsp.InitializeParams` and inspect the returned attrs object — they skip
    pygls' wire-format JSON deserialization on the way in and lsprotocol's
    converter on the way out. The bug fixed in PR #86 went undetected for
    months precisely because the unit-test layer never observed the JSON the
    client actually sees. Mirrors the rationale for `test_e2e_jdtls.py`:
    "Unit tests with mocked subprocesses cannot catch JSON-shape regressions
    because the mocks never parse the bytes."

    Each test below verifies what a real LSP client would see on the wire.
    """

    def test_initialize_with_empty_caps_advertises_all_jdtls_features_statically(
        self, server: subprocess.Popen[bytes]
    ) -> None:
        """Claude Code-style empty `capabilities` → server must list every jdtls provider statically.

        Claude Code 2.1.x ignores `client/registerCapability` and routes solely
        from the InitializeResult. If any jdtls feature is omitted here, that
        feature is unreachable on Claude Code (the original bug — `LSP
        documentSymbol` returned "No LSP server available for file type: .java"
        even though the server was running and responsive).
        """
        response = _initialize_with_caps(server, capabilities={})
        caps = response["result"]["capabilities"]
        missing = sorted(k for k in _JDTLS_PROVIDER_KEYS if caps.get(k) is None)
        assert not missing, (
            f"Static InitializeResult is missing jdtls providers {missing}; "
            "they will be unreachable on clients that don't honor "
            "client/registerCapability (e.g., Claude Code 2.1.x)."
        )

    def test_initialize_with_full_dynamic_support_omits_jdtls_features_statically(
        self, server: subprocess.Popen[bytes]
    ) -> None:
        """VS Code-style full `dynamicRegistration` claims → those providers must NOT be in static caps.

        Preserves the PR #44 invariant: when the client supports dynamic
        registration, defer jdtls features until jdtls actually warms up so
        the IDE keeps showing its own diagnostic tooltips during the gap.
        """
        capabilities = {
            "textDocument": {
                "completion": {"dynamicRegistration": True},
                "hover": {"dynamicRegistration": True},
                "definition": {"dynamicRegistration": True},
                "references": {"dynamicRegistration": True},
                "documentSymbol": {"dynamicRegistration": True},
                "callHierarchy": {"dynamicRegistration": True},
                "signatureHelp": {"dynamicRegistration": True},
                "implementation": {"dynamicRegistration": True},
                "typeDefinition": {"dynamicRegistration": True},
                "declaration": {"dynamicRegistration": True},
                "documentHighlight": {"dynamicRegistration": True},
                "rename": {"dynamicRegistration": True},
                "typeHierarchy": {"dynamicRegistration": True},
            },
            "workspace": {"symbol": {"dynamicRegistration": True}},
        }
        response = _initialize_with_caps(server, capabilities=capabilities)
        caps = response["result"]["capabilities"]
        leaked = sorted(k for k in _JDTLS_PROVIDER_KEYS if caps.get(k) is not None)
        assert not leaked, (
            f"jdtls providers {leaked} appear in static caps even though the client claimed "
            "dynamicRegistration for them — this would suppress IDE diagnostic tooltips during "
            "jdtls warm-up (PR #44 invariant)."
        )
        # Always-static caps remain regardless of negotiation outcome.
        assert caps.get("textDocumentSync") is not None
        assert caps.get("codeActionProvider") is not None

    def test_per_feature_negotiation_partial_dynamic_support(self, server: subprocess.Popen[bytes]) -> None:
        """Mixed dynamic claims → server splits per feature, not all-or-nothing.

        Guards against regressions that collapse the negotiation to a single
        "if any feature claims dynamic, treat all as dynamic" (or the inverse).
        Hover and definition claim dynamic; everything else does not. The
        server must omit hover/definition from static caps and keep the rest.
        """
        capabilities = {
            "textDocument": {
                "hover": {"dynamicRegistration": True},
                "definition": {"dynamicRegistration": True},
            }
        }
        response = _initialize_with_caps(server, capabilities=capabilities)
        caps = response["result"]["capabilities"]
        # Dynamic-claimed: must be absent from static caps.
        assert caps.get("hoverProvider") is None, (
            "hoverProvider leaked into static caps despite the client claiming dynamicRegistration"
        )
        assert caps.get("definitionProvider") is None, (
            "definitionProvider leaked into static caps despite the client claiming dynamicRegistration"
        )
        # Everything else should still be advertised statically.
        unclaimed_keys = _JDTLS_PROVIDER_KEYS - {"hoverProvider", "definitionProvider"}
        missing = sorted(k for k in unclaimed_keys if caps.get(k) is None)
        assert not missing, (
            f"Providers {missing} were dropped from static caps even though the client did NOT "
            "claim dynamicRegistration for them — per-feature split is broken."
        )


class TestE2EDiagnostics:
    def test_diagnostics_on_open(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Opening a Java file with null return should produce diagnostics."""
        java_file = tmp_path / "Test.java"
        java_file.write_text("class T { String f() { return null; } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        params = msg["params"]
        assert params["uri"] == uri
        codes = [d["code"] for d in params["diagnostics"]]
        assert "null-return" in codes

    def test_clean_file_no_diagnostics(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """A clean Java file should produce empty diagnostics."""
        java_file = tmp_path / "Clean.java"
        java_file.write_text('class T { String f() { return "ok"; } }')
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        assert len(msg["params"]["diagnostics"]) == 0

    def test_diagnostics_on_change(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Editing a file to introduce a violation should produce diagnostics.

        didChange is debounced (150ms), so we follow with didSave for immediate results.
        """
        java_file = tmp_path / "Change.java"
        java_file.write_text('class T { String f() { return "ok"; } }')
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())
        _wait_diagnostics(server)  # consume initial clean diagnostics

        _did_change(server, uri, "class T { String f() { return null; } }")
        _did_save(server, uri)
        # May receive multiple notifications (debounce + save) — collect all codes seen
        all_codes: set[str] = set()
        for _ in range(5):
            msg = _wait_diagnostics(server)
            if msg is None:
                break
            for d in msg["params"]["diagnostics"]:
                all_codes.add(d["code"])
            if "null-return" in all_codes:
                break
        assert "null-return" in all_codes, f"Expected null-return, got {all_codes}"

    def test_diagnostics_on_save(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Save should trigger immediate diagnostics."""
        java_file = tmp_path / "Save.java"
        java_file.write_text("class T { void f() { throw new RuntimeException(); } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())
        _wait_diagnostics(server)

        _did_save(server, uri)
        msg = _wait_diagnostics(server)
        assert msg is not None
        codes = [d["code"] for d in msg["params"]["diagnostics"]]
        assert "throw-statement" in codes


class TestE2EMultipleRules:
    def test_multiple_violations(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """A file with multiple violations should report all of them."""
        java_file = tmp_path / "Multi.java"
        java_file.write_text(
            "class T {\n"
            "  String f() { return null; }\n"
            "  void g() { throw new RuntimeException(); }\n"
            "  void h() { for (int i = 0; i < 10; i++) {} }\n"
            "}"
        )
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        codes = {d["code"] for d in msg["params"]["diagnostics"]}
        assert "null-return" in codes
        assert "throw-statement" in codes
        assert "imperative-loop" in codes

    def test_diagnostic_source(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """All diagnostics should have source='java-functional-lsp'."""
        java_file = tmp_path / "Source.java"
        java_file.write_text("class T { String f() { return null; } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        for diag in msg["params"]["diagnostics"]:
            assert diag["source"] == "java-functional-lsp"


class TestE2EDidClose:
    def test_close_then_reopen(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Closing a file then reopening should still produce diagnostics."""
        java_file = tmp_path / "Close.java"
        java_file.write_text("class T { String f() { return null; } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())
        _wait_diagnostics(server)

        _did_close(server, uri)
        # didClose publishes empty diagnostics — consume that notification
        _wait_diagnostics(server)

        # Re-open should still work
        _did_open(server, uri, java_file.read_text(), version=2)
        msg = _wait_diagnostics(server)
        assert msg is not None
        codes = [d["code"] for d in msg["params"]["diagnostics"]]
        assert "null-return" in codes


class TestE2EConfig:
    def test_rule_disabled_by_config(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Rules disabled in .java-functional-lsp.json should not produce diagnostics."""
        config = tmp_path / ".java-functional-lsp.json"
        config.write_text('{"rules": {"null-return": "off"}}')
        java_file = tmp_path / "Config.java"
        java_file.write_text("class T { String f() { return null; } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        codes = [d["code"] for d in msg["params"]["diagnostics"]]
        assert "null-return" not in codes

    def test_excludes_pattern(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Files matching excludes patterns should produce no diagnostics."""
        config = tmp_path / ".java-functional-lsp.json"
        config.write_text('{"excludes": ["**/generated/**"]}')
        gen_dir = tmp_path / "generated"
        gen_dir.mkdir()
        java_file = gen_dir / "Gen.java"
        java_file.write_text("class T { String f() { return null; } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        assert len(msg["params"]["diagnostics"]) == 0


class TestE2ESuppressWarnings:
    def test_suppress_warnings_annotation(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """@SuppressWarnings should suppress diagnostics for the annotated scope."""
        java_file = tmp_path / "Suppress.java"
        java_file.write_text(
            "class T {\n"
            '    @SuppressWarnings("java-functional-lsp:null-return")\n'
            "    String f() { return null; }\n"
            "\n"
            "    String g() { return null; }\n"
            "}"
        )
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        null_diags = [d for d in msg["params"]["diagnostics"] if d["code"] == "null-return"]
        # f() suppressed, g() not — should have exactly 1 null-return diagnostic on line 4 (g)
        assert len(null_diags) == 1
        assert null_diags[0]["range"]["start"]["line"] == 4


class TestE2EDiagnosticData:
    def test_diagnostics_include_data_field(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Diagnostics should include machine-readable data payload for AI agents."""
        java_file = tmp_path / "Data.java"
        java_file.write_text("class T { String f() { return null; } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        null_diags = [d for d in msg["params"]["diagnostics"] if d["code"] == "null-return"]
        assert len(null_diags) == 1
        data = null_diags[0].get("data")
        assert data is not None
        assert "fixType" in data
        assert "targetLibrary" in data
        assert "rationale" in data
        assert data["fixType"] == "WRAP_IN_OPTION"
        assert data["targetLibrary"] == "io.vavr.control.Option"

    def test_diagnostics_include_recommended_api_and_suggested_snippet(
        self, server: subprocess.Popen[bytes], tmp_path: Path
    ) -> None:
        """Issue #74: the new recommendedApi + suggestedSnippet fields must appear on the wire
        as camelCase keys under Diagnostic.data so AI agents can read them directly."""
        java_file = tmp_path / "OptUse.java"
        java_file.write_text(
            "class T {\n"
            "    String f(io.vavr.control.Option<String> myOpt) {\n"
            "        if (myOpt.isDefined()) {\n"
            "            return myOpt.get();\n"
            '        } else { return "fallback"; }\n'
            "    }\n"
            "}\n"
        )
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        unwrap_diags = [d for d in msg["params"]["diagnostics"] if d["code"] == "imperative-option-unwrap"]
        assert len(unwrap_diags) == 1
        data = unwrap_diags[0].get("data")
        assert data is not None, "Diagnostic.data must be present"
        assert "recommendedApi" in data, "recommendedApi must surface on the LSP wire"
        assert "suggestedSnippet" in data, "suggestedSnippet must surface on the LSP wire"
        # The snippet uses the real variable name from the AST.
        assert "myOpt" in data["suggestedSnippet"]
        # The recommended_api warns about the Vavr-vs-Optional API confusion.
        assert "ifPresent" in data["recommendedApi"]


def _request_code_actions(
    proc: subprocess.Popen[bytes], uri: str, diag_range: dict, diagnostics: list[dict]
) -> dict | None:
    """Send a textDocument/codeAction request and return the response."""
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "textDocument/codeAction",
            "params": {
                "textDocument": {"uri": uri},
                "range": diag_range,
                "context": {"diagnostics": diagnostics},
            },
        },
    )
    return _read_lsp(proc)


class TestE2ECodeAction:
    def test_code_action_returns_quickfix(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Code action request for a fixable diagnostic should return a quick fix."""
        java_file = tmp_path / "Action.java"
        java_file.write_text("class T { String f() { return null; } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        null_diags = [d for d in msg["params"]["diagnostics"] if d["code"] == "null-return"]
        assert len(null_diags) >= 1

        response = _request_code_actions(server, uri, null_diags[0]["range"], null_diags)
        assert response is not None
        result = response.get("result")
        assert result is not None
        assert len(result) >= 1
        action = result[0]
        assert action["kind"] == "quickfix"
        assert "edit" in action
        assert action["title"] == "Replace with Option.none()"

    def test_frozen_mutation_code_action(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Code action for frozen-mutation should suggest switching to Vavr collection."""
        java_file = tmp_path / "Frozen.java"
        java_file.write_text(
            'class T {\n    void f() {\n        List<String> list = List.of("a");\n        list.add("b");\n    }\n}\n'
        )
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        frozen_diags = [d for d in msg["params"]["diagnostics"] if d["code"] == "frozen-mutation"]
        assert len(frozen_diags) >= 1

        response = _request_code_actions(server, uri, frozen_diags[0]["range"], frozen_diags)
        assert response is not None
        result = response.get("result")
        assert result is not None
        assert len(result) >= 1
        action = result[0]
        assert action["kind"] == "quickfix"
        assert "edit" in action
        assert action["title"] == "Switch to Vavr Immutable Collection"

    def test_null_check_to_monadic_code_action(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Code action for null-check-to-monadic should suggest Option monadic flow."""
        java_file = tmp_path / "Monadic.java"
        java_file.write_text(
            "class T {\n"
            "    String f(Object user) {\n"
            "        if (user != null) {\n"
            "            return user.toString();\n"
            "        }\n"
            "        return null;\n"
            "    }\n"
            "}\n"
        )
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        monadic_diags = [d for d in msg["params"]["diagnostics"] if d["code"] == "null-check-to-monadic"]
        assert len(monadic_diags) >= 1

        response = _request_code_actions(server, uri, monadic_diags[0]["range"], monadic_diags)
        assert response is not None
        result = response.get("result")
        assert result is not None
        assert len(result) >= 1
        action = result[0]
        assert action["kind"] == "quickfix"
        assert "edit" in action
        assert action["title"] == "Convert to Option monadic flow"

    def test_chained_null_check_code_action(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """E2E: chained null-check should produce code action with orElse chain."""
        java_file = tmp_path / "Chained.java"
        java_file.write_text(
            "class T {\n"
            "    int f(String key) {\n"
            "        Integer val = map.get(key);\n"
            "        if (val != null) {\n"
            "            return val;\n"
            "        } else {\n"
            "            val = fallback.get(key);\n"
            "            if (val != null) {\n"
            "                return val;\n"
            "            }\n"
            "        }\n"
            "        return defaultVal;\n"
            "    }\n"
            "}\n"
        )
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())

        msg = _wait_diagnostics(server)
        assert msg is not None
        monadic_diags = [d for d in msg["params"]["diagnostics"] if d["code"] == "null-check-to-monadic"]
        assert len(monadic_diags) >= 1

        response = _request_code_actions(server, uri, monadic_diags[0]["range"], monadic_diags)
        assert response is not None
        result = response.get("result")
        assert result is not None
        assert len(result) >= 1

        # Find the quickfix action
        quickfix_actions = [a for a in result if a.get("kind") == "quickfix"]
        assert len(quickfix_actions) >= 1
        action = quickfix_actions[0]
        assert "edit" in action

        # Verify the edit contains an orElse chain
        changes = action["edit"].get("changes", {})
        all_new_text = " ".join(e["newText"] for edits in changes.values() for e in edits if "newText" in e)
        assert "Option.of(" in all_new_text
        assert ".orElse(" in all_new_text
        assert ".getOrElse(" in all_new_text

    def test_code_action_unknown_source_returns_none(self, server: subprocess.Popen[bytes], tmp_path: Path) -> None:
        """Code action request with diagnostics from a non-java-functional-lsp source should return null."""
        java_file = tmp_path / "Other.java"
        java_file.write_text("class T { String f() { return null; } }")
        uri = java_file.as_uri()

        _initialize(server, root_uri=tmp_path.as_uri())
        _did_open(server, uri, java_file.read_text())
        _wait_diagnostics(server)

        # Send a diagnostic from a foreign source — server should return null (no actions)
        fake_diag = {
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}},
            "code": "null-return",
            "source": "external-linter",
            "message": "some issue",
            "severity": 1,
        }
        response = _request_code_actions(server, uri, fake_diag["range"], [fake_diag])
        assert response is not None
        # Server returns null (no actions) when no diagnostics match
        result = response.get("result")
        assert result is None
