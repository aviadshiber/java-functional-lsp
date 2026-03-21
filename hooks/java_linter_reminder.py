#!/usr/bin/env python3
"""PostToolUse hook: remind Claude to address java-functional-lsp diagnostics after reading/editing Java files."""

import json
import sys


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return

    tool_input = hook_input.get("tool_input", {})
    file_path = tool_input.get("file_path", "")

    if not file_path.endswith(".java"):
        return

    result = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                "If <new-diagnostics> from java-functional-lsp appear above, "
                "fix each violation immediately. Do not explain or list them — "
                "just apply the fix in your next Edit."
            ),
        }
    }
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
