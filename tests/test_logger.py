"""Unit tests for stackpilot.logger.Logger."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from stackpilot.issues import STATUS_ACTIVE
from stackpilot.logger import Logger


def test_stdout_aligns_service_names(tmp_path: Path) -> None:
    lines: list[str] = []
    fixed = datetime(2026, 7, 22, 12, 0, 0)
    logger = Logger(
        tmp_path / "issues",
        service_names=["gateway", "auth", "users"],
        print_fn=lines.append,
        clock=lambda: fixed,
        color=False,
        auto_cleanup=False,
    )

    logger.stdout("gateway", "up")
    logger.stdout("auth", "up")
    logger.stdout("users", "up")

    assert "[gateway]" in lines[0]
    assert "[auth]" in lines[1]
    assert "[users]" in lines[2]

    # Message column (after level) should align.
    content_cols = [line.index(" up") for line in lines]
    assert len(set(content_cols)) == 1
    logger.close()


def test_stdout_and_stderr_formats(tmp_path: Path) -> None:
    lines: list[str] = []
    fixed = datetime(2026, 7, 22, 12, 41, 22)
    logger = Logger(
        tmp_path / "issues",
        service_names=["gateway"],
        print_fn=lines.append,
        clock=lambda: fixed,
        color=False,
        auto_cleanup=False,
    )

    logger.stdout("gateway", "ready")
    logger.stderr("gateway", "Database unavailable")

    assert lines[0] == "12:41:22 [gateway]  INFO ready"
    assert lines[1] == "12:41:22 [gateway]  ERROR Database unavailable"
    logger.close()


def test_error_file_records_issue_not_log(tmp_path: Path) -> None:
    issues = tmp_path / "issues"
    fixed = datetime(2026, 7, 22, 0, 5, 12, 456000)
    logger = Logger(
        issues,
        service_names=["auth"],
        clock=lambda: fixed,
        auto_cleanup=False,
    )

    logger.stdout("auth", "hello")
    logger.error_file("auth", "boom")
    logger.error_file("auth", "boom")
    logger.close()

    assert (issues / "auth.issue").is_file()
    text = (issues / "auth.issue").read_text(encoding="utf-8")
    assert "TIME                 STATUS   ERROR" in text
    assert text.count("boom") == 1
    active = logger.issue_tracker.list_issues(status=STATUS_ACTIVE)
    assert len(active) == 1
    assert active[0].root_cause == "boom"


def test_error_file_keeps_endpoint_in_message(tmp_path: Path) -> None:
    issues = tmp_path / "issues"
    fixed = datetime(2026, 7, 22, 12, 0, 0, 0)
    logger = Logger(issues, clock=lambda: fixed, auto_cleanup=False)
    logger.error_file("gateway", "GET /payments 502 Connection refused")
    logger.close()

    active = logger.issue_tracker.list_issues(status=STATUS_ACTIVE)
    assert len(active) == 1
    assert active[0].root_cause == "GET /payments 502 Connection refused"


def test_creates_stackpilot_issues_directory(tmp_path: Path) -> None:
    issues = tmp_path / ".stackpilot" / "issues"
    assert not issues.exists()
    logger = Logger(issues, auto_cleanup=False)
    assert issues.is_dir()
    assert (tmp_path / ".stackpilot").is_dir()
    logger.close()


def test_console_can_be_disabled(tmp_path: Path) -> None:
    lines: list[str] = []
    fixed = datetime(2026, 7, 22, 1, 2, 3, 0)
    logger = Logger(
        tmp_path / "issues",
        print_fn=lines.append,
        clock=lambda: fixed,
        auto_cleanup=False,
    )
    logger.set_console_enabled(False)
    logger.stdout("svc", "quiet")
    logger.stderr("svc", "also quiet")
    assert lines == []
    logger.error_file("svc", "still written")
    logger.close()
    active = logger.issue_tracker.list_issues(status=STATUS_ACTIVE)
    assert len(active) == 1
    assert active[0].root_cause == "still written"


def test_startup_buffer_holds_logs_until_flush(tmp_path: Path) -> None:
    lines: list[str] = []
    fixed = datetime(2026, 7, 22, 12, 0, 0)
    logger = Logger(
        tmp_path / "issues",
        service_names=["api"],
        print_fn=lines.append,
        clock=lambda: fixed,
        color=False,
        auto_cleanup=False,
    )
    logger.begin_startup_buffer()
    logger.stdout("api", "booting")
    assert lines == []

    logger.flush_startup_buffer()
    assert lines == ["12:00:00 [api]  INFO booting"]

    logger.stdout("api", "live")
    assert lines[-1] == "12:00:00 [api]  INFO live"
    logger.close()


def test_detects_embedded_python_logging_level(tmp_path: Path) -> None:
    lines: list[str] = []
    fixed = datetime(2026, 7, 22, 12, 0, 0)
    logger = Logger(
        tmp_path / "issues",
        service_names=["auth"],
        print_fn=lines.append,
        clock=lambda: fixed,
        color=False,
        auto_cleanup=False,
    )
    logger.stderr(
        "auth",
        "2026-07-27 11:45:09,291 | INFO | app.services.seed | Super admin exists",
    )
    logger.stderr(
        "auth",
        "C:\\venv\\pydantic\\main.py:263: UserWarning: INTERNAL_SERVICE_TOKEN default",
    )
    assert " INFO " in lines[0]
    assert " WARN " in lines[1]
    assert " ERROR " not in lines[0]
    assert " ERROR " not in lines[1]
    logger.close()


def test_detects_json_log_level_on_stderr(tmp_path: Path) -> None:
    lines: list[str] = []
    fixed = datetime(2026, 7, 22, 12, 0, 0)
    logger = Logger(
        tmp_path / "issues",
        service_names=["ai_service"],
        print_fn=lines.append,
        clock=lambda: fixed,
        color=False,
        auto_cleanup=False,
    )
    logger.stderr(
        "ai_service",
        '{"time": "2026-07-31T11:02:10+0530", "level": "INFO", '
        '"service": "ai_service", "message": "Watching for file changes"}',
    )
    assert " INFO " in lines[0]
    assert " ERROR " not in lines[0]
    logger.close()


def test_stdout_does_not_create_issue_files(tmp_path: Path) -> None:
    issues = tmp_path / "issues"
    logger = Logger(issues, auto_cleanup=False, color=False)
    logger.stdout("api", "INFO all good")
    logger.close()
    assert list(issues.glob("*.issue")) == []
    assert list(issues.glob("*.log")) == []
    assert list(issues.glob("*.json")) == []
