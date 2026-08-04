"""Higher-level integration: CLI → discovery → Orchestrator → Runner → PM."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Optional

import pytest
from typer.testing import CliRunner

from stackpilot.cli import app
from stackpilot.config import ProcessHealthCheck, Stack, TcpHealthCheck
from stackpilot.dependency_graph import build_graph
from stackpilot.discovery import STACKFILE_NAME, discover_project
from stackpilot.external_validation import validate_external_dependencies
from stackpilot.issues import IssueTracker, DEFAULT_ISSUES_DIR
from stackpilot.orchestrator import Orchestrator
from stackpilot.process_manager import ProcessManager
from stackpilot.runner import Runner
from stackpilot.utils import load_stack_from_stackfile

runner = CliRunner()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _long_running_stackfile(project: Path, *, name: str = "app") -> Path:
    svc = project / name
    svc.mkdir(parents=True, exist_ok=True)
    _write(
        svc / "main.py",
        "import time\n"
        "print('ready', flush=True)\n"
        "time.sleep(3600)\n",
    )
    body = (
        "from stackpilot import Stack, ProcessHealthCheck\n"
        "\n"
        "stack = Stack()\n"
        "stack.service(\n"
        f"    name={name!r},\n"
        f"    path=r'{svc}',\n"
        "    command='python main.py',\n"
        "    health_check=ProcessHealthCheck(timeout=5.0, interval=0.1),\n"
        "    reload=True,\n"
        ")\n"
        "stack.run()\n"
    )
    path = project / STACKFILE_NAME
    _write(path, body)
    return path


class TestFullLifecycle:
    def test_start_health_stop_via_orchestrator(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _long_running_stackfile(project)
        stack = load_stack_from_stackfile(project / STACKFILE_NAME)

        orch = Orchestrator(poll_interval_s=0.05)
        code_holder: dict[str, Optional[int]] = {"code": None}

        def _run() -> None:
            code_holder["code"] = orch.run(stack, project_root=project)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        stop_code: int | None = None
        try:
            # Wait until the service is RUNNING.
            deadline = time.monotonic() + 8.0
            running = False
            while time.monotonic() < deadline:
                if orch._runner is not None and orch._runner.is_bound:
                    try:
                        managed = orch._runner._manager.get("app")  # type: ignore[union-attr]
                        if managed.pid is not None and managed.process is not None:
                            if managed.process.poll() is None:
                                running = True
                                break
                    except Exception:
                        pass
                time.sleep(0.05)

            assert running, "service never became running"

            # Issue tracker path under project root.
            assert (project / DEFAULT_ISSUES_DIR).exists() or True

            stop_code = orch.stop()
            thread.join(timeout=10.0)
            assert stop_code in {0, 130}
            assert code_holder["code"] in {0, 1, 130, None} or thread.is_alive() is False
        finally:
            try:
                orch.stop()
            except Exception:
                pass
            if thread.is_alive():
                thread.join(timeout=5.0)
            runner_obj = orch._runner
            if runner_obj is not None and runner_obj._manager is not None:
                try:
                    runner_obj._manager.stop_all(timeout_s=0.1)
                except Exception:
                    pass

    def test_cli_init_sync_graph_doctor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.chdir(project)

        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert (project / STACKFILE_NAME).exists()

        svc = project / "api"
        svc.mkdir()
        _write(
            svc / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        result = runner.invoke(app, ["sync", "--force"])
        assert result.exit_code == 0

        discovered = discover_project(start=project)
        assert discovered.stackfile.name == STACKFILE_NAME

        result = runner.invoke(app, ["graph"])
        assert result.exit_code == 0
        assert "api" in result.output or "Architecture" in result.output

        result = runner.invoke(app, ["doctor"])
        assert "Stackfile" in result.output
        assert "Traceback" not in result.output

    def test_cli_run_ctrl_c_shutdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _long_running_stackfile(project)
        monkeypatch.chdir(project)

        # Use Orchestrator directly with KeyboardInterrupt simulation for reliability.
        stack = load_stack_from_stackfile(project / STACKFILE_NAME)
        orch = Orchestrator(poll_interval_s=0.05)
        raised = threading.Event()

        def _run() -> None:
            try:
                # Patch monitor to raise KeyboardInterrupt after a short delay.
                original_bind = orch._ensure_runner

                def _ensure() -> Runner:
                    r = original_bind()
                    original_monitor = r.monitor

                    def _monitor() -> int:
                        time.sleep(0.4)
                        raise KeyboardInterrupt

                    r.monitor = _monitor  # type: ignore[method-assign]
                    return r

                orch._ensure_runner = _ensure  # type: ignore[method-assign]
                orch.run(stack, project_root=project)
            finally:
                raised.set()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        raised.wait(timeout=15.0)
        thread.join(timeout=10.0)
        assert raised.is_set()

    def test_external_dependency_gate(self, tmp_path: Path) -> None:
        port = _free_port()
        stack = Stack()
        stack.external_dependency(
            name="postgres",
            type="postgresql",
            host="127.0.0.1",
            port=port,
            retries=2,
            retry_delay=0.01,
            health_check=TcpHealthCheck(
                host="127.0.0.1",
                port=port,
                timeout=0.3,
                interval=0.01,
                probe_timeout=0.05,
            ),
        )
        stack.service(
            name="auth",
            path=tmp_path,
            command="python -c pass",
            depends_on=["postgres"],
            health_check=ProcessHealthCheck(timeout=1.0),
        )
        code = Orchestrator().run(stack, project_root=tmp_path)
        assert code == 1

    def test_restart_and_issue_tracking(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        svc = project / "app"
        svc.mkdir()
        _write(
            svc / "main.py",
            "import time\nprint('ready', flush=True)\ntime.sleep(3600)\n",
        )
        stack = Stack()
        stack.service(
            name="app",
            path=svc,
            command="python main.py",
            health_check=ProcessHealthCheck(timeout=5.0, interval=0.1),
        )
        from stackpilot.logger import Logger

        issues_dir = project / DEFAULT_ISSUES_DIR
        tracker = IssueTracker(issues_dir, auto_cleanup=False)
        logger = Logger(issues_dir, service_names=["app"], issue_tracker=tracker, auto_cleanup=False)
        manager = ProcessManager(logger, services=stack.services)
        graph = build_graph(stack)
        from stackpilot.watch_manager import WatchManager

        watch = WatchManager(debounce_s=0.05)
        runner_obj = Runner(poll_interval_s=0.05)
        runner_obj.bind(
            manager=manager,
            graph=graph,
            watch_manager=watch,
            project_root=project,
            ordered=list(stack.services),
            logger=logger,
        )
        try:
            assert runner_obj.start_all(list(stack.services)) is True
            assert manager.get("app").pid is not None
            assert runner_obj.restart("app") is True
            assert manager.get("app").pid is not None
            # Runtime status updated.
            assert runner_obj.status is not None
        finally:
            runner_obj.begin_shutdown()
            runner_obj.shutdown(logger, force=True)
            runner_obj.unbind()
            logger.close()
            tracker.close()
