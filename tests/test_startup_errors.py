"""P1 regression: friendly startup / CLI error messages (no raw tracebacks)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stackpilot.cli import app
from stackpilot.diagnostics.errors import (
    classify_spawn_error,
    format_health_timeout,
    format_spawn_failure,
    format_user_error,
)
from stackpilot.discovery import STACKFILE_NAME

runner = CliRunner()


def _write_stackfile(directory: Path, body: str) -> Path:
    path = directory / STACKFILE_NAME
    path.write_text(body, encoding="utf-8")
    return path


class TestFriendlyErrorFormat:
    def test_user_error_shape(self) -> None:
        text = format_user_error(
            problem="Port already in use",
            reason="Another process is bound.",
            suggested_fix="Free the port.",
            service="api",
        )
        assert "Problem: Port already in use" in text
        assert "Affected service: api" in text
        assert "Reason: Another process is bound." in text
        assert "Suggested fix: Free the port." in text
        assert "Traceback" not in text

    def test_spawn_missing_executable(self) -> None:
        text = format_spawn_failure(
            service="auth",
            exc=FileNotFoundError(2, "No such file"),
            command="missing-bin --flag",
        )
        assert "Problem: Executable not found" in text
        assert "Affected service: auth" in text
        assert "Suggested fix:" in text
        assert "Traceback" not in text

    def test_spawn_permission_denied(self) -> None:
        text = format_spawn_failure(
            service="web",
            exc=PermissionError("denied"),
            command="python app.py",
        )
        assert "Problem: Permission denied" in text
        assert "Affected service: web" in text

    def test_spawn_port_in_use(self) -> None:
        exc = OSError(98, "Address already in use")
        problem, reason, fix = classify_spawn_error(exc)
        assert problem == "Port already in use"
        assert "bound" in reason.lower() or "port" in reason.lower()
        assert "doctor" in fix

    def test_spawn_invalid_command(self) -> None:
        problem, reason, fix = classify_spawn_error(ValueError("Service command is empty"))
        assert problem == "Invalid command"
        assert "empty" in reason.lower()
        assert "command=" in fix

    def test_spawn_invalid_cwd(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone"
        text = format_spawn_failure(
            service="api",
            exc=FileNotFoundError(2, "No such file", str(missing)),
            command="python main.py",
            cwd=missing,
        )
        assert "Problem: Invalid working directory" in text or "Executable not found" in text
        assert "Traceback" not in text

    def test_health_timeout_message(self) -> None:
        text = format_health_timeout(
            service="api",
            health_url="http://127.0.0.1:8000/health",
            timeout_s=30,
        )
        assert "Problem: Health endpoint missing or unhealthy" in text
        assert "Affected service: api" in text
        assert "Suggested fix:" in text


class TestCliStartupErrors:
    def test_missing_executable_no_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = tmp_path / "svc"
        svc.mkdir()
        _write_stackfile(
            tmp_path,
            "from stackpilot import Stack\n"
            "stack = Stack()\n"
            "stack.service(\n"
            "    name='api',\n"
            f"    path=r'{svc}',\n"
            "    command='this-binary-does-not-exist-xyz',\n"
            ")\n"
            "stack.run()\n",
        )
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["run"])
        combined = (result.output or "") + (getattr(result, "stderr", None) or "")
        assert result.exit_code == 1
        assert "Traceback" not in combined
        assert "Problem:" in combined
        assert "Suggested fix:" in combined
        assert "api" in combined

    def test_empty_command_friendly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = tmp_path / "svc"
        svc.mkdir()
        _write_stackfile(
            tmp_path,
            "from stackpilot import Stack\n"
            "stack = Stack()\n"
            "stack.service(\n"
            "    name='api',\n"
            f"    path=r'{svc}',\n"
            "    command='',\n"
            ")\n"
            "stack.run()\n",
        )
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["run"])
        combined = (result.output or "") + (getattr(result, "stderr", None) or "")
        assert result.exit_code == 1
        assert "Traceback" not in combined
        assert "Problem:" in combined
        assert "Suggested fix:" in combined

    def test_path_escape_friendly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        project = tmp_path / "proj"
        project.mkdir()
        _write_stackfile(
            project,
            "from stackpilot import Stack\n"
            "stack = Stack()\n"
            f"stack.service(name='x', path=r'{outside}', command='python -c pass')\n"
            "stack.run()\n",
        )
        monkeypatch.chdir(project)
        result = runner.invoke(app, ["run"])
        combined = (result.output or "") + (getattr(result, "stderr", None) or "")
        assert result.exit_code == 1
        assert "Traceback" not in combined
        assert "Problem: Configuration error" in combined or "escapes project root" in combined
        assert "Suggested fix:" in combined

    def test_dependency_unavailable_friendly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        svc = tmp_path / "svc"
        svc.mkdir()
        _write_stackfile(
            tmp_path,
            "from stackpilot import Stack, TcpHealthCheck\n"
            "stack = Stack()\n"
            "stack.external_dependency(\n"
            "    name='postgres',\n"
            "    type='postgresql',\n"
            "    host='127.0.0.1',\n"
            f"    port={port},\n"
            "    retries=2,\n"
            "    retry_delay=0.01,\n"
            f"    health_check=TcpHealthCheck(host='127.0.0.1', port={port}, "
            "timeout=0.2, interval=0.01, probe_timeout=0.05),\n"
            ")\n"
            "stack.service(\n"
            "    name='api',\n"
            f"    path=r'{svc}',\n"
            "    command='python -c \"print(1)\"',\n"
            "    depends_on=['postgres'],\n"
            ")\n"
            "stack.run()\n",
        )
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["run"])
        combined = (result.output or "") + (getattr(result, "stderr", None) or "")
        assert result.exit_code == 1
        assert "Traceback" not in combined
        assert "Dependency unavailable" in combined or "not reachable" in combined
        assert "Suggested fix:" in combined
        assert str(port) in combined
