# Java Functional LSP — VS Code Extension

Enforces functional programming best practices in Java files. Detects null usage, mutable state, exceptions, imperative loops, and Spring anti-patterns — and suggests functional alternatives using Vavr, Lombok, and idiomatic patterns.

## Prerequisites

Install the LSP server:

```bash
# Homebrew
brew install aviadshiber/tap/java-functional-lsp

# pip
pip install java-functional-lsp
```

## Install the Extension

### From VSIX (recommended)

Download the latest `.vsix` from [GitHub Releases](https://github.com/aviadshiber/java-functional-lsp/releases), then:

```bash
code --install-extension java-functional-lsp-*.vsix
```

Or in VS Code: Extensions panel → `...` menu → "Install from VSIX..."

### Build from Source

```bash
cd editors/vscode
npm install
npm run compile
npx vsce package
code --install-extension java-functional-lsp-*.vsix
```

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `javaFunctionalLsp.serverPath` | `"java-functional-lsp"` | Path to the LSP binary. Override if not on PATH. |
| `javaFunctionalLsp.enabled` | `true` | Enable/disable the extension. |

Example `settings.json`:

```json
{
  "javaFunctionalLsp.serverPath": "/opt/homebrew/bin/java-functional-lsp"
}
```

## Coexistence with Other Extensions

This extension **coexists** with the Red Hat Java extension (`redhat.java`) and other Java extensions. Diagnostics from all servers are shown together — no configuration needed.

## Rules

See the [main README](../../README.md) for the full list of 12 rules and configuration options via `.java-functional-lsp.json`.
