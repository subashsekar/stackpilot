"""Lock the v0.1.0 public CLI surface — prevent accidental command drift."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from stackpilot.cli import PUBLIC_CLI_COMMANDS, app

runner = CliRunner()

# Help summaries must match command docstrings / ``stackpilot --help``.
COMMAND_HELP = {
    "init": "Create a starter Stackfile.py in the current directory.",
    "sync": "Discover services and write Stackfile.py.",
    "run": "Start services and stream live logs.",
    "graph": "Print a professional architecture dependency visualization.",
    "status": "Show runtime status (PID, port, uptime, health).",
    "ps": "List active StackPilot processes.",
    "issues": "List service issues from .stackpilot/issues/.",
    "doctor": "Diagnose environment, Stackfile, and service configuration.",
    "version": "Print the installed StackPilot version.",
}


def _registered_command_names() -> list[str]:
    click_cmd = get_command(app)
    return list(click_cmd.commands.keys())


class TestPublicCliSurfaceFrozen:
    def test_public_cli_commands_constant(self) -> None:
        assert PUBLIC_CLI_COMMANDS == (
            "init",
            "sync",
            "run",
            "graph",
            "status",
            "ps",
            "issues",
            "doctor",
            "version",
        )

    def test_registered_commands_match_frozen_surface(self) -> None:
        names = _registered_command_names()
        assert names == list(PUBLIC_CLI_COMMANDS)
        assert len(names) == len(set(names))

    def test_root_help_lists_exact_commands_in_order(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        output = result.output
        # Rich/Click wraps rows as ``│ name   help…`` (box drawing optional).
        row = re.compile(r"(?m)^.\s*(\S+)\s{2,}")

        listed = [
            m.group(1)
            for m in row.finditer(output)
            if re.fullmatch(r"[a-z][a-z0-9_-]*", m.group(1))
        ]
        assert listed == list(PUBLIC_CLI_COMMANDS)

        for name in PUBLIC_CLI_COMMANDS:
            assert COMMAND_HELP[name] in output

    def test_each_command_has_help(self) -> None:
        for name in PUBLIC_CLI_COMMANDS:
            result = runner.invoke(app, [name, "--help"])
            assert result.exit_code == 0, name
            assert COMMAND_HELP[name] in result.output, name
            assert "--help" in result.output

    def test_error_contract_no_traceback_on_missing_stackfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 1
        combined = (result.output or "") + (getattr(result, "stderr", None) or "")
        assert "Traceback" not in combined
        assert "No Stackfile.py found." in combined

    def test_error_contract_dependency_cycle_no_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "Stackfile.py").write_text(
            "from stackpilot import Stack\n"
            "stack = Stack()\n"
            "stack.service(name='a', path='.', command='true', depends_on=['b'])\n"
            "stack.service(name='b', path='.', command='true', depends_on=['a'])\n"
            "stack.run()\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 1
        combined = (result.output or "") + (getattr(result, "stderr", None) or "")
        assert "Traceback" not in combined
        # Cycle errors render as ``a \\n ↓ \\n b \\n ↓ \\n a`` (no "cycle" word).
        assert "a" in combined and "b" in combined
        assert "↓" in combined or "v" in combined
