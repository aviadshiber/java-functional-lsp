"""Quick fix generators for functional refactoring code actions.

Each fix generator takes document context and returns a list of TextEdits.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from lsprotocol import types as lsp
from tree_sitter import Node, Tree

from .analyzers.base import (
    IGNORED_CHILDREN,
    extract_null_check_var,
    find_ancestor,
    find_nodes,
    get_parser,
    has_error_or_missing,
    references_var,
)
from .analyzers.functional_checker import is_side_effect_invocation

logger = logging.getLogger(__name__)

_MAX_CHAIN_DEPTH = 10
_COMPLEX_EXPR_TYPES: frozenset[str] = frozenset({"ternary_expression", "lambda_expression", "assignment_expression"})

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
                if any(kw in type_text for kw in ("SortedMap", "Map")):
                    collection_type = "Map"
                elif any(kw in type_text for kw in ("Multiset", "SortedSet", "Set")):
                    collection_type = "Set"
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


def _safe_expr_text(node: Node) -> str:
    """Extract expression text, parenthesizing complex expressions to avoid syntax issues."""
    text = node.text.decode("utf-8") if node.text else ""
    if node.type in _COMPLEX_EXPR_TYPES:
        return f"({text})"
    return text


def _is_identity_return(consequence: Node | None, var_name: str) -> bool:
    """Return True if consequence block has exactly one return_statement returning var_name."""
    if consequence is None:
        return False
    stmts = [c for c in consequence.named_children if c.type not in ("line_comment", "block_comment")]
    if len(stmts) != 1 or stmts[0].type != "return_statement":
        return False
    expr_children = list(stmts[0].named_children)
    if not expr_children:
        return False
    ret_node = expr_children[0]
    return ret_node.type == "identifier" and ret_node.text == var_name.encode("utf-8")


def _collect_chained_fallbacks(alternative: Node, var_name: str) -> list[str] | None:
    """Collect fallback expressions from chained identity null-checks.

    Iteratively walks nested else blocks. Each level must have exactly:
    1. expression_statement with assignment_expression (left == var_name)
    2. if_statement with null-check on var_name and identity return

    Returns list of fallback expression texts, or None if pattern doesn't match.
    """
    fallbacks: list[str] = []
    current_alt = alternative
    var_bytes = var_name.encode("utf-8")

    for _ in range(_MAX_CHAIN_DEPTH):
        stmts = [c for c in current_alt.named_children if c.type not in ("line_comment", "block_comment")]
        if len(stmts) != 2:  # noqa: PLR2004
            return None

        assign_stmt, nested_if = stmts[0], stmts[1]

        # Validate assignment statement
        if assign_stmt.type != "expression_statement":
            return None
        assign = next(
            (c for c in assign_stmt.named_children if c.type == "assignment_expression"),
            None,
        )
        if assign is None:
            return None
        left = assign.child_by_field_name("left")
        right = assign.child_by_field_name("right")
        if left is None or right is None or left.text != var_bytes:
            return None

        # Validate nested if_statement
        if nested_if.type != "if_statement":
            return None
        condition = nested_if.child_by_field_name("condition")
        if condition is None or extract_null_check_var(condition) != var_bytes:
            return None

        # Check identity return in consequence
        consequence = nested_if.child_by_field_name("consequence")
        if not _is_identity_return(consequence, var_name):
            return None

        # Extract fallback expression text, parenthesize if complex
        fallback_text = _safe_expr_text(right)
        fallbacks.append(fallback_text)

        # Continue to next level if there is a nested alternative
        next_alt = nested_if.child_by_field_name("alternative")
        if next_alt is None:
            return fallbacks
        current_alt = next_alt

    return None  # exceeded max depth


def _try_chained_null_rewrite(if_node: Node, var_name: str) -> lsp.TextEdit | None:
    """Attempt to rewrite a chained identity null-check pattern to Option.of().orElse().getOrElse().

    The pattern is:
        T var = expr1;
        if (var != null) { return var; }
        else {
            var = expr2;
            if (var != null) { return var; }
            [else { var = expr3; if (var != null) { return var; } }]
        }
        return default;

    Returns a TextEdit replacing the variable declaration through the final return, or None.
    """
    # 1. Verify consequence is identity return
    consequence = if_node.child_by_field_name("consequence")
    if not _is_identity_return(consequence, var_name):
        return None

    # 2. Require an else branch
    alternative = if_node.child_by_field_name("alternative")
    if alternative is None:
        return None

    # 3. prev_named_sibling must be local_variable_declaration containing var_name
    prev_sib = if_node.prev_named_sibling
    if prev_sib is None or prev_sib.type != "local_variable_declaration":
        return None

    # Extract initializer from the declarator
    var_bytes = var_name.encode("utf-8")
    initial_expr: str | None = None
    for declarator in prev_sib.children:
        if declarator.type != "variable_declarator":
            continue
        name_node = declarator.child_by_field_name("name")
        if name_node is None or name_node.text != var_bytes:
            continue
        value_node = declarator.child_by_field_name("value")
        if value_node is None:
            return None
        initial_expr = _safe_expr_text(value_node)
        break

    if initial_expr is None:
        return None

    # 4. Collect chained fallbacks from the else branch
    fallbacks = _collect_chained_fallbacks(alternative, var_name)
    if fallbacks is None:
        return None

    # 5. next_named_sibling must be return_statement with non-null value
    next_sib = if_node.next_named_sibling
    if next_sib is None or next_sib.type != "return_statement":
        return None

    ret_children = list(next_sib.named_children)
    if not ret_children:
        return None

    default_node = ret_children[0]
    # Null default → changing return type is unsafe, no code action
    if default_node.type == "null_literal":
        return None

    default_text = default_node.text.decode("utf-8") if default_node.text else ""
    if _is_eager(default_node):
        terminal = f".getOrElse({default_text})"
    else:
        terminal = f".getOrElse(() -> {default_text})"

    # 6. Build replacement text
    indent = " " * prev_sib.start_point[1]
    align = " " * len("return ")
    lines_out: list[str] = [f"return Option.of({initial_expr})"]
    for fb in fallbacks:
        lines_out.append(f"{indent}{align}.orElse(() -> Option.of({fb}))")
    lines_out[-1] += terminal + ";"

    # Join all but the last with newline (last has the semicolon already)
    new_text = "\n".join(lines_out)

    # 7. Replace range from variable declaration start through final return end
    replace_start = lsp.Position(line=prev_sib.start_point[0], character=prev_sib.start_point[1])
    replace_end = lsp.Position(line=next_sib.end_point[0], character=next_sib.end_point[1])

    logger.debug(
        "Chained null-check rewrite: %d fallbacks, lines %d-%d",
        len(fallbacks),
        prev_sib.start_point[0],
        next_sib.end_point[0],
    )

    return lsp.TextEdit(range=lsp.Range(start=replace_start, end=replace_end), new_text=new_text)


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
        if else_result is not None:
            terminal = else_result
        else:
            # Try chained identity null-check pattern
            if is_identity:
                chained = _try_chained_null_rewrite(if_node, var_name)
                if chained is not None:
                    return chained
            return None  # complex else, diagnostic only

    # --- Assemble new_text ---
    base = f"Option.of({var_name})"
    if is_identity:
        new_text = f"return {base}{terminal};"
    else:
        # Align the chained call to the opening parenthesis of Option.of(...)
        align = " " * (len("return Option.of(") + len(var_name) + 1)
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
    # Precompile with word-boundary anchors to avoid mangling substrings
    # (e.g. var 's' in 's.toString()').
    pattern = re.compile(rf"\b{re.escape(var_name)}\b")
    lambda_body = pattern.sub("it", return_text)
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


# --- try-catch-to-monadic ---


def _find_try_node(tree: Tree, diag_range: lsp.Range) -> Node | None:
    """Find the try_statement node at the given diagnostic range.

    The diagnostic range typically points at the ``try`` keyword (narrow range
    emitted by the analyzer), but editors may send a broader range that lands
    on a descendant token. ``descendant_for_point_range`` returns the deepest
    node containing the range; if that node is not the ``try_statement`` itself
    we walk up via ``find_ancestor``. Unlike ``_find_method_invocation``, we do
    not search children because the ``try_statement`` is always an ancestor of
    any token inside a try/catch block.
    """
    target = tree.root_node.descendant_for_point_range(
        (diag_range.start.line, diag_range.start.character),
        (diag_range.end.line, diag_range.end.character),
    )
    if target is None:
        return None
    if target.type == "try_statement":
        return target
    return find_ancestor(target, "try_statement")


def _first_method_invocation(stmt: Node) -> Node | None:
    """Return the first ``method_invocation`` descendant of ``stmt``, or None.

    Used by Pattern 2 logging detection to locate the side-effect call inside
    a catch-body prior statement. For a simple statement like
    ``logger.warn("msg", e);`` this returns the ``logger.warn`` invocation.

    Limitation: if the outer expression is a non-logging call wrapping a
    logging call (e.g. ``cache.put(k, logger.warn(...))``), this returns the
    outer call and the caller's ``is_side_effect_invocation`` check rejects
    it — which is the safe behaviour (no fix rather than incorrect fix).
    """
    for inv in find_nodes(stmt, "method_invocation"):
        return inv
    return None


def _validate_and_extract_try_catch_parts(
    try_node: Node,
) -> tuple[Node, str, str, list[Node], Node] | None:
    """Validate try/catch shape and extract parts needed to build the rewrite.

    Returns ``(try_return_expr, catch_type_text, exc_var_name, prior_stmts, catch_return_expr)``
    or ``None`` if the shape doesn't match.

    This function re-validates the same shape as
    ``exception_checker._matches_try_catch_monadic_shape`` as a defense in depth:
    the fix generator must be robust when called directly (e.g. from tests, or
    when the diagnostic is stale). Both functions must stay in sync — any shape
    rejected by one must also be rejected by the other.

    Requirements (must mirror the analyzer):
    - No resource_specification (try-with-resources)
    - No finally_clause
    - Exactly one catch_clause
    - Catch type must not be a union (``A | B``)
    - Try body is a block with a single return_statement that has an expression
    - Catch body ends with a return_statement with an expression; prior stmts are expression_statements
    """
    # Single pass over try_node.children
    has_finally = False
    has_resources = False
    catches: list[Node] = []
    for c in try_node.children:
        if c.type == "finally_clause":
            has_finally = True
        elif c.type == "resource_specification":
            has_resources = True
        elif c.type == "catch_clause":
            catches.append(c)

    if has_finally or has_resources:
        return None
    if len(catches) != 1:
        return None
    catch = catches[0]

    # Try body must be a block with a single return_statement with an expression
    body = try_node.child_by_field_name("body")
    if body is None or body.type != "block":
        return None
    try_stmts = [c for c in body.named_children if c.type not in IGNORED_CHILDREN]
    if len(try_stmts) != 1 or try_stmts[0].type != "return_statement":
        return None
    try_ret_children = [c for c in try_stmts[0].named_children if c.type not in IGNORED_CHILDREN]
    if not try_ret_children:
        return None
    try_return_expr = try_ret_children[0]

    # Extract catch parameter: type + name
    param = next((c for c in catch.children if c.type == "catch_formal_parameter"), None)
    if param is None:
        return None
    catch_type_node = next((c for c in param.children if c.type == "catch_type"), None)
    # The name is an identifier child of the catch_formal_parameter
    exc_name_node = next((c for c in param.children if c.type == "identifier"), None)
    if catch_type_node is None or exc_name_node is None:
        return None
    catch_type_text = catch_type_node.text.decode("utf-8") if catch_type_node.text else ""
    exc_var_name = exc_name_node.text.decode("utf-8") if exc_name_node.text else ""
    if not catch_type_text or not exc_var_name:
        return None
    # Reject union types (multi-catch): "A | B"
    if "|" in catch_type_text:
        return None

    # Catch body: 0+ expression_statements then return_statement
    catch_body = catch.child_by_field_name("body")
    if catch_body is None or catch_body.type != "block":
        return None
    catch_stmts = [c for c in catch_body.named_children if c.type not in IGNORED_CHILDREN]
    if not catch_stmts or catch_stmts[-1].type != "return_statement":
        return None
    prior_stmts = catch_stmts[:-1]
    if any(s.type != "expression_statement" for s in prior_stmts):
        return None
    ret_children = [c for c in catch_stmts[-1].named_children if c.type not in IGNORED_CHILDREN]
    if not ret_children:
        return None
    catch_return_expr = ret_children[0]

    return (try_return_expr, catch_type_text, exc_var_name, prior_stmts, catch_return_expr)


def _strip_trailing_semicolon(text: str) -> str:
    """Strip exactly one trailing ';' from a statement's text (not all trailing semicolons).

    ``str.rstrip(';')`` treats its argument as a **character set**, so it would
    greedily strip any number of trailing semicolons. For a well-formed
    ``expression_statement``, the text ends in exactly one ``;``. Use
    ``removesuffix`` to strip at most one, preserving any semantically
    meaningful content.
    """
    stripped = text.rstrip()
    return stripped.removesuffix(";").rstrip()


def fix_try_catch_to_monadic(
    uri: str,
    source: str,
    diag_range: lsp.Range,
    config: dict[str, Any],
    *,
    tree: Tree | None = None,
    lines: list[str] | None = None,
) -> lsp.WorkspaceEdit | None:
    """Generate a fix for try-catch-to-monadic: rewrite try/catch to a Vavr ``Try.of()`` chain.

    Supports three patterns:

    - **Pattern 1** — simple catch with default:
      ``try { return expr; } catch (E e) { return default; }``
      → ``return Try.of(() -> expr).getOrElse(default);``
      (eager vs lazy ``.getOrElse(...)`` via ``_is_eager``)

    - **Pattern 2** — logging + default:
      ``try { return expr; } catch (E e) { log(e); return default; }``
      → ``return Try.of(() -> expr).onFailure(e -> log(e)).getOrElse(default);``
      (prior statement must be a recognized side-effect invocation; otherwise
      no fix is produced)

    - **Pattern 3** — exception-dependent recovery:
      ``try { return expr; } catch (E e) { return f(e); }``
      → ``return Try.of(() -> expr).recover(E.class, e -> f(e)).get();``

    **Semantic limitation of Pattern 3:** ``.recover(E.class, ...).get()``
    re-throws any failure whose type is NOT ``E``. The original Java code
    would propagate non-E exceptions through the catch unchanged — for
    checked exceptions this is identical, but if ``risky()`` also throws
    unchecked exceptions beyond ``E``, the original code would propagate
    them as-is while the rewrite wraps them through Vavr's exception
    handling machinery. In practice the behaviour is equivalent for
    well-formed Java code where the catch type matches the method's
    declared throws list.

    **Rejected combinations:** Pattern 2 + Pattern 3 (logging + exception-
    dependent recovery) is explicitly rejected rather than silently emitting
    a hybrid ``.onFailure(...).recover(...)`` chain, since that combination
    is neither tested nor documented.

    Pass ``lines`` to avoid re-splitting the source when called from a context
    that has already split it (e.g. ``on_code_action`` in server.py).
    """
    if lines is None:
        lines = source.split("\n")
    if tree is None:
        tree = get_parser().parse(source.encode("utf-8"))

    try_node = _find_try_node(tree, diag_range)
    if try_node is None:
        logger.debug("try-catch-to-monadic: no try_statement found at range %s", diag_range)
        return None

    # Defensive gate: refuse to rewrite subtrees with ERROR or MISSING nodes.
    # Editors send partial trees during incremental typing; emitting edits for
    # those would produce invalid Java.
    if has_error_or_missing(try_node):
        logger.debug("try-catch-to-monadic: try_statement contains ERROR/MISSING nodes, skipping")
        return None

    parts = _validate_and_extract_try_catch_parts(try_node)
    if parts is None:
        logger.debug("try-catch-to-monadic: shape validation failed")
        return None
    try_return_expr, catch_type_text, exc_var_name, prior_stmts, catch_return_expr = parts

    try_body_text = try_return_expr.text.decode("utf-8") if try_return_expr.text else ""
    catch_expr_text = catch_return_expr.text.decode("utf-8") if catch_return_expr.text else ""
    if not try_body_text or not catch_expr_text:
        logger.debug("try-catch-to-monadic: empty try/catch expression text")
        return None

    uses_exc = references_var(catch_return_expr, exc_var_name.encode("utf-8"))

    # Pattern 2 gating: only allow a single prior statement that is a recognized
    # side-effect call. Also reject Pattern 2 + Pattern 3 hybrid explicitly.
    on_failure: str | None = None
    if len(prior_stmts) > 1:
        logger.debug("try-catch-to-monadic: too many prior statements (%d)", len(prior_stmts))
        return None
    if len(prior_stmts) == 1:
        if uses_exc:
            # Pattern 2 + Pattern 3 hybrid is out of scope for v1.
            logger.debug("try-catch-to-monadic: Pattern 2+3 hybrid rejected")
            return None
        prior = prior_stmts[0]
        invocation = _first_method_invocation(prior)
        if invocation is None or not is_side_effect_invocation(invocation):
            logger.debug("try-catch-to-monadic: prior statement is not a recognized side-effect call")
            return None
        prior_text = prior.text.decode("utf-8") if prior.text else ""
        stmt_text = _strip_trailing_semicolon(prior_text)
        on_failure = f".onFailure({exc_var_name} -> {stmt_text})"

    # Build Try.of(() -> <try_return_expr>) [+ .onFailure(...)]
    chain = f"Try.of(() -> {try_body_text})"
    if on_failure is not None:
        chain += on_failure

    # Pattern 3 vs Pattern 1: does the catch-return expression reference the exception var?
    if uses_exc:
        # Pattern 3: .recover(Type.class, e -> expr).get()
        chain += f".recover({catch_type_text}.class, {exc_var_name} -> {catch_expr_text}).get()"
    elif _is_eager(catch_return_expr):
        # Pattern 1 (eager): literal/identifier default
        chain += f".getOrElse({catch_expr_text})"
    else:
        # Pattern 1 (lazy): method call or complex expression
        chain += f".getOrElse(() -> {catch_expr_text})"

    # Build edits
    edits: list[lsp.TextEdit] = []
    if config.get("autoImportVavr", True):
        import_edit = ensure_import(lines, "io.vavr.control.Try")
        if import_edit is not None:
            edits.append(import_edit)

    replace_start = lsp.Position(line=try_node.start_point[0], character=try_node.start_point[1])
    replace_end = lsp.Position(line=try_node.end_point[0], character=try_node.end_point[1])
    edits.append(
        lsp.TextEdit(
            range=lsp.Range(start=replace_start, end=replace_end),
            new_text=f"return {chain};",
        )
    )

    logger.debug(
        "try-catch-to-monadic rewrite: pattern=%s, lines %d-%d",
        "recover" if uses_exc else ("onFailure+getOrElse" if on_failure else "getOrElse"),
        try_node.start_point[0],
        try_node.end_point[0],
    )

    return lsp.WorkspaceEdit(changes={uri: edits})


# --- imperative-option-unwrap ---


def _find_if_at(tree: Tree, diag_range: lsp.Range) -> Node | None:
    """Find the if_statement at the diagnostic range (which spans the whole if)."""
    target = tree.root_node.descendant_for_point_range(
        (diag_range.start.line, diag_range.start.character),
        (diag_range.end.line, diag_range.end.character),
    )
    if target is None:
        return None
    if target.type == "if_statement":
        return target
    return find_ancestor(target, "if_statement")


def fix_imperative_option_unwrap(
    uri: str,
    source: str,
    diag_range: lsp.Range,
    config: dict[str, Any],
    *,
    tree: Tree | None = None,
    lines: list[str] | None = None,
) -> lsp.WorkspaceEdit | None:
    """Rewrite ``if (opt.isDefined()) return opt.get(); else return X;`` to
    ``return opt.map(it -> it).getOrElse(X);``.

    Bails (returns None) when the shape is anything other than that exact return/else-return
    pair — keeping the diagnostic visible but avoiding a broken edit.
    """
    if lines is None:
        lines = source.split("\n")
    if tree is None:
        tree = get_parser().parse(source.encode("utf-8"))

    if_node = _find_if_at(tree, diag_range)
    if if_node is None or has_error_or_missing(if_node):
        return None

    condition = if_node.child_by_field_name("condition")
    consequence = if_node.child_by_field_name("consequence")
    alternative = if_node.child_by_field_name("alternative")
    if condition is None or consequence is None:
        return None

    # Extract the variable name from the isDefined/isPresent invocation in the condition.
    var_name: bytes | None = None
    for inv in find_nodes(condition, "method_invocation"):
        name_node = inv.child_by_field_name("name")
        obj_node = inv.child_by_field_name("object")
        if name_node is None or obj_node is None:
            continue
        if name_node.text in (b"isDefined", b"isPresent") and obj_node.text:
            var_name = obj_node.text
            break
    if var_name is None:
        return None
    var = var_name.decode("utf-8")

    # Consequence must be a block with exactly: `return <var>.get();` (or similar shape).
    cons_stmts = (
        [c for c in consequence.named_children if c.type not in IGNORED_CHILDREN]
        if consequence.type == "block"
        else [consequence]
    )
    if len(cons_stmts) != 1 or cons_stmts[0].type != "return_statement":
        return None
    ret_children = [c for c in cons_stmts[0].named_children if c.type not in IGNORED_CHILDREN]
    if not ret_children:
        return None
    ret_expr = ret_children[0]
    # Map the returned expression to a lambda body. If it's literally `var.get()`, the lambda
    # is the identity `it -> it`; otherwise rewrite occurrences of `var` to `it`.
    ret_text = ret_expr.text.decode("utf-8") if ret_expr.text else var
    if ret_text == f"{var}.get()":
        lambda_body = "it"
    else:
        pattern = re.compile(rf"\b{re.escape(var)}\.get\(\)")
        lambda_body = pattern.sub("it", ret_text)
        # If the rewrite did nothing (no .get() call to replace), bail — shape isn't safe.
        if lambda_body == ret_text:
            return None

    # Alternative (else) must be a single return statement, or absent (then no getOrElse).
    or_else: str | None = None
    if alternative is not None:
        alt_stmts = (
            [c for c in alternative.named_children if c.type not in IGNORED_CHILDREN]
            if alternative.type == "block"
            else [alternative]
        )
        if len(alt_stmts) != 1 or alt_stmts[0].type != "return_statement":
            return None
        alt_ret = [c for c in alt_stmts[0].named_children if c.type not in IGNORED_CHILDREN]
        if not alt_ret:
            return None
        alt_text = alt_ret[0].text.decode("utf-8") if alt_ret[0].text else "null"
        if _is_eager(alt_ret[0]):
            or_else = f".getOrElse({alt_text})"
        else:
            or_else = f".getOrElse(() -> {alt_text})"

    chain = f"{var}.map(it -> {lambda_body})"
    if or_else is not None:
        chain += or_else

    edits: list[lsp.TextEdit] = []
    edits.append(
        lsp.TextEdit(
            range=lsp.Range(
                start=lsp.Position(line=if_node.start_point[0], character=if_node.start_point[1]),
                end=lsp.Position(line=if_node.end_point[0], character=if_node.end_point[1]),
            ),
            new_text=f"return {chain};",
        )
    )
    return lsp.WorkspaceEdit(changes={uri: edits})


# --- mutable-dto ---


def _find_marker_annotation_at(tree: Tree, diag_range: lsp.Range, names: set[bytes]) -> Node | None:
    """Find a marker_annotation node at the diagnostic range whose name is in ``names``."""
    target = tree.root_node.descendant_for_point_range(
        (diag_range.start.line, diag_range.start.character),
        (diag_range.end.line, diag_range.end.character),
    )
    if target is None:
        return None
    cur: Node | None = target
    while cur is not None:
        if cur.type == "marker_annotation":
            name_node = cur.child_by_field_name("name")
            if name_node is not None and name_node.text in names:
                return cur
        cur = cur.parent
    return None


_CONFLICTING_LOMBOK_ANNOTATIONS = {b"NoArgsConstructor", b"AllArgsConstructor", b"RequiredArgsConstructor"}


def fix_mutable_dto(
    uri: str,
    source: str,
    diag_range: lsp.Range,
    config: dict[str, Any],
    *,
    tree: Tree | None = None,
    lines: list[str] | None = None,
) -> lsp.WorkspaceEdit | None:
    """Rewrite ``@Data`` / ``@Setter`` to ``@Value`` on the class declaration.

    Bails when the class also carries Lombok constructor annotations that conflict
    with ``@Value`` (it implies a single all-args constructor and final fields).
    """
    if lines is None:
        lines = source.split("\n")
    if tree is None:
        tree = get_parser().parse(source.encode("utf-8"))

    ann = _find_marker_annotation_at(tree, diag_range, {b"Data", b"Setter"})
    if ann is None or has_error_or_missing(ann):
        return None

    modifiers = ann.parent
    if modifiers is None or modifiers.type != "modifiers":
        return None

    # Reject if any conflicting Lombok annotation sits next to @Data/@Setter.
    for sib in modifiers.named_children:
        if sib.type == "marker_annotation":
            name_node = sib.child_by_field_name("name")
            if name_node is not None and name_node.text in _CONFLICTING_LOMBOK_ANNOTATIONS:
                return None

    name_node = ann.child_by_field_name("name")
    if name_node is None:
        return None

    edits: list[lsp.TextEdit] = []
    # Replace just the annotation name token (preserves `@`).
    edits.append(
        lsp.TextEdit(
            range=lsp.Range(
                start=lsp.Position(line=name_node.start_point[0], character=name_node.start_point[1]),
                end=lsp.Position(line=name_node.end_point[0], character=name_node.end_point[1]),
            ),
            new_text="Value",
        )
    )
    if config.get("autoImportVavr", True):
        # @Value lives in lombok.Value; keep the existing import-management helper but use a Lombok path.
        import_edit = ensure_import(lines, "lombok.Value")
        if import_edit is not None:
            edits.append(import_edit)
    return lsp.WorkspaceEdit(changes={uri: edits})


# --- field-injection ---


def _find_field_declaration_at(tree: Tree, diag_range: lsp.Range) -> Node | None:
    """Find the field_declaration whose @Autowired annotation is at the diagnostic range."""
    target = tree.root_node.descendant_for_point_range(
        (diag_range.start.line, diag_range.start.character),
        (diag_range.end.line, diag_range.end.character),
    )
    if target is None:
        return None
    return find_ancestor(target, "field_declaration")


def _find_autowired_annotation(field_decl: Node) -> Node | None:
    """Return the @Autowired marker_annotation inside a field_declaration, if present."""
    modifiers = next((c for c in field_decl.children if c.type == "modifiers"), None)
    if modifiers is None:
        return None
    for child in modifiers.named_children:
        if child.type == "marker_annotation":
            name_node = child.child_by_field_name("name")
            if name_node is not None and name_node.text == b"Autowired":
                return child
    return None


def fix_field_injection(
    uri: str,
    source: str,
    diag_range: lsp.Range,
    config: dict[str, Any],
    *,
    tree: Tree | None = None,
    lines: list[str] | None = None,
) -> lsp.WorkspaceEdit | None:
    """Rewrite a single ``@Autowired`` field to ``private final``.

    Removes the ``@Autowired`` annotation token (plus trailing whitespace) and ensures
    the field declaration includes the ``final`` modifier. We do NOT synthesise a
    constructor here — that requires class-wide analysis and risks clobbering existing
    constructors. The diagnostic remains useful as a starting point; the agent (or user)
    can follow the suggested_snippet to add the constructor.
    """
    if lines is None:
        lines = source.split("\n")
    if tree is None:
        tree = get_parser().parse(source.encode("utf-8"))

    field_decl = _find_field_declaration_at(tree, diag_range)
    if field_decl is None or has_error_or_missing(field_decl):
        return None

    ann = _find_autowired_annotation(field_decl)
    if ann is None:
        return None

    modifiers = ann.parent
    if modifiers is None:
        return None

    edits: list[lsp.TextEdit] = []

    # Remove the @Autowired annotation: extend the range to swallow trailing whitespace
    # on the same line (or the newline if @Autowired sits on its own line).
    ann_end_line = ann.end_point[0]
    ann_end_col = ann.end_point[1]
    if ann_end_line < len(lines):
        line_after = lines[ann_end_line][ann_end_col:]
        if line_after.strip() == "":
            # Annotation sits alone on its line — remove the whole line including newline.
            edits.append(
                lsp.TextEdit(
                    range=lsp.Range(
                        start=lsp.Position(line=ann.start_point[0], character=0),
                        end=lsp.Position(line=ann_end_line + 1, character=0),
                    ),
                    new_text="",
                )
            )
        else:
            # Annotation has code after it on the same line — remove annotation + one trailing space.
            trailing = 1 if line_after.startswith(" ") else 0
            edits.append(
                lsp.TextEdit(
                    range=lsp.Range(
                        start=lsp.Position(line=ann.start_point[0], character=ann.start_point[1]),
                        end=lsp.Position(line=ann_end_line, character=ann_end_col + trailing),
                    ),
                    new_text="",
                )
            )

    # Ensure `final` is present on the field. Look at modifier keywords (unnamed children).
    has_final = any(c.type == "final" for c in modifiers.children)
    if not has_final:
        # Insert `final ` right before the type. The type is a child of field_decl, not modifiers.
        type_node = field_decl.child_by_field_name("type")
        if type_node is not None:
            edits.append(
                lsp.TextEdit(
                    range=lsp.Range(
                        start=lsp.Position(line=type_node.start_point[0], character=type_node.start_point[1]),
                        end=lsp.Position(line=type_node.start_point[0], character=type_node.start_point[1]),
                    ),
                    new_text="final ",
                )
            )

    if not edits:
        return None
    return lsp.WorkspaceEdit(changes={uri: edits})


# --- Fix registry ---

_FIX_REGISTRY: dict[str, FixGenerator] = {
    "frozen-mutation": fix_frozen_mutation,
    "null-check-to-monadic": fix_null_check_to_monadic,
    "null-return": fix_null_return,
    "try-catch-to-monadic": fix_try_catch_to_monadic,
    "imperative-option-unwrap": fix_imperative_option_unwrap,
    "mutable-dto": fix_mutable_dto,
    "field-injection": fix_field_injection,
}


def get_fix(rule_id: str) -> FixGenerator | None:
    """Look up a fix generator by rule ID."""
    return _FIX_REGISTRY.get(rule_id)


def get_fix_registry_keys() -> set[str]:
    """Return the set of rule IDs that have registered fix generators."""
    return set(_FIX_REGISTRY.keys())
