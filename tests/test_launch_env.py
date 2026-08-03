"""Regression tests for launch-environment fidelity and startup diagnostics."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from stackpilot.config import ExternalDependency, ServiceSpec
from stackpilot.issues import IssueTracker, parse_traceback_exception
from stackpilot.launch_env import (
    TracebackSummary,
    build_child_env,
    compare_launch_plans,
    expected_launch_plan,
    format_startup_failure_report,
    resolve_service_argv,
)
from stackpilot.logger import Logger
from stackpilot.models import ServiceState
from stackpilot.process_manager import ProcessManager
from stackpilot.utils import split_command


def _make_venv(root: Path) -> Path:
    """Create a minimal fake venv layout for the current platform."""

    venv = root / ".venv"
    if os.name == "nt" or sys.platform.startswith("win"):
        scripts = venv / "Scripts"
        scripts.mkdir(parents=True)
        exe = scripts / "python.exe"
    else:
        scripts = venv / "bin"
        scripts.mkdir(parents=True)
        exe = scripts / "python"
    exe.write_text("", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("home = /\n", encoding="utf-8")
    return exe


def test_resolve_python_uses_service_venv_not_sys_executable(tmp_path: Path) -> None:
    venv_python = _make_venv(tmp_path)
    argv = resolve_service_argv("python -m app", cwd=tmp_path)
    assert Path(argv[0]).resolve() == venv_python.resolve()
    assert argv[0] != sys.executable
    assert argv[1:] == ["-m", "app"]


def test_resolve_python_uses_monorepo_root_venv(tmp_path: Path) -> None:
    """Services under services/ must pick up the repo-root editable venv."""

    venv_python = _make_venv(tmp_path)
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    service = tmp_path / "services" / "admin_service"
    service.mkdir(parents=True)
    argv = resolve_service_argv("python -m uvicorn app.main:app", cwd=service)
    assert Path(argv[0]).resolve() == venv_python.resolve()
    env = build_child_env(service, base={"PATH": "/usr/bin", "HOME": "/tmp"})
    assert Path(env["VIRTUAL_ENV"]).resolve() == (tmp_path / ".venv").resolve()


def test_resolve_relative_venv_path_against_cwd(tmp_path: Path) -> None:
    """Windows cannot CreateProcess relative ../.venv paths; absolutize them."""

    venv_python = _make_venv(tmp_path)
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    service = tmp_path / "services" / "admin_service"
    service.mkdir(parents=True)
    argv = resolve_service_argv(
        "../../.venv/Scripts/python.exe -m uvicorn app.main:app"
        if (tmp_path / ".venv" / "Scripts").exists()
        else "../../.venv/bin/python -m uvicorn app.main:app",
        cwd=service,
    )
    assert Path(argv[0]).is_absolute()
    assert Path(argv[0]).resolve() == venv_python.resolve()


def test_split_command_with_cwd_prefers_venv(tmp_path: Path) -> None:
    venv_python = _make_venv(tmp_path)
    argv = split_command("python -c pass", cwd=tmp_path)
    assert Path(argv[0]).resolve() == venv_python.resolve()


def test_build_child_env_sets_virtual_env_and_path(tmp_path: Path) -> None:
    _make_venv(tmp_path)
    env = build_child_env(tmp_path, base={"PATH": "/usr/bin", "HOME": "/tmp"})
    assert "VIRTUAL_ENV" in env
    assert Path(env["VIRTUAL_ENV"]).resolve() == (tmp_path / ".venv").resolve()
    assert str(tmp_path / ".venv") in env["PATH"] or ".venv" in env["PATH"].replace(
        "\\", "/"
    )


def test_build_child_env_loads_dotenv_without_override(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "PYTHONPATH=/from-dotenv\nALREADY=from-file\n",
        encoding="utf-8",
    )
    env = build_child_env(
        tmp_path,
        base={"PATH": "/usr/bin", "ALREADY": "from-parent"},
    )
    assert env["PYTHONPATH"] == "/from-dotenv"
    assert env["ALREADY"] == "from-parent"


def test_build_child_env_injects_service_urls_for_frontends(tmp_path: Path) -> None:
    env = build_child_env(
        tmp_path,
        base={"PATH": "/usr/bin"},
        services=(
            ServiceSpec(name="auth", path=tmp_path / "auth", port=8000),
            ServiceSpec(name="ai-service", path=tmp_path / "ai", port=8004),
        ),
        external_dependencies=(
            ExternalDependency(name="postgres", type="postgresql", host="127.0.0.1", port=5432),
        ),
    )
    assert env["STACKPILOT_AUTH_URL"] == "http://127.0.0.1:8000"
    assert env["STACKPILOT_AI_SERVICE_PORT"] == "8004"
    assert env["VITE_AUTH_URL"] == "http://127.0.0.1:8000"
    assert env["NEXT_PUBLIC_AI_SERVICE_URL"] == "http://127.0.0.1:8004"
    assert env["REACT_APP_AUTH_URL"] == "http://127.0.0.1:8000"
    assert env["PUBLIC_AUTH_URL"] == "http://127.0.0.1:8000"
    assert env["STACKPILOT_POSTGRES_HOST"] == "127.0.0.1"
    assert env["STACKPILOT_POSTGRES_PORT"] == "5432"


def test_build_child_env_overrides_stale_frontend_service_urls(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "NEXT_PUBLIC_GATEWAY_URL=http://localhost:8085\n"
        "NEXT_PUBLIC_HACKERRANK_SERVICE_URL=http://localhost:8004\n",
        encoding="utf-8",
    )
    env = build_child_env(
        tmp_path,
        base={"PATH": "/usr/bin"},
        services=(
            ServiceSpec(name="gateway", path=tmp_path / "gateway", port=8001),
            ServiceSpec(name="hackerrank_service", path=tmp_path / "hr", port=8004),
        ),
    )
    assert env["NEXT_PUBLIC_GATEWAY_URL"] == "http://127.0.0.1:8001"
    assert env["NEXT_PUBLIC_HACKERRANK_SERVICE_URL"] == "http://127.0.0.1:8004"


def test_compare_launch_plans_reports_differences_only(tmp_path: Path) -> None:
    _make_venv(tmp_path)
    spec = ServiceSpec(name="admin_service", path=tmp_path, command="python -m app")
    expected = expected_launch_plan(spec, base_env={"PATH": "/usr/bin"})
    # Simulate a buggy spawn that used StackPilot's interpreter and no venv.
    from stackpilot.launch_env import actual_launch_plan

    actual = actual_launch_plan(
        spec,
        argv=[sys.executable, "-m", "app"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin"},
    )
    comparison = compare_launch_plans(actual, expected)
    assert comparison.has_differences
    assert comparison.argv_differs
    keys = {d.key for d in comparison.differences}
    assert "VIRTUAL_ENV" in keys


def test_spawn_preserves_cwd_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}
    real_popen = __import__("subprocess").Popen

    class FakeProc:
        pid = 4242

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # Only intercept StackPilot service spawns (piped text mode).
            if kwargs.get("stdout") is None and not kwargs:
                # Positional CreateProcess-style calls should not reach here.
                pass
            if "cwd" in kwargs and kwargs.get("stdout") is not None:
                captured.update(kwargs)
                self.args = kwargs["args"]
                self.stdout = MagicMock()
                self.stderr = MagicMock()
                return
            # Delegate tree-kill / other subprocess uses to the real Popen.
            self._real = real_popen(*args, **kwargs)
            self.pid = self._real.pid
            self.args = self._real.args
            self.stdout = self._real.stdout
            self.stderr = self._real.stderr

        def poll(self) -> Optional[int]:
            if hasattr(self, "_real"):
                return self._real.poll()
            return None

        def terminate(self) -> None:
            if hasattr(self, "_real"):
                self._real.terminate()

        def wait(self, timeout: float | None = None) -> int:
            if hasattr(self, "_real"):
                return self._real.wait(timeout=timeout)
            return 0

        def kill(self) -> None:
            if hasattr(self, "_real"):
                self._real.kill()

    monkeypatch.setattr(
        "stackpilot.process_manager.subprocess.Popen",
        FakeProc,
    )
    # Avoid hanging pumps on MagicMock readline.
    monkeypatch.setattr(
        "stackpilot.process_manager.iter_text_lines",
        lambda stream: iter(()),
    )

    (tmp_path / ".env").write_text("PYTHONPATH=/proj/shared\n", encoding="utf-8")
    _make_venv(tmp_path)

    logger = Logger(tmp_path / "issues", service_names=["svc"], auto_cleanup=False)
    manager = ProcessManager(
        logger,
        services=(ServiceSpec(name="auth", path=tmp_path / "auth", port=8000),),
        stop_timeout_s=1.0,
    )
    spec = ServiceSpec(
        name="svc",
        path=tmp_path,
        command="python -c pass",
    )
    managed = manager.start(spec)

    assert captured["cwd"] == str(tmp_path.resolve())
    assert captured["shell"] is False
    assert "env" in captured
    assert captured["env"]["PYTHONPATH"] == "/proj/shared"
    assert "VIRTUAL_ENV" in captured["env"]
    assert captured["env"]["STACKPILOT_AUTH_URL"] == "http://127.0.0.1:8000"
    assert captured["env"]["VITE_AUTH_URL"] == "http://127.0.0.1:8000"
    assert managed.launch_cwd == str(tmp_path.resolve())
    assert managed.launch_argv is not None
    assert managed.launch_env is not None
    assert managed.state == ServiceState.RUNNING

    # Cleanup without invoking real taskkill against the fake PID.
    with manager._lock:
        managed.state = ServiceState.STOPPED
        managed.clear_runtime()
    logger.close()


def test_stderr_fully_collected_before_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pumps keep reading until EOF; traceback is not truncated."""

    lines: List[str] = []
    tb_lines = [
        "Traceback (most recent call last):",
        '  File "/usr/lib/python3.12/site-packages/uvicorn/server.py", line 1, in serve',
        "    run()",
        '  File "app/dependencies/auth.py", line 12, in <module>',
        "    from shared import tokens",
        'ModuleNotFoundError: No module named "shared"',
    ]

    class FakeStream:
        def __init__(self, payload: List[str]) -> None:
            self._payload = list(payload)
            self._i = 0

        def readline(self) -> str:
            if self._i >= len(self._payload):
                return ""
            line = self._payload[self._i]
            self._i += 1
            return line + "\n"

    class FakeProc:
        pid = 7

        def __init__(self, **kwargs: Any) -> None:
            self.args = kwargs["args"]
            self.stdout = FakeStream([])
            self.stderr = FakeStream(tb_lines)
            self._alive = True

        def poll(self) -> Optional[int]:
            return None if self._alive else 1

        def wait(self, timeout: float | None = None) -> int:
            self._alive = False
            return 1

        def terminate(self) -> None:
            self._alive = False

    monkeypatch.setattr(
        "stackpilot.process_manager.subprocess.Popen",
        FakeProc,
    )

    logger = Logger(
        tmp_path / "issues",
        service_names=["admin_service"],
        print_fn=lines.append,
        color=False,
        auto_cleanup=False,
    )
    manager = ProcessManager(
        logger,
        services=(ServiceSpec(name="auth", path=tmp_path / "auth", port=8000),),
        stop_timeout_s=1.0,
    )
    spec = ServiceSpec(
        name="admin_service",
        path=tmp_path,
        command="python -m app",
    )
    manager.start(spec)
    # Simulate natural crash: mark failed via reap after "exit".
    managed = manager.get("admin_service")
    assert managed.process is not None
    managed.process._alive = False  # type: ignore[attr-defined]
    failed = manager.reap_exited()
    assert "admin_service" in failed
    manager.wait_for_output("admin_service", timeout_s=2.0)
    time.sleep(0.05)

    joined = "\n".join(lines)
    for fragment in tb_lines:
        assert fragment in joined

    parsed = logger.issue_tracker.last_exception("admin_service")
    assert parsed is not None
    assert parsed.exception_type == "ModuleNotFoundError"
    assert parsed.exception_message == 'No module named "shared"'
    assert parsed.file_line == "app/dependencies/auth.py:12"

    manager.stop_all()
    logger.close()


