"""Integration tests for External Dependency validation and UX."""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Tuple
from unittest.mock import MagicMock, patch

import pytest

from stackpilot.config import ExternalDependency, ServiceSpec, Stack, TcpHealthCheck
from stackpilot.dependency_graph import build_graph
from stackpilot.diagnostics.external_check import diagnose_external_dependency
from stackpilot.doctor import CheckStatus, run_doctor
from stackpilot.external_validation import (
    ExternalDependencyError,
    validate_external_dependencies,
)
from stackpilot.generator import generate_stackfile
from stackpilot.orchestrator import Orchestrator
from stackpilot.scanner import scan_project
from stackpilot.status import format_status_report
from stackpilot.tcp_checker import diagnose_tcp


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _tcp_listener(host: str = "127.0.0.1") -> Iterator[Tuple[str, int]]:
    """Accept TCP connections on an ephemeral port until the context exits."""

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, 0))
    srv.listen(5)
    port = int(srv.getsockname()[1])
    stop = threading.Event()

    def _accept_loop() -> None:
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.close()
            except OSError:
                pass

    thread = threading.Thread(target=_accept_loop, daemon=True)
    thread.start()
    try:
        yield host, port
    finally:
        stop.set()
        try:
            srv.close()
        except OSError:
            pass
        thread.join(timeout=2.0)


def _stack_with_externals(
    *,
    postgres: Tuple[str, int] | None,
    redis: Tuple[str, int] | None = None,
    dependents: Tuple[str, ...] = ("auth", "users"),
) -> Stack:
    stack = Stack()
    depends: List[str] = []
    if postgres is not None:
        stack.external_dependency(
            name="postgres",
            type="postgresql",
            host=postgres[0],
            port=postgres[1],
        )
        depends.append("postgres")
    if redis is not None:
        stack.external_dependency(
            name="redis",
            type="redis",
            host=redis[0],
            port=redis[1],
        )
        depends.append("redis")
    for name in dependents:
        stack.service(
            name=name,
            path=".",
            command='python -c "pass"',
            depends_on=list(depends),
        )
    return stack


