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
                "CHECK for <new-diagnostics> from java-functional-lsp above. "
                "If any appear, you MUST acknowledge them and suggest fixes: "
                "null → Option/Either, throw → Either.left()/Try.of(), "
                "mutable → final + functional transforms, loops → .map()/.filter()/.flatMap(), "
                "@Data → @Value, @Autowired → constructor injection, @Component → @Configuration+@Bean."
            ),
        }
    }
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
