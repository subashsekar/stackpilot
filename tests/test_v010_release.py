"""v0.1.0 release readiness regressions (lifecycle, security, packaging, DX)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from stackpilot import __version__
from stackpilot.cli import PUBLIC_CLI_COMMANDS, app
from stackpilot.config import HttpHealthCheck, ServiceSpec, Stack, TcpHealthCheck
from stackpilot.diagnostics.errors import classify_spawn_error, format_spawn_failure
from stackpilot.external_validation import (
    ExternalDependencyError,
    wait_for_external_dependency,
)
from stackpilot.http_checker import probe_http
from stackpilot.logger import Logger
from stackpilot.orchestrator import Orchestrator
from stackpilot.paths import PathEscapeError, ensure_within_project
from stackpilot.process_manager import ProcessManager
from stackpilot.runner import Runner
from stackpilot.watch_manager import WatchManager, resolve_reload_dirs

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"


# ---------------------------------------------------------------------------
# Packaging / changelog consistency
# ---------------------------------------------------------------------------


class TestReleaseMetadata:
    def test_changelog_ships_0_1_0_not_only_unreleased(self) -> None:
        text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert re.search(r"^## \[0\.1\.0\]", text, re.M)
        # Shipped features must not live only under Unreleased.
        unreleased = text.split("## [0.1.0]", 1)[0]
        assert "Issue Tracker" not in unreleased
        assert "stackpilot doctor" not in unreleased
        assert __version__ == "0.1.0"

    def test_pyproject_dependency_lower_bounds(self) -> None:
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "click>=8.0" in text
        assert "typer>=0.12" in text
        assert "watchdog>=3.0" in text

    def test_manifest_excludes_bytecode(self) -> None:
        text = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        assert "global-exclude" in text
        assert "__pycache__" in text
        assert "*.py[cod]" in text or "*.pyc" in text

    def test_build_artifacts_exclude_pycache(self, tmp_path: Path) -> None:
        try:
            import build  # noqa: F401
        except ImportError:
            pytest.skip("build package not installed")

        out = tmp_path / "dist"
        proc = subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(out)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
        sdist = next(out.glob("*.tar.gz"))
        wheel = next(out.glob("*.whl"))

        with tarfile.open(sdist, "r:gz") as tf:
            names = tf.getnames()
        assert not any("__pycache__" in n or n.endswith((".pyc", ".pyo")) for n in names)
        assert any(n.endswith("py.typed") for n in names)
        assert any("CHANGELOG.md" in n for n in names)
        assert any(n.endswith("LICENSE") or "/LICENSE" in n for n in names)
        assert any("README.md" in n for n in names)

        with zipfile.ZipFile(wheel) as zf:
            wnames = zf.namelist()
        assert not any("__pycache__" in n or n.endswith((".pyc", ".pyo")) for n in wnames)
        assert any(n.endswith("py.typed") for n in wnames)


class TestExamplesRelease:
    def test_external_deps_example_is_runnable_layout(self) -> None:
        root = REPO_ROOT / "examples" / "external-deps"
        assert (root / "Stackfile.py").is_file()
        assert (root / "README.md").is_file()
        assert (root / "auth" / "main.py").is_file()
        assert (root / "gateway" / "main.py").is_file()
        readme = (REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
        assert "external-deps" in readme


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


class TestSecurityContracts:
    def test_no_shell_true_in_src(self) -> None:
        offenders: list[str] = []
        pattern = re.compile(r"shell\s*=\s*True")
        for path in SRC_ROOT.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                    continue
                # Ignore prose in docstrings / comments that mention shell=True.
                code = line.split("#", 1)[0]
                if "``shell=True``" in code or '"shell=True"' in code or "'shell=True'" in code:
                    continue
                if pattern.search(code):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{i}:{line.strip()}")
        assert offenders == []

    def test_http_scheme_rejected_at_runtime(self) -> None:
        result = probe_http("file:///etc/passwd")
        assert result.ok is False
        assert "http or https" in result.detail

    def test_ftp_scheme_rejected_in_parse(self) -> None:
        from stackpilot.config import parse_health_check

        with pytest.raises(ValueError, match="http or https"):
            parse_health_check({"type": "http", "url": "ftp://example.com/health"})

    def test_path_escape_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(PathEscapeError):
            ensure_within_project(outside, root, label="path")

    def test_reload_dirs_escape_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        svc = root / "api"
        svc.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        spec = ServiceSpec(
            name="api",
            path=svc,
            command="python -c pass",
            reload=True,
            reload_dirs=[str(outside)],
        )
        with pytest.raises(PathEscapeError):
            resolve_reload_dirs(spec, project_root=root)


# ---------------------------------------------------------------------------
# Lifecycle / reliability
# ---------------------------------------------------------------------------


class TestLifecycleInvariants:
    def test_reload_ignored_during_shutdown(self, tmp_path: Path) -> None:
        logger = Logger(tmp_path / "issues", service_names=["demo"], auto_cleanup=False)
        manager = ProcessManager(logger)
        watch = WatchManager()
        runner = Runner()
        spec = ServiceSpec(
            name="demo",
            path=tmp_path,
            command=f'{sys.executable} -c "import time; time.sleep(30)"',
        )
        runner.bind(
            manager=manager,
            graph=None,
            watch_manager=watch,
            project_root=tmp_path,
            ordered=[spec],
            logger=logger,
        )
        manager.start(spec)
        runner.begin_shutdown()
        with patch.object(runner, "_restart_with_health") as restart:
            runner.on_reload("demo", ["x.py"])
            restart.assert_not_called()
        manager.stop("demo")
        runner.unbind()
        logger.close()

    def test_double_shutdown_is_idempotent(self, tmp_path: Path) -> None:
        logger = Logger(tmp_path / "issues", service_names=["demo"], auto_cleanup=False)
        manager = ProcessManager(logger)
        watch = WatchManager()
        runner = Runner()
        spec = ServiceSpec(
            name="demo",
            path=tmp_path,
            command=f'{sys.executable} -c "import time; time.sleep(30)"',
        )
        runner.bind(
            manager=manager,
            graph=None,
            watch_manager=watch,
            project_root=tmp_path,
            ordered=[spec],
            logger=logger,
        )
        manager.start(spec)
        assert runner.shutdown(logger) == 130
        assert runner.shutdown(logger) == 130
        runner.unbind()
        logger.close()

    def test_orchestrator_shutdown_order(self, tmp_path: Path) -> None:
        events: list[str] = []
        stack = Stack()
        stack.service(
            name="demo",
            path=str(tmp_path),
            command=f'{sys.executable} -c "import time; time.sleep(60)"',
        )

        orch = Orchestrator(poll_interval_s=0.05)
        real_runner = Runner(poll_interval_s=0.05)

        def tracking_begin() -> None:
            events.append("disable_reload")
            Runner.begin_shutdown(real_runner)

        def tracking_shutdown(logger, *, force: bool = False) -> int:  # noqa: ANN001
            events.append("stop_processes")
            return 130

        real_runner.begin_shutdown = tracking_begin  # type: ignore[method-assign]
        real_runner.shutdown = tracking_shutdown  # type: ignore[method-assign]
        real_runner.unbind = lambda: events.append("unbind")  # type: ignore[method-assign]

        orch._runner = real_runner
        # Drive bind + stop path without a full monitor loop.
        from stackpilot.dependency_graph import build_graph
        from stackpilot.issues import IssueTracker
        from stackpilot.logger import Logger as Log
        from stackpilot.process_manager import ProcessManager as PM
        from stackpilot.watch_manager import WatchManager as WM

        graph = build_graph(stack)
        logger = Log(tmp_path / "issues", service_names=["demo"], auto_cleanup=False)
        manager = PM(logger)
        watch = WM()
        original_stop = watch.stop

        def tracking_watch_stop() -> None:
            events.append("stop_watchers")
            original_stop()

        watch.stop = tracking_watch_stop  # type: ignore[method-assign]
        real_runner.bind(
            manager=manager,
            graph=graph,
            watch_manager=watch,
            project_root=tmp_path,
            ordered=list(stack.services),
            logger=logger,
        )
        orch._logger = logger
        orch._watch_manager = watch
        orch._cleanup_done = False

        original_close = logger.close

        def tracking_close() -> None:
            events.append("logger_shutdown")
            original_close()

        logger.close = tracking_close  # type: ignore[method-assign]
        code = orch.stop()
        assert code == 130
        assert events == [
            "disable_reload",
            "stop_processes",
            "stop_watchers",
            "unbind",
            "logger_shutdown",
        ]

    def test_external_validation_retries_until_timeout(self) -> None:
        calls = {"n": 0}

        def flaky(_dep) -> bool:  # noqa: ANN001
            calls["n"] += 1
            return False

        dep = type("D", (), {})()
        dep.name = "postgres"
        dep.health_check = TcpHealthCheck(
            host="127.0.0.1",
            port=1,
            timeout=0.15,
            interval=0.05,
            probe_timeout=0.05,
        )
        times = iter([0.0, 0.04, 0.08, 0.12, 0.16])
        sleeps: list[float] = []

        with patch(
            "stackpilot.external_validation.check_external_dependency",
            side_effect=flaky,
        ):
            with pytest.raises(Exception):
                wait_for_external_dependency(
                    dep,  # type: ignore[arg-type]
                    clock=lambda: next(times, 1.0),
                    sleep=lambda s: sleeps.append(s),
                )
        assert calls["n"] >= 2
        assert sleeps


# ---------------------------------------------------------------------------
# DX / logger / CLI error contract
# ---------------------------------------------------------------------------


class TestDxAndLogger:
    def test_spawn_failure_diagnosis_missing_executable(self) -> None:
        diagnosis, cause, action = classify_spawn_error(
            FileNotFoundError(2, "No such file"),
            command="missing-bin --flag",
        )
        assert "Executable not found" in diagnosis
        assert "PATH" in cause
        assert "doctor" in action

    def test_spawn_failure_message_has_no_traceback(self) -> None:
        text = format_spawn_failure(
            service="auth",
            exc=PermissionError("denied"),
            command="python app.py",
        )
        assert "Traceback" not in text
        assert "Problem:" in text
        assert "Suggested fix:" in text
        assert "Affected service: auth" in text

    def test_logger_console_flag_thread_safe(self, tmp_path: Path) -> None:
        log = Logger(tmp_path / "issues", service_names=["a"], auto_cleanup=False)
        seen: list[str] = []
        log._print_fn = seen.append  # type: ignore[method-assign]
        barrier = threading.Barrier(2)

        def writer() -> None:
            barrier.wait()
            for _ in range(50):
                log.stdout("a", "hello")

        def muter() -> None:
            barrier.wait()
            log.set_console_enabled(False)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=muter)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        # After mute, further emits must be dropped (no exception / deadlock).
        before = len(seen)
        log.stdout("a", "after")
        assert len(seen) == before
        log.close()


class TestCliErrorContract:
    def test_frozen_commands_still_match(self) -> None:
        assert PUBLIC_CLI_COMMANDS == (
            "init",
            "sync",
            "run",
            "stop",
            "graph",
            "status",
            "ps",
            "issues",
            "doctor",
            "version",
        )

    def test_run_path_escape_friendly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        project = tmp_path / "proj"
        project.mkdir()
        (project / "Stackfile.py").write_text(
            "from stackpilot import Stack\n"
            "stack = Stack()\n"
            f"stack.service(name='x', path=r'{outside}', command='python -c pass')\n"
            "stack.run()\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(project)
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 1
        combined = (result.output or "") + (getattr(result, "stderr", None) or "")
        assert "Traceback" not in combined
        assert "escapes project root" in combined
