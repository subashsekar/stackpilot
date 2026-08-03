"""Regression: non-critical I/O must never terminate the orchestrator."""

from __future__ import annotations

from pathlib import Path

import pytest

from stackpilot import status as status_mod
from stackpilot.config import Stack
from stackpilot.logger import Logger
from stackpilot.models import ServiceState
from stackpilot.runner import Runner
from stackpilot.status import (
    RuntimeStatus,
    pid_is_alive,
    save_runtime_snapshot,
)


@pytest.fixture(autouse=True)
def _clear_persist_warn_cache() -> None:
    status_mod._persist_warn_seen.clear()


def _raise_permission_on_runtime_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    real_replace = os.replace

    def fake_replace(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        src_path = Path(src)
        if src_path.suffix == ".tmp":
            raise PermissionError(13, "Permission denied", str(src_path))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fake_replace)


def test_persist_swallows_permission_error_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _raise_permission_on_runtime_tmp(monkeypatch)

    status = RuntimeStatus(project_root=tmp_path)
    status.register("api", status=ServiceState.RUNNING, command="python -c pass")
    status.mark_stack_started()

    # Must not raise.
    status.persist()
    status.persist()  # second call: still no raise; warning not spammed

    out = capsys.readouterr().out
    assert "Unable to update runtime status:" in out
    assert "Permission denied" in out
    assert "Continuing without runtime persistence." in out
    assert out.count("Unable to update runtime status:") == 1
    assert not (tmp_path / ".stackpilot" / "runtime.json").is_file()


def test_save_runtime_snapshot_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _raise_permission_on_runtime_tmp(monkeypatch)

    save_runtime_snapshot(
        tmp_path,
        {
            "session_active": True,
            "services": [
                {
                    "name": "api",
                    "pid": 1,
                    "status": ServiceState.RUNNING.value,
                }
            ],
        },
    )

    out = capsys.readouterr().out
    assert "Continuing without runtime persistence." in out


def test_persist_ignores_stale_runtime_tmp_file(tmp_path: Path) -> None:
    runtime_dir = tmp_path / ".stackpilot"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    # Simulate a stale fixed-name temp file left behind by an older build.
    (runtime_dir / "runtime.tmp").write_text("stale", encoding="utf-8")

    status = RuntimeStatus(project_root=tmp_path)
    status.register("api", status=ServiceState.RUNNING, command="python -c pass")
    status.mark_stack_started()
    status.persist()

    runtime_json = runtime_dir / "runtime.json"
    assert runtime_json.is_file()
    text = runtime_json.read_text(encoding="utf-8")
    assert '"name": "api"' in text
    assert (runtime_dir / "runtime.tmp").read_text(encoding="utf-8") == "stale"


def test_runner_survives_runtime_persist_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    PermissionError while writing runtime.json must not exit the Runner
    or kill managed processes.
    """

    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
    )
    monkeypatch.chdir(tmp_path)
    _raise_permission_on_runtime_tmp(monkeypatch)

    alive_pid: dict[str, int | None] = {"pid": None}
    calls = {"n": 0}
    real_sleep = __import__("time").sleep

    def fake_sleep(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            # After first monitor poll (which refreshes/persists status),
            # confirm the service process is still alive, then stop cleanly.
            pid = alive_pid["pid"]
            assert isinstance(pid, int)
            assert pid_is_alive(pid)
            raise KeyboardInterrupt
        real_sleep(min(float(seconds), 0.05))

    monkeypatch.setattr("stackpilot.runner.time.sleep", fake_sleep)

    stack = Stack()
    stack.service(
        name="keeper",
        path=tmp_path,
        command='python -c "import time; time.sleep(30)"',
    )

    runner = Runner(logs_dir=tmp_path / "logs", poll_interval_s=0.05)

    # Capture PID after bind/start via ProcessManager on the runner.
    original_start_all = runner.start_all

    def start_all_and_record(ordered):  # type: ignore[no-untyped-def]
        ok = original_start_all(ordered)
        managed = runner._require_manager().get("keeper")
        alive_pid["pid"] = managed.pid
        assert managed.pid is not None
        assert pid_is_alive(managed.pid)
        return ok

    monkeypatch.setattr(runner, "start_all", start_all_and_record)

    code = runner.run(stack)
    assert code == 130

    joined = "\n".join(printed)
    assert "Unable to update runtime status:" in joined
    assert "Continuing without runtime persistence." in joined
    assert "Stopping StackPilot..." in joined
    assert "Startup aborted." not in joined


def test_issue_write_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue Tracker writes are optional; failures must not kill console logging."""

    issues = tmp_path / "issues"
    issues.mkdir()
    logger = Logger(issues, service_names=["api"], color=False, auto_cleanup=False)

    real_write = Path.write_text

    def boom(self: Path, data, *args, **kwargs):  # type: ignore[no-untyped-def]
        # IssueTracker writes ``<service>.tmp`` then replaces to ``.issue``.
        if self.suffix == ".tmp" or self.suffix == ".issue":
            raise PermissionError(13, "Permission denied", str(self))
        return real_write(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", boom)

    # Console logging still works; issue persistence must not raise.
    logger.stdout("api", "still running")
    logger.error_file("api", "ERROR Database unavailable")
    logger.close()

    out = capsys.readouterr().out
    assert "still running" in out
    assert "Unable to write service issue file:" in out
    assert "Continuing without issue persistence." in out


def test_issue_mkdir_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "denied" / "issues"
    real_mkdir = Path.mkdir

    def fake_mkdir(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "denied" in self.parts or self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)

    logger = Logger(target, service_names=["api"], color=False, auto_cleanup=False)
    logger.stdout("api", "ok")
    logger.error_file("api", "ERROR still ok without issues dir")
    logger.close()

    out = capsys.readouterr().out
    assert "Unable to write service issue file:" in out
    assert "Continuing without issue persistence." in out
    assert "ok" in out
