"""Tests for architecture graph presentation."""

from __future__ import annotations

from stackpilot.config import Stack
from stackpilot.dependency_graph import build_graph
from stackpilot.graph_view import (
    compute_stats,
    collect_node_displays,
    dependency_depth,
    find_root_names,
    format_architecture_report,
    format_circular_dependency,
    format_dependency_tree,
)


def _stack_diamond() -> Stack:
    stack = Stack()
    stack.external_dependency(
        name="postgres", type="postgresql", host="127.0.0.1", port=5432
    )
    stack.service(
        name="users",
        path=".",
        command="uvicorn users:app --port 8002",
        depends_on=["postgres"],
        port=8002,
    )
    stack.service(
        name="auth",
        path=".",
        command="uvicorn auth:app --port 8001",
        depends_on=["users"],
        port=8001,
    )
    stack.service(
        name="payments",
        path=".",
        command="uvicorn payments:app --port 8003",
        depends_on=["users", "postgres"],
        port=8003,
    )
    stack.service(
        name="gateway",
        path=".",
        command="uvicorn gateway:app --port 8000",
        depends_on=["auth", "payments"],
        port=8000,
    )
    return stack


class TestRootsAndDepth:
    def test_finds_gateway_as_sole_app_root(self) -> None:
        graph = build_graph(_stack_diamond())
        assert find_root_names(graph) == ["gateway"]

    def test_dependency_depth_counts_longest_chain(self) -> None:
        graph = build_graph(_stack_diamond())
        # gateway -> auth -> users -> postgres  => 3 edges
        assert dependency_depth(graph) == 3


class TestArchitectureReport:
    def test_header_footer_and_labels(self) -> None:
        graph = build_graph(_stack_diamond())
        report = format_architecture_report(
            graph,
            statuses={
                "gateway": "running",
                "auth": "running",
                "users": "stopped",
                "payments": "starting",
            },
        )
        assert "StackPilot Architecture" in report
        assert "Services : 4" in report
        assert "Running  : 2" in report
        assert "FastAPI  : 4" in report
        assert "External : 1" in report
        assert "🟢 gateway (:8000) [FastAPI]" in report
        assert "PostgreSQL" in report
        assert "🔵" in report  # external dependency glyph
        assert "Graph Generated Successfully" in report
        assert "Circular Dependencies  : None" in report

    def test_dedupes_shared_external(self) -> None:
        graph = build_graph(_stack_diamond())
        tree = format_dependency_tree(graph)
        # First occurrence is expanded; later shared external leaves are omitted.
        assert tree.count("PostgreSQL") == 1
        assert "⋯" in tree  # shared app node (users under payments)

    def test_top_level_branch_spacers(self) -> None:
        graph = build_graph(_stack_diamond())
        tree = format_dependency_tree(graph)
        # Breathing room between gateway's direct dependency branches.
        assert "\n│\n" in tree

    def test_ascii_fallback_avoids_unicode_box(self) -> None:
        graph = build_graph(_stack_diamond())
        tree = format_dependency_tree(graph, unicode=False)
        assert "+--" in tree or "`--" in tree
        assert "├──" not in tree

    def test_ungraphed_forest_of_roots(self) -> None:
        stack = Stack()
        stack.external_dependency(
            name="postgres", type="postgresql", host="127.0.0.1", port=5432
        )
        stack.service(name="gateway", path=".", command="uvicorn g:app", port=8000)
        stack.service(name="auth", path=".", command="uvicorn a:app", port=8001)
        graph = build_graph(stack)
        tree = format_dependency_tree(graph)
        assert "independent roots" in tree
        assert "🟢 gateway" in tree or "🔴 gateway" in tree
        assert "PostgreSQL" in tree
        # No fake tree connectors when there are no edges.
        assert "├──" not in tree
        assert "└──" not in tree


class TestCircularDisplay:
    def test_cycle_block_format(self) -> None:
        text = format_circular_dependency(["gateway", "auth", "users", "gateway"])
        assert text.startswith("❌ Circular Dependency")
        assert "  ↓" in text
        assert text.count("gateway") == 2


class TestStats:
    def test_stats_count_frameworks(self) -> None:
        stack = Stack()
        stack.service(name="a", path=".", command="uvicorn a:app")
        stack.service(name="b", path=".", command="flask run")
        stack.service(name="c", path=".", command="python manage.py runserver")
        stack.service(name="d", path=".", command="node server.js")
        graph = build_graph(stack)
        nodes = collect_node_displays(graph)
        stats = compute_stats(graph, nodes)
        assert stats.fastapi == 1
        assert stats.flask == 1
        assert stats.django == 1
        assert stats.node == 1
