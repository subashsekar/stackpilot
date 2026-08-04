"""Unit tests for Runner orchestration messages and crash isolation."""

from __future__ import annotations

from pathlib import Path

from stackpilot.config import Stack
from stackpilot.runner import Runner


def test_runner_prints_started_summary(tmp_path: Path, monkeypatch) -> None:
    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
    )

    stack = Stack()
    stack.service(
        name="a",
        path=tmp_path,
        command='python -c "import time; print(\'a\', flush=True); time.sleep(0.25)"',
        port=8000,
    )
    stack.service(
        name="b",
        path=tmp_path,
        command='python -c "import time; print(\'b\', flush=True); time.sleep(0.25)"',
        depends_on=["a"],
        port=8001,
    )

    code = Runner(logs_dir=tmp_path / "logs", poll_interval_s=0.05).run(stack)
    assert code == 0
    joined = "\n".join(printed)
    assert "Starting application services..." in joined
    assert "Started 2/2 services" in joined
    assert "Services ready:" in joined
    assert "http://127.0.0.1:8000" in joined
    assert "http://127.0.0.1:8001" in joined
    assert "Watching for changes..." not in joined  # no reload=True services
    assert "Press Ctrl+C to stop." in joined
    assert "All services are running." not in joined


def test_runner_reports_crash_without_tearing_down(
    tmp_path: Path, monkeypatch
) -> None:
    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
    )
    monkeypatch.chdir(tmp_path)

    stack = Stack()
    stack.service(
        name="keeper",
        path=tmp_path,
        command='python -c "import time; time.sleep(0.5)"',
    )
    stack.service(
        name="gateway",
        path=tmp_path,
        # Stay alive long enough to pass process health, then crash in monitor.
        command='python -c "import time; time.sleep(0.2); raise SystemExit(1)"',
    )

    code = Runner(logs_dir=tmp_path / "logs", poll_interval_s=0.05).run(stack)
    assert code == 1
    joined = "\n".join(printed)
    assert "Starting application services..." in joined
    assert "gateway exited (exit 1)" in joined
    assert "Issue:" in joined
    assert "Remaining services continue running." in joined
    assert not any("Stopping StackPilot" in line for line in printed)


def test_runner_shutdown_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch
) -> None:
    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
    )

    calls = {"n": 0}
    real_sleep = __import__("time").sleep

    def fake_sleep(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        real_sleep(min(float(seconds), 0.05))

    monkeypatch.setattr("stackpilot.runner.time.sleep", fake_sleep)

    stack = Stack()
    stack.service(
        name="auth",
        path=tmp_path,
        command='python -c "import time; time.sleep(30)"',
    )
    stack.service(
        name="gateway",
        path=tmp_path,
        command='python -c "import time; time.sleep(30)"',
        depends_on=["auth"],
    )

    code = Runner(logs_dir=tmp_path / "logs", poll_interval_s=0.05).run(stack)

    assert code == 130
    joined = "\n".join(printed)
    assert "Stopping StackPilot..." in joined
    assert "gateway stopped" in joined
    assert "auth stopped" in joined
    assert joined.index("gateway stopped") < joined.index("auth stopped")
    assert "Services stopped: 2/2" in joined
    assert "Shutdown time:" in joined
