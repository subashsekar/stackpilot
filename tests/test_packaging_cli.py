from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stackpilot.cli import app
from stackpilot.discovery import STACKFILE_NAME
from stackpilot.utils import load_stack_from_stackfile

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]


def _minimal_stackfile(
    directory: Path,
    *,
    with_service: bool = True,
) -> Path:
    if with_service:
        body = (
            "from stackpilot import Stack\n"
            "\n"
            "stack = Stack()\n"
            "stack.service(\n"
            '    name="demo",\n'
            '    path=".",\n'
            "    command=\"python -c \\\"print('ok')\\\"\",\n"
            ")\n"
            "stack.run()\n"
        )
    else:
        body = "from stackpilot import Stack\n\nstack = Stack()\nstack.run()\n"

    path = directory / STACKFILE_NAME
    path.write_text(body, encoding="utf-8")
    return path


class TestPackageImports:
    def test_import_stackpilot_with_stackfile_in_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stackfile.py must never shadow the installed package."""

        _minimal_stackfile(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.syspath_prepend(str(tmp_path))

        import stackpilot

        assert stackpilot.__file__ is not None
        assert Path(stackpilot.__file__).name != STACKFILE_NAME
        assert "Stackfile" not in Path(stackpilot.__file__).name
        assert hasattr(stackpilot, "Stack")

    def test_stackfile_loads_via_unique_module_name(self, tmp_path: Path) -> None:
        stackfile = _minimal_stackfile(tmp_path)
        stack = load_stack_from_stackfile(stackfile)
        assert [s.name for s in stack.services] == ["demo"]
        assert stack.services[0].path.is_absolute()


class TestCliExecution:
    def test_missing_stackfile_friendly_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["graph"])
        assert result.exit_code == 1
        combined = (result.output or "") + (getattr(result, "stderr", None) or "")
        assert "No Stackfile.py found." in combined
        assert "stackpilot init" in combined
        assert "Traceback" not in combined

    def test_init_creates_stackfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert (tmp_path / STACKFILE_NAME).exists()
        assert "Created" in result.output

    def test_graph_discovers_parent_stackfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _minimal_stackfile(tmp_path)
        nested = tmp_path / "apps" / "api"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)

        result = runner.invoke(app, ["graph"])
        assert result.exit_code == 0
        assert "demo" in result.output

    def test_status_lists_services(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _minimal_stackfile(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "demo" in result.output
        assert "Project:" in result.output
        assert "STATUS" in result.output

    def test_doctor_reports_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _minimal_stackfile(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "Stackfile.py exists" in result.output
        assert "Services discovered" in result.output
        assert "Everything looks good." in result.output
        assert "stackpilot run" in result.output

    def test_version(self) -> None:
        from stackpilot import __version__

        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert result.output.strip() == __version__

    def test_canonical_version_source(self) -> None:
        """pyproject must not hardcode a second version string."""

        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'dynamic = ["version"]' in text
        assert "version = {attr = \"stackpilot.__version__\"}" in text
        assert 'version = "0.1.0"' not in text
        from stackpilot import __version__

        assert __version__
        assert "click" in text


class TestEditableInstallAndModuleEntry:
    def test_project_scripts_entry_point_declared(self) -> None:
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'stackpilot = "stackpilot.cli:main"' in text

    def test_python_m_stackpilot_help(self) -> None:
        from stackpilot.cli import PUBLIC_CLI_COMMANDS

        env = {
            **{k: v for k, v in __import__("os").environ.items()},
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PYTHONIOENCODING": "utf-8",
        }
        proc = subprocess.run(
            [sys.executable, "-m", "stackpilot", "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
        )
        assert proc.returncode == 0
        stdout = proc.stdout or ""
        assert "StackPilot" in stdout or "stackpilot" in stdout.lower()
        for name in PUBLIC_CLI_COMMANDS:
            assert name in stdout

    def test_console_script_resolvable_after_editable_hint(self) -> None:
        """
        Verify the installed distribution exposes the console script metadata.

        Full PATH installs vary by OS; we assert importlib.metadata when present.
        """

        try:
            from importlib.metadata import entry_points
        except ImportError:  # pragma: no cover
            pytest.skip("importlib.metadata unavailable")

        eps = entry_points()
        selected = eps.select(group="console_scripts") if hasattr(eps, "select") else []
        names = {ep.name for ep in selected}
        # May be absent if package not installed in this interpreter — still OK
        # if python -m works; skip soft-fail when not installed.
        if "stackpilot" not in names:
            pytest.skip("stackpilot not installed in this environment")
        ep = next(ep for ep in selected if ep.name == "stackpilot")
        assert ep.value == "stackpilot.cli:main"
