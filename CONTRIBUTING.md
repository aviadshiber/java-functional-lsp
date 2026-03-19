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
4. Add tests in `tests/test_<analyzer>.py`
5. Update the rules table in `README.md`

## Reporting Issues

- Use the [bug report template](https://github.com/aviadshiber/java-functional-lsp/issues/new?template=bug-report.md)
- Use the [feature request template](https://github.com/aviadshiber/java-functional-lsp/issues/new?template=feature-request.md)
- For security issues, see [SECURITY.md](.github/SECURITY.md)
