"""Tests for the framework adapter layer."""

from __future__ import annotations

from pathlib import Path

from stackpilot.adapters import (
    AdapterRegistry,
    CeleryAdapter,
    DjangoAdapter,
    ExpressAdapter,
    FastAPIAdapter,
    FlaskAdapter,
    FrameworkAdapter,
    GenericAdapter,
    NestJSAdapter,
    PostgresAdapter,
    RedisAdapter,
    create_default_registry,
    default_registry,
)
from stackpilot.adapters.base import AdapterServiceSpec
from stackpilot.generator import build_command, generate_stackfile
from stackpilot.scanner import detect_framework, scan_directory, scan_project


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestFastAPIAdapter:
    def test_detects_fastapi_import_and_app(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        adapter = FastAPIAdapter()
        assert adapter.detect(service) is True
        assert detect_framework(service) == "FastAPI"

    def test_generate_uvicorn_command(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "app.py",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "@app.get('/health')\n"
            "def health():\n"
            "    return {'ok': True}\n",
        )
        spec = FastAPIAdapter().generate_service(service, port=8000)
        assert spec.command == (
            "python -m uvicorn app:app --reload --host 0.0.0.0 --port 8000"
        )
        assert spec.health == "http"
        assert spec.health_path == "/health"


class TestFlaskAdapter:
    def test_detects_flask_import(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(service / "app.py", "from flask import Flask\napp = Flask(__name__)\n")
        assert FlaskAdapter().detect(service) is True
        assert detect_framework(service) == "Flask"

    def test_generate_python_app(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(
            service / "app.py",
            "from flask import Flask\n"
            "app = Flask(__name__)\n"
            "if __name__ == '__main__':\n"
            "    app.run()\n",
        )
        # Without an assigned port, keep the direct ``python app.py`` launcher.
        spec = FlaskAdapter().generate_service(service)
        assert spec.command == "python app.py"

    def test_generate_flask_run_uses_assigned_port(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(
            service / "app.py",
            "from flask import Flask\n"
            "app = Flask(__name__)\n"
            "if __name__ == '__main__':\n"
            "    app.run(host='0.0.0.0', port=8001)\n",
        )
        # Assigned port must appear on the command even when source hardcodes
        # a different preferred port (occupied-port reassignment).
        spec = FlaskAdapter().generate_service(service, port=8002)
        assert spec.command == (
            "python -m flask --app app:app run --host 0.0.0.0 --port 8002"
        )
        assert "python app.py" not in spec.command

    def test_generate_flask_run_without_main(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(service / "app.py", "from flask import Flask\napp = Flask(__name__)\n")
        spec = FlaskAdapter().generate_service(service, port=8000)
        assert spec.command == (
            "python -m flask --app app:app run --host 0.0.0.0 --port 8000"
        )
        assert spec.reload is True


class TestDjangoAdapter:
    def test_detects_manage_and_settings(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(service / "manage.py", "#!/usr/bin/env python\n")
        _write(service / "config" / "settings.py", "DEBUG = True\n")
        assert DjangoAdapter().detect(service) is True
        assert detect_framework(service) == "Django"

    def test_requires_settings(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(service / "manage.py", "")
        assert DjangoAdapter().detect(service) is False

    def test_generate_runserver(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(service / "manage.py", "")
        _write(service / "settings.py", "")
        spec = DjangoAdapter().generate_service(service, port=8001)
        assert spec.command == "python manage.py runserver 0.0.0.0:8001"


class TestCeleryAdapter:
    def test_detects_celery_module(self, tmp_path: Path) -> None:
        service = tmp_path / "worker"
        _write(
            service / "celery.py",
            "from celery import Celery\napp = Celery('worker')\n",
        )
        assert CeleryAdapter().detect(service) is True
        assert detect_framework(service) == "Celery"

    def test_generate_worker_command(self, tmp_path: Path) -> None:
        service = tmp_path / "worker"
        _write(
            service / "celery.py",
            "from celery import Celery\napp = Celery('tasks')\n",
        )
        spec = CeleryAdapter().generate_service(service)
        # ``-A`` is the importable module (celery.py), not the Celery app label.
        assert spec.command == "celery -A celery worker"


class TestExpressAdapter:
    def test_detects_express_dependency(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "package.json",
            '{"name":"api","dependencies":{"express":"^4.18.0"}}\n',
        )
        assert ExpressAdapter().detect(service) is True
        assert detect_framework(service) == "Express"

    def test_generate_npm_run_dev(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "package.json",
            '{"dependencies":{"express":"1.0.0"},'
            '"scripts":{"dev":"node server.js","start":"node server.js"}}\n',
        )
        spec = ExpressAdapter().generate_service(service, port=3000)
        assert spec.command == "npm run dev"
        assert spec.reload is True

    def test_generate_falls_back_to_main_without_inventing_script(
        self, tmp_path: Path
    ) -> None:
        service = tmp_path / "api"
        _write(
            service / "package.json",
            '{"main":"server.js","dependencies":{"express":"1.0.0"}}\n',
        )
        _write(service / "server.js", "app.listen(4000)\n")
        spec = ExpressAdapter().generate_service(service, port=4000)
        assert spec.command == "node server.js"
        assert spec.preferred_port == 4000


class TestNestJSAdapter:
    def test_detects_nestjs_core(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "package.json",
            '{"dependencies":{"@nestjs/core":"^10.0.0"}}\n',
        )
        assert NestJSAdapter().detect(service) is True
        assert detect_framework(service) == "NestJS"

    def test_nestjs_beats_express_when_both_present(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "package.json",
            '{"dependencies":{"@nestjs/core":"10","express":"4"}}\n',
        )
        assert detect_framework(service) == "NestJS"

    def test_generate_start_dev(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "package.json",
            '{"dependencies":{"@nestjs/core":"10"},'
            '"scripts":{"start:dev":"nest start --watch","start":"node dist/main.js"}}\n',
        )
        spec = NestJSAdapter().generate_service(service)
        assert spec.command == "npm run start:dev"
        assert spec.reload is True


class TestPostgresAdapter:
    def test_detects_compose_postgres(self, tmp_path: Path) -> None:
        service = tmp_path / "db"
        _write(
            service / "docker-compose.yml",
            "services:\n  postgres:\n    image: postgres:16\n",
        )
        assert PostgresAdapter().detect(service) is True
        assert detect_framework(service) == "PostgreSQL"
        assert PostgresAdapter().external is True

    def test_ignores_unrelated_postgres_substring(self, tmp_path: Path) -> None:
        service = tmp_path / "docs"
        _write(
            service / "docker-compose.yml",
            "services:\n  web:\n    environment:\n      - APP=postgres-migrator\n",
        )
        assert PostgresAdapter().detect(service) is False

    def test_detects_postgresql_conf(self, tmp_path: Path) -> None:
        service = tmp_path / "postgres"
        _write(service / "postgresql.conf", "port = 5432\n")
        assert detect_framework(service) == "PostgreSQL"

    def test_generate_compose_up(self, tmp_path: Path) -> None:
        service = tmp_path / "db"
        _write(
            service / "docker-compose.yml",
            "services:\n  postgres:\n    image: postgres:16\n",
        )
        spec = PostgresAdapter().generate_service(service)
        assert spec.external is True
        assert spec.external_type == "postgresql"
        assert spec.fixed_port == 5432
        assert spec.health == "tcp"


class TestRedisAdapter:
    def test_detects_redis_conf(self, tmp_path: Path) -> None:
        service = tmp_path / "cache"
        _write(service / "redis.conf", "port 6379\n")
        assert RedisAdapter().detect(service) is True
        assert detect_framework(service) == "Redis"

    def test_detects_compose_redis(self, tmp_path: Path) -> None:
        service = tmp_path / "redis"
        _write(
            service / "compose.yaml",
            "services:\n  redis:\n    image: redis:7\n",
        )
        assert detect_framework(service) == "Redis"

    def test_generate_redis_server(self, tmp_path: Path) -> None:
        service = tmp_path / "cache"
        _write(service / "redis.conf", "port 6379\n")
        spec = RedisAdapter().generate_service(service)
        assert spec.external is True
        assert spec.external_type == "redis"
        assert spec.fixed_port == 6379


class TestGenericFallback:
    def test_bare_main_py(self, tmp_path: Path) -> None:
        service = tmp_path / "jobs"
        _write(service / "main.py", "print('hi')\n")
        assert GenericAdapter().detect(service) is True
        assert detect_framework(service) == "Generic"

    def test_bare_package_json(self, tmp_path: Path) -> None:
        service = tmp_path / "frontend"
        _write(service / "package.json", '{"name":"frontend"}\n')
        assert detect_framework(service) == "Generic"
        spec = GenericAdapter().generate_service(service)
        assert spec.command == "npm start"

    def test_frontend_package_prefers_dev_script(self, tmp_path: Path) -> None:
        service = tmp_path / "frontend"
        _write(
            service / "package.json",
            '{"name":"frontend","scripts":{"dev":"vite","start":"vite preview"},'
            '"devDependencies":{"vite":"^5.0.0","react":"^18.0.0"}}\n',
        )
        spec = GenericAdapter().generate_service(service)
        assert spec.command == "npm run dev"

    def test_unknown_folder_returns_none(self, tmp_path: Path) -> None:
        service = tmp_path / "docs"
        service.mkdir()
        _write(service / "README.md", "# docs\n")
        assert detect_framework(service) is None
        assert scan_directory(service) is None


class TestRegistry:
    def test_default_registry_order(self) -> None:
        names = [adapter.name for adapter in default_registry.all()]
        assert names[0] == "NestJS"
        assert names[-1] == "Generic"
        assert "FastAPI" in names
        assert "Express" in names

    def test_match_uses_priority_not_registration_order(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "package.json",
            '{"dependencies":{"@nestjs/core":"10","express":"4"}}\n',
        )
        registry = create_default_registry()
        matched = registry.match(service)
        assert matched is not None
        assert matched.name == "NestJS"

    def test_custom_adapter_registration(self, tmp_path: Path) -> None:
        class GoAdapter(FrameworkAdapter):
            name = "Go"
            priority = 5

            def detect(self, path: Path) -> bool:
                return (path / "go.mod").is_file()

            def generate_service(
                self,
                path: Path,
                *,
                port: int | None = None,
            ) -> AdapterServiceSpec:
                _ = path
                _ = port
                return AdapterServiceSpec(
                    framework=self.name,
                    command="go run .",
                    uses_port=True,
                    health="http",
                )

        registry = AdapterRegistry()
        registry.register(GenericAdapter())
        registry.register(GoAdapter())

        service = tmp_path / "svc"
        _write(service / "go.mod", "module example\n")
        _write(service / "main.py", "print(1)\n")

        matched = registry.match(service)
        assert matched is not None
        assert matched.name == "Go"

    def test_register_replaces_same_name(self) -> None:
        registry = AdapterRegistry()
        registry.register(FastAPIAdapter())
        replacement = FastAPIAdapter()
        registry.register(replacement)
        assert len(registry.all()) == 1
        assert registry.get("FastAPI") is replacement


class TestGeneratorIntegration:
    def test_generator_uses_adapter_metadata(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "gateway" / "main.py",
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
            "    return 'ok'\n",
        )
        _write(
            tmp_path / "worker" / "celery.py",
            "from celery import Celery\napp = Celery('worker')\n",
        )
        _write(tmp_path / "cache" / "redis.conf", "port 6379\n")

        services = scan_project(tmp_path)
        text = generate_stackfile(services, project_root=tmp_path)

        assert "from stackpilot import Stack, HttpHealthCheck" in text
        assert "python -m uvicorn main:app --reload" in text
        assert 'command="python -m flask --app app:app run' in text or (
            'command="python app.py"' in text
        )
        assert 'command="celery -A celery worker"' in text
        assert "stack.external_dependency(" in text
        assert 'name="cache"' in text
        assert 'type="redis"' in text
        assert "HttpHealthCheck(url=" in text
        assert 'command="redis-server ./redis.conf"' not in text
        assert "reload=True," in text

    def test_build_command_delegates_to_adapter(self, tmp_path: Path) -> None:
        service = tmp_path / "api"
        _write(
            service / "package.json",
            '{"dependencies":{"express":"4.0.0"},'
            '"scripts":{"dev":"node server.js"}}\n',
        )
        info = scan_directory(service)
        assert info is not None
        assert build_command(info, 3000) == "npm run dev"

    def test_fastapi_stackfile_shape(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "gateway" / "main.py",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "@app.get('/health')\n"
            "def health():\n"
            "    return {'ok': True}\n",
        )
        services = scan_project(tmp_path)
        text = generate_stackfile(services, project_root=tmp_path)
        assert 'name="gateway"' in text
        assert 'path="./gateway"' in text
        assert (
            'command="python -m uvicorn main:app --reload '
            '--host 0.0.0.0 --port 8000"'
        ) in text
        assert "reload=True," in text
        assert (
            'health_check=HttpHealthCheck(url="http://127.0.0.1:8000/health")'
        ) in text

    def test_fastapi_without_health_route_uses_tcp(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "gateway" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        services = scan_project(tmp_path)
        text = generate_stackfile(services, project_root=tmp_path)
        assert "TcpHealthCheck(host=" in text
        assert "HttpHealthCheck" not in text
