"""Regression tests for v0.1.0 P1 improvements."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from watchdog.events import FileSystemMovedEvent

from stackpilot.adapters.mongodb import MongoDBAdapter
from stackpilot.adapters.nestjs import NestJSAdapter
from stackpilot.adapters.rabbitmq import RabbitMQAdapter
from stackpilot.config import ServiceSpec, Stack
from stackpilot.diagnostics.errors import format_port_already_in_use
from stackpilot.launch_env import build_child_env, expected_launch_plan
from stackpilot.port_detect import describe_port_owners, process_name_for_pid
from stackpilot.scanner import scan_project
from stackpilot.utils import materialize_stack_for_project
from stackpilot.watcher import ServiceWatcher


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# P1-1 — Environment variable support
# ---------------------------------------------------------------------------


class TestServiceEnvInjection:
    def test_env_dict_injected_without_mutating_os_environ(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARENT_ONLY", "parent")
        before = dict(os.environ)
        env = build_child_env(
            tmp_path,
            base={"PATH": "/usr/bin", "PARENT_ONLY": "parent"},
            env={"APP_KEY": "from-stack", "UTF8_VAL": "café-日本語"},
        )
        assert env["APP_KEY"] == "from-stack"
        assert env["UTF8_VAL"] == "café-日本語"
        assert env["PARENT_ONLY"] == "parent"
        assert os.environ.get("APP_KEY") is None
        assert dict(os.environ) == before

    def test_env_overrides_parent_and_dotenv(self, tmp_path: Path) -> None:
        _write(tmp_path / ".env", "SHARED=from-dotenv\nONLY_DOT=1\n")
        env = build_child_env(
            tmp_path,
            base={"PATH": "/bin", "SHARED": "from-parent"},
            env={"SHARED": "from-stack"},
        )
        assert env["SHARED"] == "from-stack"
        assert env["ONLY_DOT"] == "1"

    def test_env_file_relative_to_service(self, tmp_path: Path) -> None:
        _write(tmp_path / ".env.custom", "CUSTOM=yes\nUNI=üñîçødë\n")
        env = build_child_env(
            tmp_path,
            base={"PATH": "/bin"},
            env_file=".env.custom",
        )
        assert env["CUSTOM"] == "yes"
        assert env["UNI"] == "üñîçødë"

    def test_env_overrides_env_file(self, tmp_path: Path) -> None:
        _write(tmp_path / ".env.prod", "A=file\nB=file\n")
        env = build_child_env(
            tmp_path,
            base={"PATH": "/bin"},
            env_file=".env.prod",
            env={"A": "stack"},
        )
        assert env["A"] == "stack"
        assert env["B"] == "file"

    def test_stack_service_env_roundtrip(self, tmp_path: Path) -> None:
        stack = Stack()
        stack.service(
            name="api",
            path=tmp_path,
            command="python -c pass",
            env={"TOKEN": "abc"},
            env_file=".env",
        )
        _write(tmp_path / ".env", "FROM_FILE=1\n")
        resolved = materialize_stack_for_project(stack, tmp_path)
        spec = resolved.services[0]
        assert dict(spec.env) == {"TOKEN": "abc"}
        assert spec.env_file == ".env"
        plan = expected_launch_plan(spec, base_env={"PATH": "/bin"})
        assert plan.env["TOKEN"] == "abc"
        assert plan.env["FROM_FILE"] == "1"

    def test_service_spec_env_applied_via_build_child_env(self, tmp_path: Path) -> None:
        spec = ServiceSpec(
            name="echo",
            path=tmp_path,
            command="python -c pass",
            env={"SP_P1": "injected", "UTF": "✓"},
        )
        env = build_child_env(
            tmp_path,
            base={"PATH": "/bin"},
            services=(spec,),
            env=spec.env,
            env_file=spec.env_file,
        )
        assert env["SP_P1"] == "injected"
        assert env["UTF"] == "✓"
        assert "SP_P1" not in os.environ


# ---------------------------------------------------------------------------
# P1-2 — Port conflict diagnostics
# ---------------------------------------------------------------------------


class TestPortConflictDiagnostics:
    def test_message_includes_pid_and_executable(self) -> None:
        text = format_port_already_in_use(
            port=8001,
            service="auth",
            owners=((14820, "python.exe"),),
        )
        assert "Problem: Port already in use" in text
        assert 'Service "auth" requires 8001.' in text
        assert "Current owner:" in text
        assert "PID 14820" in text
        assert "python.exe" in text
        assert "stackpilot stop" in text
        assert "change the service port" in text

    def test_message_without_owners(self) -> None:
        text = format_port_already_in_use(port=9000, service="web", owners=())
        assert "Current owner:" in text
        assert "could not determine" in text

    def test_describe_port_owners_live(self) -> None:
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            owners = describe_port_owners(port)
            assert owners
            pid, label = owners[0]
            assert pid > 0
            # Own process should resolve to a python-like name when possible.
            name = process_name_for_pid(pid)
            assert name is None or "python" in name.lower() or label
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# P1-3 — NestJS HTTP health
# ---------------------------------------------------------------------------


class TestNestJSHealthCheckController:
    def test_health_check_controller_prefers_http(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            '{"dependencies":{"@nestjs/core":"10.0.0"}}\n',
        )
        _write(
            tmp_path / "health-check.controller.ts",
            "import { Controller, Get } from '@nestjs/common';\n"
            "@Controller('health')\n"
            "export class HealthCheckController {\n"
            "  @Get()\n"
            "  check() { return { status: 'ok' }; }\n"
            "}\n",
        )
        spec = NestJSAdapter().generate_service(tmp_path)
        assert spec.health == "http"
        assert spec.health_path == "/health"

    def test_slash_health_route_prefers_http(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            '{"dependencies":{"@nestjs/core":"10.0.0"}}\n',
        )
        _write(
            tmp_path / "app.controller.ts",
            "import { Controller, Get } from '@nestjs/common';\n"
            "@Controller()\n"
            "export class AppController {\n"
            "  @Get('/health')\n"
            "  health() { return { ok: true }; }\n"
            "}\n",
        )
        spec = NestJSAdapter().generate_service(tmp_path)
        assert spec.health == "http"
        assert spec.health_path == "/health"


# ---------------------------------------------------------------------------
# P1-4 — MongoDB detection
# ---------------------------------------------------------------------------


class TestMongoDetectionP1:
    def test_mongodb_srv_uri(self, tmp_path: Path) -> None:
        infra = tmp_path / "data"
        infra.mkdir()
        _write(
            infra / ".env",
            "MONGO_URL=mongodb+srv://user:pass@cluster.example.com/app\n",
        )
        assert MongoDBAdapter().detect(infra) is True

    def test_compose_yaml_official_image(self, tmp_path: Path) -> None:
        mongo = tmp_path / "mongo"
        _write(
            mongo / "compose.yaml",
            "services:\n  db:\n    image: mongo:7\n    ports: ['27017:27017']\n",
        )
        assert MongoDBAdapter().detect(mongo) is True

    def test_bitnami_mongodb_image(self, tmp_path: Path) -> None:
        mongo = tmp_path / "mongo"
        _write(
            mongo / "docker-compose.yml",
            "services:\n  db:\n    image: bitnami/mongodb:latest\n",
        )
        assert MongoDBAdapter().detect(mongo) is True

    def test_no_false_positive_mongolian_image(self, tmp_path: Path) -> None:
        app = tmp_path / "api"
        _write(
            app / "docker-compose.yml",
            "services:\n  api:\n    image: myorg/mongolian-api:1\n",
        )
        _write(app / "main.py", "print('api')\n")
        assert MongoDBAdapter().detect(app) is False

    def test_scan_root_compose_yaml(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "compose.yaml",
            "services:\n  mongo:\n    image: mongo:6\n    ports: ['27017:27017']\n",
        )
        _write(
            tmp_path / "api" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        services = scan_project(tmp_path)
        by_name = {s.name: s.framework for s in services}
        assert by_name.get("mongodb") == "MongoDB"


# ---------------------------------------------------------------------------
# P1-5 — RabbitMQ detection
# ---------------------------------------------------------------------------


class TestRabbitDetectionP1:
    def test_amqps_uri(self, tmp_path: Path) -> None:
        infra = tmp_path / "messaging"
        infra.mkdir()
        _write(infra / ".env", "URL=amqps://guest:guest@localhost:5671/\n")
        assert RabbitMQAdapter().detect(infra) is True

    def test_compose_yaml_rabbit_image(self, tmp_path: Path) -> None:
        rabbit = tmp_path / "rabbitmq"
        _write(
            rabbit / "compose.yaml",
            "services:\n  mq:\n    image: rabbitmq:3-management\n",
        )
        assert RabbitMQAdapter().detect(rabbit) is True

    def test_bitnami_rabbitmq_image(self, tmp_path: Path) -> None:
        rabbit = tmp_path / "broker"
        _write(
            rabbit / "docker-compose.yml",
            "services:\n  mq:\n    image: bitnami/rabbitmq:latest\n",
        )
        assert RabbitMQAdapter().detect(rabbit) is True

    def test_app_folder_not_classified(self, tmp_path: Path) -> None:
        app = tmp_path / "orders"
        _write(app / "main.py", "print('orders service')\n")
        _write(app / "README.md", "Uses RabbitMQ in production someday\n")
        assert RabbitMQAdapter().detect(app) is False

    def test_no_false_positive_rabbit_named_app_image(self, tmp_path: Path) -> None:
        app = tmp_path / "api"
        _write(
            app / "docker-compose.yml",
            "services:\n  api:\n    image: myorg/rabbit-hole-api:1\n",
        )
        _write(app / "main.py", "print('api')\n")
        assert RabbitMQAdapter().detect(app) is False


# ---------------------------------------------------------------------------
# P1-6 — Windows file watching reliability
# ---------------------------------------------------------------------------


class TestWindowsWatcherP1:
    def test_atomic_replace_via_moved_fires_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        target = tmp_path / "app.py"
        target.write_text("v1\n", encoding="utf-8")
        fires: list[int] = []

        watcher = ServiceWatcher(
            "web",
            [tmp_path],
            lambda _n, _p: fires.append(1),
            debounce_s=0.08,
        )
        watcher.handler.prime(tmp_path)

        tmp = tmp_path / "app.py.tmp"
        tmp.write_text("v2\n", encoding="utf-8")
        # Atomic replace: dest is the real file; temp name is ignored.
        event = FileSystemMovedEvent(str(tmp), str(target), False)
        watcher.handler.on_moved(event)
        # Rapid duplicate moved notifications must coalesce.
        watcher.handler.on_moved(event)
        time.sleep(0.35)
        assert len(fires) == 1
        watcher.stop()

    def test_editor_temp_files_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        target = tmp_path / "app.py"
        target.write_text("v1\n", encoding="utf-8")
        fires: list[int] = []

        watcher = ServiceWatcher(
            "web",
            [tmp_path],
            lambda _n, _p: fires.append(1),
            debounce_s=0.05,
        )
        watcher.handler.prime(tmp_path)

        for name in (
            "app.py.tmp",
            "app.py___jb_tmp___",
            ".#app.py",
            "app.py~",
        ):
            junk = tmp_path / name
            junk.write_text("noise\n", encoding="utf-8")
            watcher.notify_for_tests(junk, event_type="created")
            watcher.notify_for_tests(junk, event_type="modified")

        time.sleep(0.3)
        assert fires == []
        watcher.stop()

    def test_delete_and_recreate_coalesces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        target = tmp_path / "app.py"
        target.write_text("v1\n", encoding="utf-8")
        fires: list[int] = []

        watcher = ServiceWatcher(
            "web",
            [tmp_path],
            lambda _n, _p: fires.append(1),
            debounce_s=0.1,
        )
        watcher.handler.prime(tmp_path)
        watcher.notify_for_tests(target, event_type="deleted")
        target.write_text("v2\n", encoding="utf-8")
        watcher.notify_for_tests(target, event_type="created")
        watcher.notify_for_tests(target, event_type="modified")
        time.sleep(0.4)
        assert len(fires) == 1
        watcher.stop()

    def test_rapid_saves_single_reload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        target = tmp_path / "app.py"
        target.write_text("v0\n", encoding="utf-8")
        fires: list[int] = []

        watcher = ServiceWatcher(
            "web",
            [tmp_path],
            lambda _n, _p: fires.append(1),
            debounce_s=0.12,
        )
        watcher.handler.prime(tmp_path)
        for i in range(8):
            target.write_text(f"v{i}\n", encoding="utf-8")
            watcher.notify_for_tests(target, event_type="modified")
        time.sleep(0.4)
        assert len(fires) == 1
        watcher.stop()
