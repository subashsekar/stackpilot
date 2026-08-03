"""Regression: Sync generates runnable Stackfiles for major frameworks."""

from __future__ import annotations

from pathlib import Path

from stackpilot.adapters import (
    CeleryAdapter,
    DjangoAdapter,
    ExpressAdapter,
    FastAPIAdapter,
    FlaskAdapter,
    NestJSAdapter,
)
from stackpilot.adapters.detect.ports import detect_infra_port, detect_preferred_port
from stackpilot.generator import generate_stackfile
from stackpilot.launch_env import build_child_env
from stackpilot.scanner import ServiceInfo, scan_project
from stackpilot.service_catalog import load_catalog_dependencies
from stackpilot.config import ServiceSpec


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestPortDetectionFromSources:
    def test_uvicorn_run_port(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "main.py",
            "import uvicorn\nfrom fastapi import FastAPI\n"
            "app = FastAPI()\nuvicorn.run(app, port=8123)\n",
        )
        assert detect_preferred_port(tmp_path) == 8123

    def test_flask_app_run_port(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "app.py",
            "from flask import Flask\napp = Flask(__name__)\n"
            "if __name__ == '__main__':\n"
            "    app.run(port=5055)\n",
        )
        assert detect_preferred_port(tmp_path) == 5055

    def test_express_listen_and_env_fallback(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "server.js",
            "const port = Number(process.env.PORT || 4000);\n"
            "app.listen(port);\n",
        )
        _write(
            tmp_path / "package.json",
            '{"dependencies":{"express":"4"},"scripts":{"dev":"node server.js"}}\n',
        )
        assert detect_preferred_port(tmp_path) == 4000

    def test_package_json_script_port_flag(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            '{"scripts":{"dev":"node server.js --port 4321"}}\n',
        )
        assert detect_preferred_port(tmp_path) == 4321

    def test_procfile_port(self, tmp_path: Path) -> None:
        _write(tmp_path / "Procfile", "web: uvicorn main:app --port 9099\n")
        assert detect_preferred_port(tmp_path) == 9099

    def test_compose_service_hint_host_port(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        service.mkdir()
        _write(
            service / "compose.yaml",
            "services:\n"
            "  db:\n"
            "    ports:\n"
            "      - '5433:5432'\n"
            "  api:\n"
            "    ports:\n"
            "      - '8088:8000'\n",
        )
        assert detect_preferred_port(service) == 8088

    def test_postgres_conf_and_compose_ports(self, tmp_path: Path) -> None:
        _write(tmp_path / "postgresql.conf", "port = 5544\n")
        assert detect_infra_port(tmp_path, kind="postgres") == 5544
        other = tmp_path / "db"
        other.mkdir()
        _write(
            other / "docker-compose.yml",
            "services:\n  postgres:\n    image: postgres:16\n"
            "    ports:\n      - '5433:5432'\n",
        )
        assert detect_infra_port(other, kind="postgres") == 5433


class TestFrameworkCommands:
    def test_fastapi_reload_and_detected_port(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        _write(service / ".env", "PORT=7777\n")
        spec = FastAPIAdapter().generate_service(service, port=7777)
        assert "--reload" in spec.command
        assert "--port 7777" in spec.command
        assert spec.reload is True
        assert spec.preferred_port == 7777

    def test_flask_invalid_bare_app_uses_flask_run(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(service / "app.py", "from flask import Flask\napp = Flask(__name__)\n")
        spec = FlaskAdapter().generate_service(service, port=5001)
        assert "flask --app app:app run" in spec.command
        assert "--port 5001" in spec.command
        assert "python app.py" not in spec.command

    def test_django_manage_runserver(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(service / "manage.py", "#!/usr/bin/env python\n")
        _write(service / "settings.py", "DEBUG = True\n")
        spec = DjangoAdapter().generate_service(service, port=8002)
        assert spec.command == "python manage.py runserver 0.0.0.0:8002"
        assert spec.reload is True

    def test_express_never_invents_missing_script(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "package.json",
            '{"main":"index.js","dependencies":{"express":"4"}}\n',
        )
        _write(
            service / "index.js",
            "app.listen(3005)\n",
        )
        spec = ExpressAdapter().generate_service(service)
        assert spec.command == "node index.js"
        assert "npm run" not in spec.command
        assert spec.preferred_port == 3005

    def test_nestjs_prefers_existing_start_dev(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "package.json",
            '{"dependencies":{"@nestjs/core":"10"},'
            '"scripts":{"start:dev":"nest start --watch"}}\n',
        )
        spec = NestJSAdapter().generate_service(service)
        assert spec.command == "npm run start:dev"
        assert spec.reload is True

    def test_celery_module_worker_and_broker_depends(self, tmp_path: Path) -> None:
        service = tmp_path / "worker"
        _write(
            service / "celery.py",
            "from celery import Celery\n"
            "app = Celery('jobs', broker='redis://localhost:6379/0')\n",
        )
        spec = CeleryAdapter().generate_service(service)
        assert spec.command == "celery -A celery worker"
        assert spec.depends_on == ("redis",)

    def test_celery_package_module(self, tmp_path: Path) -> None:
        service = tmp_path / "jobs"
        _write(
            service / "jobs" / "celery.py",
            "from celery import Celery\napp = Celery('jobs')\n",
        )
        assert CeleryAdapter().detect(service) is True
        spec = CeleryAdapter().generate_service(service)
        assert spec.command == "celery -A jobs worker"


class TestCatalogAndComposeDependsOn:
    def test_services_yaml_map_preserves_depends_on(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "services.yaml",
            "services:\n"
            "  auth:\n"
            "    depends_on: []\n"
            "    external: [postgresql]\n"
            "  gateway:\n"
            "    depends_on: [auth]\n",
        )
        deps = load_catalog_dependencies(
            tmp_path,
            known_services=["auth", "gateway"],
            known_externals=["postgres"],
        )
        assert deps["auth"] == ("postgres",)
        assert deps["gateway"] == ("auth",)

    def test_services_yaml_list_still_works(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "services.yaml",
            "services:\n"
            "  - name: gateway\n"
            "    depends_on: [auth]\n"
            "  - name: auth\n"
            "    depends_on: []\n",
        )
        deps = load_catalog_dependencies(
            tmp_path,
            known_services=["auth", "gateway"],
            known_externals=[],
        )
        assert deps["gateway"] == ("auth",)

    def test_compose_map_depends_on_in_generated_stackfile(
        self, tmp_path: Path
    ) -> None:
        auth = tmp_path / "auth"
        gateway = tmp_path / "gateway"
        _write(
            auth / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        _write(
            gateway / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        _write(
            tmp_path / "docker-compose.yml",
            "services:\n"
            "  auth:\n"
            "    image: auth\n"
            "  gateway:\n"
            "    image: gateway\n"
            "    depends_on:\n"
            "      auth:\n"
            "        condition: service_healthy\n",
        )
        services = scan_project(tmp_path)
        text = generate_stackfile(services, project_root=tmp_path)
        assert 'name="gateway"' in text
        assert 'depends_on=["auth"]' in text

    def test_celery_broker_merged_when_redis_present(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "worker" / "celery.py",
            "from celery import Celery\n"
            "app = Celery('w', broker='redis://127.0.0.1:6379/0')\n",
        )
        _write(tmp_path / "cache" / "redis.conf", "port 6379\n")
        services = scan_project(tmp_path)
        text = generate_stackfile(services, project_root=tmp_path)
        assert 'command="celery -A celery worker"' in text
        assert 'depends_on=["cache"]' in text or 'depends_on=["redis"]' in text


class TestGeneratedReloadAndListenEnv:
    def test_stackfile_emits_reload_true(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "api" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        text = generate_stackfile(
            [ServiceInfo(name="api", path=tmp_path / "api", framework="FastAPI")],
            project_root=tmp_path,
        )
        assert "reload=True," in text

    def test_launch_env_sets_port_from_stackfile(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        service.mkdir()
        spec = ServiceSpec(
            name="api",
            path=service,
            command="npm run dev",
            port=4000,
        )
        env = build_child_env(service, services=(spec,))
        assert env.get("PORT") == "4000"
        assert env.get("FLASK_RUN_PORT") == "4000"
