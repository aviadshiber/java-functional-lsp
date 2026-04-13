# java-functional-lsp

[![CI](https://github.com/aviadshiber/java-functional-lsp/actions/workflows/test.yml/badge.svg)](https://github.com/aviadshiber/java-functional-lsp/actions/workflows/test.yml)
[![PyPI version](https://img.shields.io/pypi/v/java-functional-lsp?v=1)](https://pypi.org/project/java-functional-lsp/)
[![Python](https://img.shields.io/pypi/pyversions/java-functional-lsp?v=1)](https://pypi.org/project/java-functional-lsp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A Java Language Server that provides three things in one:

1. **Full Java language support** — completions, hover, go-to-definition, compile errors, missing imports — by proxying [Eclipse jdtls](https://github.com/eclipse-jdtls/eclipse.jdt.ls) under the hood
2. **16 functional programming rules** — catches anti-patterns and suggests Vavr/Lombok/Spring alternatives, all before compilation
3. **Code actions (quick fixes)** — automated refactoring via LSP `textDocument/codeAction`, with machine-readable diagnostic metadata for AI agents

Designed for teams using **Vavr**, **Lombok**, and **Spring** with a functional-first approach.

## What it checks

### Java language (via jdtls)

When [jdtls](https://github.com/eclipse-jdtls/eclipse.jdt.ls) is installed, the server proxies all standard Java language features:

- Compile errors and warnings
- Missing imports and unresolved symbols
- Type mismatches
- Completions, hover, go-to-definition, find references

Install jdtls separately: `brew install jdtls` (requires JDK 21+). The server auto-detects a Java 21+ installation even when the IDE's project SDK is older (e.g., Java 8) by probing `JDTLS_JAVA_HOME`, `JAVA_HOME`, `/usr/libexec/java_home -v 21+` (macOS), and `java` on PATH. Without jdtls, the server runs in standalone mode — the 16 custom rules still work, but you won't get compile errors or completions.

### Functional programming rules

| Rule | Detects | Suggests | Quick Fix |
|------|---------|----------|-----------|
| `null-literal-arg` | `null` passed as method argument | `Option.none()` or default value | — |
| `null-return` | `return null` | `Option.of()`, `Option.none()`, or `Either` | ✅ |
| `null-assignment` | `Type x = null` | `Option<Type>` | — |
| `null-field-assignment` | Field initialized to `null` | `Option<T>` with `Option.none()` | — |
| `throw-statement` | `throw new XxxException(...)` | `Either.left()` or `Try.of()` | — |
| `catch-rethrow` | catch block that wraps + rethrows | `Try.of().toEither()` | — |
| `mutable-variable` | Local variable reassignment | Final variables + functional transforms | — |
| `imperative-loop` | `for`/`while` loops | `.map()`/`.filter()`/`.flatMap()`/`.foldLeft()` | — |
| `mutable-dto` | `@Data` or `@Setter` on class | `@Value` (immutable) | — |
| `imperative-option-unwrap` | `if (opt.isDefined()) { opt.get() }` | `map()`/`flatMap()`/`fold()` | — |
| `field-injection` | `@Autowired` on field | Constructor injection | — |
| `component-annotation` | `@Component`/`@Service`/`@Repository` | `@Configuration` + `@Bean` | — |
| `frozen-mutation` | Mutation on `List.of()`/`Collections.unmodifiable*` | `io.vavr.collection.List` | ✅ |
| `null-check-to-monadic` | `if (x != null) { return x.foo(); }` | `Option.of(x).map(...)` | ✅ |
| `try-catch-to-monadic` | `try { return x(); } catch (E e) { return d; }` | `Try.of(() -> x()).getOrElse(d)` | ✅ |
| `impure-method` | Method mixing pure logic with side-effects | Extract pure logic; wrap IO in `Try` | — |

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
- `suppressJdtlsPatterns` — list of regex patterns to suppress jdtls diagnostics (see below)

**Spring-aware behavior:**
- `throw-statement`, `catch-rethrow`, and `try-catch-to-monadic` are automatically suppressed inside `@Bean` methods
- `mutable-dto` suggests `@ConstructorBinding` instead of `@Value` when the class has `@ConfigurationProperties`

**Inline suppression** with `@SuppressWarnings`:

```java
// Suppress a specific rule on a method
@SuppressWarnings("java-functional-lsp:null-return")
public String findUser() { return null; }  // no diagnostic

// Suppress multiple rules
@SuppressWarnings({"java-functional-lsp:null-return", "java-functional-lsp:throw-statement"})
public String findUser() { ... }

// Suppress all java-functional-lsp rules
@SuppressWarnings("java-functional-lsp")
public String legacyMethod() { ... }
```

Works on classes, methods, constructors, fields, and local variables. Suppression applies to the annotated scope — a class-level annotation suppresses all methods within it.

### Lombok support

Projects using [Lombok](https://projectlombok.org/) need the Lombok Java agent for jdtls to process `@Builder`, `@Value`, `@Data`, `@Slf4j`, and other annotations. Without it, jdtls reports false "method undefined" errors for generated methods.

The server auto-discovers `lombok.jar` from these locations (first match wins):

1. **Project config** — add to `.java-functional-lsp.json`:
   ```json
   { "lombok": "/path/to/lombok.jar" }
   ```
2. **Environment variable** — `LOMBOK_JAR=/path/to/lombok.jar`
3. **Maven cache** — auto-discovered from `~/.m2/repository/org/projectlombok/lombok/`
4. **Dedicated directory** — `~/.jdtls-libs/lombok.jar`

If Lombok is used in your project but the jar isn't found, the server automatically filters common Lombok false-positive errors (like "builder() is undefined") so they don't clutter your diagnostics.

### Suppressing jdtls false positives

In large monorepos, jdtls may report false "method undefined" errors for Lombok-generated methods (e.g., `@Value(staticConstructor = "of")`) in modules it hasn't fully indexed. The server automatically filters common Lombok patterns (`builder()`, `toBuilder()`, `of()`, `create()`, `log`, `*Builder` types, blank final fields).

For project-specific patterns (e.g., MapStruct mappers, other annotation processors), use `suppressJdtlsPatterns` to add custom regex filters:

```json
{
  "suppressJdtlsPatterns": [
    "The method \\w+Mapper\\(\\) is undefined",
    "cannot be resolved to a type"
  ]
}
```

Each entry is a regex matched against jdtls diagnostic messages. Invalid patterns are skipped with a warning.

## Code actions (quick fixes)

The server provides LSP code actions (`textDocument/codeAction`) that automatically refactor code. When your editor shows a diagnostic with a lightbulb icon, clicking it applies the fix:

| Rule | Code Action | What it does |
|------|-------------|--------------|
| `frozen-mutation` | Switch to Vavr Immutable Collection | Rewrites `List.of()` → `io.vavr.collection.List.of()`, `.add(x)` → `= list.append(x)`, adds import |
| `null-check-to-monadic` | Convert to Option monadic flow | Rewrites `if (x != null) { return x.foo(); }` → `Option.of(x).map(...)`, supports chained fallbacks via `.orElse()`, adds import |
| `null-return` | Replace with Option.none() | Rewrites `return null` → `return Option.none()`, adds import |
| `try-catch-to-monadic` | Convert try/catch to Try monadic flow | Rewrites `try { return expr; } catch (E e) { return default; }` → `Try.of(() -> expr).getOrElse(default)`. Supports 3 patterns: simple default (eager/lazy `.getOrElse`), logging + default (`.onFailure().getOrElse`), and exception-dependent recovery (`.recover(E.class, ...).get()`). Skips try-with-resources, finally, multi-catch, and union types. Adds import. |

Quick fixes automatically add the required Vavr import if it's not already present. Disable auto-import with `"autoImportVavr": false` in config.

## Agent mode (AI integration)

Every diagnostic includes a machine-readable `data` payload designed for AI agents like Claude Code:

```json
{
  "code": "frozen-mutation",
  "message": "Runtime Exception Risk: Mutating a frozen structure...",
  "data": {
    "fixType": "REPLACE_WITH_VAVR_LIST",
    "targetLibrary": "io.vavr.collection.List",
    "rationale": "Runtime mutation of List.of() causes UnsupportedOperationException. Use Vavr for safe, persistent immutability."
  }
}
```

This lets agents confidently apply fixes without guessing libraries or patterns — the `fixType` tells them *what* to do, `targetLibrary` tells them *which dependency*, and `rationale` tells them *why*.

**Agent mode configuration** in `.java-functional-lsp.json`:

```json
{
  "autoImportVavr": true,
  "strictPurity": true
}
```

| Key | Default | Effect |
|-----|---------|--------|
| `autoImportVavr` | `true` | Quick fixes auto-add Vavr/Option imports |
| `strictPurity` | `false` | When `true`, `impure-method` uses WARNING severity instead of HINT |

> **Note:** The machine-readable `data` payload is always included in diagnostics when available — no configuration needed.

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
