"""Regression guard: __version__ in src must equal pyproject.toml's project.version.

PR #88 (v0.11.0) bumped pyproject.toml to 0.11.0 but left __init__.py at 0.10.0,
shipping a package whose --version output disagreed with its metadata. v0.11.1
hotfixed it. This test prevents the same drift from recurring.
"""

from __future__ import annotations

import re
from pathlib import Path

from java_functional_lsp import __version__

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_version_matches_pyproject() -> None:
    text = PYPROJECT.read_text()
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert match is not None, f"pyproject.toml at {PYPROJECT} has no top-level version line"
    pyproject_version = match.group(1)
    assert __version__ == pyproject_version, (
        f"java_functional_lsp.__version__={__version__!r} disagrees with "
        f"pyproject.toml version={pyproject_version!r}. Update both when bumping."
    )
