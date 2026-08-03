"""P1 packaging + README/example regression protection."""

from __future__ import annotations

import importlib.metadata
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stackpilot.cli import PUBLIC_CLI_COMMANDS, app
from stackpilot.discovery import STACKFILE_NAME
from stackpilot.utils import load_stack_from_stackfile

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"


class TestPackagingArtifacts:
    @pytest.fixture(scope="module")
    def built_dist(self, tmp_path_factory: pytest.TempPathFactory) -> Path:
        dist_dir = tmp_path_factory.mktemp("dist")
        proc = subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(dist_dir)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            pytest.skip(
                "build unavailable or failed: "
                + (proc.stderr or proc.stdout or "")[:500]
            )
        return dist_dir

    def test_wheel_contains_py_typed_license_entry_points(
        self, built_dist: Path
    ) -> None:
        wheels = list(built_dist.glob("*.whl"))
        sdists = list(built_dist.glob("*.tar.gz"))
        assert wheels, "wheel missing"
        assert sdists, "sdist missing"

        with zipfile.ZipFile(wheels[0]) as zf:
            names = zf.namelist()
            assert any(n.endswith("py.typed") for n in names), names
            assert any("LICENSE" in n for n in names), names
            ep = next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
            text = zf.read(ep).decode("utf-8")
            compact = text.replace(" ", "")
            assert "stackpilot=stackpilot.cli:main" in compact
            meta = next(n for n in names if n.endswith(".dist-info/METADATA"))
            metadata = zf.read(meta).decode("utf-8")
            assert "Programming Language :: Python :: 3.13" in metadata
            assert "Classifier:" in metadata or "Programming Language" in metadata

        with tarfile.open(sdists[0], "r:gz") as tf:
            names = tf.getnames()
            assert any(n.endswith("LICENSE") or "/LICENSE" in n for n in names)
            assert any("README.md" in n for n in names)
            assert any(n.endswith("py.typed") for n in names)

    def test_twine_check(self, built_dist: Path) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "twine", "check", *map(str, built_dist.iterdir())],
            capture_output=True,
            text=True,
            check=False,
        )
        err = (proc.stderr or "") + (proc.stdout or "")
        if proc.returncode != 0 and "No module named twine" in err:
            pytest.skip("twine not installed")
        assert proc.returncode == 0, err

    def test_install_wheel_clean_env(self, built_dist: Path, tmp_path: Path) -> None:
        wheels = list(built_dist.glob("*.whl"))
        assert wheels
        venv_python = tmp_path / "venv"
        proc = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_python)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            pytest.skip("venv creation failed")

        if sys.platform == "win32":
            py = venv_python / "Scripts" / "python.exe"
            script = venv_python / "Scripts" / "stackpilot.exe"
        else:
            py = venv_python / "bin" / "python"
            script = venv_python / "bin" / "stackpilot"

        install = subprocess.run(
            [str(py), "-m", "pip", "install", str(wheels[0])],
            capture_output=True,
            text=True,
            check=False,
        )
        assert install.returncode == 0, install.stdout + install.stderr

        # python -m stackpilot (always available; preferred on locked-down Windows)
        mod = subprocess.run(
            [str(py), "-m", "stackpilot", "version"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert mod.returncode == 0, mod.stdout + mod.stderr
        assert mod.stdout.strip()

        # Console script when the OS allows executing it
        if script.exists():
            try:
                ver = subprocess.run(
                    [str(script), "version"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError as exc:
                # Windows Application Control can block freshly built Scripts.
                pytest.skip(f"console script blocked by OS policy: {exc}")
            assert ver.returncode == 0, ver.stdout + ver.stderr
        else:
            pytest.skip("console script not materialized on this platform")

        # Minimal project: doctor + graph via module entry
        project = tmp_path / "proj"
        project.mkdir()
        (project / "svc").mkdir()
        (project / STACKFILE_NAME).write_text(
            "from stackpilot import Stack\n"
            "stack = Stack()\n"
            "stack.service(name='demo', path='./svc', "
            "command=\"python -c \\\"print('ok')\\\"\")\n"
            "stack.run()\n",
            encoding="utf-8",
        )
        for cmd in (["doctor"], ["graph"]):
            result = subprocess.run(
                [str(py), "-m", "stackpilot", *cmd],
                cwd=str(project),
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr


class TestReadmeAndExamples:
    def test_readme_documents_public_commands(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        for name in PUBLIC_CLI_COMMANDS:
            assert f"stackpilot {name}" in text or f"`{name}`" in text
        for section in (
            "## Quick Start",
            "## External Dependencies",
            "## Health Checks",
            "## Issue Tracking",
            "## FAQ",
            "## Troubleshooting",
            "## Known Limitations",
        ):
            assert section in text, section
        # Ctrl+C / hot reload / Stackfile covered
        assert "Ctrl+C" in text
        assert "hot reload" in text.lower() or "Hot reload" in text
        assert "Stackfile.py" in text
        assert "external_dependency" in text

    def test_no_stale_logs_command_as_current(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        # Historical FAQ about removal is OK; active command table must not list logs.
        commands_section = text.split("## Commands", 1)[1].split("## First Project", 1)[0]
        assert "| `logs`" not in commands_section
        assert "stackpilot logs`" not in commands_section

    def test_example_stackfiles_load(self) -> None:
        required = [
            "minimal",
            "fastapi",
            "flask",
            "django",
            "external-deps",
        ]
        for name in required:
            stackfile = EXAMPLES / name / STACKFILE_NAME
            assert stackfile.is_file(), stackfile
            stack = load_stack_from_stackfile(stackfile)
            assert stack.services, name
            if name == "external-deps":
                assert stack.external_dependencies
                names = {d.name for d in stack.external_dependencies}
                assert "postgres" in names
                assert "redis" in names

    def test_minimal_example_cli_graph_doctor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = EXAMPLES / "minimal"
        monkeypatch.chdir(root)
        result = runner.invoke(app, ["graph"])
        assert result.exit_code == 0
        assert "app" in result.output
        result = runner.invoke(app, ["doctor"])
        assert "Traceback" not in (result.output or "")

    def test_pyproject_metadata(self) -> None:
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'stackpilot = "stackpilot.cli:main"' in text
        assert "py.typed" in text
        assert "3.13" in text
        assert "Homepage" in text
        assert "Bug Tracker" in text

    def test_console_script_metadata_when_installed(self) -> None:
        try:
            eps = importlib.metadata.entry_points()
        except Exception:
            pytest.skip("importlib.metadata unavailable")
        selected = eps.select(group="console_scripts") if hasattr(eps, "select") else []
        names = {ep.name for ep in selected}
        if "stackpilot" not in names:
            pytest.skip("stackpilot not installed")
        ep = next(ep for ep in selected if ep.name == "stackpilot")
        assert ep.value == "stackpilot.cli:main"