def test_issue_parser_skips_uvicorn_frame() -> None:
    tb = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "/opt/venv/lib/python3.12/site-packages/uvicorn/config.py", line 10, in load',
            "    self.loaded_app = import_from_string(self.app)",
            '  File "app/dependencies/auth.py", line 12, in <module>',
            "    from shared import tokens",
            'ModuleNotFoundError: No module named "shared"',
        ]
    )
    parsed = parse_traceback_exception(tb)
    assert parsed.exception_type == "ModuleNotFoundError"
    assert parsed.exception_message == 'No module named "shared"'
    assert parsed.file_line == "app/dependencies/auth.py:12"


def test_startup_failure_diagnostics_format(tmp_path: Path) -> None:
    _make_venv(tmp_path)
    spec = ServiceSpec(
        name="admin_service",
        path=tmp_path,
        command="python -m uvicorn app:app",
    )
    expected = expected_launch_plan(spec, base_env={"PATH": "/bin"})
    from stackpilot.launch_env import actual_launch_plan

    actual = actual_launch_plan(
        spec,
        argv=[sys.executable, "-m", "uvicorn", "app:app"],
        cwd=tmp_path,
        env={"PATH": "/bin"},
    )
    comparison = compare_launch_plans(actual, expected)
    summary = TracebackSummary(
        exception_type="ModuleNotFoundError",
        exception_message='No module named "shared"',
        file_line="app/dependencies/auth.py:12",
    )
    text = format_startup_failure_report(
        service="admin_service",
        cwd=tmp_path,
        command=spec.command,
        python_executable=sys.executable,
        comparison=comparison,
        summary=summary,
    )
    assert "Application startup failed" in text
    assert "admin_service" in text
    assert "Working Directory:" in text
    assert "Command:" in text
    assert "Python Executable:" in text
    assert "Environment Differences:" in text
    assert "ModuleNotFoundError" in text
    assert 'No module named "shared"' in text
    assert "app/dependencies/auth.py:12" in text
    assert "Likely Cause:" in text


