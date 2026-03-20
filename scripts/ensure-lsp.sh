#!/usr/bin/env bash
# Ensure java-functional-lsp is installed.
# Called by the SessionStart hook when the plugin is active.
# Upgrades to latest if already installed.

set -euo pipefail

if ! command -v java-functional-lsp &>/dev/null; then
    echo '{"systemMessage": "Installing java-functional-lsp..."}'
    pip install --quiet java-functional-lsp 2>&1 >&2 || {
        echo '{"systemMessage": "java-functional-lsp install failed. Run: brew install aviadshiber/tap/java-functional-lsp"}'
        exit 0  # Don't block session start
    }
    echo '{"systemMessage": "java-functional-lsp installed successfully."}'
else
    # Silently upgrade to latest in background
    pip install --quiet --upgrade java-functional-lsp 2>/dev/null >&2 &
fi
