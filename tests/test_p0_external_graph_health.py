"""P0 release blockers: external edges, Windows graph, health failure UX."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from stackpilot.cli import _print_architecture_report, app
from stackpilot.config import ExternalDependency, HttpHealthCheck, ServiceSpec, Stack
from stackpilot.dependency_graph import build_graph
from stackpilot.diagnostics.errors import (
    format_health_http_failure,
    format_health_timeout,
)
from stackpilot.discovery import STACKFILE_NAME
from stackpilot.generator import generate_stackfile
from stackpilot.graph_view import format_architecture_report
from stackpilot.http_checker import HttpProbeResult
from stackpilot.relation_infer import (
    fill_missing_stack_dependencies,
    infer_service_dependencies,
)
from stackpilot.runner import Runner
from stackpilot.scanner import scan_project


runner = CliRunner()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _app_spec(tmp: Path, name: str, *, env: str = "", port: int = 8000) -> ServiceSpec:
    root = tmp / name
    root.mkdir(parents=True, exist_ok=True)
    _write(root / "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    if env:
        _write(root / ".env", env)
    return ServiceSpec(
        name=name,
        path=root,
        command=f"uvicorn main:app --port {port}",
        port=port,
    )


def _externals() -> list[ExternalDependency]:
    return [
        ExternalDependency(name="redis", type="redis", host="127.0.0.1", port=6379),
        ExternalDependency(
            name="postgres", type="postgresql", host="127.0.0.1", port=5432
        ),
        ExternalDependency(
            name="mongodb", type="mongodb", host="127.0.0.1", port=27017
        ),
        ExternalDependency(
            name="rabbitmq", type="rabbitmq", host="127.0.0.1", port=5672
        ),
    ]


# ---------------------------------------------------------------------------
# P0-1 / P0-4 — external dependency generation + graph edges
# ---------------------------------------------------------------------------


class TestExternalDependencyGeneration:
    def test_infers_redis_postgres_mongodb_rabbitmq_from_env(
        self, tmp_path: Path
    ) -> None:
        auth = _app_spec(
            tmp_path, "auth", env="REDIS_URL=redis://redis:6379/0\n", port=8101
        )
        payments = _app_spec(
            tmp_path,
            "payments",
            env="DATABASE_URL=postgresql://user:pass@postgres:5432/app\n",
            port=8102,
        )
        analytics = _app_spec(
            tmp_path,
            "analytics",
            env="MONGODB_URL=mongodb://mongodb:27017/app\n",
            port=8103,
        )
        notifications = _app_spec(
            tmp_path,
            "notifications",
            env="AMQP_URL=amqp://guest:guest@rabbitmq:5672/\n",
            port=8104,
        )
        deps = infer_service_dependencies(
            project_root=tmp_path,
            services=[auth, payments, analytics, notifications],
            external_dependencies=_externals(),
        )
        assert "redis" in deps["auth"]
        assert "postgres" in deps["payments"]
        assert "mongodb" in deps["analytics"]
        assert "rabbitmq" in deps["notifications"]

    def test_sync_stackfile_emits_external_depends_on(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "docker-compose.yml",
            "services:\n"
            "  redis:\n"
            "    image: redis:7\n"
            "    ports: ['6379:6379']\n"
            "  postgres:\n"
            "    image: postgres:16\n"
            "    ports: ['5432:5432']\n"
            "  mongo:\n"
            "    image: mongo:6\n"
            "    ports: ['27017:27017']\n"
            "  rabbitmq:\n"
            "    image: rabbitmq:3\n"
            "    ports: ['5672:5672']\n",
        )
        _write(
            tmp_path / "auth" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        _write(tmp_path / "auth" / ".env", "REDIS_URL=redis://redis:6379/0\n")
        _write(
            tmp_path / "payments" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        _write(
            tmp_path / "payments" / ".env",
            "DATABASE_URL=postgresql://u:p@postgres:5432/app\n",
        )
        _write(
            tmp_path / "analytics" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        _write(
            tmp_path / "analytics" / ".env",
            "MONGODB_URL=mongodb://mongodb:27017/app\n",
        )
        _write(
            tmp_path / "notifications" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        _write(
            tmp_path / "notifications" / ".env",
            "AMQP_URL=amqp://guest:guest@rabbitmq:5672/\n",
        )

        services = scan_project(tmp_path)
        text = generate_stackfile(services, project_root=tmp_path)

        assert "stack.external_dependency(" in text
        assert 'type="redis"' in text
        assert 'type="postgresql"' in text
        assert 'type="mongodb"' in text
        assert 'type="rabbitmq"' in text

        # Extract depends_on for each app.
        assert 'name="auth"' in text
        assert 'name="payments"' in text
        assert 'name="analytics"' in text
        assert 'name="notifications"' in text

        # Parse depends_on lines near each service by simple scanning.
        lines = text.splitlines()
        current: str | None = None
        by_service: dict[str, list[str]] = {}
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("name=") and '"' in stripped:
                current = stripped.split('"', 2)[1]
            if current and "depends_on=" in stripped:
                by_service[current] = stripped
                current = None

        assert "redis" in by_service.get("auth", "")
        assert "postgres" in by_service.get("payments", "")
        assert "mongodb" in by_service.get("analytics", "")
        assert "rabbitmq" in by_service.get("notifications", "")

    def test_external_dependency_graph_shows_edges(self, tmp_path: Path) -> None:
        auth = _app_spec(
            tmp_path, "auth", env="REDIS_URL=redis://redis:6379/0\n", port=8101
        )
        stack = Stack()
        for dep in _externals():
            stack.external_dependency(
                name=dep.name,
                type=dep.type,
                host=dep.host,
                port=dep.port,
            )
        stack.service(
            name="auth",
            path=str(auth.path),
            command=auth.command,
            port=8101,
            depends_on=["users"],  # app edge present; redis should still be filled
        )
        # users is missing from stack — use empty + fill path instead
        stack = Stack()
        for dep in _externals():
            stack.external_dependency(
                name=dep.name,
                type=dep.type,
                host=dep.host,
                port=dep.port,
            )
        stack.service(
            name="auth",
            path=str(auth.path),
            command=auth.command,
            port=8101,
        )
        filled = fill_missing_stack_dependencies(stack, project_root=tmp_path)
        assert "redis" in filled.services[0].depends_on

        graph = build_graph(filled)
        report = format_architecture_report(graph, unicode=False)
        assert "auth" in report
        assert "redis" in report.lower() or "Redis" in report
        assert graph.is_external("redis")
        assert any(d.name == "redis" for d in graph.required_externals())

    def test_fill_appends_external_to_existing_depends_on(
        self, tmp_path: Path
    ) -> None:
        auth = _app_spec(
            tmp_path,
            "auth",
            env=(
                "USERS_SERVICE_URL=http://users:8102\n"
                "REDIS_URL=redis://redis:6379/0\n"
            ),
            port=8101,
        )
        users = _app_spec(tmp_path, "users", port=8102)
        stack = Stack()
        stack.external_dependency(
            name="redis", type="redis", host="127.0.0.1", port=6379
        )
        stack.service(
            name="users",
            path=str(users.path),
            command=users.command,
            port=8102,
        )
        stack.service(
            name="auth",
            path=str(auth.path),
            command=auth.command,
            port=8101,
            depends_on=["users"],
        )
        filled = fill_missing_stack_dependencies(stack, project_root=tmp_path)
        by_name = {s.name: s for s in filled.services}
        assert "users" in by_name["auth"].depends_on
        assert "redis" in by_name["auth"].depends_on


# ---------------------------------------------------------------------------
# P0-2 / P0-4 — graph on Windows (cp1252 / Rich failure)
# ---------------------------------------------------------------------------


class TestGraphWindowsSafe:
    def test_print_architecture_report_falls_back_on_cp1252(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack = Stack()
        stack.external_dependency(
            name="postgres", type="postgresql", host="127.0.0.1", port=5432
        )
        stack.service(
            name="api",
            path=".",
            command="uvicorn api:app",
            depends_on=["postgres"],
            port=8000,
        )
        graph = build_graph(stack)
        unicode_report = format_architecture_report(graph, unicode=True)
        ascii_report = format_architecture_report(graph, unicode=False)

        class _Cp1252(io.TextIOBase):
            encoding = "cp1252"

            def __init__(self) -> None:
                self.chunks: list[str] = []

            def write(self, s: str) -> int:  # type: ignore[override]
                s.encode("cp1252")
                self.chunks.append(s)
                return len(s)

            def flush(self) -> None:
                return None

        sink = _Cp1252()
        monkeypatch.setattr(sys, "stdout", sink)

        # Force Rich path to raise so we exercise the broad fallback.
        with patch("rich.console.Console") as console_cls:
            console_cls.return_value.print.side_effect = UnicodeEncodeError(
                "cp1252", "🟢", 0, 1, "ordinal not in range"
            )
            _print_architecture_report(
                unicode_report, ascii_fallback=ascii_report
            )

        joined = "".join(sink.chunks)
        assert "Traceback" not in joined
        assert "api" in joined
        assert "🟢" not in joined

    def test_print_architecture_report_never_raises_on_rich_crash(self) -> None:
        ascii_report = "Architecture\napi\nStartup order: api"
        with patch("rich.console.Console") as console_cls:
            console_cls.side_effect = RuntimeError("rich exploded")
            # Must not raise.
            _print_architecture_report(
                "Architecture\n🟢 api\n",
                ascii_fallback=ascii_report,
            )

    def test_graph_cli_exits_clean_under_forced_encoding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(
            tmp_path / STACKFILE_NAME,
            "from stackpilot import Stack, HttpHealthCheck\n"
            "stack = Stack()\n"
            "stack.external_dependency("
            "name='redis', type='redis', host='127.0.0.1', port=6379)\n"
            "stack.service(\n"
            "  name='api', path='.', command='true',\n"
            "  depends_on=['redis'], port=8000,\n"
            "  health_check=HttpHealthCheck(url='http://127.0.0.1:8000/health'),\n"
            ")\n",
        )
        monkeypatch.chdir(tmp_path)

        with patch(
            "stackpilot.cli._print_architecture_report",
            side_effect=lambda report, **kwargs: None,
        ):
            result = runner.invoke(app, ["graph"])
        assert result.exit_code == 0
        assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# P0-3 / P0-4 — health 404 / 500 / timeout UX
# ---------------------------------------------------------------------------


class TestHealthFailureUx:
    def test_timeout_message_shape(self) -> None:
        text = format_health_timeout(
            service="api",
            health_url="http://127.0.0.1:8000/missing",
            timeout_s=5.0,
        )
        assert "Problem:" in text
        assert "Reason:" in text
        assert "Suggested fix:" in text
        assert "Traceback" not in text
        assert "asyncio" not in text.lower()

    def test_wrong_health_url_404_shape(self) -> None:
        text = format_health_http_failure(
            service="api",
            health_url="http://127.0.0.1:8000/nope",
            kind="not_found",
            detail="404 Not Found",
            configured_path="/nope",
            discovered_routes=["/health", "/ready"],
        )
        assert "Problem: Health endpoint not found" in text
        assert "Reason:" in text
        assert "404" in text
        assert "Suggested fix:" in text
        assert "/health" in text
        assert "Traceback" not in text

    def test_http_500_shape(self) -> None:
        text = format_health_http_failure(
            service="payments",
            health_url="http://127.0.0.1:8001/health",
            kind="failed",
            detail="500 Internal Server Error",
        )
        assert "Problem: Health check failed" in text
        assert "500" in text
        assert "Suggested fix:" in text
        assert "Traceback" not in text

    def test_probe_timeout_shape(self) -> None:
        text = format_health_http_failure(
            service="api",
            health_url="http://127.0.0.1:8000/health",
            kind="timeout",
        )
        assert "Problem: Health check timed out" in text
        assert "Reason:" in text
        assert "Suggested fix:" in text

    def test_runner_prints_problem_reason_fix_on_404(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        spec = ServiceSpec(
            name="api",
            path=tmp_path,
            command="true",
            health_check=HttpHealthCheck(
                url="http://127.0.0.1:9/missing",
                timeout=0.1,
                probe_timeout=0.05,
            ),
        )
        process = MagicMock()
        process.poll.return_value = None

        runner_obj = Runner(poll_interval_s=0.01)
        with patch(
            "stackpilot.runner.probe_http",
            return_value=HttpProbeResult(
                kind="not_found",
                status_code=404,
                detail="404 Not Found",
            ),
        ):
            runner_obj._print_health_failure(spec, process=process)

        out = capsys.readouterr().out
        assert "Problem:" in out
        assert "Reason:" in out
        assert "Suggested fix:" in out
        assert "Traceback" not in out
        assert "asyncio" not in out.lower()

    def test_runner_prints_problem_reason_fix_on_500(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        spec = ServiceSpec(
            name="api",
            path=tmp_path,
            command="true",
            health_check=HttpHealthCheck(
                url="http://127.0.0.1:9/health",
                timeout=0.1,
                probe_timeout=0.05,
            ),
        )
        process = MagicMock()
        process.poll.return_value = None

        runner_obj = Runner(poll_interval_s=0.01)
        with patch(
            "stackpilot.runner.probe_http",
            return_value=HttpProbeResult(
                kind="failed",
                status_code=500,
                detail="500 Internal Server Error",
            ),
        ):
            runner_obj._print_health_failure(spec, process=process)

        out = capsys.readouterr().out
        assert "Problem: Health check failed" in out
        assert "Reason:" in out
        assert "Suggested fix:" in out
        assert "Traceback" not in out
