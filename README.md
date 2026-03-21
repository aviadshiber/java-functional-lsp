# java-functional-lsp

[![CI](https://github.com/aviadshiber/java-functional-lsp/actions/workflows/test.yml/badge.svg)](https://github.com/aviadshiber/java-functional-lsp/actions/workflows/test.yml)
[![PyPI version](https://img.shields.io/pypi/v/java-functional-lsp?v=1)](https://pypi.org/project/java-functional-lsp/)
[![Python](https://img.shields.io/pypi/pyversions/java-functional-lsp?v=1)](https://pypi.org/project/java-functional-lsp/)
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

# Optional: install jdtls for full Java language support (completions, hover, go-to-def)
brew install jdtls
```

**Requirements:**
- Python 3.10+ (for the LSP server)
- JDK 21+ (only if using jdtls — jdtls 1.57+ requires JDK 21 as its runtime, but can analyze Java 8+ source code)

## IDE Setup

### VS Code

Install the extension from a `.vsix` file ([download from releases](https://github.com/aviadshiber/java-functional-lsp/releases)):

```bash
# Download and install
gh release download --repo aviadshiber/java-functional-lsp --pattern "*.vsix" --dir /tmp
code --install-extension /tmp/java-functional-lsp-*.vsix
```

Or build from source:

```bash
cd editors/vscode
npm install && npm run compile
npx vsce package
code --install-extension java-functional-lsp-*.vsix
```

The extension is a thin launcher — it just starts the `java-functional-lsp` binary for `.java` files. **Updating rules only requires upgrading the LSP binary** (`brew upgrade java-functional-lsp` or `pip install --upgrade java-functional-lsp`). The VSIX itself rarely needs updating.

Configure the binary path in settings if needed (`javaFunctionalLsp.serverPath`). See [editors/vscode/README.md](editors/vscode/README.md) for details.

### IntelliJ IDEA

Use the [LSP4IJ](https://github.com/redhat-developer/lsp4ij) plugin (works on Community & Ultimate):

1. Install **LSP4IJ** from the JetBrains Marketplace
2. **Settings** → **Languages & Frameworks** → **Language Servers** → **`+`**
3. Set **Command**: `java-functional-lsp`, then in **Mappings** → **File name patterns** add `*.java` with Language Id `java`

See [editors/intellij/README.md](editors/intellij/README.md) for detailed instructions.

### Claude Code

Install as a plugin directly from GitHub:

```bash
claude plugin add https://github.com/aviadshiber/java-functional-lsp.git
```

This registers the LSP server, adds auto-install hooks, and provides the `/lint-java` command.

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

### Other Editors

Any LSP client that supports stdio transport can use this server. Point it to the `java-functional-lsp` command for `java` files.

| Editor | Config |
|--------|--------|
| **Neovim** | `vim.lsp.start({ cmd = {"java-functional-lsp"}, filetypes = {"java"} })` |
| **Emacs (eglot)** | `(add-to-list 'eglot-server-programs '(java-mode "java-functional-lsp"))` |
| **Sublime Text** | LSP package → add server with `"command": ["java-functional-lsp"]` |

## Configuration

Create `.java-functional-lsp.json` in your project root to customize rules:

```json
{
  "excludes": ["**/generated/**", "**/vendor/**"],
  "rules": {
    "null-literal-arg": "warning",
    "throw-statement": "info",
    "imperative-loop": "hint",
    "mutable-dto": "off"
  }
}
```

**Options:**
- `excludes` — glob patterns for files/directories to skip entirely (supports `**` for multi-segment wildcards)
- `rules` — per-rule severity: `error`, `warning` (default), `info`, `hint`, `off`

**Spring-aware behavior:**
- `throw-statement` and `catch-rethrow` are automatically suppressed inside `@Bean` methods
- `mutable-dto` suggests `@ConstructorBinding` instead of `@Value` when the class has `@ConfigurationProperties`

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
