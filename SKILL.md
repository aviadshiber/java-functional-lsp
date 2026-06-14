---
name: java-functional-lsp
description: Java LSP with full language support (completions, hover, go-to-def, compile errors) plus 17 functional programming rules with automated quick fixes. Auto-invoke when setting up Java language support or discussing Java linting configuration.
allowed-tools: Bash
disable-model-invocation: true
---

# Java Functional LSP

A Java LSP server that wraps jdtls and adds 17 functional programming rules with code actions (quick fixes). Gives you **full Java language support** (completions, hover, go-to-def, compile errors) **plus** custom diagnostics with machine-readable metadata for AI agents — all before compilation.

## Prerequisites

```bash
# Install the LSP server
brew install aviadshiber/tap/java-functional-lsp
# Or: pip install java-functional-lsp

# Install jdtls for full Java support (optional)
brew install jdtls
```

Without jdtls, the server runs in standalone mode — custom rules still work, but no completions/hover/compile errors.

## Rules (17 checks)

| Rule | Detects | Suggests | Quick Fix |
|------|---------|----------|-----------|
| `null-literal-arg` | `null` passed as argument | `Option.none()` or default | — |
| `null-return` | `return null` | `Option.of()`, `Option.none()`, or `Either` | ✅ |
| `null-assignment` | `Type x = null` | `Option<Type>` | — |
| `null-field-assignment` | Field initialized to `null` | `Option<T>` with `Option.none()` | — |
| `throw-statement` | `throw new XxxException(...)` | `Either.left()` or `Try.of()` | — |
| `catch-rethrow` | catch wraps + rethrows | `Try.of().toEither()` | — |
| `mutable-variable` | Variable reassignment | Final + functional transforms | — |
| `imperative-loop` | `for`/`while` loops | `.map()`/`.filter()`/`.flatMap()` | — |
| `mutable-dto` | `@Data` or `@Setter` | `@Value` (immutable) | ✅ |
| `imperative-option-unwrap` | `if (opt.isDefined()) { opt.get() }` | `map()`/`flatMap()`/`fold()` | ✅ |
| `field-injection` | `@Autowired` on field | Constructor injection | — |
| `component-annotation` | `@Component`/`@Service`/`@Repository` | `@Configuration` + `@Bean` | — |
| `frozen-mutation` | Mutation on `List.of()`/`Collections.unmodifiable*` | `io.vavr.collection.List` | ✅ |
| `null-check-to-monadic` | `if (x != null) { return x.foo(); }` | `Option.of(x).map(...)` | ✅ |
| `try-catch-to-monadic` | `try { return x(); } catch (E e) { return d; }` | `Try.of(() -> x()).getOrElse(d)` | ✅ |
| `impure-method` | Method mixing pure logic with side-effects | Extract pure logic; wrap IO in `Try` / return `Either.left` instead of throwing | — |
| `option-map-nullable` | `Option.map(x -> x.get(k))` followed by chained call (`Some(null)` risk) | `.flatMap(x -> Option.of(...))` | — |

## Code Actions (Quick Fixes)

Rules marked ✅ provide automated `textDocument/codeAction` fixes:

- **frozen-mutation** → "Switch to Vavr Immutable Collection" — rewrites type, init, and mutation call to Vavr persistent API, adds import
- **null-check-to-monadic** → "Convert to Option monadic flow" — rewrites `if (x != null)` to `Option.of(x).map(...)`, supports chained fallbacks via `.orElse()`, adds import
- **null-return** → "Replace with Option.none()" — replaces `null` with `Option.none()`, adds import
- **try-catch-to-monadic** → "Convert try/catch to Try monadic flow" — rewrites `try { return expr; } catch (E e) { return default; }` to `Try.of(() -> expr).getOrElse(default)`. Supports 3 patterns: simple default, logging + default (`.onFailure().getOrElse`), and exception-dependent recovery (`.recover(E.class, ...).get()`). Skips try-with-resources, finally, multi-catch, union types. Adds import.
- **imperative-option-unwrap** → "Convert to Option.map().getOrElse()" — rewrites `if (opt.isDefined()) return opt.get(); else return X;` to `return opt.map(it -> ...).getOrElse(X);` (lazy supplier for non-eager defaults). Bails on missing else or complex bodies.
- **mutable-dto** → "Replace @Data with @Value" — swaps the annotation and adds `import lombok.Value` (disable with `"autoImportLombok": false`). Skips `@Setter`, `@ConfigurationProperties`, and conflicting Lombok constructor annotations.

