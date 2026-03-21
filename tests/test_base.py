"""Tests for base.py tree traversal helper functions."""

from java_functional_lsp.analyzers.base import (
    collect_nodes_by_type,
    find_ancestor,
    find_nodes,
    find_nodes_multi,
    get_parser,
    has_ancestor,
)


def _parse(source: str):
    parser = get_parser()
    return parser.parse(source.encode())


class TestFindNodes:
    def test_finds_null_literal(self):
        tree = _parse("class T { void f() { return null; } }")
        nodes = list(find_nodes(tree.root_node, "null_literal"))
        assert len(nodes) == 1
        assert nodes[0].text == b"null"

    def test_finds_multiple_matches(self):
        tree = _parse("class T { void f() { return null; } void g() { return null; } }")
        nodes = list(find_nodes(tree.root_node, "null_literal"))
        assert len(nodes) == 2

    def test_finds_nested_nodes(self):
        tree = _parse("""
            class Outer {
                class Inner {
                    void f() { return null; }
                }
            }
        """)
        nodes = list(find_nodes(tree.root_node, "null_literal"))
        assert len(nodes) == 1

    def test_no_match_returns_empty(self):
        tree = _parse("class T { void f() { return 42; } }")
        nodes = list(find_nodes(tree.root_node, "null_literal"))
        assert len(nodes) == 0

    def test_empty_class(self):
        tree = _parse("class T { }")
        nodes = list(find_nodes(tree.root_node, "method_declaration"))
        assert len(nodes) == 0


class TestFindNodesMulti:
    def test_finds_multiple_types(self):
        tree = _parse("""
            class T {
                void f() {
                    for (int i = 0; i < 10; i++) {}
                    while (true) {}
                }
            }
        """)
        nodes = list(find_nodes_multi(tree.root_node, {"for_statement", "while_statement"}))
        assert len(nodes) == 2

    def test_empty_set_returns_nothing(self):
        tree = _parse("class T { void f() { return null; } }")
        nodes = list(find_nodes_multi(tree.root_node, set()))
        assert len(nodes) == 0


class TestHasAncestor:
    def test_has_method_ancestor(self):
        tree = _parse("class T { void f() { return null; } }")
        null_nodes = list(find_nodes(tree.root_node, "null_literal"))
        assert len(null_nodes) == 1
        assert has_ancestor(null_nodes[0], {"method_declaration"})

    def test_no_matching_ancestor(self):
        tree = _parse("class T { void f() { return null; } }")
        null_nodes = list(find_nodes(tree.root_node, "null_literal"))
        assert not has_ancestor(null_nodes[0], {"constructor_declaration"})

    def test_multiple_ancestor_types(self):
        tree = _parse("class T { void f() { return null; } }")
        null_nodes = list(find_nodes(tree.root_node, "null_literal"))
        assert has_ancestor(null_nodes[0], {"method_declaration", "constructor_declaration"})


class TestFindAncestor:
    def test_finds_nearest_ancestor(self):
        tree = _parse("class T { void f() { return null; } }")
        null_nodes = list(find_nodes(tree.root_node, "null_literal"))
        ancestor = find_ancestor(null_nodes[0], "method_declaration")
        assert ancestor is not None
        assert ancestor.type == "method_declaration"

    def test_returns_none_when_not_found(self):
        tree = _parse("class T { void f() { return null; } }")
        null_nodes = list(find_nodes(tree.root_node, "null_literal"))
        assert find_ancestor(null_nodes[0], "constructor_declaration") is None


class TestCollectNodesByType:
    def test_collects_multiple_types_single_pass(self):
        tree = _parse("""
            class T {
                void f() {
                    return null;
                    throw new Exception();
                }
            }
        """)
        buckets = collect_nodes_by_type(
            tree.root_node, {"null_literal", "throw_statement", "method_declaration"}
        )
        assert len(buckets["null_literal"]) == 1
        assert len(buckets["throw_statement"]) == 1
        assert len(buckets["method_declaration"]) == 1

    def test_empty_buckets_for_missing_types(self):
        tree = _parse("class T { }")
        buckets = collect_nodes_by_type(tree.root_node, {"null_literal", "throw_statement"})
        assert len(buckets["null_literal"]) == 0
        assert len(buckets["throw_statement"]) == 0
