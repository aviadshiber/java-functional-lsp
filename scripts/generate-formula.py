#!/usr/bin/env python3
"""Generate a Homebrew formula for java-functional-lsp.

Usage: python3 scripts/generate-formula.py <version>
"""
from __future__ import annotations

import json
import sys
import urllib.request


def get_pypi_sdist_info(package: str, version: str) -> tuple[str, str]:
    """Get the sdist URL and sha256 for a package from PyPI."""
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())

    for file_info in data.get("urls", []):
        if file_info["filename"].endswith(".tar.gz"):
            return file_info["url"], file_info["digests"]["sha256"]

    for file_info in data.get("urls", []):
        if file_info["packagetype"] == "sdist":
            return file_info["url"], file_info["digests"]["sha256"]

    raise ValueError(f"No sdist found for {package}=={version}")


def generate_formula(version: str) -> str:
    """Generate the Homebrew formula."""
    sdist_url, sha256 = get_pypi_sdist_info("java-functional-lsp", version)

    return f'''class JavaFunctionalLsp < Formula
  desc "Java LSP server enforcing functional programming best practices"
  homepage "https://github.com/aviadshiber/java-functional-lsp"
  url "{sdist_url}"
  sha256 "{sha256}"
  license "MIT"

  depends_on "python@3.12"
  depends_on "jdtls" => :recommended

  def install
    python3 = "python3.12"
    venv = libexec/"venv"
    system python3, "-m", "venv", venv
    system venv/"bin/pip", "install", "--upgrade", "pip"
    system venv/"bin/pip", "install", buildpath
    bin.install_symlink Dir[venv/"bin/java-functional-lsp"]
  end

  test do
    assert_match "java-functional-lsp", shell_output("#{{bin}}/java-functional-lsp --help 2>&1", 1)
  end
end
'''


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <version>", file=sys.stderr)
        sys.exit(1)
    print(generate_formula(sys.argv[1]))
