"""Tests for Day 10 DX: dashboard, logger, status, shutdown, crash summaries."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from stackpilot.config import ServiceSpec, Stack, TcpHealthCheck
from stackpilot.dashboard import (
    ascii_fallback_dx,
    format_crash_report,
    format_shutdown_summary,
)
from stackpilot.logger import Logger, detect_log_level
from stackpilot.models import ManagedService, ServiceState, configured_port
from stackpilot.runner import Runner
from stackpilot.status import RuntimeStatus


# ---------------------------------------------------------------------------
# Dashboard formatting
# ---------------------------------------------------------------------------


def test_shutdown_summary_format() -> None:
    text = format_shutdown_summary(
        stopped_names=["gateway", "auth", "users"],
        total=3,
        shutdown_time_s=2.34,
    )
    assert "Stopping StackPilot..." in text
    assert "✓ gateway stopped" in text
    assert "✓ auth stopped" in text
    assert "✓ users stopped" in text
    assert "Summary:" in text
    assert "Services stopped: 3/3" in text
    assert "Shutdown time: 2.3s" in text


def test_crash_summary_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    issues_path = tmp_path / ".stackpilot" / "issues" / "payments.issue"
    issues_path.parent.mkdir(parents=True)
    issues_path.write_text("", encoding="utf-8")

    text = format_crash_report(service="payments", exit_code=1, log_path=issues_path)
    assert "payments exited (Exit Code: 1)" in text
    assert "Issue recorded:" in text
    assert ".stackpilot/issues/payments.issue" in text
    assert "Remaining services continue running..." in text
    assert "restart" not in text.lower()
    assert "Service Failed" not in text


def test_ascii_fallback_dx() -> None:
    raw = "❌ ✗ ✓ → … · ━"
    assert ascii_fallback_dx(raw) == "X X + -> ... - -"


# ---------------------------------------------------------------------------
# Logger formatting
# ---------------------------------------------------------------------------


def test_logger_console_includes_timestamp_service_level(tmp_path: Path) -> None:
    lines: list[str] = []
    fixed = datetime(2026, 7, 22, 12, 41, 22)
    logger = Logger(
        tmp_path / "logs",
        service_names=["gateway", "payments"],
        print_fn=lines.append,
        clock=lambda: fixed,
        color=False,
    )
    logger.stdout("gateway", "Gateway started")
    logger.stderr("payments", "Connection refused")
    logger.close()

    assert lines[0] == "12:41:22 [gateway]   INFO Gateway started"
    assert lines[1] == "12:41:22 [payments]  ERROR Connection refused"


def test_logger_detects_embedded_level(tmp_path: Path) -> None:
    lines: list[str] = []
    fixed = datetime(2026, 7, 22, 9, 0, 0)
    logger = Logger(
        tmp_path / "logs",
        service_names=["api"],
        print_fn=lines.append,
        clock=lambda: fixed,
        color=False,
    )
    logger.stdout("api", "WARN slow query")
    logger.stdout("api", "[ERROR] boom")
    logger.close()

    assert lines[0] == "09:00:00 [api]  WARN slow query"
    assert lines[1] == "09:00:00 [api]  ERROR boom"


def test_detect_log_level_helpers() -> None:
    assert detect_log_level("INFO hello", default="ERROR") == ("INFO", "hello")
    assert detect_log_level("plain", default="INFO") == ("INFO", "plain")
    assert detect_log_level("WARNING: x", default="INFO") == ("WARN", "x")


def test_error_file_records_actionable_stderr_only(tmp_path: Path) -> None:
    issues = tmp_path / "issues"
    fixed = datetime(2026, 7, 22, 0, 5, 12, 456000)
    logger = Logger(
        issues,
        service_names=["auth"],
        clock=lambda: fixed,
        auto_cleanup=False,
    )
    logger.error_file("auth", "INFO still on stderr")
    logger.error_file("auth", "plain stderr")
    logger.close()

    active = logger.issue_tracker.list_issues(status="ACTIVE")
    assert len(active) == 1
    assert active[0].root_cause == "plain stderr"


# ---------------------------------------------------------------------------
# Runtime metadata / status tracking
# ---------------------------------------------------------------------------


def test_managed_service_runtime_metadata(tmp_path: Path) -> None:
    spec = ServiceSpec(
        name="auth",
        path=tmp_path,
        command="python -c pass",
        health_check=TcpHealthCheck(host="127.0.0.1", port=8001),
    )
    managed = ManagedService(spec=spec)
    assert managed.status == ServiceState.STOPPED
    assert managed.port == 8001
    assert managed.uptime is None
    assert managed.pid is None
    assert managed.started_at is None

    managed.pid = 42
    managed.state = ServiceState.RUNNING
    managed.mark_started()
    assert managed.started_at is not None
    assert managed.uptime is not None
    assert managed.uptime >= 0.0
    assert managed.status == ServiceState.RUNNING


def test_configured_port_from_health(tmp_path: Path) -> None:
    assert (
        configured_port(
            ServiceSpec(name="x", path=tmp_path, command="python -c pass")
        )
        is None
    )
    assert (
        configured_port(
            ServiceSpec(
                name="x",
                path=tmp_path,
                command="python -c pass",
                health_check=TcpHealthCheck(host="127.0.0.1", port=6379),
            )
        )
        == 6379
    )
    assert (
        configured_port(
            ServiceSpec(
                name="x",
                path=tmp_path,
                command="python -c pass",
                port=8000,
            )
        )
        == 8000
    )


def test_runtime_status_tracking(tmp_path: Path) -> None:
    status = RuntimeStatus()
    spec = ServiceSpec(
        name="redis",
        path=tmp_path,
        command="python -c pass",
        health_check=TcpHealthCheck(host="127.0.0.1", port=6379),
    )
    status.register_specs([spec])
    status.mark_stack_started()
    assert status.startup_elapsed_s is not None

    managed = ManagedService(spec=spec, state=ServiceState.RUNNING, pid=99)
    managed.mark_started()
    status.sync_managed(managed)

    snap = status.get("redis")
    assert snap.pid == 99
    assert snap.port == 6379
    assert snap.status == ServiceState.RUNNING
    assert snap.started_at is not None
    assert snap.uptime is not None
    assert status.running_count() == 1


# ---------------------------------------------------------------------------
# Integration: runner shutdown + crash summaries
# ---------------------------------------------------------------------------


def test_runner_prints_dashboard_and_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    joined = "\n".join(printed)

    assert code == 130
    assert "Starting application services..." in joined
    assert "Watching for changes..." in joined
    assert "Press Ctrl+C to stop." in joined
    assert "All services are running." not in joined
    assert "Stopping StackPilot..." in joined
    assert "✓ gateway stopped" in joined or "+ gateway stopped" in joined
    assert "✓ auth stopped" in joined or "+ auth stopped" in joined
    assert "Services stopped: 2/2" in joined
    assert "Shutdown time:" in joined
    assert "Stopped 2 services." not in joined


def test_runner_crash_report_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        name="payments",
        path=tmp_path,
        command='python -c "import time; time.sleep(0.2); raise SystemExit(1)"',
    )

    code = Runner(logs_dir=tmp_path / ".stackpilot" / "issues", poll_interval_s=0.05).run(
        stack
    )
    joined = "\n".join(printed)

    assert code == 1
    assert "payments exited (Exit Code: 1)" in joined
    assert "Issue recorded:" in joined
    assert "Remaining services continue running..." in joined
    assert "Restarting" not in joined
    assert not any("Stopping StackPilot" in line for line in printed)
