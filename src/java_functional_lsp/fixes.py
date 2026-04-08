"""Quick fix generators for functional refactoring code actions.

Each fix generator takes document context and returns a list of TextEdits.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from lsprotocol import types as lsp
from tree_sitter import Node, Tree

from .analyzers.base import extract_null_check_var, find_ancestor, find_nodes, get_parser

# Type for fix generator functions: (uri, source, diag_range, config, *, tree, lines) -> WorkspaceEdit | None
FixGenerator = Callable[..., lsp.WorkspaceEdit | None]

# --- Import management ---


def _find_import_insert_position(lines: list[str]) -> int:
    """Find the line number where a new import should be inserted.

    Returns the line after the last existing import, or after the package declaration,
    or line 0 if neither exists.
    """
    last_import_line = -1
    package_line = -1

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import "):
            last_import_line = i
        elif stripped.startswith("package "):
            package_line = i

    if last_import_line >= 0:
        return last_import_line + 1
    if package_line >= 0:
        # +2 to leave a blank line after the package declaration, but clamp to file length
        return min(package_line + 2, len(lines))
    return 0


def _has_import(lines: list[str], import_path: str) -> bool:
    """Check if an import statement already exists."""
    target = f"import {import_path};"
    return any(line.strip() == target for line in lines)


_JAVA_IMPORT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")


def ensure_import(lines: list[str], import_path: str) -> lsp.TextEdit | None:
    """Return a TextEdit adding the import if it doesn't exist, or None.

    Returns None for invalid import paths (must match a valid Java FQN pattern).
    """
    if not _JAVA_IMPORT_RE.match(import_path):
        return None
    if _has_import(lines, import_path):
        return None
    insert_line = _find_import_insert_position(lines)
    return lsp.TextEdit(
        range=lsp.Range(
            start=lsp.Position(line=insert_line, character=0),
            end=lsp.Position(line=insert_line, character=0),
        ),
        new_text=f"import {import_path};\n",
    )


# --- Fix generators ---


def _find_method_invocation(tree: Tree, diag_range: lsp.Range) -> Node | None:
    """Find the method_invocation node at or near the diagnostic range."""
    target = tree.root_node.descendant_for_point_range(
        (diag_range.start.line, diag_range.start.character),
        (diag_range.end.line, diag_range.end.character),
    )
    if target is None:
        return None
    if target.type == "method_invocation":
        return target
    # Search children first (diagnostic range may cover the expression_statement)
    for child in find_nodes(target, "method_invocation"):
        return child
    # Then walk up
    return find_ancestor(target, "method_invocation")


def _find_frozen_decl_info(tree: Tree, var_name: str) -> tuple[str, Node | None]:
    """Find the variable declaration for var_name and return (collection_type, decl_node).

    Scans local_variable_declaration nodes once to detect both the collection type
    and the declaration node — avoiding a second full tree walk.

    Returns ("List", None) if no matching declaration is found.
    """
    var_bytes = var_name.encode("utf-8")
    for decl in find_nodes(tree.root_node, "local_variable_declaration"):
        for declarator in find_nodes(decl, "variable_declarator"):
            name_node = declarator.child_by_field_name("name")
            if name_node is None or name_node.text != var_bytes:
                continue
            # Determine collection type from the type annotation
            type_node = decl.child_by_field_name("type")
            collection_type = "List"
            if type_node is not None and type_node.text:
                type_text = type_node.text.decode("utf-8")
                if type_text.startswith("Set"):
                    collection_type = "Set"
                elif type_text.startswith("Map"):
                    collection_type = "Map"
            return collection_type, decl
    return "List", None


def fix_frozen_mutation(
    uri: str,
    source: str,
    diag_range: lsp.Range,
    config: dict[str, Any],
    *,
    tree: Tree | None = None,
    lines: list[str] | None = None,
) -> lsp.WorkspaceEdit | None:
    """Generate a fix for frozen-mutation: rewrite to Vavr persistent collection.

    Rewrites the variable declaration from List.of(...) to io.vavr.collection.List.of(...)
    and the mutation call (e.g. .add(x)) to the persistent equivalent (e.g. = var.append(x)).

    Pass ``lines`` to avoid re-splitting the source when called from a context that has
    already split it (e.g. ``on_code_action`` in server.py).
    """
    if lines is None:
        lines = source.split("\n")
    if tree is None:
        tree = get_parser().parse(source.encode("utf-8"))

    edits: list[lsp.TextEdit] = []

    invocation = _find_method_invocation(tree, diag_range)
    if invocation is None:
        return None

    method_name = invocation.child_by_field_name("name")
    obj_node = invocation.child_by_field_name("object")
    if method_name is None or obj_node is None:
        return None

    var_name = obj_node.text.decode("utf-8") if obj_node.text else ""

    # Single pass: detect collection type AND locate the declaration node
    collection_type, decl_node = _find_frozen_decl_info(tree, var_name)
    if config.get("autoImportVavr", True):
        import_edit = ensure_import(lines, f"io.vavr.collection.{collection_type}")
        if import_edit is not None:
            edits.append(import_edit)

    args_node = invocation.child_by_field_name("arguments")
    args_text = args_node.text.decode("utf-8") if args_node and args_node.text else "()"

    # Map mutation method to Vavr persistent equivalent
    mutation_map = {
        b"add": "append",
        b"addAll": "appendAll",
        b"remove": "remove",
        b"sort": "sorted",
        b"set": "update",
    }
    vavr_method = mutation_map.get(method_name.text, "append") if method_name.text else "append"

    # Find the expression_statement containing this invocation to replace the whole statement
    stmt = invocation.parent
    if stmt is not None and stmt.type == "expression_statement":
        stmt_start = lsp.Position(line=stmt.start_point[0], character=stmt.start_point[1])
        stmt_end = lsp.Position(line=stmt.end_point[0], character=stmt.end_point[1])
        new_text = f"{var_name} = {var_name}.{vavr_method}{args_text};"
        edits.append(lsp.TextEdit(range=lsp.Range(start=stmt_start, end=stmt_end), new_text=new_text))

    # Rewrite the declaration using the already-found decl_node (no second tree walk)
    if decl_node is not None:
        _add_vavr_decl_rewrite_from_node(decl_node, var_name, edits, collection_type)

    if not edits:
        return None

    return lsp.WorkspaceEdit(changes={uri: edits})


def _add_vavr_decl_rewrite_from_node(
    decl: Node, var_name: str, edits: list[lsp.TextEdit], collection_type: str = "List"
) -> None:
    """Rewrite a variable declaration node's type + initializer to Vavr.

    Accepts a pre-located decl node so the caller avoids a redundant tree walk.
    """
    var_bytes = var_name.encode("utf-8")
    for declarator in find_nodes(decl, "variable_declarator"):
        name_node = declarator.child_by_field_name("name")
        if name_node is None or name_node.text != var_bytes:
            continue
        value_node = declarator.child_by_field_name("value")
        if value_node is None:
            continue
        # Extract the arguments from the factory call
        init_args = ""
        if value_node.type == "method_invocation":
            args_node = value_node.child_by_field_name("arguments")
            if args_node and args_node.text:
                init_args = args_node.text.decode("utf-8")

        # Rewrite the entire declaration
        decl_start = lsp.Position(line=decl.start_point[0], character=decl.start_point[1])
        decl_end = lsp.Position(line=decl.end_point[0], character=decl.end_point[1])

        # Try to extract the generic type from the original declaration
        type_node = decl.child_by_field_name("type")
        generic = ""
        if type_node is not None:
            for child in find_nodes(type_node, "type_arguments"):
                if child.text:
                    generic = child.text.decode("utf-8")
                    break

        vavr_type = f"io.vavr.collection.{collection_type}"
        new_decl = f"{vavr_type}{generic} {var_name} = {vavr_type}.of{init_args};"
        edits.append(lsp.TextEdit(range=lsp.Range(start=decl_start, end=decl_end), new_text=new_decl))
        return


def _find_if_node(tree: Tree, diag_range: lsp.Range) -> Node | None:
    """Find the if_statement node at the given diagnostic range."""
    target = tree.root_node.descendant_for_point_range(
        (diag_range.start.line, diag_range.start.character),
        (diag_range.end.line, diag_range.end.character),
    )
    if target is None:
        return None
    if target.type == "if_statement":
        return target
    return find_ancestor(target, "if_statement")


def _extract_null_check_var_str(if_node: Node) -> str | None:
    """Extract the variable name from an if(x != null) condition as a string."""
    condition = if_node.child_by_field_name("condition")
    if condition is None:
        return None
    var_bytes = extract_null_check_var(condition)
    if var_bytes is None:
        return None
    return var_bytes.decode("utf-8")


_EAGER_NODE_TYPES: frozenset[str] = frozenset(
    {
        "string_literal",
        "number_literal",
        "decimal_integer_literal",
        "hex_integer_literal",
        "octal_integer_literal",
        "binary_integer_literal",
        "decimal_floating_point_literal",
        "boolean_literal",
        "character_literal",
        "null_literal",
        "identifier",
        "field_access",
    }
)


def _is_eager(node: Node) -> bool:
    """Return True if the node is a literal or simple identifier (safe to use eagerly in getOrElse)."""
    return node.type in _EAGER_NODE_TYPES


def _else_terminal(alternative: Node) -> str | None:
    """Derive the terminal suffix (.getOrElse / '') from an else branch node.

    Returns:
        ``""``  — else returns null (bare ``Option.of(x)`` is the right result).
        ``".getOrElse(...)"`` — else returns a simple/eager value.
        ``".getOrElse(() -> ...)"`` — else returns a lazy expression.
        ``None`` — complex else that cannot be rewritten.
    """
    if alternative.type == "return_statement":
        else_stmts = [alternative]
    else:
        else_stmts = [c for c in alternative.named_children if c.type not in ("line_comment", "block_comment")]

    if len(else_stmts) != 1 or else_stmts[0].type != "return_statement":
        return None  # complex else

    val_children = list(else_stmts[0].named_children)
    if not val_children:
        return None

    val_node = val_children[0]
    val_text = val_node.text.decode("utf-8") if val_node.text else ""
    if val_node.type == "null_literal":
        return ""
    if _is_eager(val_node):
        return f".getOrElse({val_text})"
    return f".getOrElse(() -> {val_text})"


def _build_monadic_rewrite(if_node: Node, var_name: str) -> lsp.TextEdit | None:
    """Build the Option.of().map() replacement TextEdit for a null-check if-block.

    Handles four cases:
    - Identity return (return x) + null fallback → ``return Option.of(x);``
    - Non-identity return + null fallback → ``return Option.of(x)\\n.map(it -> ...);``
    - Identity return + else value → ``return Option.of(x).getOrElse(val);``
    - Non-identity return + else value → ``return Option.of(x)\\n.map(it -> ...).getOrElse(val);``
    """
    consequence = if_node.child_by_field_name("consequence")
    if consequence is None:
        return None

    stmts = [c for c in consequence.named_children if c.type not in ("line_comment", "block_comment")]
    if len(stmts) != 1 or stmts[0].type != "return_statement":
        return None

    expr_children = list(stmts[0].named_children)
    if not expr_children:
        return None

    return_text = expr_children[0].text.decode("utf-8") if expr_children[0].text else ""
    is_identity = return_text == var_name

    replace_start = lsp.Position(line=if_node.start_point[0], character=if_node.start_point[1])
    replace_end = lsp.Position(line=if_node.end_point[0], character=if_node.end_point[1])

    indent = " " * if_node.start_point[1]
    # Align the chained call to the opening parenthesis of Option.of(...)
    align = " " * (len("return Option.of(") + len(var_name) + 1)

    # --- Determine terminal suffix and (possibly) extend the replace range ---
    alternative = if_node.child_by_field_name("alternative")
    if alternative is None:
        # No else branch — look for an immediately following `return null;`
        next_sib = if_node.next_named_sibling
        is_null_return = (
            next_sib is not None
            and next_sib.type == "return_statement"
            and any(c.type == "null_literal" for c in next_sib.named_children)
        )
        if not is_null_return or next_sib is None:
            return None
        replace_end = lsp.Position(line=next_sib.end_point[0], character=next_sib.end_point[1])
        terminal = ""  # bare Option — no .getOrNull()
    else:
        else_result = _else_terminal(alternative)
        if else_result is None:
            return None  # complex else
        terminal = else_result

    # --- Assemble new_text ---
    base = f"Option.of({var_name})"
    if is_identity:
        new_text = f"return {base}{terminal};"
    else:
        map_expr = _to_map_expression(return_text, var_name)
        new_text = f"return {base}\n{indent}{align}{map_expr}{terminal};"

    return lsp.TextEdit(range=lsp.Range(start=replace_start, end=replace_end), new_text=new_text)


def fix_null_check_to_monadic(
    uri: str,
    source: str,
    diag_range: lsp.Range,
    config: dict[str, Any],
    *,
    tree: Tree | None = None,
    lines: list[str] | None = None,
) -> lsp.WorkspaceEdit | None:
    """Generate a fix for null-check-to-monadic: rewrite if(x != null) to Option.of(x).map(...).

    Pass ``lines`` to avoid re-splitting the source when called from a context that has
    already split it (e.g. ``on_code_action`` in server.py).
    """
    if lines is None:
        lines = source.split("\n")
    if tree is None:
        tree = get_parser().parse(source.encode("utf-8"))

    edits: list[lsp.TextEdit] = []

    auto_import = config.get("autoImportVavr", True)
    if auto_import:
        import_edit = ensure_import(lines, "io.vavr.control.Option")
        if import_edit is not None:
            edits.append(import_edit)

    if_node = _find_if_node(tree, diag_range)
    if if_node is None:
        return None

    var_name = _extract_null_check_var_str(if_node)
    if var_name is None:
        return None

    rewrite_edit = _build_monadic_rewrite(if_node, var_name)
    if rewrite_edit is None:
        return None

    edits.append(rewrite_edit)
    return lsp.WorkspaceEdit(changes={uri: edits})


def _to_map_expression(return_text: str, var_name: str) -> str:
    """Convert a return expression into a .map() lambda call.

    Uses ``it`` as the lambda parameter to avoid shadowing the outer Java variable.
    E.g. 'user.getName()' with var='user' -> '.map(it -> it.getName())'
    """
    # Use 'it' as lambda param to avoid shadowing the outer variable in Java.
    # Word-boundary replace to avoid mangling substrings (e.g. var 's' in 's.toString()').
    lambda_body = re.sub(rf"\b{re.escape(var_name)}\b", "it", return_text)
    return f".map(it -> {lambda_body})"


def fix_null_return(
    uri: str,
    source: str,
    diag_range: lsp.Range,
    config: dict[str, Any],
    *,
    tree: Tree | None = None,  # Unused but kept for uniform call signature with other fix generators
    lines: list[str] | None = None,
) -> lsp.WorkspaceEdit | None:
    """Generate a fix for null-return: replace 'return null;' with 'return Option.none();'.

    The ``tree`` parameter is unused here but kept to maintain a uniform call signature
    with the other fix generators (server.py calls all generators with ``tree=tree``).

    Pass ``lines`` to avoid re-splitting the source when called from a context that has
    already split it (e.g. ``on_code_action`` in server.py).
    """
    if lines is None:
        lines = source.split("\n")
    edits: list[lsp.TextEdit] = []

    auto_import = config.get("autoImportVavr", True)
    if auto_import:
        import_edit = ensure_import(lines, "io.vavr.control.Option")
        if import_edit is not None:
            edits.append(import_edit)

    # Replace return null with return Option.none()
    edits.append(
        lsp.TextEdit(
            range=lsp.Range(
                start=lsp.Position(line=diag_range.start.line, character=diag_range.start.character),
                end=lsp.Position(line=diag_range.end.line, character=diag_range.end.character),
            ),
            new_text="Option.none()",
        )
    )

    return lsp.WorkspaceEdit(changes={uri: edits})


# --- Fix registry ---

_FIX_REGISTRY: dict[str, FixGenerator] = {
    "frozen-mutation": fix_frozen_mutation,
    "null-check-to-monadic": fix_null_check_to_monadic,
    "null-return": fix_null_return,
}


def get_fix(rule_id: str) -> FixGenerator | None:
    """Look up a fix generator by rule ID."""
    return _FIX_REGISTRY.get(rule_id)


def get_fix_registry_keys() -> set[str]:
    """Return the set of rule IDs that have registered fix generators."""
    return set(_FIX_REGISTRY.keys())
