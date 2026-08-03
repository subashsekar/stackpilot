"""CLI UX tests: status, ps, issues — Day 11 runtime management."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stackpilot.cli import app
from stackpilot.discovery import STACKFILE_NAME
from stackpilot.status import (
    derive_health,
    detect_framework,
    format_ps_table,
    format_status_report,
    format_uptime,
    load_runtime_snapshot,
    pid_is_alive,
    runtime_status_path,
)

runner = CliRunner()


def _seed_issue_log(path: Path, service: str, root_cause: str) -> None:
    _ = service
    path.write_text(
        "TIME       STATUS   ERROR                          FILE:LINE\n"
        "-----------------------------------------------------------------------\n"
        f"12:00:00   ACTIVE   {root_cause:<30}   -\n",
        encoding="utf-8",
    )


def _write_stackfile(directory: Path) -> Path:
    body = """\
from stackpilot import Stack

stack = Stack()
stack.service(
    name="auth",
    path=".",
    command="uvicorn app:app --port 8001",
)
stack.service(
    name="gateway",
    path=".",
    command="python main.py",
    port=8000,
    depends_on=["auth"],
)
stack.run()
"""
    path = directory / STACKFILE_NAME
    path.write_text(body, encoding="utf-8")
    return path


def _write_runtime(directory: Path, *, alive_pid: int | None = None) -> Path:
    pid = alive_pid if alive_pid is not None else 424242
    payload = {
        "project": directory.name,
        "project_root": str(directory.resolve()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "session_active": True,
        "services": [
            {
                "name": "auth",
                "pid": pid,
                "port": 8001,
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "uptime": 12.0,
                "framework": "uvicorn",
                "command": "uvicorn app:app --port 8001",
                "exit_code": None,
                "health": "healthy",
            },
            {
                "name": "gateway",
                "pid": None,
                "port": 8000,
                "status": "failed",
                "started_at": None,
                "uptime": None,
                "framework": "python",
                "command": "python main.py",
                "exit_code": 1,
                "health": "unhealthy",
            },
        ],
    }
    path = runtime_status_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestDetectFramework:
    def test_uvicorn_and_python(self) -> None:
        assert detect_framework("uvicorn app:app --reload") == "uvicorn"
        assert detect_framework('python -c "print(1)"') == "python"
        assert detect_framework("flask run") == "flask"
        assert detect_framework("") == "-"


class TestDeriveHealth:
    def test_status_mapping(self) -> None:
        assert derive_health("running") == "healthy"
        assert derive_health("failed") == "unhealthy"
        assert derive_health("stopped") == "stopped"
        assert derive_health("starting") == "starting"


class TestPidLiveness:
    def test_current_process_is_alive(self) -> None:
        assert pid_is_alive(os.getpid()) is True
        assert pid_is_alive(0) is False
        assert pid_is_alive(-1) is False

    def test_load_snapshot_keeps_live_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_stackfile(tmp_path)
        _write_runtime(tmp_path, alive_pid=os.getpid())
        monkeypatch.chdir(tmp_path)
        snap = load_runtime_snapshot(tmp_path)
        assert snap is not None
        assert snap["session_active"] is True
        auth = next(s for s in snap["services"] if s["name"] == "auth")
        assert auth["status"] == "running"
        assert auth["pid"] == os.getpid()
        assert auth["health"] == "healthy"


class TestStatusFormatting:
    def test_status_report_table(self) -> None:
        text = format_status_report(
            project_name="demo",
            session_active=True,
            services=[
                {
                    "name": "auth",
                    "framework": "uvicorn",
                    "port": 8001,
                    "pid": 99,
                    "status": "running",
                    "uptime": 65,
                    "health": "healthy",
                }
            ],
        )
        assert "Project: demo" in text
        assert "Running services: 1" in text
        assert "Healthy services: 1" in text
        assert "Applications" in text
        assert "SERVICE" in text
        assert "STATUS" in text
        # Column order: Service | Status | PID | Port | Uptime | Framework
        header = next(line for line in text.splitlines() if line.startswith("SERVICE"))
        assert header.index("STATUS") < header.index("PID")
        assert header.index("PID") < header.index("PORT")
        assert header.index("PORT") < header.index("UPTIME")
        assert header.index("UPTIME") < header.index("FRAMEWORK")
        assert "auth" in text
        assert "uvicorn" in text
        assert "8001" in text
        assert format_uptime(65) == "1m05s"

    def test_ps_only_active(self) -> None:
        text = format_ps_table(
            [
                {"name": "auth", "pid": 1, "port": 8001, "status": "running"},
                {"name": "dead", "pid": None, "port": 9, "status": "stopped"},
            ]
        )
        assert "auth" in text
        assert "dead" not in text
        assert "PID" in text


class TestCliStatusPsLogs:
    def test_status_without_runtime_shows_stopped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_stackfile(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "Project:" in result.output
        assert "auth" in result.output
        assert "gateway" in result.output
        assert "stopped" in result.output
        assert "StackPilot is not running." in result.output
        assert "stackpilot run" in result.output

    def test_status_with_runtime_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_stackfile(tmp_path)
        _write_runtime(tmp_path, alive_pid=os.getpid())
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "Running services: 1" in result.output
        assert "Healthy services: 1" in result.output
        assert "Failed services: 1" in result.output
        assert "uvicorn" in result.output
        assert "failed" in result.output

    def test_ps_lists_active_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_stackfile(tmp_path)
        _write_runtime(tmp_path, alive_pid=os.getpid())
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["ps"])
        assert result.exit_code == 0
        assert "auth" in result.output
        assert "gateway" not in result.output
        assert str(os.getpid()) in result.output

    def test_ps_empty_when_not_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_stackfile(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["ps"])
        assert result.exit_code == 0
        assert "No active StackPilot processes." in result.output

    def test_issues_lists_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_stackfile(tmp_path)
        issues = tmp_path / ".stackpilot" / "issues"
        issues.mkdir(parents=True)
        _seed_issue_log(issues / "auth.issue", "auth", "auth boom")
        _seed_issue_log(issues / "gateway.issue", "gateway", "gw boom")
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["issues"])
        assert result.exit_code == 0
        assert "ACTIVE ISSUES" in result.output
        assert "auth boom" in result.output
        assert "gw boom" in result.output

    def test_issues_single_service(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_stackfile(tmp_path)
        issues = tmp_path / ".stackpilot" / "issues"
        issues.mkdir(parents=True)
        _seed_issue_log(issues / "auth.issue", "auth", "auth only")
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["issues", "auth"])
        assert result.exit_code == 0
        assert "ISSUES (auth)" in result.output
        assert "auth only" in result.output

    def test_issues_unknown_service(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_stackfile(tmp_path)
        (tmp_path / ".stackpilot" / "issues").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["issues", "missing"])
        assert result.exit_code == 1
        combined = result.output + (result.stderr or "")
        assert "Unknown service: 'missing'" in combined

    def test_issues_none_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_stackfile(tmp_path)
        (tmp_path / ".stackpilot" / "issues").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["issues"])
        assert result.exit_code == 0
        assert "No active service issues." in result.output

    def test_help_lists_runtime_commands(self) -> None:
        """Runtime commands remain on the frozen v0.1 public surface."""

        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "status" in result.output
        assert "ps" in result.output
        assert "issues" in result.output
