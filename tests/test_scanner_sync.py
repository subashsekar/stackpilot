from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stackpilot.cli import app
from stackpilot.discovery import STACKFILE_NAME
from stackpilot.generator import (
    BASE_PORT,
    StackfileExistsError,
    assign_ports,
    build_command,
    generate_stackfile,
    write_stackfile,
)
from stackpilot.scanner import (
    IGNORED_DIRECTORY_NAMES,
    ServiceInfo,
    detect_framework,
    scan_directory,
    scan_project,
)
from stackpilot.sync import sync_project
from stackpilot.utils import load_stack_from_stackfile

runner = CliRunner()


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _fastapi_main(path: Path) -> Path:
    return _write(
        path,
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/health')\n"
        "def health():\n"
        "    return {'ok': True}\n",
    )


def _flask_app(path: Path) -> Path:
    return _write(
        path,
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "@app.get('/')\n"
        "def index():\n"
        "    return 'ok'\n"
        "if __name__ == '__main__':\n"
        "    app.run()\n",
    )


class TestFrameworkDetection:
    def test_detects_fastapi_from_main_py(self, tmp_path: Path) -> None:
        service = tmp_path / "gateway"
        _fastapi_main(service / "main.py")
        assert detect_framework(service) == "FastAPI"

    def test_detects_fastapi_from_app_py(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _fastapi_main(service / "app.py")
        assert detect_framework(service) == "FastAPI"

    def test_bare_main_py_is_generic(self, tmp_path: Path) -> None:
        service = tmp_path / "gateway"
        _write(service / "main.py", "print('hello')\n")
        assert detect_framework(service) == "Generic"

    def test_detects_django(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(service / "manage.py", "#!/usr/bin/env python\n")
        _write(service / "project" / "settings.py", "DEBUG = True\n")
        assert detect_framework(service) == "Django"

    def test_detects_flask_from_app_py(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _flask_app(service / "app.py")
        assert detect_framework(service) == "Flask"

    def test_detects_celery(self, tmp_path: Path) -> None:
        service = tmp_path / "worker"
        _write(service / "celery.py", "from celery import Celery\n")
        assert detect_framework(service) == "Celery"

    def test_bare_package_json_is_generic(self, tmp_path: Path) -> None:
        service = tmp_path / "frontend"
        _write(service / "package.json", '{"name": "frontend"}\n')
        assert detect_framework(service) == "Generic"

    def test_detects_generic_from_run_py(self, tmp_path: Path) -> None:
        service = tmp_path / "jobs"
        _write(service / "run.py", "print('run')\n")
        assert detect_framework(service) == "Generic"

    def test_unknown_folder_returns_none(self, tmp_path: Path) -> None:
        service = tmp_path / "docs"
        service.mkdir()
        _write(service / "README.md", "# docs\n")
        assert detect_framework(service) is None
        assert scan_directory(service) is None


class TestIgnoredDirectories:
    def test_ignored_names_match_day5_list(self) -> None:
        expected = {
            ".git",
            ".github",
            ".idea",
            ".vscode",
            ".venv",
            "venv",
            "env",
            "virtualenv",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            "dist",
            "build",
            ".stackpilot",
        }
        assert IGNORED_DIRECTORY_NAMES == frozenset(expected)

    def test_does_not_scan_ignored_directories(self, tmp_path: Path) -> None:
        for name in (".git", ".venv", "node_modules", ".stackpilot", "build"):
            decoy = tmp_path / name / "secret-service"
            _fastapi_main(decoy / "main.py")

        real = tmp_path / "auth"
        _fastapi_main(real / "main.py")

        services = scan_project(tmp_path)
        assert [s.name for s in services] == ["auth"]


class TestNestedScanning:
    def test_finds_nested_services(self, tmp_path: Path) -> None:
        _fastapi_main(tmp_path / "apps" / "gateway" / "main.py")
        _write(tmp_path / "apps" / "auth" / "manage.py", "")
        _write(tmp_path / "apps" / "auth" / "auth" / "settings.py", "")
        _write(tmp_path / "workers" / "tasks" / "celery.py", "from celery import Celery\n")

        services = scan_project(tmp_path)
        by_name = {s.name: s.framework for s in services}

        assert by_name == {
            "gateway": "FastAPI",
            "auth": "Django",
            "tasks": "Celery",
        }

    def test_compose_postgres_at_monorepo_does_not_hide_apps(
        self, tmp_path: Path
    ) -> None:
        """Parent-folder sync must still find nested apps under compose roots."""
        monorepo = tmp_path / "enterprise-test-platform"
        _write(
            monorepo / "docker-compose.yml",
            "services:\n"
            "  postgres:\n"
            "    image: postgres:16\n"
            "  redis:\n"
            "    image: redis:7\n",
        )
        _fastapi_main(monorepo / "services" / "auth_service" / "main.py")
        _fastapi_main(monorepo / "services" / "admin_service" / "main.py")
        _write(monorepo / "services" / "web" / "manage.py", "")
        _write(monorepo / "services" / "web" / "web" / "settings.py", "")

        services = scan_project(tmp_path)
        by_name = {s.name: s.framework for s in services}

        assert by_name["auth_service"] == "FastAPI"
        assert by_name["admin_service"] == "FastAPI"
        assert by_name["web"] == "Django"
        assert by_name["postgres"] == "PostgreSQL"
        # Monorepo folder name must not become the external dependency name.
        assert "enterprise-test-platform" not in by_name

        text = generate_stackfile(services, project_root=tmp_path)
        assert 'name="postgres"' in text
        assert "stack.external_dependency(" in text
        assert text.count("stack.service(") == 3

    def test_dedicated_postgres_folder_wins_over_compose_root(
        self, tmp_path: Path
    ) -> None:
        monorepo = tmp_path / "platform"
        _write(
            monorepo / "docker-compose.yml",
            "services:\n  postgres:\n    image: postgres:16\n",
        )
        _write(monorepo / "postgres" / "postgresql.conf", "port = 5432\n")
        # Avoid ``api/main.py`` — FastAPI treats that relative path as an
        # entrypoint for the parent directory itself.
        _fastapi_main(monorepo / "services" / "auth" / "main.py")

        services = scan_project(tmp_path)
        by_name = {s.name: s for s in services}

        assert set(by_name) == {"auth", "postgres"}
        assert by_name["postgres"].path == (monorepo / "postgres").resolve()
        assert by_name["auth"].framework == "FastAPI"


class TestScanDirectory:
    def test_returns_service_info(self, tmp_path: Path) -> None:
        service = tmp_path / "users"
        _fastapi_main(service / "main.py")

        info = scan_directory(service)
        assert info is not None
        assert info == ServiceInfo(
            name="users",
            path=service.resolve(),
            framework="FastAPI",
        )


class TestStackfileGeneration:
    def test_generates_one_service(self, tmp_path: Path) -> None:
        gateway = tmp_path / "gateway"
        _fastapi_main(gateway / "main.py")
        services = scan_project(tmp_path)

        text = generate_stackfile(services, project_root=tmp_path)

        assert text.startswith("from stackpilot import Stack, HttpHealthCheck\n")
        assert 'name="gateway"' in text
        assert 'path="./gateway"' in text
        assert (
            'command="python -m uvicorn main:app --reload '
            '--host 0.0.0.0 --port 8000"' in text
        )
        assert "port=8000," in text
        assert text.strip().endswith("stack.run()")

    def test_generates_five_services(self, tmp_path: Path) -> None:
        names = ["gateway", "auth", "users", "payments", "email"]
        for name in names:
            _fastapi_main(tmp_path / name / "main.py")

        services = scan_project(tmp_path)
        text = generate_stackfile(services, project_root=tmp_path)
        ports = assign_ports(services)

        assert len(services) == 5
        assert len(ports) == 5
        assert ports == list(range(BASE_PORT, BASE_PORT + 5))
        for service, port in zip(services, ports, strict=True):
            assert f'name="{service.name}"' in text
            assert f"--port {port}" in text
        assert text.count("stack.service(") == 5
        assert text.strip().endswith("stack.run()")

    def test_mixed_fastapi_and_flask(self, tmp_path: Path) -> None:
        _fastapi_main(tmp_path / "api" / "main.py")
        _flask_app(tmp_path / "web" / "app.py")

        services = scan_project(tmp_path)
        text = generate_stackfile(services, project_root=tmp_path)

        assert {s.framework for s in services} == {"FastAPI", "Flask"}
        assert "python -m uvicorn main:app --reload" in text
        assert 'command="python app.py"' in text
        assert "--port 8000" in text
        assert "port=8001," in text

    def test_duplicate_port_prevention(self, tmp_path: Path) -> None:
        for name in ("a", "b", "c", "d"):
            _fastapi_main(tmp_path / name / "main.py")

        services = scan_project(tmp_path)
        ports = assign_ports(services)
        text = generate_stackfile(services, project_root=tmp_path)

        assert len(ports) == len(set(ports))
        found = [int(p) for p in re.findall(r"--port (\d+)", text)]
        assert found == ports
        assert len(found) == len(set(found))

    def test_generic_uses_python_main(self, tmp_path: Path) -> None:
        service = tmp_path / "auth"
        _write(service / "main.py", "print('auth')\n")
        info = scan_directory(service)
        assert info is not None
        assert build_command(info, 8000) == "python main.py"


class TestOverwriteProtection:
    def test_write_stackfile_aborts_without_force(self, tmp_path: Path) -> None:
        _fastapi_main(tmp_path / "auth" / "main.py")
        existing = tmp_path / STACKFILE_NAME
        existing.write_text("# existing\n", encoding="utf-8")
        services = scan_project(tmp_path)

        with pytest.raises(StackfileExistsError):
            write_stackfile(services, project_root=tmp_path, force=False)

        assert existing.read_text(encoding="utf-8") == "# existing\n"

    def test_write_stackfile_force_overwrites(self, tmp_path: Path) -> None:
        _fastapi_main(tmp_path / "auth" / "main.py")
        existing = tmp_path / STACKFILE_NAME
        existing.write_text("# existing\n", encoding="utf-8")
        services = scan_project(tmp_path)

        result = write_stackfile(services, project_root=tmp_path, force=True)

        assert result.overwritten is True
        content = existing.read_text(encoding="utf-8")
        assert 'name="auth"' in content
        assert "command=" in content
        assert "stack.run()" in content

    def test_sync_prompts_then_declines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fastapi_main(tmp_path / "auth" / "main.py")
        existing = tmp_path / STACKFILE_NAME
        existing.write_text("# keep\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["sync"], input="n\n")
        assert result.exit_code == 1
        assert existing.read_text(encoding="utf-8") == "# keep\n"

    def test_sync_prompts_then_accepts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fastapi_main(tmp_path / "auth" / "main.py")
        existing = tmp_path / STACKFILE_NAME
        existing.write_text("# keep\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["sync"], input="y\n")
        assert result.exit_code == 0
        content = existing.read_text(encoding="utf-8")
        assert 'name="auth"' in content
        assert "python -m uvicorn" in content

    def test_sync_force_skips_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fastapi_main(tmp_path / "auth" / "main.py")
        (tmp_path / STACKFILE_NAME).write_text("# keep\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["sync", "--force"])
        assert result.exit_code == 0
        content = (tmp_path / STACKFILE_NAME).read_text(encoding="utf-8")
        assert 'name="auth"' in content


class TestSyncCli:
    def test_sync_creates_runnable_stackfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("gateway", "auth", "users", "payment"):
            _fastapi_main(tmp_path / name / "main.py")

        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["sync"])

        assert result.exit_code == 0
        assert "Scanning project..." in result.output
        assert "Generated Stackfile.py" in result.output
        assert "Found 4 services." in result.output

        stack = load_stack_from_stackfile(tmp_path / STACKFILE_NAME)
        assert len(stack.services) == 4
        assert all(spec.command for spec in stack.services)
        ports = [
            int(re.search(r"--port (\d+)", spec.command).group(1))  # type: ignore[union-attr]
            for spec in stack.services
        ]
        assert ports == sorted(ports)
        assert len(set(ports)) == 4

    def test_sync_from_subdirectory_updates_nearest_stackfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sync must discover the nearest Stackfile.py like other CLI commands."""

        _fastapi_main(tmp_path / "auth" / "main.py")
        nested = tmp_path / "apps" / "api"
        nested.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        create = runner.invoke(app, ["sync"])
        assert create.exit_code == 0
        stackfile = tmp_path / STACKFILE_NAME
        assert stackfile.is_file()

        monkeypatch.chdir(nested)
        result = runner.invoke(app, ["sync", "--force"])
        assert result.exit_code == 0
        assert stackfile.is_file()
        assert not (nested / STACKFILE_NAME).exists()
        content = stackfile.read_text(encoding="utf-8")
        assert 'name="auth"' in content


class TestSyncLibraryConfirm:
    def test_sync_project_force(self, tmp_path: Path) -> None:
        _write(tmp_path / "auth" / "main.py", "print('x')\n")
        (tmp_path / STACKFILE_NAME).write_text("# old\n", encoding="utf-8")

        result = sync_project(project_root=tmp_path, force=True)
        assert result.overwritten is True
        content = result.output_path.read_text(encoding="utf-8")
        assert 'command="python main.py"' in content
