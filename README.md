# java-functional-lsp

[![CI](https://github.com/aviadshiber/java-functional-lsp/actions/workflows/test.yml/badge.svg)](https://github.com/aviadshiber/java-functional-lsp/actions/workflows/test.yml)
[![PyPI version](https://img.shields.io/pypi/v/java-functional-lsp?v=1)](https://pypi.org/project/java-functional-lsp/)
[![Python](https://img.shields.io/pypi/pyversions/java-functional-lsp?v=1)](https://pypi.org/project/java-functional-lsp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A Java Language Server that wraps [Eclipse JDT.LS](https://github.com/eclipse-jdtls/eclipse.jdt.ls) and adds **functional programming best practices enforcement**. Get full Java language support (completions, hover, go-to-definition, compile errors) **plus** 12 custom rules for teams using **Vavr**, **Lombok**, and **Spring**.

## Features

**Full Java language support** (via jdtls proxy):
- Code completions, hover documentation, go-to-definition
- Find references, document symbols
- Compile errors and warnings — **before you even build**

**12 custom functional programming rules** (via tree-sitter):

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

**For full Java support**, also install jdtls:
```bash
brew install jdtls
```

Without jdtls, the server runs in **standalone mode** — custom rules still work, but you won't get completions, hover, or compile errors.

## Usage with Claude Code

Install the `deeperdive-java-linter` plugin from the DeeperDive marketplace, which registers this server as a Java LSP.

Or manually add to your Claude Code config:

```json
{
  "lspServers": {
    "java-functional": {
      "command": "java-functional-lsp",
      "extensionToLanguage": { ".java": "java" },
      "startupTimeout": 120000
    }
  }
}
```

## CLI Check Mode

Run as a standalone linter without an LSP client:

```bash
java-functional-lsp check File.java
java-functional-lsp check --dir src/
```

Exits 1 if diagnostics found, 0 if clean. Useful for CI pipelines and pre-commit hooks.

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

```
Client (editor/Claude Code)
    ↕ LSP over stdio
java-functional-lsp
    |                  \
    | proxy             tree-sitter
    ↓                   analyzers
  jdtls                   ↓
    ↓                 12 custom rules
  completions,        (null, exceptions,
  hover, go-to-def,   mutations, Spring)
  compile errors
```

The server proxies standard Java LSP requests to **jdtls** (Eclipse JDT Language Server) and runs **tree-sitter** analysis in parallel for custom rule enforcement. Diagnostics from both sources are merged before being sent to the client.

If jdtls is not installed, the server automatically falls back to **standalone mode** with custom rules only.

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
