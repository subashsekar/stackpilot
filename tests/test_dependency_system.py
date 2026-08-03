"""
Dependency-system QA suite for StackPilot.

Uses the checked-in ``tests/fixtures/stackpilot-test`` project plus synthetic
graphs. Does not modify the StackPilot library.

Note: the library raises ``CircularDependencyError`` (cycle) and
``MissingDependencyError`` (unknown dependency names). Tests assert those
types and the user-visible message content.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from stackpilot.cli import app
from stackpilot.dependency_graph import (
    CircularDependencyError,
    MissingDependencyError,
    build_graph,
    format_cycle_path,
)

from tests.helpers.dependency_qa import (
    STACKPILOT_TEST_ROOT,
    STACKFILE,
    assert_constraints,
    build_test_graph,
    count_start_occurrences,
    generate_random_dag,
    load_test_stack,
    measure_topo_ms,
    short_sleep_env,
    stack_with_cycle,
    stack_with_duplicate_redis_dep,
    stack_with_independent_services,
    stack_with_missing_dependency,
    start_ordered_once,
)
from tests.helpers.expected import (
    AUTH_SUBGRAPH_EXCLUDED,
    AUTH_SUBGRAPH_ORDER,
    EXPECTED_CYCLE_PATH,
    EXPECTED_GRAPH_FRAGMENTS,
    FULL_ORDER_CONSTRAINTS,
    FULL_STARTUP_ORDER,
    GATEWAY_SUBGRAPH_ORDER,
)

cli = CliRunner()


@pytest.fixture(scope="module")
def test_project() -> Path:
    assert STACKFILE.is_file(), f"Expected Stackfile at {STACKFILE}"
    for name in FULL_STARTUP_ORDER:
        main = STACKPILOT_TEST_ROOT / name / "main.py"
        assert main.is_file(), f"Missing service script: {main}"
    return STACKPILOT_TEST_ROOT


# ---------------------------------------------------------------------------
# TEST 1 — stackpilot graph
# ---------------------------------------------------------------------------


class Test1GraphOutput:
    def test_graph_cli_prints_readable_tree(
        self, test_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(test_project)
        result = cli.invoke(app, ["graph"])
        assert result.exit_code == 0, result.output
        output = result.output
        for fragment in EXPECTED_GRAPH_FRAGMENTS:
            assert fragment in output, f"Missing {fragment!r} in graph output:\n{output}"
        # ASCII / Unicode tree connectors for nested deps
        assert (
            "+--" in output
            or "`--" in output
            or "├──" in output
            or "└──" in output
        )
        assert "StackPilot Architecture" in output
        assert "Graph Generated Successfully" in output

    def test_graph_api_tree_contains_services(self, test_project: Path) -> None:
        tree = build_test_graph().format_ascii_tree()
        for fragment in EXPECTED_GRAPH_FRAGMENTS:
            assert fragment in tree


# ---------------------------------------------------------------------------
# TEST 2 — stackpilot run (full stack order)
# ---------------------------------------------------------------------------


class Test2FullStartupOrder:
    def test_topological_order_matches_documented_sequence(
        self, test_project: Path
    ) -> None:
        order = build_test_graph().topological_order()
        assert order == FULL_STARTUP_ORDER

    def test_order_satisfies_all_constraints(self, test_project: Path) -> None:
        order = build_test_graph().topological_order()
        assert_constraints(order, FULL_ORDER_CONSTRAINTS)

    def test_process_start_order_matches_topo(
        self, test_project: Path, tmp_path: Path
    ) -> None:
        stack = load_test_stack()
        ordered = build_graph(stack.services).ordered_specs()
        with short_sleep_env(0.05):
            started = start_ordered_once(ordered, logs_dir=tmp_path / "logs")
        assert started == FULL_STARTUP_ORDER
        counts = count_start_occurrences(started)
        assert all(v == 1 for v in counts.values())


# ---------------------------------------------------------------------------
# TEST 3 — stackpilot run auth (selective)
# ---------------------------------------------------------------------------


class Test3SelectiveAuth:
    def test_resolve_auth_subgraph(self, test_project: Path) -> None:
        resolved = build_test_graph().resolve_for("auth")
        assert resolved == AUTH_SUBGRAPH_ORDER
        for excluded in AUTH_SUBGRAPH_EXCLUDED:
            assert excluded not in resolved

    def test_process_starts_only_auth_closure(
        self, test_project: Path, tmp_path: Path
    ) -> None:
        stack = load_test_stack()
        ordered = build_graph(stack.services).ordered_specs(target="auth")
        with short_sleep_env(0.05):
            started = start_ordered_once(ordered, logs_dir=tmp_path / "logs-auth")
        assert started == AUTH_SUBGRAPH_ORDER
        for excluded in AUTH_SUBGRAPH_EXCLUDED:
            assert excluded not in started


# ---------------------------------------------------------------------------
# TEST 4 — stackpilot run gateway (full closure)
# ---------------------------------------------------------------------------


class Test4SelectiveGateway:
    def test_resolve_gateway_includes_all_required(
        self, test_project: Path
    ) -> None:
        resolved = build_test_graph().resolve_for("gateway")
        assert resolved == GATEWAY_SUBGRAPH_ORDER
        assert set(resolved) == set(FULL_STARTUP_ORDER)

    def test_process_starts_full_gateway_closure(
        self, test_project: Path, tmp_path: Path
    ) -> None:
        stack = load_test_stack()
        ordered = build_graph(stack.services).ordered_specs(target="gateway")
        with short_sleep_env(0.05):
            started = start_ordered_once(ordered, logs_dir=tmp_path / "logs-gw")
        assert started == GATEWAY_SUBGRAPH_ORDER


# ---------------------------------------------------------------------------
# TEST 5 — cycle detection
# ---------------------------------------------------------------------------


class Test5CycleDetection:
    def test_users_to_gateway_raises_circular_dependency(
        self, test_project: Path
    ) -> None:
        graph = build_graph(stack_with_cycle().services)
        with pytest.raises(CircularDependencyError) as exc_info:
            graph.validate()

        cycle = exc_info.value.cycle
        assert cycle[0] == cycle[-1]
        assert set(cycle) == {"gateway", "auth", "users"}

        # Same cycle ring; DFS may report any rotation. Normalize to gateway.
        body = list(cycle[:-1])
        start = body.index("gateway")
        normalized = tuple(body[start:] + body[:start] + ["gateway"])
        assert normalized == ("gateway", "auth", "users", "gateway")
        assert format_cycle_path(normalized) == EXPECTED_CYCLE_PATH

        message = str(exc_info.value)
        assert "gateway" in message
        assert "auth" in message
        assert "users" in message
        assert "↓" in message


# ---------------------------------------------------------------------------
# TEST 6 — missing / unknown dependency
# ---------------------------------------------------------------------------


class Test6MissingDependency:
    def test_unknown_rabbitmq_raises(self, test_project: Path) -> None:
        graph = build_graph(stack_with_missing_dependency().services)
        with pytest.raises(MissingDependencyError) as exc_info:
            graph.validate()

        message = str(exc_info.value)
        assert "rabbitmq" in message
        # Library wording: missing / unknown service
        assert "unknown service" in message.lower() or "Missing dependencies" in message


# ---------------------------------------------------------------------------
# TEST 7 — duplicate dependency entries
# ---------------------------------------------------------------------------


class Test7DuplicateDependency:
    def test_duplicate_redis_ignored_gracefully(self, test_project: Path) -> None:
        graph = build_graph(stack_with_duplicate_redis_dep().services)
        # No error on validate / topo
        order = graph.topological_order()
        assert order.count("redis") == 1
        assert "payments" in order
        # Edge list deduped
        assert graph.edges["payments"].count("redis") == 1


# ---------------------------------------------------------------------------
# TEST 8 — independent services
# ---------------------------------------------------------------------------


class Test8IndependentServices:
    def test_metrics_and_docs_do_not_break_core_constraints(
        self, test_project: Path
    ) -> None:
        graph = build_graph(stack_with_independent_services().services)
        order = graph.topological_order()

        assert "metrics" in order
        assert "docs" in order
        assert_constraints(order, FULL_ORDER_CONSTRAINTS)
        # Independents are not required by gateway selective resolve
        gateway_only = graph.resolve_for("gateway")
        assert "metrics" not in gateway_only
        assert "docs" not in gateway_only


# ---------------------------------------------------------------------------
# TEST 9 — performance (100 services)
# ---------------------------------------------------------------------------


class Test9Performance:
    def test_topo_sort_100_services_under_100ms(self) -> None:
        specs = generate_random_dag(100, seed=7)
        elapsed_ms = measure_topo_ms(specs, repeats=10)
        assert elapsed_ms < 100.0, f"topo sort took {elapsed_ms:.2f} ms (target < 100 ms)"


# ---------------------------------------------------------------------------
# TEST 10 — stress (250 services)
# ---------------------------------------------------------------------------


class Test10Stress:
    def test_250_services_no_recursion_error_unique_order(self) -> None:
        specs = generate_random_dag(250, seed=99, max_deps=4)
        graph = build_graph(specs)

        # Must not raise RecursionError
        order = graph.topological_order()
        assert len(order) == 250
        assert len(set(order)) == 250

        counts = count_start_occurrences(order)
        assert all(v == 1 for v in counts.values())

        # Every edge respected
        index = {name: i for i, name in enumerate(order)}
        for spec in specs:
            for dep in spec.depends_on:
                assert index[dep] < index[spec.name]

    def test_250_selective_resolve_unique(self) -> None:
        specs = generate_random_dag(250, seed=123, max_deps=4)
        graph = build_graph(specs)
        target = specs[-1].name
        resolved = graph.resolve_for(target)
        assert resolved[-1] == target
        assert len(resolved) == len(set(resolved))
        assert all(name in graph.specs for name in resolved)
