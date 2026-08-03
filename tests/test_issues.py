"""Tests for the StackPilot compact ``.issue`` tracker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stackpilot.cli import app
from stackpilot.issues import (
    STATUS_ACTIVE,
    STATUS_FIXED,
    IssueTracker,
    extract_traceback_location,
    format_issues_report,
    parse_issue_table,
)
from stackpilot.logger import Logger

runner = CliRunner()


def _clock_at(when: datetime):
    return lambda: when


def test_creates_issue_files(tmp_path: Path) -> None:
    tracker = IssueTracker(tmp_path / "issues", auto_cleanup=False)
    tracker.record_error("auth", root_cause="Database connection refused")
    path = tmp_path / "issues" / "auth.issue"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "TIME                 STATUS   ERROR                          FILE:LINE" in text
    assert "ACTIVE" in text
    assert "Database connection refused" in text
    assert list((tmp_path / "issues").glob("*.issue")) == [path]
    assert list((tmp_path / "issues").glob("*.json")) == []
    assert list((tmp_path / "issues").glob("*.log")) == []
    tracker.close()


def test_multiple_issues_in_one_service(tmp_path: Path) -> None:
    t0 = datetime(2026, 7, 23, 12, 59, 59, tzinfo=timezone.utc)
    tracker = IssueTracker(
        tmp_path / "issues",
        clock=_clock_at(t0),
        auto_cleanup=False,
    )
    tracker.record_error(
        "auth",
        root_cause="Database connection refused",
        file="database.py",
        line=42,
    )
    tracker.record_error(
        "auth",
        root_cause="Unexpected indent",
        file="routes.py",
        line=91,
    )
    text = (tmp_path / "issues" / "auth.issue").read_text(encoding="utf-8")
    assert "Database connection refused" in text
    assert "database.py:42" in text
    assert "Unexpected indent" in text
    assert "routes.py:91" in text
    assert len(tracker.list_issues(status=STATUS_ACTIVE)) == 2
    tracker.close()


def test_duplicate_issue_detection(tmp_path: Path) -> None:
    tracker = IssueTracker(tmp_path / "issues", auto_cleanup=False)
    a = tracker.record_error(
        "auth",
        root_cause="Database connection refused",
        file="database.py",
        line=42,
    )
    b = tracker.record_error(
        "auth",
        root_cause="Database connection refused",
        file="database.py",
        line=42,
    )
    assert a is not None and b is not None
    assert a.id == b.id
    text = (tmp_path / "issues" / "auth.issue").read_text(encoding="utf-8")
    assert text.count("Database connection refused") == 1
    assert len(tracker.list_issues(status=STATUS_ACTIVE)) == 1
    tracker.close()


def test_reactivates_fixed_fingerprint_instead_of_stacking(tmp_path: Path) -> None:
    t0 = datetime(2026, 7, 30, 12, 42, 38, tzinfo=timezone.utc)
    tracker = IssueTracker(
        tmp_path / "issues",
        clock=_clock_at(t0),
        auto_cleanup=False,
    )
    tracker.record_error(
        "admin_service",
        root_cause="No module named 'shared'",
        file="services/admin_service/app/dependencies/auth.py",
        line=12,
    )
    tracker.mark_fixed("admin_service")
    assert len(tracker.list_issues(status=STATUS_FIXED)) == 1
    assert len(tracker.list_issues(status=STATUS_ACTIVE)) == 0

    t1 = datetime(2026, 7, 30, 13, 17, 48, tzinfo=timezone.utc)
    tracker._clock = _clock_at(t1)
    again = tracker.record_error(
        "admin_service",
        root_cause="No module named 'shared'",
        file="services/admin_service/app/dependencies/auth.py",
        line=12,
    )
    assert again is not None
    assert again.status == STATUS_ACTIVE
    assert again.first_seen == "2026-07-30T13:17:48"
    rows = tracker.list_issues()
    assert len(rows) == 1
    assert rows[0].status == STATUS_ACTIVE
    text = (tmp_path / "issues" / "admin_service.issue").read_text(encoding="utf-8")
    assert text.count("No module named 'shared'") == 1
    assert "FIXED" not in text
    tracker.close()


def test_ingest_skips_userwarning_and_stack_snippet(tmp_path: Path) -> None:
    tracker = IssueTracker(tmp_path / "issues", auto_cleanup=False)
    warn = (
        r"C:\Users\subas\recentthink-ai-be\.venv\Lib\site-packages\pydantic\main.py:263: "
        "UserWarning: INTERNAL_SERVICE_TOKEN is using the insecure default value; "
        "set a strong INTERNAL_SERVICE_TOKEN before deploying."
    )
    assert tracker.ingest_stderr("admin_service", warn) is None
    assert (
        tracker.ingest_stderr(
            "admin_service",
            "  validated_self = self.__pydantic_validator__.validate_python("
            "data, self_instance=self)",
        )
        is None
    )
    assert tracker.list_issues() == []
    assert not (tmp_path / "issues" / "admin_service.issue").exists()

    # Real errors still record.
    issue = tracker.ingest_stderr(
        "admin_service",
        "ModuleNotFoundError: No module named 'shared'",
    )
    assert issue is not None
    assert issue.root_cause == "No module named 'shared'"
    tracker.close()


def test_ingest_skips_json_info_keeps_json_error(tmp_path: Path) -> None:
    tracker = IssueTracker(tmp_path / "issues", auto_cleanup=False)
    assert (
        tracker.ingest_stderr(
            "ai_service",
            '{"time": "2026-07-31T11:02:10+0530", "level": "INFO", '
            '"message": "Watching for file changes"}',
        )
        is None
    )
    issue = tracker.ingest_stderr(
        "ai_service",
        '{"time": "2026-07-31T11:02:10+0530", "level": "ERROR", '
        '"message": "boom"}',
    )
    assert issue is not None
    assert "boom" in issue.root_cause
    tracker.close()


def test_ingest_skips_embedded_info_and_access_logs(tmp_path: Path) -> None:
    """Django/uvicorn INFO on stderr must not become Issue Tracker rows."""

    tracker = IssueTracker(tmp_path / "issues", auto_cleanup=False)
    noise = [
        "2026-07-31 14:59:55,624 | admin_service | INFO | Service starting...",
        "2026-07-31 14:59:56,062 | admin_service | INFO | "
        "Application startup complete (delay=0.385s)",
        "2026-07-31 14:59:57,602 | admin_service | INFO | "
        "timestamp=2026-07-31T09:29:57.602088+00:00 service=admin_service "
        "method=GET path=/health status=404",
        '2026-07-31 14:59:57,603 | django.server | INFO | '
        '"GET /health HTTP/1.1" 301 0',
        '2026-07-31 14:59:57,606 | admin_service | INFO | '
        "timestamp=2026-07-31T09:29:57.606246+00:00 service=admin_service "
        "method=GET path=/health/ status=200",
        '2026-07-31 14:59:57,606 | django.server | INFO | '
        '"GET /health/ HTTP/1.1" 200 66',
        '[31/Jul/2026 15:07:10] "GET / HTTP/1.1" 200 61',
    ]
    for line in noise:
        assert tracker.ingest_stderr("admin_service", line) is None

    issue = tracker.ingest_stderr(
        "admin_service",
        "2026-07-31 15:08:00,001 | admin_service | ERROR | Database unavailable",
    )
    assert issue is not None
    assert "Database unavailable" in issue.root_cause
    assert len(tracker.list_issues(status=STATUS_ACTIVE)) == 1
    tracker.close()


def test_active_to_fixed_transition(tmp_path: Path) -> None:
    t0 = datetime(2026, 7, 23, 13, 0, 0, tzinfo=timezone.utc)
    tracker = IssueTracker(
        tmp_path / "issues",
        clock=_clock_at(t0),
        auto_cleanup=False,
    )
    tracker.record_error("auth", root_cause="boom", file="app.py", line=1)
    updated = tracker.mark_fixed("auth")
    assert len(updated) == 1
    assert updated[0].status == STATUS_FIXED

    text = (tmp_path / "issues" / "auth.issue").read_text(encoding="utf-8")
    assert "FIXED" in text
    assert "ACTIVE" not in text.splitlines()[2]  # data row is FIXED
    assert "boom" in text
    tracker.close()


def test_automatic_removal_after_one_hour(tmp_path: Path) -> None:
    t0 = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)
    tracker = IssueTracker(
        tmp_path / "issues",
        clock=_clock_at(t0),
        auto_cleanup=False,
    )
    tracker.record_error("auth", root_cause="boom")
    tracker.mark_fixed("auth")
    path = tmp_path / "issues" / "auth.issue"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "2026-07-23T12:00:00" in text or "2026-07-23T" in text

    mid = IssueTracker(
        tmp_path / "issues",
        clock=_clock_at(t0 + timedelta(minutes=30)),
        auto_cleanup=False,
    )
    assert mid.cleanup() == 0
    assert path.is_file()
    assert len(mid.list_issues(status=STATUS_FIXED)) == 1
    assert mid.list_issues(status=STATUS_FIXED)[0].delete_after is not None
    mid.close()

    later = IssueTracker(
        tmp_path / "issues",
        clock=_clock_at(t0 + timedelta(hours=1, minutes=1)),
        auto_cleanup=False,
    )
    # Constructor cleanup removes the FIXED row and deletes the empty file.
    assert not path.exists()
    assert later.list_issues() == []
    later.close()
    tracker.close()


def test_fixed_retention_survives_next_day_clock_wrap(tmp_path: Path) -> None:
    """ISO timestamps must expire even when checked at a similar clock time next day."""

    t0 = datetime(2026, 7, 23, 15, 0, 0, tzinfo=timezone.utc)
    tracker = IssueTracker(
        tmp_path / "issues",
        clock=_clock_at(t0),
        auto_cleanup=False,
    )
    tracker.record_error("auth", root_cause="boom")
    tracker.mark_fixed("auth")
    path = tmp_path / "issues" / "auth.issue"
    tracker.close()

    next_day = IssueTracker(
        tmp_path / "issues",
        clock=_clock_at(t0 + timedelta(days=1, minutes=20)),
        auto_cleanup=False,
    )
    # Constructor runs cleanup() — file must already be gone.
    assert not path.exists()
    assert next_day.list_issues() == []
    next_day.close()


def test_legacy_hhmmss_fixed_expires_via_file_mtime(tmp_path: Path) -> None:
    """Old time-only FIXED rows must not stick forever across days."""

    import os

    path = tmp_path / "issues"
    path.mkdir()
    issue = path / "auth.issue"
    issue.write_text(
        "TIME       STATUS   ERROR                          FILE:LINE\n"
        "-----------------------------------------------------------------------\n"
        "15:00:00   FIXED    boom                             -\n",
        encoding="utf-8",
    )
    now = datetime(2026, 7, 24, 15, 20, 0, tzinfo=timezone.utc)
    stale = now.timestamp() - (2 * 3600)
    os.utime(issue, (stale, stale))

    tracker = IssueTracker(path, clock=_clock_at(now), auto_cleanup=False)
    assert not issue.exists()
    assert tracker.list_issues() == []
    tracker.close()


def test_automatic_deletion_of_empty_issue_files(tmp_path: Path) -> None:
    t0 = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)
    tracker = IssueTracker(
        tmp_path / "issues",
        clock=_clock_at(t0),
        auto_cleanup=False,
    )
    tracker.record_error("auth", root_cause="a")
    tracker.record_error("auth", root_cause="b")
    tracker.mark_fixed("auth")
    path = tmp_path / "issues" / "auth.issue"
    assert path.is_file()

    later = IssueTracker(
        tmp_path / "issues",
        clock=_clock_at(t0 + timedelta(hours=2)),
        auto_cleanup=False,
    )
    assert later.cleanup() == 0  # already cleaned in __init__
    assert not path.exists()
    assert list((tmp_path / "issues").glob("*.issue")) == []
    later.close()
    tracker.close()


def test_writer_failures_do_not_stop_stackpilot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    issues = tmp_path / "issues"
    issues.mkdir()
    lines: list[str] = []
    logger = Logger(
        issues,
        service_names=["api"],
        print_fn=lines.append,
        color=False,
        auto_cleanup=False,
    )

    real_write = Path.write_text

    def boom(self: Path, data, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.suffix in {".tmp", ".issue"}:
            raise PermissionError(13, "Permission denied", str(self))
        return real_write(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", boom)

    logger.stdout("api", "still running")
    logger.stderr("api", "ERROR Database unavailable")
    logger.error_file("api", "ERROR Database unavailable")
    logger.close()

    assert any("still running" in line for line in lines)
    assert any("Database unavailable" in line for line in lines)
    out = capsys.readouterr().out
    assert "Unable to write service issue file:" in out
    assert "Continuing without issue persistence." in out


def test_traceback_extracts_message_file_line_not_full_tb(tmp_path: Path) -> None:
    tb = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "/app/database.py", line 42, in connect',
            "    raise ConnectionError('Database connection refused')",
            "ConnectionError: Database connection refused",
        ]
    )
    assert extract_traceback_location(tb) == ("database.py", 42)

    tracker = IssueTracker(tmp_path / "issues", auto_cleanup=False)
    logger = Logger(
        tmp_path / "issues",
        issue_tracker=tracker,
        auto_cleanup=False,
        color=False,
    )
    for line in tb.splitlines():
        logger.error_file("auth", line)
    text = (tmp_path / "issues" / "auth.issue").read_text(encoding="utf-8")
    assert "Database connection refused" in text
    assert "database.py:42" in text
    assert "Traceback" not in text
    assert 'File "' not in text
    logger.close()


def test_traceback_keeps_relative_folder_path() -> None:
    tb = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "backend/database.py", line 42, in connect',
            "    return engine.connect()",
            '  File "/usr/lib/python3.12/site-packages/sqlalchemy/engine.py", line 99, in connect',
            "    raise ConnectionError('Database connection refused')",
            "ConnectionError: Database connection refused",
        ]
    )
    assert extract_traceback_location(tb) == ("backend/database.py", 42)


def test_traceback_uses_project_relative_path(tmp_path: Path) -> None:
    auth_dir = tmp_path / "stackpilot-test" / "auth"
    auth_dir.mkdir(parents=True)
    source = auth_dir / "database.py"
    source.write_text("raise ConnectionError('x')\n", encoding="utf-8")

    tb = "\n".join(
        [
            "Traceback (most recent call last):",
            f'  File "{source}", line 2, in connect',
            "    raise ConnectionError('Database connection refused')",
            "ConnectionError: Database connection refused",
        ]
    )
    assert extract_traceback_location(tb, project_root=tmp_path) == (
        "stackpilot-test/auth/database.py",
        2,
    )

    issues_dir = tmp_path / ".stackpilot" / "issues"
    tracker = IssueTracker(issues_dir, auto_cleanup=False)
    for line in tb.splitlines():
        tracker.ingest_stderr("auth", line)
    text = (issues_dir / "auth.issue").read_text(encoding="utf-8")
    assert "stackpilot-test/auth/database.py:2" in text
    tracker.close()


def test_traceback_prefers_deepest_application_frame() -> None:
    tb = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "backend/database.py", line 42, in connect',
            "    return engine.connect()",
            '  File "/usr/lib/python3.12/site-packages/sqlalchemy/engine.py", line 99, in connect',
            "    raise ConnectionError('Database connection refused')",
            "ConnectionError: Database connection refused",
        ]
    )
    assert extract_traceback_location(tb) == ("backend/database.py", 42)


def test_traceback_ignores_stdlib_and_site_packages_only() -> None:
    tb = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "/usr/lib/python3.12/threading.py", line 10, in run',
            "    self._target()",
            '  File "/usr/lib/python3.12/site-packages/uvicorn/server.py", line 5, in serve',
            "    raise RuntimeError('boom')",
            "RuntimeError: boom",
        ]
    )
    assert extract_traceback_location(tb) == (None, None)


def test_service_crashed_row_reuses_traceback_location(tmp_path: Path) -> None:
    tracker = IssueTracker(tmp_path / "issues", auto_cleanup=False)
    tb = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "backend/database.py", line 42, in connect',
            "    raise ConnectionError('Database connection refused')",
            "ConnectionError: Database connection refused",
        ]
    )
    for line in tb.splitlines():
        tracker.ingest_stderr("auth", line)
    crash = tracker.record_error("auth", root_cause="Service crashed", exit_code=1)
    assert crash is not None
    assert crash.file_line == "backend/database.py:42"
    text = (tmp_path / "issues" / "auth.issue").read_text(encoding="utf-8")
    assert "backend/database.py:42" in text
    assert "Service crashed" in text
    tracker.close()


def test_has_active_detects_open_rows(tmp_path: Path) -> None:
    tracker = IssueTracker(tmp_path / "issues", auto_cleanup=False)
    assert tracker.has_active("auth") is False
    tracker.record_error(
        "auth",
        root_cause="Database connection refused",
        file="database.py",
        line=42,
    )
    assert tracker.has_active("auth") is True
    tracker.mark_fixed("auth")
    assert tracker.has_active("auth") is False
    tracker.close()


def test_failed_status_skips_crash_row_when_stderr_active(tmp_path: Path) -> None:
    from stackpilot.models import ManagedService, ServiceState
    from stackpilot.status import RuntimeStatus

    tracker = IssueTracker(tmp_path / "issues", auto_cleanup=False)
    tb = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "database.py", line 2, in connect',
            "    raise ConnectionError('Database connection refused')",
            "ConnectionError: Database connection refused",
        ]
    )
    for line in tb.splitlines():
        tracker.ingest_stderr("auth", line)

    status = RuntimeStatus(project_root=tmp_path)
    status.set_issue_tracker(tracker)
    managed = ManagedService(
        spec=__import__("stackpilot.config", fromlist=["ServiceSpec"]).ServiceSpec(
            name="auth",
            path=tmp_path,
            command="python -c pass",
        )
    )
    managed.state = ServiceState.FAILED
    managed.exit_code = 1
    status.sync_managed(managed)

    text = (tmp_path / "issues" / "auth.issue").read_text(encoding="utf-8")
    assert "Database connection refused" in text
    assert "database.py:2" in text
    assert "Service crashed" not in text
    assert text.count("ACTIVE") == 1
    tracker.close()


def test_format_active_report(tmp_path: Path) -> None:
    tracker = IssueTracker(tmp_path / "issues", auto_cleanup=False)
    tracker.record_error(
        "auth",
        root_cause="Database connection refused",
        file="database.py",
        line=42,
    )
    tracker.record_error("notifications", root_cause="SMTP authentication failed")
    text = format_issues_report(
        tracker.list_issues(status=STATUS_ACTIVE),
        heading="ACTIVE ISSUES",
        empty_message="✓ No active service issues.",
    )
    assert "auth\nDatabase connection refused (database.py:42)" in text
    assert "notifications\nSMTP authentication failed" in text
    tracker.close()


def _write_stackfile(root: Path) -> None:
    (root / "Stackfile.py").write_text(
        "from stackpilot import Stack\n"
        "stack = Stack()\n"
        "stack.service(name='auth', path='.', command='python -c \"pass\"')\n"
        "stack.service(name='notifications', path='.', command='python -c \"pass\"')\n",
        encoding="utf-8",
    )


def _seed_issue(
    path: Path,
    *,
    time: str = "12:59:59",
    status: str = "ACTIVE",
    error: str = "boom",
    file_line: str = "-",
) -> None:
    path.write_text(
        "TIME       STATUS   ERROR                          FILE:LINE\n"
        "-----------------------------------------------------------------------\n"
        f"{time:<8}   {status:<6}   {error:<30}   {file_line}\n",
        encoding="utf-8",
    )


def test_cli_lists_active_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_stackfile(tmp_path)
    issues = tmp_path / ".stackpilot" / "issues"
    issues.mkdir(parents=True)
    _seed_issue(
        issues / "auth.issue",
        error="Database connection refused",
        file_line="database.py:42",
    )
    _seed_issue(
        issues / "notifications.issue",
        error="SMTP authentication failed",
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["issues"])
    assert result.exit_code == 0
    assert "ACTIVE ISSUES" in result.output
    assert "Database connection refused" in result.output
    assert "SMTP authentication failed" in result.output


def test_cli_no_active_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_stackfile(tmp_path)
    (tmp_path / ".stackpilot" / "issues").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["issues"])
    assert result.exit_code == 0
    assert "No active service issues." in result.output


def test_parse_roundtrip_table() -> None:
    text = (
        "TIME       STATUS   ERROR                          FILE:LINE\n"
        "-----------------------------------------------------------------------\n"
        "12:59:59   ACTIVE   Database connection refused   database.py:42\n"
        "13:15:12   ACTIVE   Unexpected indent             routes.py:91\n"
    )
    rows = parse_issue_table(text)
    assert len(rows) == 2
    assert rows[0].error == "Database connection refused"
    assert rows[0].file_line == "database.py:42"
    assert rows[1].error == "Unexpected indent"
    assert rows[1].status == STATUS_ACTIVE
