#!/usr/bin/env python3
"""Quick test: run all analyzers on a sample Java snippet and on real files."""

import sys
from java_functional_lsp.analyzers.base import get_parser
from java_functional_lsp.analyzers.null_checker import NullChecker
from java_functional_lsp.analyzers.exception_checker import ExceptionChecker
from java_functional_lsp.analyzers.mutation_checker import MutationChecker
from java_functional_lsp.analyzers.spring_checker import SpringChecker

SAMPLE = b"""
package com.example;

import lombok.Data;
import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Autowired;

@Data
@Service
public class BadExample {

    @Autowired
    private SomeService someService;

    private String name = null;

    public String findName(String id) {
        if (id == null) {
            return null;
        }
        String result = someService.lookup(id, null);
        result = result.toUpperCase();

        for (String item : getItems()) {
            System.out.println(item);
        }

        if (optionalValue.isDefined()) {
            return optionalValue.get();
        }

        try {
            riskyOperation();
        } catch (Exception e) {
            throw new RuntimeException("Failed", e);
        }

        throw new IllegalStateException("unreachable");
    }
}
"""

def main():
    parser = get_parser()
    config = {}  # all rules enabled at default severity
    analyzers = [NullChecker(), ExceptionChecker(), MutationChecker(), SpringChecker()]

    # Test against sample
    print("=== Testing against sample snippet ===")
    tree = parser.parse(SAMPLE)
    all_diags = []
    for analyzer in analyzers:
        diags = analyzer.analyze(tree, SAMPLE, config)
        all_diags.extend(diags)

    all_diags.sort(key=lambda d: (d.line, d.col))
    for d in all_diags:
        print(f"  L{d.line + 1}:{d.col}  [{d.severity.name}] {d.code}: {d.message}")

    print(f"\n  Total: {len(all_diags)} diagnostics\n")

    # Test against real files if provided
    if len(sys.argv) > 1:
        for path in sys.argv[1:]:
            print(f"=== {path} ===")
            with open(path, "rb") as f:
                source = f.read()
            tree = parser.parse(source)
            all_diags = []
            for analyzer in analyzers:
                diags = analyzer.analyze(tree, source, config)
                all_diags.extend(diags)
            all_diags.sort(key=lambda d: (d.line, d.col))
            for d in all_diags:
                print(f"  L{d.line + 1}:{d.col}  [{d.severity.name}] {d.code}: {d.message}")
            print(f"  Total: {len(all_diags)} diagnostics\n")


if __name__ == "__main__":
    main()
