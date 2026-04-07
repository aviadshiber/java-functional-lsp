"""Quick fix generators for functional refactoring code actions.

Each fix generator takes document context and returns a list of TextEdits.
"""

from __future__ import annotations

from typing import Any

from lsprotocol import types as lsp

from .analyzers.base import find_nodes, get_parser

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
        return package_line + 2  # blank line after package
    return 0


def _has_import(lines: list[str], import_path: str) -> bool:
    """Check if an import statement already exists."""
    target = f"import {import_path};"
    return any(line.strip() == target for line in lines)


def ensure_import(lines: list[str], import_path: str) -> lsp.TextEdit | None:
    """Return a TextEdit adding the import if it doesn't exist, or None."""
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


def fix_frozen_mutation(
    uri: str, source: str, diag_range: lsp.Range, config: dict[str, Any]
) -> lsp.WorkspaceEdit | None:
    """Generate a fix for frozen-mutation: rewrite to Vavr persistent collection.

    Rewrites the variable declaration from List.of(...) to io.vavr.collection.List.of(...)
    and the mutation call (e.g. .add(x)) to the persistent equivalent (e.g. = var.append(x)).
    """
    lines = source.split("\n")
    parser = get_parser()
    tree = parser.parse(source.encode("utf-8"))

    edits: list[lsp.TextEdit] = []

    # Add Vavr import
    auto_import = config.get("autoImportVavr", True)
    if auto_import:
        import_edit = ensure_import(lines, "io.vavr.collection.List")
        if import_edit is not None:
            edits.append(import_edit)

    # Find the mutation call at the diagnostic range
    diag_line = diag_range.start.line
    diag_col = diag_range.start.character

    target_node = tree.root_node.descendant_for_point_range(
        (diag_line, diag_col), (diag_range.end.line, diag_range.end.character)
    )
    if target_node is None:
        return None

    # Find the method_invocation at or near the diagnostic position
    invocation = None
    if target_node.type == "method_invocation":
        invocation = target_node
    else:
        # Search children first (diagnostic range may cover the expression_statement)
        for child in find_nodes(target_node, "method_invocation"):
            invocation = child
            break
        # Then try walking up
        if invocation is None:
            node = target_node.parent
            while node is not None and node.type != "method_invocation":
                node = node.parent
            invocation = node
    if invocation is None:
        return None

    method_name = invocation.child_by_field_name("name")
    obj_node = invocation.child_by_field_name("object")
    if method_name is None or obj_node is None:
        return None

    var_name = obj_node.text.decode("utf-8") if obj_node.text else ""
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
    vavr_method = mutation_map.get(method_name.text, "append")

    # Find the expression_statement containing this invocation to replace the whole statement
    stmt = invocation.parent
    if stmt is not None and stmt.type == "expression_statement":
        stmt_start = lsp.Position(line=stmt.start_point[0], character=stmt.start_point[1])
        stmt_end = lsp.Position(line=stmt.end_point[0], character=stmt.end_point[1])
        new_text = f"{var_name} = {var_name}.{vavr_method}{args_text};"
        edits.append(lsp.TextEdit(range=lsp.Range(start=stmt_start, end=stmt_end), new_text=new_text))

    # Try to find and rewrite the variable declaration to use Vavr type
    _add_vavr_decl_rewrite(tree, var_name, edits)

    if not edits:
        return None

    return lsp.WorkspaceEdit(changes={uri: edits})


def _add_vavr_decl_rewrite(tree: Any, var_name: str, edits: list[lsp.TextEdit]) -> None:
    """Find the variable declaration for var_name and rewrite its type + init to Vavr."""
    var_bytes = var_name.encode("utf-8")
    for decl in find_nodes(tree.root_node, "local_variable_declaration"):
        for declarator in find_nodes(decl, "variable_declarator"):
            name_node = declarator.child_by_field_name("name")
            if name_node is None or name_node.text != var_bytes:
                continue
            # Rewrite the whole declaration
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

            new_decl = f"io.vavr.collection.List{generic} {var_name} = io.vavr.collection.List.of{init_args};"
            edits.append(lsp.TextEdit(range=lsp.Range(start=decl_start, end=decl_end), new_text=new_decl))
            return


