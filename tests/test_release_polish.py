"""Release polish: examples layout and packaging metadata stay consistent."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stackpilot.cli import PUBLIC_CLI_COMMANDS, app
from stackpilot.discovery import STACKFILE_NAME
from stackpilot.generator import generate_stackfile
from stackpilot.scanner import scan_project

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_ROOT = REPO_ROOT / "examples"

EXPECTED_EXAMPLES = (
    ("fastapi", "api", "FastAPI"),
    ("flask", "web", "Flask"),
    ("django", "web", "Django"),
    ("celery", "worker", "Celery"),
    ("express", "app", "Express"),
    ("nestjs", "app", "NestJS"),
)


class TestExamplesLayout:
    def test_examples_exist_with_stackfile_and_readme(self) -> None:
        assert (EXAMPLES_ROOT / "README.md").is_file()
        for name, service_dir, _framework in EXPECTED_EXAMPLES:
            project = EXAMPLES_ROOT / name
            assert project.is_dir(), name
            assert (project / "README.md").is_file(), name
            assert (project / STACKFILE_NAME).is_file(), name
            assert (project / service_dir).is_dir(), f"{name}/{service_dir}"

    def test_examples_scan_to_expected_framework(self) -> None:
        for name, service_dir, framework in EXPECTED_EXAMPLES:
            project = EXAMPLES_ROOT / name
            services = scan_project(project)
            assert len(services) == 1, name
            assert services[0].name == service_dir
            assert services[0].framework == framework

    def test_example_stackfiles_match_generator(self) -> None:
        for name, _service_dir, _framework in EXPECTED_EXAMPLES:
            project = EXAMPLES_ROOT / name
            services = scan_project(project)
            expected = generate_stackfile(services, project_root=project)
            actual = (project / STACKFILE_NAME).read_text(encoding="utf-8")
            assert actual == expected, name


class TestCliHelpPolish:
    def test_root_help_mentions_examples(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "StackPilot" in result.output
        for name in PUBLIC_CLI_COMMANDS:
            assert name in result.output

    def test_run_help_includes_examples(self) -> None:
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "stackpilot run" in result.output

    def test_missing_stackfile_points_to_init_sync_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 1
        combined = (result.output or "") + (getattr(result, "stderr", None) or "")
        assert "No Stackfile.py found." in combined
        assert "stackpilot init" in combined
        assert "stackpilot sync" in combined
        assert "stackpilot run" in combined


class TestPackagingMetadata:
    def test_pyproject_has_release_urls_and_typed_marker(self) -> None:
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'name = "stackpilot"' in text
        assert "Homepage" in text
        assert "Repository" in text
        assert "Documentation" in text
        assert "Bug Tracker" in text
        assert 'stackpilot = "stackpilot.cli:main"' in text
        assert 'stackpilot = ["py.typed"]' in text
        assert (REPO_ROOT / "src" / "stackpilot" / "py.typed").is_file()

    def test_build_sdist_and_wheel(self, tmp_path: Path) -> None:
        try:
            import build  # noqa: F401
        except ImportError:
            pytest.skip("build package not installed")

        out = tmp_path / "dist"
        proc = subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(out)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
        artifacts = list(out.iterdir())
        assert any(p.suffix == ".whl" for p in artifacts)
        assert any(p.name.endswith(".tar.gz") for p in artifacts)
