---
name: java-functional-lsp
description: Java LSP with full language support (completions, hover, go-to-def, compile errors) plus 12 functional programming rules enforcement. Auto-invoke when setting up Java language support or discussing Java linting configuration.
allowed-tools: Bash
disable-model-invocation: true
---

# Java Functional LSP

A Java LSP server that wraps jdtls and adds 12 functional programming rules. Gives you **full Java language support** (completions, hover, go-to-def, compile errors) **plus** custom diagnostics — all before compilation.

## Prerequisites

```bash
# Install the LSP server
brew install aviadshiber/tap/java-functional-lsp
# Or: pip install java-functional-lsp

# Install jdtls for full Java support (optional)
brew install jdtls
```

Without jdtls, the server runs in standalone mode — custom rules still work, but no completions/hover/compile errors.

## Rules (12 checks)

| Rule | Detects | Suggests |
|------|---------|----------|
| `null-literal-arg` | `null` passed as argument | `Option.none()` or default |
| `null-return` | `return null` | `Option.of()`, `Option.none()`, or `Either` |
| `null-assignment` | `Type x = null` | `Option<Type>` |
| `null-field-assignment` | Field initialized to `null` | `Option<T>` with `Option.none()` |
| `throw-statement` | `throw new XxxException(...)` | `Either.left()` or `Try.of()` |
| `catch-rethrow` | catch wraps + rethrows | `Try.of().toEither()` |
| `mutable-variable` | Variable reassignment | Final + functional transforms |
| `imperative-loop` | `for`/`while` loops | `.map()`/`.filter()`/`.flatMap()` |
| `mutable-dto` | `@Data` or `@Setter` | `@Value` (immutable) |
| `imperative-option-unwrap` | `if (opt.isDefined()) { opt.get() }` | `map()`/`flatMap()`/`fold()` |
| `field-injection` | `@Autowired` on field | Constructor injection |
| `component-annotation` | `@Component`/`@Service`/`@Repository` | `@Configuration` + `@Bean` |

## Configuration

Create `.java-functional-lsp.json` in your project root:

```json
{
  "rules": {
    "imperative-loop": "hint",
    "mutable-variable": "info",
    "throw-statement": "off"
  }
}
```

Severity levels: `error`, `warning` (default), `info`, `hint`, `off`.

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

## Troubleshooting

- **"java-functional-lsp not found"**: Run `brew install aviadshiber/tap/java-functional-lsp`
- **No completions/hover**: Install jdtls: `brew install jdtls`
- **Too many warnings**: Create `.java-functional-lsp.json` to tune severity or disable noisy rules
