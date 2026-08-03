"""Unit tests for process spawning and command splitting."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from stackpilot.config import ServiceSpec
from stackpilot.logger import Logger
from stackpilot.models import ServiceState
from stackpilot.process_manager import ProcessManager
from stackpilot.utils import split_command


def test_split_command_rewrites_python_launcher() -> None:
    argv = split_command('python -c "print(1)"')
    assert argv[0] == sys.executable
    assert argv[1] == "-c"
    assert argv[2] == "print(1)"


def test_split_command_list_passthrough() -> None:
    argv = split_command([sys.executable, "-c", "print(1)"])
    assert argv == [sys.executable, "-c", "print(1)"]


def test_split_command_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        split_command("   ")


def test_spawn_never_uses_shell(tmp_path: Path) -> None:
    lines: list[str] = []
    logger = Logger(tmp_path / "logs", service_names=["demo"], print_fn=lines.append)
    manager = ProcessManager(logger, stop_timeout_s=2.0)

    # Use sys.executable so the assertion is PATH-independent (fresh clones
    # often run ``.venv/Scripts/python -m pytest`` without activating the venv).
    spec = ServiceSpec(
        name="demo",
        path=tmp_path,
        command=f'{sys.executable} -c "import time; print(\'up\', flush=True); time.sleep(2)"',
    )
    managed = manager.start(spec)
    assert managed.state == ServiceState.RUNNING
    assert managed.process is not None
    # argv list (not a shell string) and exact interpreter — proves shell=False.
    assert isinstance(managed.process.args, (list, tuple))
    assert Path(managed.process.args[0]).resolve() == Path(sys.executable).resolve()

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not any("up" in line for line in lines):
        time.sleep(0.05)

    assert any(line.endswith("up") for line in lines)
    manager.stop("demo")
    logger.close()


def test_stderr_goes_to_console_and_issue_tracker(tmp_path: Path) -> None:
    lines: list[str] = []
    issues = tmp_path / "issues"
    logger = Logger(
        issues,
        service_names=["boom"],
        print_fn=lines.append,
        color=False,
        auto_cleanup=False,
    )
    manager = ProcessManager(logger, stop_timeout_s=2.0)

    spec = ServiceSpec(
        name="boom",
        path=tmp_path,
        command='python -c "import sys; print(\'boom\', file=sys.stderr, flush=True)"',
    )
    manager.start(spec)

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not manager.all_finished():
        manager.reap_exited()
        time.sleep(0.05)
    manager.reap_exited()
    time.sleep(0.15)
    logger.close()

    assert any("[boom]" in line and "ERROR" in line and line.endswith("boom") for line in lines)
    active = logger.issue_tracker.list_issues(status="ACTIVE")
    assert len(active) == 1
    assert active[0].root_cause == "boom"
    assert (issues / "boom.issue").is_file()
    assert "boom" in (issues / "boom.issue").read_text(encoding="utf-8")


def test_one_crash_does_not_stop_others(tmp_path: Path) -> None:
    logger = Logger(tmp_path / "logs", service_names=["ok", "crash"])
    manager = ProcessManager(logger, stop_timeout_s=2.0)

    ok = ServiceSpec(
        name="ok",
        path=tmp_path,
        command='python -c "import time; time.sleep(5)"',
    )
    crash = ServiceSpec(
        name="crash",
        path=tmp_path,
        command='python -c "raise SystemExit(1)"',
    )
    manager.start(ok)
    manager.start(crash)

    deadline = time.monotonic() + 3.0
    failed: dict = {}
    while time.monotonic() < deadline and "crash" not in failed:
        failed.update(manager.reap_exited())
        time.sleep(0.05)

    assert "crash" in failed
    assert failed["crash"].exit_code == 1
    assert manager.state_of("ok") == ServiceState.RUNNING
    assert manager.state_of("crash") == ServiceState.FAILED

    manager.stop_all()
    logger.close()