def test_process_cleanup_still_works_after_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeProc:
        pid = 99

        def __init__(self, **kwargs: Any) -> None:
            self.args = kwargs["args"]
            self.stdout = MagicMock()
            self.stderr = MagicMock()
            self._code: Optional[int] = None

        def poll(self) -> Optional[int]:
            return self._code

        def terminate(self) -> None:
            self._code = 0

        def wait(self, timeout: float | None = None) -> int:
            self._code = 0
            return 0

        def kill(self) -> None:
            self._code = -9

    monkeypatch.setattr(
        "stackpilot.process_manager.subprocess.Popen",
        FakeProc,
    )
    monkeypatch.setattr(
        "stackpilot.process_manager.iter_text_lines",
        lambda stream: iter(()),
    )
    monkeypatch.setattr(
        "stackpilot.process_manager.signal_process_tree",
        lambda *a, **k: None,
    )

    logger = Logger(tmp_path / "issues", service_names=["x"], auto_cleanup=False)
    manager = ProcessManager(logger, stop_timeout_s=0.5)
    managed = manager.start(
        ServiceSpec(name="x", path=tmp_path, command="python -c pass")
    )
    assert managed.state == ServiceState.RUNNING
    manager.wait_for_output("x", timeout_s=0.2)
    stopped = manager.stop("x")
    assert stopped.state == ServiceState.STOPPED
    assert manager.state_of("x") == ServiceState.STOPPED
    logger.close()


def test_ingest_preserves_long_traceback(tmp_path: Path) -> None:
    tracker = IssueTracker(tmp_path / "issues", auto_cleanup=False)
    lines = ["Traceback (most recent call last):"]
    for i in range(50):
        lines.append(f'  File "app/module_{i}.py", line {i}, in f')
        lines.append("    x()")
    lines.append('ModuleNotFoundError: No module named "shared"')
    for line in lines:
        tracker.ingest_stderr("svc", line)
    parsed = tracker.last_exception("svc")
    assert parsed is not None
    assert parsed.exception_type == "ModuleNotFoundError"
    assert parsed.file_line == "app/module_49.py:49"
    tracker.close()