## Agent-Ready Diagnostics

Every diagnostic includes a machine-readable `data` payload:

```json
{
  "fixType": "REPLACE_WITH_VAVR_LIST",
  "targetLibrary": "io.vavr.collection.List",
  "rationale": "Runtime mutation of List.of() causes UnsupportedOperationException.",
  "recommendedApi": ".append / .appendAll / .update / .remove (returns a new persistent collection)",
  "suggestedSnippet": "list = list.append(\"c\");  // returns a new persistent collection"
}
```

This lets AI agents apply fixes with confidence — `fixType` says what to do, `targetLibrary` says which dependency, `rationale` says why, `recommendedApi` names the exact method (e.g. Vavr `Option` uses `forEach`, not `ifPresent`), and `suggestedSnippet` is a paste-able fix built from real AST variable names.

## Configuration

Create `.java-functional-lsp.json` in your project root:

```json
{
  "excludes": ["**/generated/**", "**/vendor/**"],
  "rules": {
    "imperative-loop": "hint",
    "mutable-variable": "info",
    "throw-statement": "off"
  },
  "autoImportVavr": true,
  "strictPurity": false
}
```

- `excludes` — glob patterns to skip files/directories entirely
- `rules` — per-rule severity: `error`, `warning` (default), `info`, `hint`, `off`
- `autoImportVavr` — quick fixes auto-add Vavr imports (default: `true`)
- `strictPurity` — `impure-method` uses WARNING instead of HINT (default: `false`)
- `throw-statement`/`catch-rethrow`/`try-catch-to-monadic` auto-suppressed in `@Bean` methods
- `mutable-dto` suggests `@ConstructorBinding` for `@ConfigurationProperties` classes
- Inline suppression: `@SuppressWarnings("java-functional-lsp:rule-id")` on any declaration

## Automatic Enforcement

The plugin includes two PostToolUse hooks:

- **Edit/MultiEdit/Write** → `hooks/post_tool_lint.py` lints the edited `.java` file (single-file, <2s) and injects any violations into Claude's context so they get fixed immediately. Silent on clean files; internal errors are swallowed (always exits 0) so the editing session is never broken.
- **Read** → `hooks/java_linter_reminder.py` reminds Claude to act on LSP diagnostics shown for the file.

## On-Demand Linting

Use `/lint-java <path>` to force-run the linter on specific files or directories.

## Releasing a New Version

To release a new version:

1. Bump version in `src/java_functional_lsp/__init__.py` and `pyproject.toml`
2. Update `.claude-plugin/plugin.json` version to match
3. Commit and push to main
4. Create a GitHub release with a tag matching `v*` (e.g., `v0.3.0`)
5. CI automatically publishes to PyPI and builds the VS Code extension `.vsix`
6. Run `python3 scripts/generate-formula.py <version>` and update the Homebrew tap

## Enabling LSP in Claude Code

Declare `lspServers` in `~/.claude/settings.json` or in `plugin.json` — Claude Code enables the LSP tool automatically when `lspServers` is configured. `ENABLE_LSP_TOOL=1` is no longer needed.

For containers or CI, add a `.lsp.json` at the project root instead of installing the plugin:
```json
{ "java-functional": { "command": "java-functional-lsp", "extensionToLanguage": { ".java": "java" }, "startupTimeout": 120000, "restartOnCrash": true, "maxRestarts": 5 } }
```

To nudge Claude to act on diagnostics, add to your project's `CLAUDE.md`:
```
After writing or editing Java code, check LSP diagnostics before moving on.
Fix any violations immediately — do not explain, just apply the fix.
```

## Troubleshooting

- **No diagnostics in Claude Code**: Ensure `lspServers` is configured (in `plugin.json` or `settings.json`), restart Claude Code
- **"java-functional-lsp not found"**: Run `brew install aviadshiber/tap/java-functional-lsp`
- **No completions/hover**: Install jdtls: `brew install jdtls` (requires JDK 21+)
- **Too many warnings**: Create `.java-functional-lsp.json` with `excludes` or per-rule severity
- **Plugin not active**: Run `claude plugin list` to verify, then `/reload-plugins`
