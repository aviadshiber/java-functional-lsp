---
name: lint-java
description: Run java-functional-lsp linter on Java files and report diagnostics
allowed-tools: Bash, Read, Glob
---

# Java Functional Linter

Run the Java code quality linter on specified files or directories.

## Arguments

$ARGUMENTS - Java files, directories, or glob patterns to lint. If empty, lint all .java files in the current directory.

## Instructions

1. Determine the target files:
   - If $ARGUMENTS is provided, use it as the path(s)
   - If empty, use the current working directory

2. Run the linter:
   ```bash
   java-functional-lsp check $ARGUMENTS
   ```
   If no arguments: `java-functional-lsp check --dir .`

3. Present the results clearly:
   - Group diagnostics by file
   - For each diagnostic, show the line number, rule, and suggestion
   - If the file is short, show the offending line of code
   - Summarize total issues at the end

4. If the user asks to fix issues, suggest concrete code changes:
   - `null-return` → wrap in `Option.of()` or return `Either.left(error)`
   - `throw-statement` → convert to `Either.left()` or `Try.of()`
   - `mutable-variable` → make final, use functional transforms
   - `imperative-loop` → replace with `.map()`, `.filter()`, `.flatMap()`
   - `mutable-dto` → change `@Data` to `@Value`
   - `field-injection` → convert to constructor injection
   - `component-annotation` → move to `@Configuration` class with `@Bean`