def _find_if_node(tree: Any, diag_range: lsp.Range) -> Any | None:
    """Find the if_statement node at the given diagnostic range."""
    target = tree.root_node.descendant_for_point_range(
        (diag_range.start.line, diag_range.start.character),
        (diag_range.end.line, diag_range.end.character),
    )
    node = target
    while node is not None and node.type != "if_statement":
        node = node.parent
    return node


def _extract_null_check_var(if_node: Any) -> str | None:
    """Extract the variable name from an if(x != null) condition."""
    condition = if_node.child_by_field_name("condition")
    if condition is None:
        return None

    inner = condition
    if inner.type == "parenthesized_expression" and inner.named_child_count == 1:
        inner = inner.named_children[0]

    if inner.type != "binary_expression":
        return None

    # Must have != operator
    if not any(c.type == "!=" for c in inner.children):
        return None

    left = inner.child_by_field_name("left")
    right = inner.child_by_field_name("right")
    if left is None or right is None:
        return None

    # Determine which side is the variable and which is null
    var_node = left if right.type == "null_literal" else (right if left.type == "null_literal" else None)
    if var_node is not None and var_node.type == "identifier" and var_node.text:
        result: str = var_node.text.decode("utf-8")
        return result
    return None


def _build_monadic_rewrite(if_node: Any, var_name: str) -> lsp.TextEdit | None:
    """Build the Option.of().map() replacement TextEdit for a null-check if-block."""
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
    map_expr = _to_map_expression(return_text, var_name)

    replace_start = lsp.Position(line=if_node.start_point[0], character=if_node.start_point[1])
    replace_end = lsp.Position(line=if_node.end_point[0], character=if_node.end_point[1])

    # Extend range to include following `return null;`
    next_sib = if_node.next_named_sibling
    if next_sib is not None and next_sib.type == "return_statement":
        if any(c.type == "null_literal" for c in next_sib.named_children):
            replace_end = lsp.Position(line=next_sib.end_point[0], character=next_sib.end_point[1])

    indent = " " * if_node.start_point[1]
    new_text = f"return Option.of({var_name})\n{indent}             {map_expr}.getOrNull();"
    return lsp.TextEdit(range=lsp.Range(start=replace_start, end=replace_end), new_text=new_text)


def fix_null_check_to_monadic(
    uri: str, source: str, diag_range: lsp.Range, config: dict[str, Any]
) -> lsp.WorkspaceEdit | None:
    """Generate a fix for null-check-to-monadic: rewrite if(x != null) to Option.of(x).map(...)."""
    lines = source.split("\n")
    parser = get_parser()
    tree = parser.parse(source.encode("utf-8"))

    edits: list[lsp.TextEdit] = []

    auto_import = config.get("autoImportVavr", True)
    if auto_import:
        import_edit = ensure_import(lines, "io.vavr.control.Option")
        if import_edit is not None:
            edits.append(import_edit)

    if_node = _find_if_node(tree, diag_range)
    if if_node is None:
        return None

    var_name = _extract_null_check_var(if_node)
    if var_name is None:
        return None

    rewrite_edit = _build_monadic_rewrite(if_node, var_name)
    if rewrite_edit is None:
        return None

    edits.append(rewrite_edit)
    return lsp.WorkspaceEdit(changes={uri: edits})


def _to_map_expression(return_text: str, var_name: str) -> str:
    """Convert a return expression into a .map() call.

    E.g. 'user.getName()' with var='user' -> '.map(User::getName)'
    Falls back to lambda if the expression is complex.
    """
    # Simple case: var.method()
    if return_text.startswith(f"{var_name}.") and return_text.endswith("()"):
        method = return_text[len(var_name) + 1 : -2]
        if method.isidentifier():
            # Use method reference style — capitalize var name as type guess
            type_name = var_name[0].upper() + var_name[1:] if var_name else var_name
            return f".map({type_name}::{method})"

    # Fallback: lambda expression
    return f".map({var_name} -> {return_text})"


def fix_null_return(uri: str, source: str, diag_range: lsp.Range, config: dict[str, Any]) -> lsp.WorkspaceEdit | None:
    """Generate a fix for null-return: replace 'return null;' with 'return Option.none();'."""
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

_FIX_REGISTRY: dict[str, Any] = {
    "frozen-mutation": fix_frozen_mutation,
    "null-check-to-monadic": fix_null_check_to_monadic,
    "null-return": fix_null_return,
}


def get_fix(rule_id: str) -> Any | None:
    """Look up a fix generator by rule ID."""
    return _FIX_REGISTRY.get(rule_id)
