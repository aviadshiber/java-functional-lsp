# Java Functional LSP — IntelliJ IDEA Setup

Use [LSP4IJ](https://github.com/redhat-developer/lsp4ij) (by Red Hat) to connect `java-functional-lsp` to IntelliJ IDEA. Works on both **Community** and **Ultimate** editions.

## Prerequisites

1. Install the LSP server:

   ```bash
   # Homebrew
   brew install aviadshiber/tap/java-functional-lsp

   # pip
   pip install java-functional-lsp
   ```

2. Verify it's on your PATH:

   ```bash
   java-functional-lsp --help
   ```

   If not on PATH, note the full path (e.g., `which java-functional-lsp`).

## Step 1: Install LSP4IJ Plugin

1. Open IntelliJ IDEA
2. Go to **Settings** → **Plugins** → **Marketplace**
3. Search for **"LSP4IJ"**
4. Click **Install** and restart the IDE

## Step 2: Add Language Server

1. Go to **Settings** → **Languages & Frameworks** → **Language Servers**
2. Click the **`+`** button to add a new server
3. Configure:

| Field | Value |
|-------|-------|
| **Name** | `Java Functional LSP` |
| **Command** | `java-functional-lsp` |

> **PATH issues?** If IntelliJ can't find the binary, use the full path:
> - Homebrew: `/opt/homebrew/bin/java-functional-lsp`
> - pip: `~/.local/bin/java-functional-lsp` (or check with `which java-functional-lsp`)
>
> On macOS, you can also use: `sh -c java-functional-lsp`

## Step 3: Configure File Mappings

In the **Mappings** tab of the server configuration, you can map by Language, File type, or File name patterns. The simplest option:

1. Click the **File name patterns** sub-tab
2. Click **`+`** to add a pattern
3. Set **File name patterns**: `*.java`
4. Set **Language Id**: `java`

## Step 4: Verify

1. Open any `.java` file in your project
2. Check the bottom status bar — you should see the LSP server connecting
3. Write code that triggers a rule (e.g., `return null;`) — a warning should appear

## Configuration

Project-level rules are configured via `.java-functional-lsp.json` in your project root. See the [main README](../../README.md) for details.

## Coexistence with IntelliJ's Java Support

LSP4IJ is designed to **supplement** IntelliJ's native Java support, not replace it. Your custom functional programming diagnostics appear alongside IntelliJ's built-in inspections. No conflicts.

## Troubleshooting

### Server doesn't start
- Check **Settings** → **Languages & Frameworks** → **Language Servers** → select your server → **Traces** tab for error logs
- Verify the binary works: run `java-functional-lsp check --help` in a terminal

### No diagnostics appear
- Ensure the file mapping is set to Language: `Java`, Language ID: `java`
- Check that `.java-functional-lsp.json` doesn't have rules set to `"off"`
- Try restarting the language server: **Tools** → **Language Servers** → **Restart**

### PATH not found
- IntelliJ may not inherit your shell's PATH. Use the full absolute path to the binary in the server command.
