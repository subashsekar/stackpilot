from __future__ import annotations

from pathlib import Path

import pytest

from stackpilot.config import ServiceSpec, Stack
from stackpilot.dependency_graph import (
    CircularDependencyError,
    DependencyGraph,
    MissingDependencyError,
    UnknownServiceError,
    build_graph,
    format_cycle_path,
)


def _spec(
    name: str,
    *,
    depends_on: tuple[str, ...] = (),
) -> ServiceSpec:
    return ServiceSpec(
        name=name,
        path=Path("."),
        command="true",
        depends_on=depends_on,
    )


class TestTopologicalSorting:
    def test_dependencies_come_before_dependents(self) -> None:
        graph = build_graph(
            [
                _spec("gateway", depends_on=("auth", "users")),
                _spec("auth"),
                _spec("users", depends_on=("auth",)),
            ]
        )

        order = graph.topological_order()

        assert order.index("auth") < order.index("users")
        assert order.index("auth") < order.index("gateway")
        assert order.index("users") < order.index("gateway")
        assert set(order) == {"auth", "users", "gateway"}

    def test_independent_services_are_all_included(self) -> None:
        graph = build_graph([_spec("a"), _spec("b"), _spec("c")])
        assert graph.topological_order() == ["a", "b", "c"]

    def test_stack_depends_on_flows_into_graph(self) -> None:
        stack = Stack()
        stack.service(name="auth", path=".", command="true")
        stack.service(
            name="gateway",
            path=".",
            command="true",
            depends_on=["auth"],
        )

        order = build_graph(stack.services).topological_order()
        assert order == ["auth", "gateway"]


class TestCycleDetection:
    def test_detects_simple_cycle(self) -> None:
        graph = build_graph(
            [
                _spec("a", depends_on=("b",)),
                _spec("b", depends_on=("a",)),
            ]
        )

        cycle = graph.detect_cycle()
        assert cycle is not None
        assert cycle[0] == cycle[-1]
        assert set(cycle[:-1]) == {"a", "b"}

    def test_detects_longer_cycle(self) -> None:
        graph = build_graph(
            [
                _spec("gateway", depends_on=("auth",)),
                _spec("auth", depends_on=("users",)),
                _spec("users", depends_on=("gateway",)),
            ]
        )

        with pytest.raises(CircularDependencyError) as exc_info:
            graph.topological_order()

        message = str(exc_info.value)
        assert "gateway" in message
        assert "auth" in message
        assert "users" in message
        assert "↓" in message
        assert message == format_cycle_path(exc_info.value.cycle)

    def test_format_cycle_path_is_readable(self) -> None:
        rendered = format_cycle_path(["gateway", "auth", "users", "gateway"])
        assert rendered == (
            "gateway\n"
            "↓\n"
            "\n"
            "auth\n"
            "↓\n"
            "\n"
            "users\n"
            "↓\n"
            "\n"
            "gateway"
        )

    def test_no_cycle_returns_none(self) -> None:
        graph = build_graph(
            [
                _spec("gateway", depends_on=("auth",)),
                _spec("auth"),
            ]
        )
        assert graph.detect_cycle() is None


class TestMissingDependencyDetection:
    def test_missing_dependency_raises(self) -> None:
        graph = build_graph(
            [
                _spec("gateway", depends_on=("auth",)),
            ]
        )

        with pytest.raises(MissingDependencyError) as exc_info:
            graph.validate()

        assert "auth" in str(exc_info.value)
        assert "gateway" in str(exc_info.value)

    def test_multiple_missing_dependencies_are_reported(self) -> None:
        graph = build_graph(
            [
                _spec("gateway", depends_on=("auth", "users")),
            ]
        )

        with pytest.raises(MissingDependencyError) as exc_info:
            graph.validate()

        message = str(exc_info.value)
        assert "auth" in message
        assert "users" in message


class TestSelectiveDependencyResolution:
    def test_resolve_includes_transitive_dependencies(self) -> None:
        graph = build_graph(
            [
                _spec("gateway", depends_on=("auth", "users")),
                _spec("auth"),
                _spec("users", depends_on=("auth",)),
                _spec("worker"),
            ]
        )

        resolved = graph.resolve_for("gateway")
        assert resolved == ["auth", "users", "gateway"]
        assert "worker" not in resolved

    def test_resolve_leaf_service_returns_only_itself(self) -> None:
        graph = build_graph(
            [
                _spec("gateway", depends_on=("auth",)),
                _spec("auth"),
            ]
        )
        assert graph.resolve_for("auth") == ["auth"]

    def test_resolve_unknown_service_raises(self) -> None:
        graph = build_graph([_spec("auth")])
        with pytest.raises(UnknownServiceError):
            graph.resolve_for("missing")

    def test_ordered_specs_selective(self) -> None:
        specs = [
            _spec("gateway", depends_on=("auth",)),
            _spec("auth"),
            _spec("worker"),
        ]
        graph = DependencyGraph.from_services(specs)
        ordered = graph.ordered_specs(target="gateway")
        assert [s.name for s in ordered] == ["auth", "gateway"]


class TestAsciiTree:
    def test_tree_lists_dependencies(self) -> None:
        graph = build_graph(
            [
                _spec("gateway", depends_on=("auth", "users")),
                _spec("auth"),
                _spec("users"),
            ]
        )

        tree = graph.format_ascii_tree()
        assert "gateway" in tree
        assert "├──" in tree or "└──" in tree or "+--" in tree or "`--" in tree
        assert "auth" in tree
        assert "users" in tree

    def test_tree_uses_true_roots_and_dedupes(self) -> None:
        graph = build_graph(
            [
                _spec("gateway", depends_on=("auth", "payments")),
                _spec("auth", depends_on=("redis",)),
                _spec("payments", depends_on=("redis",)),
                _spec("redis"),
            ]
        )
        tree = graph.format_ascii_tree()
        # Only gateway is a root — dependents are nested.
        assert tree.splitlines()[0].startswith("🔴 gateway") or tree.splitlines()[0].startswith("[x] gateway")
        assert tree.count("redis") >= 1
        # Second redis appearance is a dedupe marker, not a full re-print of a subtree.
        assert "⋯" in tree or tree.count("\n🔴 redis") + tree.count("\n[x] redis") <= 1
