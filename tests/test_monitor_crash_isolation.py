"""Regression: one crashed service must not end Runner.monitor()."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from stackpilot.config import ServiceSpec, Stack
from stackpilot.dependency_graph import build_graph
from stackpilot.logger import Logger
from stackpilot.models import ServiceState
from stackpilot.orchestrator import Orchestrator
from stackpilot.process_manager import ProcessManager
from stackpilot.runner import Runner
from stackpilot.watch_manager import WatchManager


def test_monitor_stays_alive_after_one_crash(
    tmp_path: Path, monkeypatch
) -> None:
    """
    When auth exits non-zero:

    - monitor loop keeps running
    - keeper keeps logging
    - issue file is written / updated
    - hot reload still works for the surviving service
    - orchestration session is not torn down
    """

    monkeypatch.chdir(tmp_path)
    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
    )

    crash_src = tmp_path / "auth_main.py"
    crash_src.write_text(
        "import sys\n"
        "import time\n"
        "print('auth started', flush=True)\n"
        "time.sleep(0.15)\n"
        "print('Traceback (most recent call last):', file=sys.stderr, flush=True)\n"
        "print('  File \"backend/database.py\", line 42, in connect', "
        "file=sys.stderr, flush=True)\n"
        "print(\"ConnectionError: Database connection refused\", "
        "file=sys.stderr, flush=True)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    keeper_src = tmp_path / "keeper_main.py"
    keeper_src.write_text(
        "import time\n"
        "print('keeper started', flush=True)\n"
        "n = 0\n"
        "while True:\n"
        "    print(f'keeper tick {n}', flush=True)\n"
        "    n += 1\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )

    issues_dir = tmp_path / ".stackpilot" / "issues"
    stack = Stack()
    stack.service(
        name="keeper",
        path=tmp_path,
        command=f'python "{keeper_src.name}"',
        reload=True,
        reload_dirs=["."],
    )
    stack.service(
        name="auth",
        path=tmp_path,
        command=f'python "{crash_src.name}"',
    )

    orchestrator = Orchestrator(
        logs_dir=issues_dir,
        poll_interval_s=0.05,
        reload_debounce_s=0.15,
    )
    result: dict[str, int] = {}

    def _run() -> None:
        result["code"] = orchestrator.run(stack)

    thread = threading.Thread(target=_run, name="stackpilot-monitor-test", daemon=True)
    thread.start()
    code: int | None = None
    try:
        issue_path = issues_dir / "auth.issue"
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            joined = "\n".join(printed)
            if (
                "auth exited (exit 1)" in joined
                and "Remaining services continue running." in joined
                and issue_path.is_file()
            ):
                break
            time.sleep(0.05)
        else:
            raise AssertionError(
                "crash notice / issue file not observed\n" + "\n".join(printed)
            )

        # Monitor must still be alive after the crash.
        assert thread.is_alive(), "monitor loop exited after a single crash"

        text = issue_path.read_text(encoding="utf-8")
        assert "ACTIVE" in text
        assert "database.py:42" in text
        assert "Issue:" in "\n".join(printed)
        assert any("auth.issue" in line for line in printed)

        # Surviving service continues streaming logs after the crash banner.
        ticks_before = sum(1 for line in printed if "keeper tick" in line)
        time.sleep(0.45)
        ticks_after = sum(1 for line in printed if "keeper tick" in line)
        assert ticks_after > ticks_before, "keeper stopped logging after sibling crash"

        # Watcher remains registered; reload callback still works post-crash.
        runner = orchestrator._runner
        assert runner is not None
        assert runner._watch_manager is not None
        assert "keeper" in runner._watch_manager.watched_services

        runner.on_reload("keeper", [str(keeper_src)])
        reload_deadline = time.monotonic() + 5.0
        while time.monotonic() < reload_deadline:
            joined = "\n".join(printed)
            if "Reloading keeper" in joined and (
                "keeper reloaded" in joined
                or "keeper started" in joined
                or "✓ keeper" in joined
            ):
                break
            time.sleep(0.05)
        else:
            raise AssertionError(
                "hot reload did not run after crash\n" + "\n".join(printed)
            )

        assert thread.is_alive(), "monitor loop died during/after reload"
        assert not any("Stopping StackPilot" in line for line in printed)

        # Explicit stop ends the session (Ctrl+C path).
        code = orchestrator.stop()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert code == 130
        assert "Stopping StackPilot..." in "\n".join(printed)
    finally:
        try:
            orchestrator.stop()
        except Exception:
            pass
        if thread.is_alive():
            thread.join(timeout=5.0)
        # Force-kill any leftover keeper/auth PIDs if stop raced the join.
        runner = orchestrator._runner
        if runner is not None and runner._manager is not None:
            try:
                runner._manager.stop_all(timeout_s=0.1)
            except Exception:
                pass


def test_monitor_loop_does_not_return_while_sibling_running(tmp_path: Path) -> None:
    """Unit-level: monitor() returns only after every service finishes."""

    logger = Logger(tmp_path / "issues", service_names=["ok", "crash"], color=False)
    manager = ProcessManager(logger, stop_timeout_s=2.0)
    watch = WatchManager(debounce_s=0.1)

    ok = ServiceSpec(
        name="ok",
        path=tmp_path,
        command='python -c "import time; print(\'ok\', flush=True); time.sleep(2.0)"',
    )
    crash = ServiceSpec(
        name="crash",
        path=tmp_path,
        command=(
            'python -c "import time,sys; time.sleep(0.2); '
            "print('Traceback (most recent call last):', file=sys.stderr, flush=True); "
            "print('  File \\\"app.py\\\", line 9, in main', file=sys.stderr, flush=True); "
            "print('RuntimeError: boom', file=sys.stderr, flush=True); "
            'raise SystemExit(1)"'
        ),
    )

    runner = Runner(logs_dir=tmp_path / "issues", poll_interval_s=0.05)
    graph = build_graph([ok, crash])
    runner.bind(
        manager=manager,
        graph=graph,
        watch_manager=watch,
        project_root=tmp_path,
        ordered=[ok, crash],
        logger=logger,
    )
    manager.start(ok)
    manager.start(crash)

    done = threading.Event()
    exit_code: dict[str, int] = {}

    def _monitor() -> None:
        exit_code["code"] = runner.monitor()
        done.set()

    thread = threading.Thread(target=_monitor, daemon=True)
    thread.start()
    try:
        # Wait until crash is observed, then assert monitor is still running.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if manager.state_of("crash") == ServiceState.FAILED:
                break
            time.sleep(0.05)
        assert manager.state_of("crash") == ServiceState.FAILED
        assert manager.state_of("ok") == ServiceState.RUNNING
        assert not done.is_set(), "monitor returned while sibling still running"

        issue = tmp_path / "issues" / "crash.issue"
        assert issue.is_file()
        text = issue.read_text(encoding="utf-8")
        assert "app.py:9" in text or "boom" in text or "Service crashed" in text

        manager.stop("ok")
        assert done.wait(timeout=5.0), "monitor did not return after all services finished"
        assert exit_code["code"] == 1
    finally:
        try:
            manager.stop_all(timeout_s=0.1)
        except Exception:
            pass
        if thread.is_alive():
            thread.join(timeout=2.0)
        runner.unbind()
        logger.close()
