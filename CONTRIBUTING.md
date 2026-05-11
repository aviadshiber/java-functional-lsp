# Contributing to java-functional-lsp

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Setup

```bash
git clone https://github.com/aviadshiber/java-functional-lsp.git
cd java-functional-lsp
uv sync
```

## Making Changes

1. Branch from `main` with a descriptive name (e.g., `feat/new-rule`, `fix/false-positive`)
2. Follow existing code patterns in `src/java_functional_lsp/analyzers/`
3. Add tests for new rules or behavior changes
4. Ensure all checks pass before submitting

## Pull Request Guidelines

- Keep PRs focused — one rule or one fix per PR
- Write a clear description of what and why
- All CI checks must pass
- Maintainer review is required

## Code Style

- **Ruff** for linting and formatting (line length: 120)
- **mypy** in strict mode for type checking
- **pytest** for tests with coverage reporting

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run mypy src/
uv run pytest
```

## Adding a New Rule

1. Choose the appropriate analyzer in `src/java_functional_lsp/analyzers/`
2. Add the detection logic using tree-sitter node walking (see `base.py` helpers)
3. Add the rule ID and message to the module's `_MESSAGES` dict
4. Add a `DiagnosticData` entry to the module's `_DATA` dict with `fix_type`, `target_library`, and `rationale`. Where helpful for AI agents, also set:
   - `recommended_api` — a library-agnostic API hint paired with `target_library` (e.g. `"forEach (NOT ifPresent — Vavr Option uses forEach)"`, `"@Value"`, `"Try.of(() -> ...).getOrElse(...)"`). Prevents agents from picking the wrong API surface.
   - `suggested_snippet` — a concrete, paste-able snippet built per-instance from AST node text (real variable names, real return expressions). Pattern: define a `_build_<rule>_data(node)` helper that reads the AST and produces the snippet, then pass `data=_build_<rule>_data(node)` instead of `data=_DATA["rule-id"]`. Leave the field `None` rather than fabricating a misleading placeholder when the AST shape isn't trivially templatable.
5. Pass `data=_DATA["rule-id"]` (or the builder result) when creating the `Diagnostic`
6. Add tests in `tests/test_<analyzer>.py` (including a test verifying the `data` field — both `fix_type` and, when set, `recommended_api` / `suggested_snippet` with real AST-derived text)
7. Optionally add a quick fix generator in `src/java_functional_lsp/fixes.py` and register it in `_FIX_REGISTRY` + add its title to `_FIX_TITLES` in `server.py` (an import-time assertion catches mismatches). Skip the registry if the rewrite needs class-wide analysis you can't safely automate; the diagnostic + `suggested_snippet` are usually enough for an AI agent to apply the change.
8. Update the rules table in `README.md`

## Test Architecture

The project has a layered test suite:

- **Unit tests** (`tests/test_*_checker.py`, `tests/test_fixes.py`, `tests/test_proxy.py`) — fast, focused, run in the main CI matrix across Python 3.10-3.13 on Ubuntu + macOS
- **Server integration tests** (`tests/test_server.py: TestServerInternals`) — exercise the server pipeline (config loading, diagnostic conversion, code actions) in-process
- **LSP lifecycle tests** (`tests/test_server.py: TestLspLifecycle`) — **zero mocks** — spawn the real server as a subprocess via pygls `LanguageClient`, connect over stdio, exercise the full LSP round-trip (initialize, didOpen, publishDiagnostics, codeAction, didChange)
- **jdtls e2e tests** (`tests/test_e2e_jdtls.py`) — **zero mocks** — spawn real jdtls, exercise definition/references/hover/completion/documentSymbol forwarding. Auto-skip when jdtls is not installed. Run in a dedicated CI integration job.

Coverage threshold is **80%**. Bump the version in both `pyproject.toml` and `src/java_functional_lsp/__init__.py` when making source changes (a pre-commit hook enforces this).

## Reporting Issues

- Use the [bug report template](https://github.com/aviadshiber/java-functional-lsp/issues/new?template=bug-report.md)
- Use the [feature request template](https://github.com/aviadshiber/java-functional-lsp/issues/new?template=feature-request.md)
- For security issues, see [SECURITY.md](.github/SECURITY.md)
