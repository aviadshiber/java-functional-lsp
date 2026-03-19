# java-functional-lsp

[![CI](https://github.com/aviadshiber/java-functional-lsp/actions/workflows/test.yml/badge.svg)](https://github.com/aviadshiber/java-functional-lsp/actions/workflows/test.yml)
[![PyPI version](https://img.shields.io/pypi/v/java-functional-lsp)](https://pypi.org/project/java-functional-lsp/)
[![Python](https://img.shields.io/pypi/pyversions/java-functional-lsp)](https://pypi.org/project/java-functional-lsp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A Java Language Server that enforces functional programming best practices. Designed for teams using **Vavr**, **Lombok**, and **Spring** with a functional-first approach.

## What it checks

| Rule | Detects | Suggests |
|------|---------|----------|
| `null-literal-arg` | `null` passed as method argument | `Option.none()` or default value |
| `null-return` | `return null` | `Option.of()`, `Option.none()`, or `Either` |
| `null-assignment` | `Type x = null` | `Option<Type>` |
| `null-field-assignment` | Field initialized to `null` | `Option<T>` with `Option.none()` |
| `throw-statement` | `throw new XxxException(...)` | `Either.left()` or `Try.of()` |
| `catch-rethrow` | catch block that wraps + rethrows | `Try.of().toEither()` |
| `mutable-variable` | Local variable reassignment | Final variables + functional transforms |
| `imperative-loop` | `for`/`while` loops | `.map()`/`.filter()`/`.flatMap()`/`.foldLeft()` |
| `mutable-dto` | `@Data` or `@Setter` on class | `@Value` (immutable) |
| `imperative-option-unwrap` | `if (opt.isDefined()) { opt.get() }` | `map()`/`flatMap()`/`fold()` |
| `field-injection` | `@Autowired` on field | Constructor injection |
| `component-annotation` | `@Component`/`@Service`/`@Repository` | `@Configuration` + `@Bean` |

## Install

```bash
# Homebrew
brew install aviadshiber/tap/java-functional-lsp

# pip
pip install java-functional-lsp

# From source
pip install git+https://github.com/aviadshiber/java-functional-lsp.git
```

## Usage with Claude Code

Install the `deeperdive-java-linter` plugin from the DeeperDive marketplace, which registers this server as a Java LSP.

Or manually add to your Claude Code config:

```json
{
  "lspServers": {
    "java-functional": {
      "command": "java-functional-lsp",
      "extensionToLanguage": { ".java": "java" }
    }
  }
}
```

## Configuration

Create `.deeperdive-linter.json` in your project root to customize rules:

```json
{
  "rules": {
    "null-literal-arg": "warning",
    "throw-statement": "info",
    "imperative-loop": "hint",
    "mutable-dto": "off"
  }
}
```

Severity levels: `error`, `warning`, `info`, `hint`, `off`.
All rules default to `warning` when not configured.

## How it works

Uses [tree-sitter](https://tree-sitter.github.io/) with the Java grammar for fast, incremental AST parsing. No Java compiler or classpath needed — analysis runs on raw source files.

The server speaks the Language Server Protocol (LSP) via stdio, making it compatible with any LSP client.

## Development

```bash
# Clone and setup
git clone https://github.com/aviadshiber/java-functional-lsp.git
cd java-functional-lsp
uv sync

# Run checks
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for full guidelines.

## License

MIT
