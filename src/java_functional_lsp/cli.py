"""CLI check mode — run java-functional-lsp as a standalone linter without LSP."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .analyzers.base import Analyzer, Diagnostic, Severity, get_parser
from .analyzers.exception_checker import ExceptionChecker
from .analyzers.mutation_checker import MutationChecker
from .analyzers.null_checker import NullChecker
from .analyzers.spring_checker import SpringChecker

_ANALYZERS: list[Analyzer] = [NullChecker(), ExceptionChecker(), MutationChecker(), SpringChecker()]

_SEVERITY_SYMBOLS = {
    Severity.ERROR: "E",
    Severity.WARNING: "W",
    Severity.INFO: "I",
    Severity.HINT: "H",
}


def load_config(start_path: Path) -> dict[str, Any]:
    """Walk up from start_path to find .java-functional-lsp.json."""
    current = start_path if start_path.is_dir() else start_path.parent
    while current != current.parent:
        config_path = current / ".java-functional-lsp.json"
        if config_path.exists():
            try:
                result: dict[str, Any] = json.loads(config_path.read_text())
                return result
            except (json.JSONDecodeError, OSError):
                pass
        current = current.parent
    return {}


def check_file(path: Path, config: dict[str, Any]) -> list[Diagnostic]:
    """Analyze a single Java file and return diagnostics."""
    parser = get_parser()
    source = path.read_bytes()
    tree = parser.parse(source)

    all_diags: list[Diagnostic] = []
    for analyzer in _ANALYZERS:
        diags = analyzer.analyze(tree, source, config)
        all_diags.extend(diags)

    all_diags.sort(key=lambda d: (d.line, d.col))
    return all_diags


def format_diagnostic(path: Path, d: Diagnostic) -> str:
    """Format a diagnostic as a single line: path:line:col: [S] code: message."""
    sym = _SEVERITY_SYMBOLS.get(d.severity, "W")
    return f"{path}:{d.line + 1}:{d.col}: [{sym}] {d.code}: {d.message}"


def main() -> None:
    """CLI entry point: java-functional-lsp check <files...>"""
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print("Usage: java-functional-lsp check <file.java> [file2.java ...]")
        print("       java-functional-lsp check --dir <directory>")
        print("       java-functional-lsp          (start LSP server on stdio)")
        sys.exit(0)

    if args[0] != "check":
        # Not check mode — fall through to LSP server
        from .server import main as lsp_main

        lsp_main()
        return

    args = args[1:]  # skip "check"

    # Collect files
    files: list[Path] = []
    if args and args[0] == "--dir":
        if len(args) < 2:
            print("Error: --dir requires a directory path", file=sys.stderr)
            sys.exit(1)
        directory = Path(args[1])
        if not directory.is_dir():
            print(f"Error: {directory} is not a directory", file=sys.stderr)
            sys.exit(1)
        files = sorted(directory.rglob("*.java"))
    else:
        for arg in args:
            p = Path(arg)
            if p.is_file():
                files.append(p)
            elif p.is_dir():
                files.extend(sorted(p.rglob("*.java")))
            else:
                print(f"Warning: {arg} not found, skipping", file=sys.stderr)

    if not files:
        print("No .java files found", file=sys.stderr)
        sys.exit(1)

    # Load config from first file's directory
    config = load_config(files[0])

    total_diags = 0
    for path in files:
        diags = check_file(path, config)
        for d in diags:
            print(format_diagnostic(path, d))
        total_diags += len(diags)

    if total_diags > 0:
        print(f"\n{total_diags} diagnostic(s) in {len(files)} file(s)", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"No issues found in {len(files)} file(s)", file=sys.stderr)
        sys.exit(0)
