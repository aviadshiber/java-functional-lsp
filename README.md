# java-functional-lsp

[![CI](https://github.com/aviadshiber/java-functional-lsp/actions/workflows/test.yml/badge.svg)](https://github.com/aviadshiber/java-functional-lsp/actions/workflows/test.yml)
[![PyPI version](https://img.shields.io/pypi/v/java-functional-lsp?v=1)](https://pypi.org/project/java-functional-lsp/)
[![Python](https://img.shields.io/pypi/pyversions/java-functional-lsp?v=1)](https://pypi.org/project/java-functional-lsp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A Java Language Server that provides three things in one:

1. **Full Java language support** — completions, hover, go-to-definition, compile errors, missing imports — by proxying [Eclipse jdtls](https://github.com/eclipse-jdtls/eclipse.jdt.ls) under the hood
2. **17 functional programming rules** — catches anti-patterns and suggests Vavr/Lombok/Spring alternatives, all before compilation
3. **Code actions (quick fixes)** — automated refactoring via LSP `textDocument/codeAction`, with machine-readable diagnostic metadata for AI agents

Designed for teams using **Vavr**, **Lombok**, and **Spring** with a functional-first approach.

## What it checks

### Java language (via jdtls)

When [jdtls](https://github.com/eclipse-jdtls/eclipse.jdt.ls) is installed, the server proxies all standard Java language features:

- Compile errors and warnings
- Missing imports and unresolved symbols
- Type mismatches
- Completions, hover, go-to-definition, find references

Install jdtls separately: `brew install jdtls` (requires JDK 21+). The server auto-detects a Java 21+ installation even when the IDE's project SDK is older (e.g., Java 8) by probing `JDTLS_JAVA_HOME`, `JAVA_HOME`, `/usr/libexec/java_home -v 21+` (macOS), and `java` on PATH. Without jdtls, the server runs in standalone mode — the 17 custom rules still work, but you won't get compile errors or completions.

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
| `mutable-dto` | `@Data` or `@Setter` on class | `@Value` (immutable); `record` instead at `sourceLevel` 16+ | ✅ |
| `imperative-option-unwrap` | `if (opt.isDefined()) { opt.get() }` | `map()`/`flatMap()`/`fold()` | ✅ |
| `field-injection` | `@Autowired` on field | Constructor injection | — |
| `component-annotation` | `@Component`/`@Service`/`@Repository` | `@Configuration` + `@Bean` | — |
| `frozen-mutation` | Mutation on `List.of()`/`Collections.unmodifiable*` | `io.vavr.collection.List` | ✅ |
| `null-check-to-monadic` | `if (x != null) { return x.foo(); }` | `Option.of(x).map(...)` | ✅ |
| `try-catch-to-monadic` | `try { return x(); } catch (E e) { return d; }` | `Try.of(() -> x()).getOrElse(d)` | ✅ |
| `impure-method` | Method mixing pure logic with side-effects | Extract pure logic; wrap IO in `Try` / return `Either.left` instead of throwing | — |
| `option-map-nullable` | `Option.map(x -> x.get(k))` followed by chained call (`Some(null)` risk) | `.flatMap(x -> Option.of(...))` | — |

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

The server automatically detects JetBrains IDEs and disables the jdtls proxy (IntelliJ provides native Java support). To force-enable jdtls, set `JAVA_FUNCTIONAL_LSP_JDTLS=on` in the server command environment.

See [editors/intellij/README.md](editors/intellij/README.md) for detailed instructions.

### Claude Code

**Step 1: Enable LSP support** (required, one-time):

Add `lspServers` to `~/.claude/settings.json` (the plugin handles this automatically — only needed for manual setup):
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

**Step 2: Install the plugin:**

```bash
claude plugin add https://github.com/aviadshiber/java-functional-lsp.git
```

This registers the LSP server, adds auto-install hooks, a PostToolUse hook that lints every `.java` file after Edit/Write and feeds the violations back to Claude as context (plus a reminder hook on Read), and the `/lint-java` command.

**Manual hook setup (without the plugin)** — add the lint hook to `~/.claude/settings.json`, pointing at a checkout of this repo:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|MultiEdit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/java-functional-lsp/hooks/post_tool_lint.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

The hook is failure-safe: it only fires on `.java` files, lints just the edited file (well under 2s), stays silent when the file is clean, and always exits 0 so a linter problem can never break the editing session.

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
| No diagnostics appear | Ensure `lspServers` is configured (plugin or settings.json), restart |
| "java-functional-lsp not found" | Run `brew install aviadshiber/tap/java-functional-lsp` |
| Plugin not active | Run `claude plugin list` to verify, then `/reload-plugins` |
| Diagnostics slow on first open | Normal — tree-sitter parses on first load, then incremental |
| Java errors show up one tool call after the edit | Claude Code doesn't wait for LSP diagnostics after Edit/Write ([anthropics/claude-code#93321](https://github.com/anthropics/claude-code/issues/93321)). The plugin's hook waits up to 3s for them; if jdtls is slower (large projects), raise `JAVA_FUNCTIONAL_LSP_HOOK_WAIT` (max 4). See [Fresh jdtls diagnostics after edits](#fresh-jdtls-diagnostics-after-edits) |
| False "X cannot be resolved" / "The hierarchy of the type X is inconsistent" on classes from another Maven group of the same repository | The server imports such modules automatically when m2e reports them missing. See [Dependencies on modules in other Maven groups](#dependencies-on-modules-in-other-maven-groups) for the log lines to check, the budget (`JAVA_FUNCTIONAL_LSP_DEPENDENCY_MODULES`) and the stale-install case |

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
  "sourceLevel": 17,
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
- `sourceLevel` — the project's Java language/source level, as an int (e.g. `17`) or version string (`"17"`, legacy `"1.8"`). Also accepted as `javaVersion`. Defaults to `8` if unset, so existing configs are unaffected. Currently only changes the `mutable-dto` recommendation (see below); higher source levels unlock more rewrite targets over time (records, sealed types, pattern matching, text blocks).
- `suppressJdtlsPatterns` — list of regex patterns to suppress jdtls diagnostics (see below)

**Spring-aware behavior:**
- `throw-statement`, `catch-rethrow`, and `try-catch-to-monadic` are automatically suppressed inside `@Bean` methods
- `mutable-dto` suggests `@ConstructorBinding` instead of `@Value` when the class has `@ConfigurationProperties`

**Source-level-aware behavior:**
- `mutable-dto` recommends a `record` instead of `@Value` once `sourceLevel` is 16 or higher (records became final/non-preview in JDK 16) — a plain immutable DTO is simpler as a built-in `record` with no Lombok dependency. It still calls out `@Value` as the fallback when the class needs `@With`/`@Builder`/`@Jacksonized`, is used as a facade, or relies on AOP proxying (records are `final` and can't be proxied). Below `sourceLevel` 16 (including the default), the message and quick fix are unchanged — `@Value` only.

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

If Lombok is used in your project but the jar isn't found, the server logs a warning.

### jdtls settings

The server sends Maven/Gradle settings to jdtls at startup via `initializationOptions.settings`. Defaults are optimized for Maven monorepos (Maven enabled, Gradle disabled, build artifact exclusions). Override via `.java-functional-lsp.json`:

```json
{
  "jdtls": {
    "settings": {
      "java": {
        "import": {
          "maven": { "enabled": true },
          "gradle": { "enabled": true },
          "exclusions": ["**/node_modules/**", "**/target/**"]
        },
        "configuration": { "updateBuildConfiguration": "automatic" },
        "maven": { "downloadSources": true }
      }
    }
  }
}
```

Custom settings fully replace the defaults (no merge). See the [jdtls Preferences reference](https://github.com/eclipse-jdtls/eclipse.jdt.ls/blob/main/org.eclipse.jdt.ls.core/src/org/eclipse/jdt/ls/core/internal/preferences/Preferences.java) for all available keys.

### jdtls cache

The jdtls Eclipse workspace index is cached in `~/.cache/jdtls-data/`. Warm starts (~10-20s) reuse this cache; cold starts (60-120s) rebuild from scratch. The cache is automatically invalidated when jdtls or Java is upgraded, but **not** when java-functional-lsp is upgraded — our Python code changes don't affect the Eclipse index.

To force a clean rebuild: `rm -rf ~/.cache/jdtls-data/`

### Dependencies on modules in other Maven groups

To keep jdtls fast and within its heap, the server imports only the Maven *group* of the file you open (its tightest parent pom with `<modules>`), plus up to 5 more groups as you navigate. A module in that group can depend on a reactor module in *another* group. m2e then looks that dependency up in the local Maven repository at the reactor's version, often a `${revision}` default that is never installed. Every symbol from that module then shows as a false error: "cannot be resolved", "is undefined for the type", or "The hierarchy of the type X is inconsistent" ([#110](https://github.com/aviadshiber/java-functional-lsp/issues/110)). The server fixes this by importing the missing module itself.

How it works:

1. m2e reports the dependency as an error on the dependent's `pom.xml`: `Missing artifact com.example:common:jar:1.0` (prefixed with `Offline / ` when offline).
2. The server looks the `groupId:artifactId` up in an index of the reactor. The index is built once per session and covers the modules reachable from the reactor root through `<modules>`, with all profiles included.
3. If the artifact is a module of the repository and no imported folder covers it yet, the server adds the module's directory as a jdtls workspace folder. Additions are batched into one `didChangeWorkspaceFolders` per 500 ms. An added module that is missing its own in-repo dependencies reports them the same way, so the dependency closure is imported only as far as it is actually missing.
4. jdtls fixes the dependent's classpath within a fraction of a second, but it does not re-check files that are already open. When jdtls reports that a project's classpath was updated, the server asks it to re-validate every open `.java` file of that project (`java.project.refreshDiagnostics`). Those results go through the [diagnostics hold](#fresh-jdtls-diagnostics-after-edits).

If you later open a file in a group that contains one of these modules, the group folder replaces the module folder in the same workspace change. That module folder is never re-added.

Configuration:

| Setting | Values | Effect |
|---------|--------|--------|
| `JAVA_FUNCTIONAL_LSP_DEPENDENCY_MODULES` (environment) | `0`–`200` (default `60`) | Session budget of dependency-module folders; `0` disables the import |
| `{"jdtls": {"dependencyModules": N}}` in `.java-functional-lsp.json` | same | Same budget; the environment variable wins |

Limits:

- **Budget.** At most N module folders are added per session. A folder that a group later replaces still counts. When the budget runs out, the log shows a `WARNING` listing the modules left unresolved. Raise the budget, or open a file in those modules' group.
- **Stale local installs are not detected.** If an old build of the sibling is installed at the same version in your local repository, m2e resolves the dependency from that jar and reports nothing. You then see the installed version's API, not the source. Run `mvn install` for that module again, or delete it from the local repository.
- **Maven only.** Gradle projects are unchanged.
- **Index bounds.** The index skips poms larger than 1 MB, poms with `<!DOCTYPE`/`<!ENTITY` declarations (in any encoding), symlinked poms, modules outside the reactor root, coordinates with characters other than letters, digits, `_`, `.` and `-` (such as `${revision}`), and any `groupId:artifactId` declared by two directories. It reads at most 5000 poms, for at most 10 s.

Troubleshooting (the server log is its stderr, as captured by your LSP client):

- `jdtls: pom.xml errors changed for <pom>: … Missing artifact g:a:…` means m2e could not resolve that dependency.
- `jdtls: reactor index of <root>: N modules from M poms` means the index was built. `(truncated)` means it hit a bound.
- `jdtls: importing K dependency module(s) (used/budget): g:a (path), …` means those modules were added.
- `jdtls: classpath of <project> updated, refreshing K open file(s)` means open files were re-checked.
- If the errors stay and nothing was imported, the artifact is not a module of this reactor (check the `<modules>` path that should reach it), or it is a real external artifact missing from your repository.
- m2e caches failed lookups: `*.lastUpdated` files in the local repository, and the jdtls workspace in `~/.cache/jdtls-data/`. If errors persist after the module is imported, clear the jdtls cache as described above.

### Fresh jdtls diagnostics after edits

jdtls re-validates a file 0.4–2.4s after the last edit to *any* file, and its results carry no document version. So after an edit, java-functional-lsp holds back jdtls diagnostics for that file until jdtls publishes again, rather than re-sending the previous edit's results as if they were current ([#109](https://github.com/aviadshiber/java-functional-lsp/issues/109)):

| Mode | Default for | After an edit |
|------|-------------|---------------|
| `custom-first` | Claude Code | Custom diagnostics publish at once, without jdtls errors; the full set follows when jdtls has re-validated |
| `hold-all` | Other editors | Nothing is published for the file until jdtls has re-validated (no flickering squiggles) |
| `off` | — | Previous behavior: custom diagnostics plus the last known jdtls diagnostics after 150ms |

With Claude Code two more things happen:

- **The plugin's PostToolUse hook waits for fresh results.** Claude Code attaches diagnostics as soon as its PostToolUse hooks finish, without waiting for the server ([anthropics/claude-code#93321](https://github.com/anthropics/claude-code/issues/93321)). The server records when it last published final diagnostics for each file, and `hooks/post_tool_lint.py` waits until that is newer than the edit (at most `JAVA_FUNCTIONAL_LSP_HOOK_WAIT` seconds, default 3, max 4), so jdtls's result for the edit arrives in the same tool result. The markers live in a private per-user temp directory and contain only a timestamp.
- **jdtls gets a didSave after each edit.** Claude Code writes the file itself; when the edited buffer matches the file on disk, the server forwards a didSave. Without it, jdtls can re-check dependent files against the old version of the edited one (e.g. a caller keeps reporting a constructor's old arity) and never correct them.

If jdtls doesn't publish in time (3s after the last edit, stretched when jdtls is slow, 10s at most), the last known jdtls diagnostics are used. Files whose module is still being imported, and files matched by `java.diagnostic.filter`, are never held. While you edit the same file in quick succession, its diagnostics update only once the burst settles.

Environment variables:

| Variable | Values | Effect |
|----------|--------|--------|
| `JAVA_FUNCTIONAL_LSP_DIAG_HOLD` | `custom-first`, `hold-all`, `off` | Overrides the mode above; `off` restores the previous behavior |
| `JAVA_FUNCTIONAL_LSP_HOOK_WAIT` | seconds, `0`–`4` (default `3`) | How long the Claude Code PostToolUse hook waits for fresh jdtls diagnostics; `0` disables the wait |
| `JAVA_FUNCTIONAL_LSP_LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING` | Verbosity of java-functional-lsp's own logs; `DEBUG` adds one line per publish decision (file name, trigger, counts). Library logging (pygls) is unaffected — note that pygls already logs the JSON it sends, including diagnostic messages, at `INFO` |

The log reports `jdtls freshness: released=… too_early=… timeout=… late_correction=…` every 100 decisions and when jdtls stops. Repeated `timeout` lines mean jdtls is slow or stuck on that module. A growing `late_correction` count means jdtls results were released too early.

### Suppressing jdtls diagnostics

For project-specific jdtls false positives (e.g., annotation processor methods, MapStruct mappers), use `suppressJdtlsPatterns` to add custom regex filters:

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
| `imperative-option-unwrap` | Convert to Option.map().getOrElse() | Rewrites `if (opt.isDefined()) return opt.get(); else return X;` → `return opt.map(it -> ...).getOrElse(X);` (lazy `getOrElse(() -> ...)` for non-eager defaults). Bails on missing else or complex bodies. |
| `mutable-dto` | Replace @Data with @Value | Replaces the `@Data` annotation with `@Value` and adds `import lombok.Value`. Skips `@Setter`, `@ConfigurationProperties`, and conflicting Lombok constructor annotations. This quick fix is currently `@Value`-only regardless of `sourceLevel` — the diagnostic message/snippet recommend a `record` at `sourceLevel` 16+, but applying that rewrite safely (enumerating fields into record components) isn't automated yet; use the suggested snippet as a manual starting point. |

Quick fixes automatically add the required Vavr import if it's not already present. Disable auto-import with `"autoImportVavr": false` in config (`"autoImportLombok": false` for the Lombok import added by the `mutable-dto` fix).

## Agent mode (AI integration)

Every diagnostic includes a machine-readable `data` payload designed for AI agents like Claude Code:

```json
{
  "code": "frozen-mutation",
  "message": "Runtime Exception Risk: Mutating a frozen structure...",
  "data": {
    "fixType": "REPLACE_WITH_VAVR_LIST",
    "targetLibrary": "io.vavr.collection.List",
    "rationale": "Runtime mutation of List.of() causes UnsupportedOperationException. Use Vavr for safe, persistent immutability.",
    "recommendedApi": ".append / .appendAll / .update / .remove (returns a new persistent collection)",
    "suggestedSnippet": "list = list.append(\"c\");  // returns a new persistent collection"
  }
}
```

This lets agents confidently apply fixes without guessing libraries or patterns — the `fixType` tells them *what* to do, `targetLibrary` tells them *which dependency*, and `rationale` tells them *why*. `recommendedApi` names the exact method on the target library (e.g. Vavr `Option` uses `forEach`, **not** `ifPresent`) and `suggestedSnippet` is a paste-able fix built from the real AST variable names.

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
| `autoImportLombok` | `true` | The `mutable-dto` quick fix auto-adds `import lombok.Value` |
| `strictPurity` | `false` | When `true`, `impure-method` uses WARNING severity instead of HINT |
| `sourceLevel` (alias `javaVersion`) | `8` | Java source level; `mutable-dto` recommends `record` over `@Value` at 16+ |

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
