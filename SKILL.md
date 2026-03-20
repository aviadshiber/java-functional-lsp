---
name: java-functional-lsp
description: Java LSP with full language support (completions, hover, go-to-def, compile errors) plus 12 functional programming rules enforcement. Auto-invoke when setting up Java language support or discussing Java linting configuration.
allowed-tools: Bash
disable-model-invocation: true
---

# Java Functional LSP

A Java LSP that wraps jdtls and adds functional programming code quality enforcement. Gives you **full Java language support** (completions, hover, go-to-def, compile errors) **plus** 12 custom rules — all before compilation.

## Prerequisites

```bash
# Install the LSP server
brew install aviadshiber/tap/java-functional-lsp
# Or: pip install java-functional-lsp

# Install jdtls for full Java support (completions, hover, compile errors)
brew install jdtls
```

Without jdtls, the server runs in standalone mode — custom rules still work, but no completions/hover/compile errors.

## What It Provides

**From jdtls** (Java language intelligence):
- Code completions, hover documentation
- Go-to-definition, find references
- Compile errors and warnings in real-time

**Custom rules** (12 functional programming checks):

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

Create `.deeperdive-linter.json` in your project root to customize rules:

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

## Troubleshooting

- **"java-functional-lsp not found"**: Run `brew install aviadshiber/tap/java-functional-lsp`
- **No completions/hover**: Install jdtls: `brew install jdtls`
- **Too many warnings**: Create `.deeperdive-linter.json` to tune severity or disable noisy rules
