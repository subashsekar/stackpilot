"""Tests for ExternalDependency vs application service separation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from stackpilot.config import ExternalDependency, ServiceSpec, Stack, TcpHealthCheck
from stackpilot.dependency_graph import (
    DependencyGraph,
    DuplicateServiceError,
    UnknownServiceError,
    build_graph,
)
from stackpilot.external_validation import (
    ExternalDependencyError,
    format_external_unavailable,
    validate_external_dependencies,
)
from stackpilot.generator import generate_stackfile
from stackpilot.scanner import scan_project
from stackpilot.status import format_status_report


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _app(name: str = "auth", *, depends_on: tuple[str, ...] = ()) -> ServiceSpec:
    return ServiceSpec(
        name=name,
        path=Path("."),
        command="true",
        depends_on=depends_on,
    )


class TestExternalDependencyModel:
    def test_defaults_to_tcp_health_check(self) -> None:
        dep = ExternalDependency(
            name="postgres",
            type="postgresql",
            host="127.0.0.1",
            port=5432,
        )
        assert isinstance(dep.health_check, TcpHealthCheck)
        assert dep.health_check.host == "127.0.0.1"
        assert dep.health_check.port == 5432
        assert dep.health_check.timeout == 10.0
        assert dep.display_name == "PostgreSQL"

    def test_stack_api_registers_external(self) -> None:
        stack = Stack()
        stack.external_dependency(
            name="redis",
            type="redis",
            host="127.0.0.1",
            port=6379,
        )
        stack.service(
            name="auth",
            path=".",
            command="true",
            depends_on=["redis"],
        )
        assert len(stack.services) == 1
        assert len(stack.external_dependencies) == 1
        assert stack.external_dependencies[0].name == "redis"


class TestDependencyGraphExternals:
    def test_external_nodes_resolve_depends_on(self) -> None:
        graph = DependencyGraph.from_services(
            [_app(depends_on=("postgres",))],
            [
                ExternalDependency(
                    name="postgres",
                    type="postgresql",
                    host="127.0.0.1",
                    port=5432,
                )
            ],
        )
        graph.validate()
        ordered = graph.ordered_specs()
        assert [s.name for s in ordered] == ["auth"]
        assert graph.is_external("postgres")
        assert graph.required_externals()[0].name == "postgres"

    def test_ascii_tree_marks_external(self) -> None:
        stack = Stack()
        stack.external_dependency(
            name="postgres",
            type="postgresql",
            host="127.0.0.1",
            port=5432,
        )
        stack.external_dependency(
            name="redis",
            type="redis",
            host="127.0.0.1",
            port=6379,
        )
        stack.service(
            name="auth",
            path=".",
            command="true",
            depends_on=["postgres", "redis"],
        )
        tree = build_graph(stack).format_ascii_tree()
        assert "PostgreSQL" in tree or "postgres" in tree
        assert "Redis" in tree or "redis" in tree
        assert "auth" in tree
        assert (
            "├──" in tree
            or "└──" in tree
            or "[external]" in tree
            or "External Infrastructure" in tree
        )

    def test_cannot_start_external_as_target(self) -> None:
        graph = DependencyGraph.from_services(
            [_app(depends_on=("postgres",))],
            [
                ExternalDependency(
                    name="postgres",
                    type="postgresql",
                    host="127.0.0.1",
                    port=5432,
                )
            ],
        )
        with pytest.raises(UnknownServiceError, match="external dependency"):
            graph.resolve_for("postgres")

    def test_duplicate_name_with_service_rejected(self) -> None:
        with pytest.raises(DuplicateServiceError):
            DependencyGraph.from_services(
                [_app(name="postgres")],
                [
                    ExternalDependency(
                        name="postgres",
                        type="postgresql",
                        host="127.0.0.1",
                        port=5432,
                    )
                ],
            )


class TestExternalValidation:
    def test_unavailable_message_format(self) -> None:
        dep = ExternalDependency(
            name="postgres",
            type="postgresql",
            host="127.0.0.1",
            port=5432,
        )
        text = format_external_unavailable(dep, dependents=["auth", "users"])
        assert "Checking PostgreSQL..." in text
        assert "✗ PostgreSQL is not reachable." in text
        assert "Problem: Dependency unavailable" in text
        assert "Host: 127.0.0.1" in text
        assert "Port: 5432" in text
        assert "Services depending on PostgreSQL:" in text
        assert "- auth" in text
        assert "- users" in text
        assert "Suggested fix:" in text
        assert "Startup aborted." in text

    def test_validate_aborts_when_unreachable(self) -> None:
        stack = Stack()
        stack.external_dependency(
            name="postgres",
            type="postgresql",
            host="127.0.0.1",
            port=5432,
            health_check=TcpHealthCheck(
                host="127.0.0.1",
                port=5432,
                timeout=0.05,
                interval=0.01,
                probe_timeout=0.05,
            ),
        )
        stack.service(
            name="auth",
            path=".",
            command="true",
            depends_on=["postgres"],
        )
        graph = build_graph(stack)
        ordered = graph.ordered_specs()
        with patch(
            "stackpilot.external_validation.check_external_dependency",
            return_value=False,
        ):
            with pytest.raises(ExternalDependencyError) as exc_info:
                validate_external_dependencies(graph, ordered_services=ordered)
        assert "PostgreSQL is not reachable" in str(exc_info.value)

    def test_validate_skips_when_reachable(self) -> None:
        stack = Stack()
        stack.external_dependency(
            name="postgres",
            type="postgresql",
            host="127.0.0.1",
            port=5432,
        )
        stack.service(
            name="auth",
            path=".",
            command="true",
            depends_on=["postgres"],
        )
        graph = build_graph(stack)
        with patch(
            "stackpilot.external_validation.check_external_dependency",
            return_value=True,
        ):
            validate_external_dependencies(
                graph,
                ordered_services=graph.ordered_specs(),
            )

    def test_scenario_a_prints_success_marks(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stack = Stack()
        stack.external_dependency(
            name="postgres",
            type="postgresql",
            host="127.0.0.1",
            port=5432,
        )
        stack.external_dependency(
            name="redis",
            type="redis",
            host="127.0.0.1",
            port=6379,
        )
        stack.service(
            name="auth",
            path=".",
            command="true",
            depends_on=["postgres", "redis"],
        )
        graph = build_graph(stack)
        with patch(
            "stackpilot.external_validation.check_external_dependency",
            return_value=True,
        ):
            validate_external_dependencies(
                graph,
                ordered_services=graph.ordered_specs(),
            )
        out = capsys.readouterr().out
        assert "Checking external dependencies..." in out
        assert "✓ PostgreSQL (127.0.0.1:5432)" in out
        assert "✓ Redis (127.0.0.1:6379)" in out


class TestGeneratorEmitsExternals:
    def test_postgres_and_redis_are_external_dependencies(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path / "auth" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        _write(tmp_path / "postgres" / "postgresql.conf", "port = 5432\n")
        _write(tmp_path / "redis" / "redis.conf", "port 6379\n")

        services = scan_project(tmp_path)
        text = generate_stackfile(services, project_root=tmp_path)

        assert "stack.external_dependency(" in text
        assert 'name="postgres"' in text
        assert 'type="postgresql"' in text
        assert 'name="redis"' in text
        assert 'type="redis"' in text
        assert 'command="docker compose up postgres"' not in text
        assert 'command="redis-server' not in text
        assert "stack.service(" in text
        assert 'name="auth"' in text


class TestStatusSections:
    def test_status_has_applications_and_externals(self) -> None:
        text = format_status_report(
            project_name="demo",
            services=[
                {
                    "name": "auth",
                    "status": "stopped",
                    "pid": None,
                    "port": 8000,
                    "uptime": None,
                    "framework": "uvicorn",
                    "health": "stopped",
                }
            ],
            session_active=False,
            external_dependencies=[
                {
                    "name": "postgres",
                    "type": "postgresql",
                    "host": "127.0.0.1",
                    "port": 5432,
                    "status": "unreachable",
                }
            ],
        )
        assert "Applications" in text
        assert "External Dependencies" in text
        assert "postgres" in text
        assert "auth" in text


class TestOrchestratorDoesNotStartExternals:
    def test_ordered_services_exclude_externals(self) -> None:
        stack = Stack()
        stack.external_dependency(
            name="postgres",
            type="postgresql",
            host="127.0.0.1",
            port=5432,
        )
        stack.service(
            name="auth",
            path=".",
            command="true",
            depends_on=["postgres"],
        )
        from stackpilot.orchestrator import Orchestrator

        orch = Orchestrator()
        ordered = orch._ordered_services(stack, target=None)
        assert [s.name for s in ordered] == ["auth"]
        assert all(isinstance(s, ServiceSpec) for s in ordered)
