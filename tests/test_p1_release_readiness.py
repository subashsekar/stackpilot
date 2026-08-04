"""P1 release-readiness regressions (NestJS labeling, docs, example ports)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stackpilot.cli import PUBLIC_CLI_COMMANDS, app
from stackpilot.config import ServiceSpec, Stack
from stackpilot.discovery import STACKFILE_NAME
from stackpilot.generator import generate_stackfile
from stackpilot.graph_view import (
    _FRAMEWORK_PATH_CACHE,
    _detect_framework_label,
    format_architecture_report,
)
from stackpilot.scanner import scan_project
from stackpilot.utils import load_stack_from_stackfile

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_ROOT = REPO_ROOT / "examples"
DOC_FILES = (
    "README.md",
    "FAQ.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "examples/README.md",
)

# HTTP example projects → unique Stackfile ports (not all on 8000).
EXAMPLE_PORTS: dict[str, dict[str, int]] = {
    "fastapi": {"api": 8001},
    "flask": {"web": 8002},
    "django": {"web": 8003},
    "express": {"app": 8004},
    "nestjs": {"app": 8005},
    "external-deps": {"auth": 8006, "gateway": 8007},
}


@pytest.fixture(autouse=True)
def _clear_framework_label_cache() -> None:
    _FRAMEWORK_PATH_CACHE.clear()
    yield
    _FRAMEWORK_PATH_CACHE.clear()


class TestNestJSFrameworkLabeling:
    def test_npm_start_dev_labeled_nestjs_not_express(self, tmp_path: Path) -> None:
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "package.json").write_text(
            '{"dependencies":{"@nestjs/core":"10.0.0"},'
            '"scripts":{"start:dev":"node main.js"}}\n',
            encoding="utf-8",
        )
        (app_dir / "main.js").write_text(
            "require('http').createServer().listen(8005);\n",
            encoding="utf-8",
        )
        spec = ServiceSpec(
            name="app",
            path=app_dir,
            command="npm run start:dev",
            port=8005,
        )
        assert _detect_framework_label(spec) == "NestJS"

    def test_express_npm_dev_still_labeled_express(self, tmp_path: Path) -> None:
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "package.json").write_text(
            '{"dependencies":{"express":"4.18.0"},'
            '"scripts":{"dev":"node server.js"}}\n',
            encoding="utf-8",
        )
        (app_dir / "server.js").write_text(
            "require('express')().listen(8004);\n",
            encoding="utf-8",
        )
        spec = ServiceSpec(
            name="app",
            path=app_dir,
            command="npm run dev",
            port=8004,
        )
        assert _detect_framework_label(spec) == "Express"

    def test_nestjs_example_graph_shows_nestjs_bracket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = EXAMPLES_ROOT / "nestjs"
        monkeypatch.chdir(root)
        result = runner.invoke(app, ["graph"])
        assert result.exit_code == 0, result.output
        assert "NestJS" in result.output
        assert "[Express]" not in result.output
        assert "Applications" in result.output or "NestJS" in result.output

    def test_nestjs_beats_express_when_both_deps_present(self, tmp_path: Path) -> None:
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "package.json").write_text(
            '{"dependencies":{"@nestjs/core":"10","express":"4"},'
            '"scripts":{"start:dev":"nest start --watch"}}\n',
            encoding="utf-8",
        )
        spec = ServiceSpec(
            name="app",
            path=app_dir,
            command="npm run start:dev",
        )
        assert _detect_framework_label(spec) == "NestJS"


class TestExamplePorts:
    def test_http_examples_use_unique_ports(self) -> None:
        seen: set[int] = set()
        for project, services in EXAMPLE_PORTS.items():
            stack = load_stack_from_stackfile(
                EXAMPLES_ROOT / project / STACKFILE_NAME
            )
            by_name = {s.name: s for s in stack.services}
            for name, port in services.items():
                assert name in by_name, f"{project}/{name}"
                assert by_name[name].port == port, f"{project}/{name}"
                assert port not in seen, f"duplicate port {port}"
                seen.add(port)
                assert port != 8000
        assert len(seen) == sum(len(v) for v in EXAMPLE_PORTS.values())

    def test_example_health_urls_match_ports(self) -> None:
        for project, services in EXAMPLE_PORTS.items():
            stack = load_stack_from_stackfile(
                EXAMPLES_ROOT / project / STACKFILE_NAME
            )
            for svc in stack.services:
                if svc.name not in services:
                    continue
                port = services[svc.name]
                hc = svc.health_check
                url = getattr(hc, "url", None)
                if url:
                    assert f":{port}" in url, f"{project}/{svc.name}: {url}"

    def test_framework_examples_match_generator_ports(self) -> None:
        for name in ("fastapi", "flask", "django", "express", "nestjs"):
            project = EXAMPLES_ROOT / name
            services = scan_project(project)
            expected = generate_stackfile(services, project_root=project)
            actual = (project / STACKFILE_NAME).read_text(encoding="utf-8")
            assert actual == expected, name
            for port in EXAMPLE_PORTS[name].values():
                assert f"port={port}," in actual


class TestDocumentationConsistency:
    def test_public_commands_documented_in_readme(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "Nine commands" not in text
        assert "Ten commands" in text
        commands_section = text.split("## Commands", 1)[1].split(
            "## First Project", 1
        )[0]
        for name in PUBLIC_CLI_COMMANDS:
            assert f"stackpilot {name}" in text or f"`{name}`" in commands_section
            assert f"### `stackpilot {name}`" in commands_section

    def test_docs_have_no_placeholders(self) -> None:
        banned = (
            "coming soon",
            "TODO",
            "FIXME",
            "TBD",
            "placeholder",
            "lorem ipsum",
        )
        for rel in DOC_FILES:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            lower = text.lower()
            for token in banned:
                # Allow historical FAQ mentioning removed `logs` command.
                assert token not in lower, f"{rel} contains {token!r}"

    def test_docs_agree_on_frozen_cli(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        for name in PUBLIC_CLI_COMMANDS:
            assert name in contributing or f"`{name}`" in contributing or name in readme
        # CONTRIBUTING must list stop alongside the rest.
        assert "stop" in contributing
        assert "stackpilot stop" in readme

    def test_example_readme_health_urls_match_stackfiles(self) -> None:
        for project, services in EXAMPLE_PORTS.items():
            readme = (EXAMPLES_ROOT / project / "README.md").read_text(
                encoding="utf-8"
            )
            for port in services.values():
                assert f"127.0.0.1:{port}" in readme, f"{project} missing :{port}"

    def test_changelog_0_1_0_owns_shipped_features(self) -> None:
        text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert re.search(r"^## \[0\.1\.0\]", text, re.M)
        unreleased = text.split("## [0.1.0]", 1)[0]
        # Unreleased may only hold the heading — no shipped feature bullets.
        body = unreleased.split("## [Unreleased]", 1)[-1].strip()
        assert "Issue Tracker" not in body
        assert "stackpilot doctor" not in body
        assert "startup waves" not in body.lower()
        section_010 = text.split("## [0.1.0]", 1)[1]
        assert "stackpilot stop" in section_010 or "`stop`" in section_010
        # Historical mistake: stop must not be listed as removed in 0.1.0.
        removed = ""
        if "### Removed" in section_010:
            removed = section_010.split("### Removed", 1)[1]
            next_ver = re.search(r"^## \[", removed, re.M)
            if next_ver:
                removed = removed[: next_ver.start()]
        assert "stackpilot stop" not in removed

    def test_readme_example_paths_exist(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        for match in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", text):
            target = match.group(2)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = (REPO_ROOT / target.split("#", 1)[0]).resolve()
            assert path.exists(), f"broken README link: {target}"

    def test_pyproject_and_version_consistent(self) -> None:
        from stackpilot import __version__

        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'stackpilot = "stackpilot.cli:main"' in pyproject
        assert 'version = {attr = "stackpilot.__version__"}' in pyproject
        assert __version__ == "0.1.0"
        assert PUBLIC_CLI_COMMANDS == (
            "init",
            "sync",
            "run",
            "stop",
            "graph",
            "status",
            "ps",
            "issues",
            "doctor",
            "version",
        )
