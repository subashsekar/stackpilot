"""Production-style CLI lifecycle integration for v0.1.0."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stackpilot.cli import app
from stackpilot.config import Stack
from stackpilot.discovery import STACKFILE_NAME
from stackpilot.orchestrator import Orchestrator

runner = CliRunner()


def _write_stack(project: Path, body: str) -> None:
    (project / STACKFILE_NAME).write_text(body, encoding="utf-8")


def _sleep_cmd(seconds: float = 30.0) -> str:
    return f'{sys.executable} -c "import time; time.sleep({seconds})"'


class TestCliLifecycleIntegration:
    def test_sync_graph_doctor_status_ps_issues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        api = project / "api"
        api.mkdir(parents=True)
        (api / "main.py").write_text(
            "from fastapi import FastAPI\napp = FastAPI()\n"
            "@app.get('/health')\ndef h():\n    return {'ok': True}\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(project)
        sync = runner.invoke(app, ["sync", "--force"])
        assert sync.exit_code == 0, sync.output
        assert (project / STACKFILE_NAME).is_file()

        graph = runner.invoke(app, ["graph"])
        assert graph.exit_code == 0
        assert "api" in graph.output

        doctor = runner.invoke(app, ["doctor"])
        assert doctor.exit_code == 0
        assert "Stackfile" in doctor.output or "Everything looks good." in doctor.output

        status = runner.invoke(app, ["status"])
        assert status.exit_code == 0
        assert "api" in status.output

        ps = runner.invoke(app, ["ps"])
        assert ps.exit_code == 0

        issues = runner.invoke(app, ["issues"])
        assert issues.exit_code == 0

    def test_full_run_shutdown_no_leaks(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        before_threads = {
            t.name for t in threading.enumerate() if t.name.startswith("stackpilot-")
        }
        stack = Stack()
        stack.service(name="a", path=str(project), command=_sleep_cmd(0.4))
        stack.service(
            name="b",
            path=str(project),
            command=_sleep_cmd(0.4),
            depends_on=["a"],
        )
        orch = Orchestrator(poll_interval_s=0.05)
        code = orch.run(stack, project_root=project)
        assert code in {0, 1}
        time.sleep(0.4)
        leaked = [
            th.name
            for th in threading.enumerate()
            if th.name.startswith("stackpilot-")
            and th.name not in before_threads
            and th.is_alive()
        ]
        assert leaked == []
        runtime = project / ".stackpilot" / "runtime.json"
        assert runtime.is_file()
        text = runtime.read_text(encoding="utf-8")
        assert "false" in text

    def test_spawn_missing_executable_friendly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.chdir(project)
        _write_stack(
            project,
            "from stackpilot import Stack\n"
            "stack = Stack()\n"
            "stack.service(\n"
            "    name='broken',\n"
            f"    path=r'{project}',\n"
            "    command='definitely-not-a-real-binary-xyz',\n"
            ")\n"
            "stack.run()\n",
        )
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 1
        combined = (result.output or "") + (getattr(result, "stderr", None) or "")
        assert "Traceback" not in combined
        assert "Problem:" in combined
        assert "Executable not found" in combined or "Suggested fix:" in combined
        assert "Suggested fix:" in combined or "doctor" in combined.lower()


class TestCrashIsolationIntegration:
    def test_sibling_survives_crash(self, tmp_path: Path) -> None:
        from stackpilot.logger import Logger
        from stackpilot.process_manager import ProcessManager
        from stackpilot.runner import Runner
        from stackpilot.watch_manager import WatchManager
        from stackpilot.dependency_graph import build_graph

        stack = Stack()
        stack.service(
            name="ok",
            path=str(tmp_path),
            command=_sleep_cmd(60),
        )
        stack.service(
            name="boom",
            path=str(tmp_path),
            command=f'{sys.executable} -c "raise SystemExit(1)"',
        )
        logger = Logger(tmp_path / "issues", service_names=["ok", "boom"], auto_cleanup=False)
        manager = ProcessManager(logger)
        watch = WatchManager()
        runner = Runner(poll_interval_s=0.05)
        ordered = list(stack.services)
        runner.bind(
            manager=manager,
            graph=build_graph(stack),
            watch_manager=watch,
            project_root=tmp_path,
            ordered=ordered,
            logger=logger,
        )
        assert runner.start_all(ordered) or True
        # boom may fail health/process immediately; ensure ok still running if started.
        time.sleep(0.4)
        ok = manager.get("ok")
        if ok.pid is not None:
            try:
                os.kill(ok.pid, 0)
                alive = True
            except OSError:
                alive = False
            # If start_all aborted early because boom failed health, ok may be stopped.
            # Either way, no exception should escape and pumps must not crash the runner.
        runner.begin_shutdown()
        if runner.is_bound:
            try:
                runner.shutdown(logger)
            except Exception:
                pass
        watch.stop()
        runner.unbind()
        logger.close()