class TestDependencyReachable:
    def test_dependency_reachable(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _tcp_listener() as (host, port):
            stack = _stack_with_externals(postgres=(host, port), dependents=("auth",))
            graph = build_graph(stack)
            validate_external_dependencies(
                graph,
                ordered_services=graph.ordered_specs(),
            )
        out = capsys.readouterr().out
        assert "Checking external dependencies..." in out
        assert f"✓ PostgreSQL ({host}:{port})" in out
        assert "Startup aborted." not in out


class TestDependencyUnreachable:
    def test_dependency_unreachable_aborts(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        closed = _free_port()
        stack = _stack_with_externals(
            postgres=("127.0.0.1", closed),
            dependents=("auth", "users"),
        )
        # Keep the retry window short for the closed-port regression.
        dep = stack.external_dependencies[0]
        object.__setattr__(
            dep,
            "health_check",
            TcpHealthCheck(
                host="127.0.0.1",
                port=closed,
                timeout=0.2,
                interval=0.05,
                probe_timeout=0.05,
            ),
        )
        graph = build_graph(stack)
        with pytest.raises(ExternalDependencyError) as exc_info:
            validate_external_dependencies(
                graph,
                ordered_services=graph.ordered_specs(),
            )
        out = capsys.readouterr().out
        assert "Checking external dependencies..." in out
        assert "✗ PostgreSQL is not reachable." in out
        assert "Services depending on PostgreSQL:" in out
        assert "- auth" in out
        assert "- users" in out
        assert "Startup aborted." in out
        assert "PostgreSQL is not reachable" in str(exc_info.value)


class TestMultipleDependencies:
    def test_multiple_dependencies_all_reachable(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _tcp_listener() as pg, _tcp_listener() as rd:
            stack = _stack_with_externals(
                postgres=pg,
                redis=rd,
                dependents=("auth",),
            )
            graph = build_graph(stack)
            validate_external_dependencies(
                graph,
                ordered_services=graph.ordered_specs(),
            )
        out = capsys.readouterr().out
        assert f"✓ PostgreSQL ({pg[0]}:{pg[1]})" in out
        assert f"✓ Redis ({rd[0]}:{rd[1]})" in out


class TestDependencyTimeout:
    def test_dependency_timeout_classified(self) -> None:
        # Non-routable TEST-NET address forces a connect timeout on most hosts.
        result = diagnose_tcp("192.0.2.1", 5432, connect_timeout=0.2)
        assert result.ok is False
        assert result.kind == "timeout"
        assert "timed out" in result.detail


class TestGraphRendering:
    def test_graph_marks_externals(self) -> None:
        stack = Stack()
        stack.external_dependency(
            name="postgres", type="postgresql", host="127.0.0.1", port=5432
        )
        stack.external_dependency(
            name="redis", type="redis", host="127.0.0.1", port=6379
        )
        stack.service(
            name="auth",
            path=".",
            command="true",
            depends_on=["postgres", "redis"],
        )
        stack.service(
            name="gateway",
            path=".",
            command="true",
            depends_on=["auth"],
        )
        tree = build_graph(stack).format_ascii_tree()
        assert "PostgreSQL" in tree or "postgres" in tree
        assert "Redis" in tree or "redis" in tree
        assert "gateway" in tree
        assert "auth" in tree
        assert "├──" in tree or "└──" in tree or "[external]" in tree


class TestStatusOutput:
    def test_status_separates_applications_and_externals(self) -> None:
        text = format_status_report(
            project_name="demo",
            services=[
                {
                    "name": "auth",
                    "status": "running",
                    "pid": 100,
                    "port": 8000,
                    "uptime": 1.0,
                    "framework": "uvicorn",
                    "health": "healthy",
                }
            ],
            session_active=True,
            external_dependencies=[
                {
                    "name": "postgres",
                    "type": "postgresql",
                    "host": "127.0.0.1",
                    "port": 5432,
                    "status": "reachable",
                },
                {
                    "name": "redis",
                    "type": "redis",
                    "host": "127.0.0.1",
                    "port": 6379,
                    "status": "unreachable",
                },
            ],
        )
        app_i = text.index("Applications")
        ext_i = text.index("External Dependencies")
        assert app_i < ext_i
        assert text.index("auth") > app_i
        assert text.index("postgres") > ext_i
        apps_section = text[app_i:ext_i]
        assert "postgres" not in apps_section
        assert "redis" not in apps_section


class TestDoctorOutput:
    def test_doctor_unreachable_and_incorrect_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed = _free_port()
        body = f"""\
from stackpilot import Stack

stack = Stack()
stack.external_dependency(
    name="postgres",
    type="postgresql",
    host="127.0.0.1",
    port={closed},
)
stack.external_dependency(
    name="redis",
    type="redis",
    host="no-such-stackpilot-host.invalid",
    port=6379,
)
stack.service(
    name="auth",
    path=".",
    command="python -c \\"print('ok')\\"",
    depends_on=["postgres", "redis"],
)
stack.run()
"""
        (tmp_path / "Stackfile.py").write_text(body, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        report = run_doctor(start=tmp_path)
        check = next(
            c for c in report.checks if c.name == "External dependencies reachable"
        )
        assert check.status == CheckStatus.FAIL
        detail = check.detail or ""
        assert "PostgreSQL" in detail
        assert "Redis" in detail
        assert (
            "incorrect host" in detail
            or "DNS" in detail
            or "dns" in detail.lower()
            or "could not be resolved" in detail
        )

    def test_doctor_reachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _tcp_listener() as (host, port):
            body = f"""\
from stackpilot import Stack

stack = Stack()
stack.external_dependency(
    name="postgres",
    type="postgresql",
    host={host!r},
    port={port},
)
stack.service(
    name="auth",
    path=".",
    command="python -c \\"print('ok')\\"",
    depends_on=["postgres"],
)
stack.run()
"""
            (tmp_path / "Stackfile.py").write_text(body, encoding="utf-8")
            monkeypatch.chdir(tmp_path)
            report = run_doctor(start=tmp_path)
        check = next(
            c for c in report.checks if c.name == "External dependencies reachable"
        )
        assert check.status == CheckStatus.OK

    def test_doctor_incorrect_port_refused(self) -> None:
        closed = _free_port()
        dep = ExternalDependency(
            name="postgres",
            type="postgresql",
            host="127.0.0.1",
            port=closed,
        )
        result = diagnose_external_dependency(dep)
        assert result.ok is False
        # Closed local ports usually refuse; some Windows setups time out instead.
        assert result.kind in {"refused", "unreachable", "timeout"}
        assert str(closed) in result.detail

    def test_diagnose_tcp_classifies_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_args, **_kwargs):
            raise ConnectionRefusedError("simulated")

        monkeypatch.setattr(
            "stackpilot.tcp_checker.socket.create_connection",
            _raise,
        )
        result = diagnose_tcp("127.0.0.1", 5432, connect_timeout=0.5)
        assert result.ok is False
        assert result.kind == "refused"
        assert "incorrect host/port" in result.detail


class TestScannerDetection:
    def test_scanner_emits_externals_not_executable_services(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path / "auth" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        _write(tmp_path / "postgres" / "postgresql.conf", "port = 5432\n")
        _write(tmp_path / "redis" / "redis.conf", "port 6379\n")

        services = scan_project(tmp_path)
        by_name = {s.name: s for s in services}
        assert "postgres" in by_name
        assert "redis" in by_name
        assert by_name["postgres"].framework == "PostgreSQL"
        assert by_name["redis"].framework == "Redis"

        text = generate_stackfile(services, project_root=tmp_path)
        assert 'name="postgres"' in text
        assert "stack.external_dependency(" in text
        assert 'stack.service(\n    name="postgres"' not in text
        assert 'stack.service(\n    name="redis"' not in text
        assert "redis-server" not in text
        assert "docker compose up postgres" not in text


class TestGeneratorOutput:
    def test_generator_deterministic(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "auth" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        _write(tmp_path / "postgres" / "postgresql.conf", "port = 5432\n")
        _write(tmp_path / "redis" / "redis.conf", "port 6379\n")

        first = generate_stackfile(scan_project(tmp_path), project_root=tmp_path)
        second = generate_stackfile(scan_project(tmp_path), project_root=tmp_path)
        assert first == second
        assert "stack.external_dependency(" in first
        assert 'type="postgresql"' in first
        assert 'type="redis"' in first


class TestApplicationStartupBlocked:
    def test_application_startup_blocked_when_external_down(self) -> None:
        closed = _free_port()
        stack = _stack_with_externals(
            postgres=("127.0.0.1", closed),
            dependents=("auth",),
        )
        mock_runner = MagicMock()
        mock_runner.start_all.return_value = True
        orch = Orchestrator(runner=mock_runner)
        code = orch.run(stack)
        assert code == 1
        mock_runner.start_all.assert_not_called()
        mock_runner.bind.assert_not_called()


class TestApplicationStartupAllowed:
    def test_application_startup_allowed_when_externals_up(
        self, tmp_path: Path
    ) -> None:
        with _tcp_listener() as pg, _tcp_listener() as rd:
            stack = Stack()
            stack.external_dependency(
                name="postgres",
                type="postgresql",
                host=pg[0],
                port=pg[1],
            )
            stack.external_dependency(
                name="redis",
                type="redis",
                host=rd[0],
                port=rd[1],
            )
            stack.service(
                name="auth",
                path=str(tmp_path),
                command='python -c "import time; time.sleep(60)"',
                depends_on=["postgres", "redis"],
            )

            started: List[str] = []

            def _fake_start_all(ordered) -> bool:
                for spec in ordered:
                    assert isinstance(spec, ServiceSpec)
                    started.append(spec.name)
                return False

            mock_runner = MagicMock()
            mock_runner.start_all.side_effect = _fake_start_all
            orch = Orchestrator(runner=mock_runner, logs_dir=tmp_path / "issues")
            code = orch.run(stack, project_root=tmp_path)

        assert code == 1
        mock_runner.start_all.assert_called_once()
        assert started == ["auth"]
        assert "postgres" not in started
        assert "redis" not in started


class TestProcessManagerNeverSeesExternals:
    def test_ordered_specs_never_include_externals(self) -> None:
        stack = Stack()
        stack.external_dependency(
            name="postgres", type="postgresql", host="127.0.0.1", port=5432
        )
        stack.external_dependency(
            name="redis", type="redis", host="127.0.0.1", port=6379
        )
        stack.service(
            name="auth",
            path=".",
            command="true",
            depends_on=["postgres", "redis"],
        )
        stack.service(
            name="gateway",
            path=".",
            command="true",
            depends_on=["auth"],
        )
        ordered = build_graph(stack).ordered_specs()
        assert [s.name for s in ordered] == ["auth", "gateway"]
        assert all(isinstance(s, ServiceSpec) for s in ordered)

    def test_validate_does_not_call_process_manager(self) -> None:
        closed = _free_port()
        stack = _stack_with_externals(
            postgres=("127.0.0.1", closed),
            dependents=("auth",),
        )
        graph = build_graph(stack)
        with patch("stackpilot.process_manager.ProcessManager") as pm_cls:
            with pytest.raises(ExternalDependencyError):
                validate_external_dependencies(
                    graph,
                    ordered_services=graph.ordered_specs(),
                )
            pm_cls.assert_not_called()
