# java-functional-lsp

[![CI](https://github.com/aviadshiber/java-functional-lsp/actions/workflows/test.yml/badge.svg)](https://github.com/aviadshiber/java-functional-lsp/actions/workflows/test.yml)
[![PyPI version](https://img.shields.io/pypi/v/java-functional-lsp?v=1)](https://pypi.org/project/java-functional-lsp/)
[![Python](https://img.shields.io/pypi/pyversions/java-functional-lsp?v=1)](https://pypi.org/project/java-functional-lsp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A Java Language Server that provides two things in one:

1. **Full Java language support** — completions, hover, go-to-definition, compile errors, missing imports — by proxying [Eclipse jdtls](https://github.com/eclipse-jdtls/eclipse.jdt.ls) under the hood
2. **12 functional programming rules** — catches anti-patterns and suggests Vavr/Lombok/Spring alternatives, all before compilation

Designed for teams using **Vavr**, **Lombok**, and **Spring** with a functional-first approach.

## What it checks

### Java language (via jdtls)

When [jdtls](https://github.com/eclipse-jdtls/eclipse.jdt.ls) is installed, the server proxies all standard Java language features:

- Compile errors and warnings
- Missing imports and unresolved symbols
- Type mismatches
- Completions, hover, go-to-definition, find references

Install jdtls separately: `brew install jdtls` (requires JDK 21+). Without jdtls, the server runs in standalone mode — the 12 custom rules still work, but you won't get compile errors or completions.

### Functional programming rules

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

# Optional: install jdtls for full Java language support (see above)
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

**Step 1: Enable LSP support** (required, one-time):

Add to `~/.claude/settings.json`:
```json
{
  "env": {
    "ENABLE_LSP_TOOL": "1"
  }
}
```

**Step 2: Install the plugin:**

```bash
claude plugin add https://github.com/aviadshiber/java-functional-lsp.git
```

This registers the LSP server, adds auto-install hooks, a PostToolUse hook that reminds Claude to fix violations on every `.java` file edit, and the `/lint-java` command.

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

**Alternative: project-level `.lsp.json`** — instead of installing the plugin or editing global config, add a `.lsp.json` file at your project root:

```json
{
  "java-functional": {
    "command": "java-functional-lsp",
    "extensionToLanguage": { ".java": "java" }
  }
}
```

This is useful for CI environments, containers, or ensuring all team members get the LSP server without individual setup. The `java-functional-lsp` binary must still be installed (`pip install java-functional-lsp` or `brew install aviadshiber/tap/java-functional-lsp`).

**Step 3: Nudge Claude to prefer LSP** (recommended):

Add to `~/.claude/rules/code-intelligence.md`:
```markdown
# Code Intelligence

Prefer LSP over Grep/Glob/Read for code navigation:
- goToDefinition / goToImplementation to jump to source
- findReferences to see all usages across the codebase
- hover for type info without reading the file

After writing or editing code, check LSP diagnostics before
moving on. Fix any type errors or missing imports immediately.
```

**Troubleshooting:**

| Issue | Fix |
|-------|-----|
| No diagnostics appear | Ensure `ENABLE_LSP_TOOL=1` is set, restart Claude Code |
| "java-functional-lsp not found" | Run `brew install aviadshiber/tap/java-functional-lsp` |
| Plugin not active | Run `claude plugin list` to verify, then `/reload-plugins` |
| Diagnostics slow on first open | Normal — tree-sitter parses on first load, then incremental |

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

The server has two layers:

- **Custom rules** — uses [tree-sitter](https://tree-sitter.github.io/) with the Java grammar for sub-millisecond AST analysis (~0.4ms per file). No compiler or classpath needed — runs on raw source files.
- **Java language features** — proxies [Eclipse jdtls](https://github.com/eclipse-jdtls/eclipse.jdt.ls) for compile errors, completions, hover, go-to-definition, and references. Diagnostics from both layers are merged and published together.

The server speaks the Language Server Protocol (LSP) via stdio, making it compatible with any LSP client.

## Development

```bash
# Clone and setup
git clone https://github.com/aviadshiber/java-functional-lsp.git
cd java-functional-lsp
uv sync
git config core.hooksPath .githooks

# Run checks
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
uv run pytest
```

Git hooks in `.githooks/` enforce quality automatically:
- **pre-commit** — runs lint, format, type check, and tests before each commit
- **pre-push** — blocks direct pushes to main (use feature branches + PRs)

See [CONTRIBUTING.md](CONTRIBUTING.md) for full guidelines.

## License

MIT
