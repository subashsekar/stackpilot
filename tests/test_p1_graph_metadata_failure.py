"""P1 regressions: graph scale/sections, runtime metadata, failure UX."""

from __future__ import annotations

import time
from pathlib import Path

from stackpilot.config import Stack
from stackpilot.dependency_graph import build_graph
from stackpilot.graph_view import (
    format_architecture_report,
    format_connections,
    format_dependency_tree,
    format_external_infrastructure,
)
from stackpilot.issues import IssueTracker
from stackpilot.launch_env import TracebackSummary, format_startup_failure_report
from stackpilot.status import (
    detect_framework,
    detect_language,
    format_ps_table,
    format_status_report,
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _stack_mixed_frameworks(tmp_path: Path) -> Stack:
    """FastAPI, Django, Flask, Express, NestJS + Redis/Postgres/Mongo/RabbitMQ."""

    fastapi = tmp_path / "api"
    django = tmp_path / "admin"
    flask_dir = tmp_path / "web"
    express = tmp_path / "gateway_js"
    nest = tmp_path / "identity"
    for d in (fastapi, django, flask_dir, express, nest):
        d.mkdir(parents=True, exist_ok=True)

    _write(
        express / "package.json",
        '{"name":"gateway","dependencies":{"express":"^4.0.0"}}\n',
    )
    _write(express / "server.js", "const express = require('express');\n")
    _write(
        nest / "package.json",
        '{"name":"identity","dependencies":{"@nestjs/core":"^10.0.0"}}\n',
    )
    _write(nest / "nest-cli.json", '{"collection":"@nestjs/schematics"}\n')
    _write(nest / "src" / "main.ts", "async function bootstrap() {}\n")

    stack = Stack()
    for name, typ, port in (
        ("postgres", "postgresql", 5432),
        ("redis", "redis", 6379),
        ("mongo", "mongodb", 27017),
        ("rabbit", "rabbitmq", 5672),
    ):
        stack.external_dependency(name=name, type=typ, host="127.0.0.1", port=port)

    stack.service(
        name="api",
        path=str(fastapi),
        command="uvicorn app:app --port 8000",
        port=8000,
        depends_on=["postgres", "redis"],
    )
    stack.service(
        name="admin",
        path=str(django),
        command="python manage.py runserver 8001",
        port=8001,
        depends_on=["postgres"],
    )
    stack.service(
        name="web",
        path=str(flask_dir),
        command="flask run --port 8002",
        port=8002,
        depends_on=["redis"],
    )
    stack.service(
        name="gateway_js",
        path=str(express),
        command="node server.js",
        port=8003,
        depends_on=["api", "rabbit"],
    )
    stack.service(
        name="identity",
        path=str(nest),
        command="npm run start:dev",
        port=8004,
        depends_on=["mongo", "api"],
    )
    return stack


class TestP1GraphScaleAndSections:
    def test_50_services_readable_and_fast(self) -> None:
        stack = Stack()
        stack.external_dependency(
            name="redis", type="redis", host="127.0.0.1", port=6379
        )
        stack.external_dependency(
            name="postgres", type="postgresql", host="127.0.0.1", port=5432
        )
        prev = None
        for i in range(50):
            name = f"svc{i:02d}"
            deps = [prev] if prev else ["redis", "postgres"]
            if prev:
                deps = [prev, "redis"]
            stack.service(
                name=name,
                path=".",
                command=f"uvicorn {name}:app --port {8000 + i}",
                port=8000 + i,
                depends_on=deps,
            )
            prev = name

        began = time.perf_counter()
        graph = build_graph(stack)
        report = format_architecture_report(graph, unicode=False)
        ascii_ok = format_architecture_report(graph, unicode=True)
        elapsed = time.perf_counter() - began

        assert elapsed < 2.0
        assert "Applications" in report
        assert "External Infrastructure" in report
        assert "Connections" in report
        ext = format_external_infrastructure(graph, unicode=False)
        assert ext.count("Redis") == 1
        assert ext.count("PostgreSQL") == 1
        # Compact mode: externals omitted from the application tree.
        tree = format_dependency_tree(graph, unicode=False)
        assert "Redis" not in tree
        assert "svc00" in report
        assert "svc49" in report
        assert "Graph Generated Successfully" in ascii_ok

    def test_mixed_frameworks_and_externals_separated(self, tmp_path: Path) -> None:
        stack = _stack_mixed_frameworks(tmp_path)
        graph = build_graph(stack)
        report = format_architecture_report(graph, unicode=True)

        assert "Applications" in report
        assert "External Infrastructure" in report
        assert "Connections" in report
        for label in ("Redis", "PostgreSQL", "MongoDB", "RabbitMQ"):
            assert label in report
            assert format_external_infrastructure(graph).count(label) == 1

        tree = format_dependency_tree(graph)
        for label in ("Redis", "PostgreSQL", "MongoDB", "RabbitMQ"):
            assert label not in tree

        connections = format_connections(graph)
        assert "api →" in connections or "api ->" in connections
        assert "Redis" in connections or "redis" in connections.lower()

        assert "FastAPI" in report
        assert "Django" in report or "django" in report.lower()
        assert "Flask" in report
        assert "Express" in report or "Node" in report
        assert "NestJS" in report or "Node" in report


class TestP1RuntimeMetadata:
    def test_frameworks_never_dash_for_node(self) -> None:
        assert detect_framework("npm run start:dev") != "-"
        assert detect_framework("node server.js") != "-"
        assert detect_framework("npx nest start") == "nestjs"
        assert detect_framework("uvicorn app:app") in {"uvicorn", "fastapi"}
        assert detect_framework("flask run") == "flask"
        assert detect_framework("python manage.py runserver") == "django"

    def test_status_and_ps_columns(self) -> None:
        services = [
            {
                "name": "api",
                "status": "running",
                "pid": 11,
                "port": 8000,
                "uptime": 12,
                "health": "healthy",
                "framework": "uvicorn",
                "language": "Python",
                "command": "uvicorn app:app",
            },
            {
                "name": "gateway",
                "status": "running",
                "pid": 22,
                "port": 3000,
                "uptime": 10,
                "health": "healthy",
                "framework": "express",
                "language": "JavaScript",
                "command": "node server.js",
            },
            {
                "name": "identity",
                "status": "stopped",
                "pid": None,
                "port": 3001,
                "uptime": None,
                "health": "stopped",
                "framework": "nestjs",
                "language": "TypeScript",
                "command": "npm run start:dev",
            },
        ]
        status = format_status_report(
            project_name="demo",
            session_active=True,
            services=services,
        )
        for col in (
            "STATUS",
            "PID",
            "PORT",
            "HEALTH",
            "FRAMEWORK",
            "LANGUAGE",
        ):
            assert col in status
        assert "express" in status
        assert "nestjs" in status
        assert "JavaScript" in status
        assert "TypeScript" in status
        assert detect_framework("node server.js") != "-"

        ps = format_ps_table(services)
        assert "gateway" in ps
        assert "identity" not in ps
        assert "express" in ps
        assert "JavaScript" in ps
        assert detect_language("npm run start:dev", framework="nestjs") == "TypeScript"


class TestP1FailureUx:
    def test_application_output_then_problem_reason_fix(self, tmp_path: Path) -> None:
        tb = (
            "Traceback (most recent call last):\n"
            '  File "main.py", line 1, in <module>\n'
            "    raise RuntimeError('boom')\n"
            "RuntimeError: boom"
        )
        text = format_startup_failure_report(
            service="api",
            cwd=tmp_path,
            command="uvicorn main:app",
            python_executable="python",
            summary=TracebackSummary(
                exception_type="RuntimeError",
                exception_message="boom",
                file_line="main.py:1",
            ),
            application_output=tb,
        )
        assert text.index("Application Output") < text.index("Problem:")
        assert text.index("Problem:") < text.index("Reason:")
        assert text.index("Reason:") < text.index("Suggested fix:")
        assert "RuntimeError: boom" in text
        assert "Traceback (most recent call last)" in text
        assert "stackpilot/" not in text.lower() or "stackpilot/runner" not in text

    def test_issue_tracker_preserves_application_output(self, tmp_path: Path) -> None:
        tracker = IssueTracker(tmp_path / "issues", auto_cleanup=False)
        try:
            tracker.ingest_stderr("api", "Traceback (most recent call last):")
            tracker.ingest_stderr(
                "api", '  File "main.py", line 1, in <module>'
            )
            tracker.ingest_stderr("api", "ValueError: bad")
            out = tracker.last_application_output("api")
            assert out is not None
            assert "Traceback (most recent call last)" in out
            assert "ValueError: bad" in out

            tracker.ingest_stderr("web", "Error: Cannot find module 'x'")
            tracker.ingest_stderr("web", "    at Object.<anonymous> (server.js:1:1)")
            node_out = tracker.last_application_output("web")
            assert node_out is not None
            assert "Cannot find module" in node_out
            assert "at Object" in node_out
        finally:
            tracker.close()
