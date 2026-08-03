"""Tests for improved project detection (layouts, PMs, ports, validation)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from stackpilot.adapters.celery import CeleryAdapter
from stackpilot.adapters.detect import (
    detect_asgi_entrypoint,
    detect_flask_entrypoint,
    detect_node_package_manager,
    detect_preferred_port,
    detect_python_package_manager,
    python_run_prefix,
    resolve_python_executable,
    validate_detected_services,
)
from stackpilot.adapters.detect.venv import detect_venv_dir
from stackpilot.adapters.django import DjangoAdapter
from stackpilot.adapters.express import ExpressAdapter
from stackpilot.adapters.fastapi import FastAPIAdapter
from stackpilot.adapters.flask import FlaskAdapter
from stackpilot.adapters.nestjs import NestJSAdapter
from stackpilot.generator import assign_ports, generate_stackfile
from stackpilot.scanner import ServiceInfo, detect_framework, scan_project
from stackpilot.sync import sync_project


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _touch_venv(directory: Path, name: str = ".venv") -> Path:
    venv = directory / name
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    _write(venv / "pyvenv.cfg", "home = /usr\n")
    python = venv / "bin" / "python"
    python.write_text("", encoding="utf-8")
    # On Windows the resolver prefers Scripts/python.exe when present.
    scripts = venv / "Scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "python.exe").write_text("", encoding="utf-8")
    return venv


class TestFastAPILayouts:
    def test_app_main_layout(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "app" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        assert FastAPIAdapter().detect(service) is True
        entry = detect_asgi_entrypoint(service)
        assert entry is not None
        assert entry.target == "app.main:app"
        spec = FastAPIAdapter().generate_service(service, port=8080)
        assert "app.main:app" in spec.command
        assert "--port 8080" in spec.command

    def test_src_main_layout(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "src" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        entry = detect_asgi_entrypoint(service)
        assert entry is not None
        assert entry.module == "main"
        assert entry.app_dir == "src"
        spec = FastAPIAdapter().generate_service(service, port=8000)
        assert "main:app" in spec.command
        assert "--app-dir src" in spec.command

    def test_api_main_layout(self, tmp_path: Path) -> None:
        service = tmp_path / "svc"
        _write(
            service / "api" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        assert detect_framework(service) == "FastAPI"
        entry = detect_asgi_entrypoint(service)
        assert entry is not None
        assert entry.target == "api.main:app"

    def test_server_py(self, tmp_path: Path) -> None:
        service = tmp_path / "svc"
        _write(
            service / "server.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        assert detect_framework(service) == "FastAPI"
        spec = FastAPIAdapter().generate_service(service, port=8000)
        assert "server:app" in spec.command

    def test_application_attr(self, tmp_path: Path) -> None:
        service = tmp_path / "svc"
        _write(
            service / "application.py",
            "from fastapi import FastAPI\napplication = FastAPI()\n",
        )
        entry = detect_asgi_entrypoint(service)
        assert entry is not None
        assert entry.target == "application:application"

    def test_apirouter_signal(self, tmp_path: Path) -> None:
        service = tmp_path / "svc"
        _write(
            service / "main.py",
            "from fastapi import APIRouter\nrouter = APIRouter()\n",
        )
        assert FastAPIAdapter().detect(service) is True

    def test_nested_package_not_double_scanned(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "gateway" / "app" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        services = scan_project(tmp_path)
        assert [s.name for s in services] == ["gateway"]
        assert services[0].framework == "FastAPI"


class TestFlaskLayouts:
    def test_create_app_factory(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(
            service / "app.py",
            "from flask import Flask\n\ndef create_app():\n    return Flask(__name__)\n",
        )
        assert FlaskAdapter().detect(service) is True
        entry = detect_flask_entrypoint(service)
        assert entry is not None
        assert entry.is_factory is True
        spec = FlaskAdapter().generate_service(service, port=5000)
        assert "flask --app app:create_app run" in spec.command
        assert "--port 5000" in spec.command

    def test_wsgi_module(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(
            service / "wsgi.py",
            "from flask import Flask\napp = Flask(__name__)\n",
        )
        assert detect_framework(service) == "Flask"
        spec = FlaskAdapter().generate_service(service, port=5000)
        assert "flask --app wsgi:app run" in spec.command


class TestDjangoLayouts:
    def test_wsgi_without_settings_still_detects(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(service / "manage.py", "import django\n")
        _write(service / "project" / "wsgi.py", "application = None\n")
        assert DjangoAdapter().detect(service) is True

    def test_asgi_and_settings(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(service / "manage.py", "")
        _write(service / "config" / "settings.py", "DEBUG = True\n")
        _write(service / "config" / "asgi.py", "application = None\n")
        assert detect_framework(service) == "Django"


class TestExpressAndNestLayouts:
    def test_express_pnpm(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "package.json",
            '{"dependencies":{"express":"4.0.0"},"scripts":{"dev":"node index.js"}}\n',
        )
        _write(service / "pnpm-lock.yaml", "lockfileVersion: 9\n")
        assert ExpressAdapter().detect(service) is True
        spec = ExpressAdapter().generate_service(service)
        assert spec.command == "pnpm run dev"

    def test_express_yarn(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "package.json",
            '{"dependencies":{"express":"4"},"scripts":{"dev":"node index.js"}}\n',
        )
        _write(service / "yarn.lock", "# yarn\n")
        assert ExpressAdapter().generate_service(service).command == "yarn dev"

    def test_express_bun(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "package.json",
            '{"dependencies":{"express":"4"},"scripts":{"dev":"node index.js"}}\n',
        )
        _write(service / "bun.lockb", "")
        assert ExpressAdapter().generate_service(service).command == "bun run dev"

    def test_nestjs_npm(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "package.json",
            '{"dependencies":{"@nestjs/core":"10"},"scripts":{"start:dev":"nest start"}}\n',
        )
        _write(service / "package-lock.json", "{}")
        assert NestJSAdapter().generate_service(service).command == "npm run start:dev"


class TestPackageManagers:
    def test_poetry(self, tmp_path: Path) -> None:
        _write(tmp_path / "pyproject.toml", "[tool.poetry]\nname='x'\n")
        assert detect_python_package_manager(tmp_path) == "poetry"

    def test_uv_lock(self, tmp_path: Path) -> None:
        _write(tmp_path / "uv.lock", "version = 1\n")
        assert detect_python_package_manager(tmp_path) == "uv"

    def test_pipenv(self, tmp_path: Path) -> None:
        _write(tmp_path / "Pipfile", "[[source]]\n")
        assert detect_python_package_manager(tmp_path) == "pipenv"

    def test_pip_requirements(self, tmp_path: Path) -> None:
        _write(tmp_path / "requirements.txt", "fastapi\n")
        assert detect_python_package_manager(tmp_path) == "pip"

    def test_node_managers(self, tmp_path: Path) -> None:
        _write(tmp_path / "package.json", "{}")
        assert detect_node_package_manager(tmp_path) == "npm"
        _write(tmp_path / "yarn.lock", "")
        assert detect_node_package_manager(tmp_path) == "yarn"
        _write(tmp_path / "pnpm-lock.yaml", "")
        assert detect_node_package_manager(tmp_path) == "pnpm"
        _write(tmp_path / "bun.lock", "")
        assert detect_node_package_manager(tmp_path) == "bun"

    def test_fastapi_uses_poetry_runner(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(service / "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        _write(service / "poetry.lock", "")
        _write(service / "pyproject.toml", "[tool.poetry]\nname='api'\n")
        with patch(
            "stackpilot.adapters.detect.package_manager.cli_is_runnable",
            return_value=True,
        ):
            spec = FastAPIAdapter().generate_service(service, port=8000)
        assert spec.command.startswith("poetry run python -m uvicorn")

    def test_python_run_prefix_falls_back_when_uv_unrunnable(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "uv.lock", "version = 1\n")
        with patch(
            "stackpilot.adapters.detect.package_manager.cli_is_runnable",
            return_value=False,
        ):
            assert python_run_prefix(tmp_path, python="python") == "python"

    def test_python_run_prefix_uses_uv_when_runnable(self, tmp_path: Path) -> None:
        _write(tmp_path / "uv.lock", "version = 1\n")
        with patch(
            "stackpilot.adapters.detect.package_manager.cli_is_runnable",
            return_value=True,
        ):
            assert python_run_prefix(tmp_path) == "uv run python"


class TestVenvDetection:
    def test_detects_dot_venv(self, tmp_path: Path) -> None:
        _touch_venv(tmp_path, ".venv")
        assert detect_venv_dir(tmp_path) is not None
        exe = resolve_python_executable(tmp_path)
        assert exe != "python"
        assert "python" in exe

    def test_walks_parents_for_monorepo_venv(self, tmp_path: Path) -> None:
        _touch_venv(tmp_path, ".venv")
        (tmp_path / "uv.lock").write_text("", encoding="utf-8")
        service = tmp_path / "services" / "admin_service"
        service.mkdir(parents=True)
        found = detect_venv_dir(service)
        assert found is not None
        assert found.resolve() == (tmp_path / ".venv").resolve()
        # Stackfile keeps bare python; spawn resolves the parent venv.
        assert resolve_python_executable(service) == "python"

    def test_prefers_service_local_venv_over_parent(self, tmp_path: Path) -> None:
        _touch_venv(tmp_path, ".venv")
        (tmp_path / "Stackfile.py").write_text("stack = None\n", encoding="utf-8")
        service = tmp_path / "services" / "api"
        service.mkdir(parents=True)
        _touch_venv(service, ".venv")
        found = detect_venv_dir(service)
        assert found is not None
        assert found.resolve() == (service / ".venv").resolve()


class TestPortDetection:
    def test_annotated_port_in_nested_config(self, tmp_path: Path) -> None:
        service = tmp_path / "auth"
        _write(
            service / "app" / "core" / "config.py",
            "SERVICE_NAME = 'auth'\nPORT: int = 8001\n",
        )
        assert detect_preferred_port(service) == 8001

    def test_env_port(self, tmp_path: Path) -> None:
        _write(tmp_path / ".env", "PORT=9090\nDEBUG=1\n")
        assert detect_preferred_port(tmp_path) == 9090

    def test_compose_port(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "docker-compose.yml",
            "services:\n  api:\n    ports:\n      - '8088:8000'\n",
        )
        assert detect_preferred_port(tmp_path) == 8088

    def test_compose_yaml(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "compose.yaml",
            "services:\n  web:\n    ports:\n      - 3001:3000\n",
        )
        assert detect_preferred_port(tmp_path) == 3001

    def test_assign_ports_prefers_env(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(service / "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        _write(service / ".env", "PORT=7777\n")
        services = scan_project(tmp_path)
        ports = assign_ports(services)
        assert ports == [7777]


class TestValidationWarnings:
    def test_fastapi_missing_uvicorn_warns(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(service / "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        warnings = validate_detected_services(
            [ServiceInfo(name="api", path=service, framework="FastAPI")]
        )
        assert any("uvicorn" in w.message for w in warnings)

    def test_django_missing_manage_warns(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(service / "config" / "settings.py", "DEBUG=True\n")
        _write(service / "config" / "wsgi.py", "")
        # Detect without manage.py via settings+wsgi
        assert DjangoAdapter().detect(service) is True
        warnings = validate_detected_services(
            [ServiceInfo(name="web", path=service, framework="Django")]
        )
        assert any("manage.py" in w.message for w in warnings)

    def test_express_missing_package_json_warns(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        service.mkdir()
        warnings = validate_detected_services(
            [ServiceInfo(name="api", path=service, framework="Express")]
        )
        assert any("package.json" in w.message for w in warnings)

    def test_sync_continues_with_warnings(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "api" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        result = sync_project(project_root=tmp_path, force=True)
        assert result.output_path.is_file()
        assert any("uvicorn" in w for w in result.warnings)


class TestGeneratorOutput:
    def test_readable_grouped_stackfile(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "gateway" / "main.py",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "@app.get('/health')\n"
            "def health():\n"
            "    return {'ok': True}\n",
        )
        _write(
            tmp_path / "worker" / "celery.py",
            "from celery import Celery\napp = Celery('worker')\n",
        )
        services = scan_project(tmp_path)
        text = generate_stackfile(services, project_root=tmp_path)
        assert "stack = Stack()" in text
        assert 'name="gateway"' in text
        assert "HttpHealthCheck" in text
        # Process health omitted (runtime default) — no duplicated defaults.
        assert "ProcessHealthCheck" not in text
        assert text.strip().endswith("stack.run()")


class TestCeleryRegression:
    def test_celery_still_generates_worker(self, tmp_path: Path) -> None:
        service = tmp_path / "worker"
        _write(
            service / "celery.py",
            "from celery import Celery\napp = Celery('tasks')\n",
        )
        assert CeleryAdapter().detect(service) is True
        assert CeleryAdapter().generate_service(service).command == (
            "celery -A celery worker"
        )

    def test_celery_uses_uv(self, tmp_path: Path) -> None:
        service = tmp_path / "worker"
        _write(
            service / "celery.py",
            "from celery import Celery\napp = Celery('tasks')\n",
        )
        _write(service / "uv.lock", "version = 1\n")
        with patch(
            "stackpilot.adapters.celery.cli_is_runnable",
            return_value=True,
        ):
            assert CeleryAdapter().generate_service(service).command == (
                "uv run celery -A celery worker"
            )

    def test_celery_falls_back_when_uv_unrunnable(self, tmp_path: Path) -> None:
        service = tmp_path / "worker"
        _write(
            service / "celery.py",
            "from celery import Celery\napp = Celery('tasks')\n",
        )
        _write(service / "uv.lock", "version = 1\n")
        with patch(
            "stackpilot.adapters.celery.cli_is_runnable",
            return_value=False,
        ):
            assert CeleryAdapter().generate_service(service).command == (
                "celery -A celery worker"
            )


class TestIgnoreAndMonorepoRegression:
    def test_skips_virtualenv_directory(self, tmp_path: Path) -> None:
        decoy = tmp_path / "virtualenv" / "secret"
        _write(
            decoy / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        real = tmp_path / "api"
        _write(
            real / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        assert [s.name for s in scan_project(tmp_path)] == ["api"]

    def test_example_generated_stackfile_shape(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "gateway" / "app" / "main.py",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "@app.get('/health')\n"
            "def health():\n"
            "    return {'ok': True}\n",
        )
        _write(
            tmp_path / "web" / "app.py",
            "from flask import Flask\n"
            "app = Flask(__name__)\n"
            "@app.get('/')\n"
            "def index():\n"
            "    return 'ok'\n"
            "if __name__ == '__main__':\n"
            "    app.run()\n",
        )
        services = scan_project(tmp_path)
        text = generate_stackfile(services, project_root=tmp_path)
        assert "from stackpilot import Stack, HttpHealthCheck" in text
        assert "uvicorn app.main:app" in text
        assert 'command="python app.py"' in text
        assert "reload=True," in text
        assert "stack.run()" in text
