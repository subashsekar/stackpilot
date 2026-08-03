"""Regression: runtime artifacts always root under the discovered project."""

from __future__ import annotations

from pathlib import Path

import pytest

from stackpilot.config import Stack
from stackpilot.discovery import STACKFILE_NAME
from stackpilot.issues import DEFAULT_ISSUES_DIR
from stackpilot.orchestrator import Orchestrator
from stackpilot.status import RUNTIME_STATUS_FILE, runtime_status_path


def test_orchestrator_roots_artifacts_under_project_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Running from a subdirectory must write ``.stackpilot/`` under the
    Stackfile project root — never under the subdirectory cwd.
    """

    (tmp_path / STACKFILE_NAME).write_text(
        "from stackpilot import Stack\n\nstack = Stack()\nstack.run()\n",
        encoding="utf-8",
    )
    nested = tmp_path / "apps" / "gateway"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

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
        name="worker",
        path=tmp_path,
        command='python -c "import time; time.sleep(30)"',
    )

    code = Orchestrator(poll_interval_s=0.05).run(stack, project_root=tmp_path)
    assert code == 130

    project_issues = tmp_path / DEFAULT_ISSUES_DIR
    project_runtime = runtime_status_path(tmp_path)
    cwd_stackpilot = nested / ".stackpilot"

    assert project_issues.is_dir()
    assert project_runtime.is_file() or (tmp_path / RUNTIME_STATUS_FILE).exists()
    assert not cwd_stackpilot.exists()
