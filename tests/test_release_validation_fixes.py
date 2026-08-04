"""Regression tests for v0.1.0 remaining release issues."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from stackpilot.adapters.flask import FlaskAdapter
from stackpilot.adapters.mongodb import MongoDBAdapter
from stackpilot.adapters.nestjs import NestJSAdapter
from stackpilot.adapters.rabbitmq import RabbitMQAdapter
from stackpilot.cli import app
from stackpilot.config import ServiceSpec
from stackpilot.generator import generate_stackfile
from stackpilot.runtime_control import (
    detect_stale_session,
    format_stale_session_error,
    read_runtime_payload,
    stop_runtime_session,
)
from stackpilot.scanner import scan_project
from stackpilot.status import RUNTIME_STATUS_FILE, save_runtime_snapshot
from stackpilot.watch_manager import WatchManager
from stackpilot.watcher import ServiceWatcher, should_takeover_native_reload

runner = CliRunner()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _stackfile(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "Stackfile.py",
        "from stackpilot import Stack\n"
        "stack = Stack()\n"
        "stack.service(name='web', path='.', command='python -c pass')\n"
        "stack.run()\n",
    )


class TestFlaskAssignedPort:
    def test_assigned_port_uses_flask_run(self, tmp_path: Path) -> None:
        service = tmp_path / "web"
        _write(
            service / "app.py",
            "from flask import Flask\n"
            "app = Flask(__name__)\n"
            "if __name__ == '__main__':\n"
            "    app.run(host='0.0.0.0', port=8001)\n",
        )
        spec = FlaskAdapter().generate_service(service, port=8002)
        assert "--port 8002" in spec.command
        assert "flask" in spec.command
        assert "python app.py" not in spec.command

    def test_sync_stackfile_embeds_assigned_port(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "web" / "app.py",
            "from flask import Flask\n"
            "app = Flask(__name__)\n"
            "if __name__ == '__main__':\n"
            "    app.run(port=8001)\n",
        )
        # Second service steals preferred 8001 → Flask must rebind.
        _write(
            tmp_path / "api" / "main.py",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "PORT = 8001\n",
        )
        services = scan_project(tmp_path)
        text = generate_stackfile(services, project_root=tmp_path)
        assert "python -m flask --app app:app run --host 0.0.0.0 --port" in text
        assert "python app.py" not in text
        # Preferred 8001 is taken by FastAPI; Flask must land on a different port.
        import re

        flask_ports = [
            int(m.group(1))
            for m in re.finditer(
                r'flask --app app:app run --host 0\.0\.0\.0 --port (\d+)',
                text,
            )
        ]
        assert flask_ports, text
        assert 8001 not in flask_ports
        assert all(p >= 8000 for p in flask_ports)


class TestStackpilotStop:
    def test_stop_no_session(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _stackfile(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0
        assert "No running StackPilot session." in result.output

    def test_stop_without_stackfile_reports_no_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0
        assert "No running StackPilot session." in result.output
        assert "Traceback" not in result.output

    def test_stop_runtime_without_stackfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        save_runtime_snapshot(
            tmp_path,
            {
                "session_active": True,
                "services": [
                    {
                        "name": "auth",
                        "pid": 515151,
                        "status": "RUNNING",
                        "command": "true",
                    }
                ],
            },
        )
        state = {"alive": {515151}}

        def _alive(pid: int) -> bool:
            return pid in state["alive"]

        def _fake_signal(pid: int, *, graceful: bool) -> None:
            state["alive"].discard(pid)

        with (
            patch("stackpilot.runtime_control.pid_is_alive", side_effect=_alive),
            patch(
                "stackpilot.runtime_control.signal_process_tree",
                side_effect=_fake_signal,
            ) as sig,
            patch("stackpilot.runtime_control._wait_until_dead"),
        ):
            result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0
        assert "Stopping auth..." in result.output
        assert "Stopped 1 service." in result.output
        assert "No orphan processes detected." in result.output
        sig.assert_called()

    def test_stop_terminates_recorded_pids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stackfile(tmp_path)
        monkeypatch.chdir(tmp_path)
        save_runtime_snapshot(
            tmp_path,
            {
                "session_active": True,
                "services": [
                    {
                        "name": "auth",
                        "pid": 424242,
                        "port": 8000,
                        "status": "RUNNING",
                        "command": "python -c pass",
                    },
                    {
                        "name": "gateway",
                        "pid": 424243,
                        "port": 8001,
                        "status": "RUNNING",
                        "command": "python -c pass",
                    },
                ],
            },
        )

        signaled: list[int] = []
        state = {"alive": {424242, 424243}}

        def _alive(pid: int) -> bool:
            return pid in state["alive"]

        def _fake_signal(pid: int, *, graceful: bool) -> None:
            signaled.append(pid)
            state["alive"].discard(pid)

        with (
            patch("stackpilot.runtime_control.pid_is_alive", side_effect=_alive),
            patch(
                "stackpilot.runtime_control.signal_process_tree",
                side_effect=_fake_signal,
            ),
            patch("stackpilot.runtime_control._wait_until_dead"),
        ):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert "Stopping auth..." in result.output
        assert "Stopping gateway..." in result.output
        assert "Stopped 2 services." in result.output
        assert "No orphan processes detected." in result.output
        assert set(signaled) == {424242, 424243}

    def test_stop_ignores_dead_pids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stackfile(tmp_path)
        monkeypatch.chdir(tmp_path)
        save_runtime_snapshot(
            tmp_path,
            {
                "session_active": True,
                "services": [
                    {
                        "name": "user",
                        "pid": 999001,
                        "status": "RUNNING",
                        "command": "true",
                    }
                ],
            },
        )
        with patch("stackpilot.runtime_control.pid_is_alive", return_value=False):
            result = stop_runtime_session(tmp_path)
        assert result.exit_code == 0
        assert "No live StackPilot processes" in result.message


class TestStaleRuntimeRecovery:
    def test_run_blocks_on_stale_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stackfile(tmp_path)
        monkeypatch.chdir(tmp_path)
        save_runtime_snapshot(
            tmp_path,
            {
                "session_active": True,
                "services": [
                    {
                        "name": "web",
                        "pid": 777001,
                        "port": 8000,
                        "status": "RUNNING",
                        "command": "python -c pass",
                    }
                ],
            },
        )
        with patch("stackpilot.runtime_control.pid_is_alive", return_value=True):
            result = runner.invoke(app, ["run"])
        assert result.exit_code == 1
        combined = (result.output or "") + (getattr(result, "stderr", None) or "")
        assert "Existing StackPilot session detected" in combined
        assert "stackpilot stop" in combined
        assert "Traceback" not in combined

    def test_run_force_clears_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stackfile(tmp_path)
        monkeypatch.chdir(tmp_path)
        save_runtime_snapshot(
            tmp_path,
            {
                "session_active": True,
                "services": [
                    {
                        "name": "web",
                        "pid": 777002,
                        "port": 8000,
                        "status": "RUNNING",
                        "command": "python -c pass",
                    }
                ],
            },
        )

        from stackpilot.runtime_control import StopResult

        with (
            patch(
                "stackpilot.runtime_control.detect_stale_session",
                return_value=__import__(
                    "stackpilot.runtime_control", fromlist=["StaleSession"]
                ).StaleSession(live_services=("web",)),
            ),
            patch(
                "stackpilot.runtime_control.stop_runtime_session",
                return_value=StopResult(
                    stopped_names=("web",),
                    already_dead=(),
                    message="ok",
                ),
            ) as stop_fn,
            patch("stackpilot.cli.Orchestrator") as orch,
        ):
            orch.return_value.run.return_value = 0
            result = runner.invoke(app, ["run", "--force"])

        assert result.exit_code == 0
        stop_fn.assert_called_once()
        orch.return_value.run.assert_called_once()
    def test_detect_stale_from_occupied_ports(self, tmp_path: Path) -> None:
        save_runtime_snapshot(
            tmp_path,
            {
                "session_active": True,
                "services": [
                    {
                        "name": "web",
                        "pid": None,
                        "port": 8123,
                        "status": "STOPPED",
                        "command": "true",
                    }
                ],
            },
        )
        specs = [
            ServiceSpec(name="web", path=tmp_path, command="true", port=8123),
        ]
        with patch(
            "stackpilot.runtime_control.is_port_in_use",
            return_value=True,
        ):
            stale = detect_stale_session(tmp_path, specs)
        assert stale is not None
        assert 8123 in stale.occupied_ports
        text = format_stale_session_error(stale)
        assert "Existing StackPilot session detected" in text
        assert "stackpilot stop" in text


class TestCorruptedRuntime:
    def test_corrupted_runtime_stop_recovers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stackfile(tmp_path)
        monkeypatch.chdir(tmp_path)
        path = tmp_path / RUNTIME_STATUS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")

        result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0
        assert "corrupted" in result.output.lower()
        assert "Traceback" not in result.output
        parsed = read_runtime_payload(tmp_path)
        assert parsed.corrupted is False

    def test_corrupted_runtime_detect_stale(self, tmp_path: Path) -> None:
        path = tmp_path / RUNTIME_STATUS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]", encoding="utf-8")
        stale = detect_stale_session(tmp_path)
        assert stale is not None
        assert "corrupted" in stale.reason.lower()


class TestMongoDetection:
    def test_detects_compose_mongo_image(self, tmp_path: Path) -> None:
        mongo = tmp_path / "mongo"
        _write(
            mongo / "docker-compose.yml",
            "services:\n"
            "  mongo:\n"
            "    image: mongo:6\n"
            "    ports:\n"
            "      - '27017:27017'\n",
        )
        assert MongoDBAdapter().detect(mongo) is True
        spec = MongoDBAdapter().generate_service(mongo)
        assert spec.external is True
        assert spec.external_type == "mongodb"
        assert spec.fixed_port == 27017

    def test_detects_mongodb_uri_in_env(self, tmp_path: Path) -> None:
        mongo = tmp_path / "mongodb"
        mongo.mkdir()
        _write(mongo / ".env", "DATABASE_URL=mongodb://127.0.0.1:27017/app\n")
        assert MongoDBAdapter().detect(mongo) is True

    def test_detects_uri_in_non_app_infra_dir(self, tmp_path: Path) -> None:
        infra = tmp_path / "data"
        infra.mkdir()
        _write(infra / ".env", "MONGO_URL=mongodb://127.0.0.1:27017/app\n")
        assert MongoDBAdapter().detect(infra) is True

    def test_no_false_positive_on_app_name(self, tmp_path: Path) -> None:
        app = tmp_path / "gateway"
        _write(app / "main.py", "MONGO_HINT = 'not really'\n")
        assert MongoDBAdapter().detect(app) is False

    def test_no_false_positive_soft_mention_with_unrelated_compose(
        self, tmp_path: Path
    ) -> None:
        app = tmp_path / "api"
        _write(
            app / "docker-compose.yml",
            "services:\n  redis:\n    image: redis:7\n",
        )
        _write(app / ".env", "NOTE=we might use mongodb later\n")
        _write(app / "main.py", "print('api')\n")
        assert MongoDBAdapter().detect(app) is False

    def test_app_with_mongo_uri_not_classified_as_mongo(self, tmp_path: Path) -> None:
        app = tmp_path / "api"
        _write(app / "main.py", "print('api')\n")
        _write(app / ".env", "DATABASE_URL=mongodb://127.0.0.1:27017/app\n")
        assert MongoDBAdapter().detect(app) is False

    def test_sync_emits_external_dependency(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "mongo" / "docker-compose.yml",
            "services:\n  mongo:\n    image: mongo:6\n    ports: ['27017:27017']\n",
        )
        _write(
            tmp_path / "api" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        services = scan_project(tmp_path)
        text = generate_stackfile(services, project_root=tmp_path)
        assert 'type="mongodb"' in text
        assert "stack.external_dependency(" in text

    def test_sync_discovers_mongo_in_shared_compose(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "infra" / "docker-compose.yml",
            "services:\n"
            "  postgres:\n"
            "    image: postgres:16\n"
            "  mongo:\n"
            "    image: mongo:6\n"
            "    ports: ['27017:27017']\n"
            "  rabbitmq:\n"
            "    image: rabbitmq:3\n"
            "    ports: ['5672:5672']\n"
            "  redis:\n"
            "    image: redis:7\n",
        )
        _write(
            tmp_path / "api" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        services = scan_project(tmp_path)
        by_name = {s.name: s.framework for s in services}
        assert by_name["api"] == "FastAPI"
        assert by_name["postgres"] == "PostgreSQL"
        assert by_name["mongodb"] == "MongoDB"
        assert by_name["rabbitmq"] == "RabbitMQ"
        assert by_name["redis"] == "Redis"
        text = generate_stackfile(services, project_root=tmp_path)
        assert 'type="mongodb"' in text
        assert 'type="rabbitmq"' in text
        assert 'type="postgresql"' in text
        assert 'type="redis"' in text

    def test_sync_discovers_mongo_rabbit_at_project_root_compose(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path / "docker-compose.yml",
            "services:\n"
            "  mongo:\n"
            "    image: mongo:6\n"
            "    ports: ['27017:27017']\n"
            "  rabbitmq:\n"
            "    image: rabbitmq:3\n"
            "    ports: ['5672:5672']\n",
        )
        _write(
            tmp_path / "api" / "main.py",
            "from fastapi import FastAPI\napp = FastAPI()\n",
        )
        services = scan_project(tmp_path)
        by_name = {s.name: s.framework for s in services}
        assert by_name["api"] == "FastAPI"
        assert by_name["mongodb"] == "MongoDB"
        assert by_name["rabbitmq"] == "RabbitMQ"
        text = generate_stackfile(services, project_root=tmp_path)
        assert 'type="mongodb"' in text
        assert 'type="rabbitmq"' in text


class TestRabbitDetection:
    def test_detects_rabbitmq_compose(self, tmp_path: Path) -> None:
        rabbit = tmp_path / "rabbitmq"
        _write(
            rabbit / "docker-compose.yml",
            "services:\n"
            "  rabbitmq:\n"
            "    image: rabbitmq:3\n"
            "    ports:\n"
            "      - '5672:5672'\n",
        )
        assert RabbitMQAdapter().detect(rabbit) is True
        spec = RabbitMQAdapter().generate_service(rabbit)
        assert spec.external_type == "rabbitmq"
        assert spec.fixed_port == 5672

    def test_detects_amqp_uri(self, tmp_path: Path) -> None:
        rabbit = tmp_path / "broker"
        rabbit.mkdir()
        _write(rabbit / ".env", "BROKER_URL=amqp://guest:guest@localhost:5672/\n")
        assert RabbitMQAdapter().detect(rabbit) is True

    def test_detects_amqp_uri_in_non_app_dir(self, tmp_path: Path) -> None:
        infra = tmp_path / "messaging"
        infra.mkdir()
        _write(infra / ".env", "BROKER_URL=amqps://guest:guest@localhost:5671/\n")
        assert RabbitMQAdapter().detect(infra) is True

    def test_no_false_positive(self, tmp_path: Path) -> None:
        app = tmp_path / "api"
        _write(app / "main.py", "print('no broker here')\n")
        assert RabbitMQAdapter().detect(app) is False

    def test_app_with_amqp_uri_not_classified_as_rabbit(self, tmp_path: Path) -> None:
        app = tmp_path / "api"
        _write(app / "main.py", "print('api')\n")
        _write(app / ".env", "BROKER_URL=amqp://guest:guest@localhost:5672/\n")
        assert RabbitMQAdapter().detect(app) is False


class TestNestJSHttpHealth:
    def test_terminus_uses_http_health(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            '{"dependencies":{"@nestjs/core":"10.0.0","@nestjs/terminus":"10.0.0"}}\n',
        )
        _write(tmp_path / "main.ts", "async function bootstrap() {}\n")
        spec = NestJSAdapter().generate_service(tmp_path)
        assert spec.health == "http"
        assert spec.health_path == "/health"

    def test_controller_object_path_health(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            '{"dependencies":{"@nestjs/core":"10.0.0"}}\n',
        )
        _write(
            tmp_path / "health.controller.ts",
            "import { Controller, Get } from '@nestjs/common';\n"
            "@Controller({ path: 'health' })\n"
            "export class HealthController {\n"
            "  @Get()\n"
            "  check() { return { ok: true }; }\n"
            "}\n",
        )
        spec = NestJSAdapter().generate_service(tmp_path)
        assert spec.health == "http"
        assert spec.health_path == "/health"

    def test_get_health_route_uses_http(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            '{"dependencies":{"@nestjs/core":"10.0.0"}}\n',
        )
        _write(
            tmp_path / "app.controller.ts",
            "import { Controller, Get } from '@nestjs/common';\n"
            "@Controller()\n"
            "export class AppController {\n"
            "  @Get()\n"
            "  root() { return { ok: true }; }\n"
            "  @Get('health')\n"
            "  health() { return { ok: true }; }\n"
            "}\n",
        )
        spec = NestJSAdapter().generate_service(tmp_path)
        assert spec.health == "http"
        assert spec.health_path == "/health"

    def test_root_get_alone_falls_back_to_tcp(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            '{"dependencies":{"@nestjs/core":"10.0.0"}}\n',
        )
        _write(
            tmp_path / "app.controller.ts",
            "import { Controller, Get } from '@nestjs/common';\n"
            "@Controller()\n"
            "export class AppController {\n"
            "  @Get()\n"
            "  root() { return { ok: true }; }\n"
            "}\n",
        )
        spec = NestJSAdapter().generate_service(tmp_path)
        assert spec.health == "tcp"

    def test_falls_back_to_tcp_without_health(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            '{"dependencies":{"@nestjs/core":"10.0.0"},'
            '"scripts":{"start":"node dist/main.js"}}\n',
        )
        _write(tmp_path / "main.ts", "console.log('hi')\n")
        spec = NestJSAdapter().generate_service(tmp_path)
        assert spec.health == "tcp"


class TestWindowsWatcherReliability:
    def test_touch_updates_mtime_and_schedules_reload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        target = tmp_path / "app.py"
        target.write_text("print(1)\n", encoding="utf-8")
        fires: list[tuple[str, tuple[str, ...]]] = []

        watcher = ServiceWatcher(
            "web",
            [tmp_path],
            lambda name, paths: fires.append((name, tuple(paths))),
            debounce_s=0.05,
        )
        watcher.handler.prime(tmp_path)
        # Simulate a content-preserving touch that advances mtime.
        time.sleep(0.05)
        target.write_text("print(1)\n", encoding="utf-8")
        os.utime(target, None)
        watcher.notify_for_tests(target, event_type="modified")
        time.sleep(0.25)
        assert len(fires) == 1
        assert fires[0][0] == "web"
        watcher.stop()

    def test_unchanged_signature_does_not_reload_after_prime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        target = tmp_path / "app.py"
        target.write_text("print(1)\n", encoding="utf-8")
        fires: list[int] = []

        watcher = ServiceWatcher(
            "web",
            [tmp_path],
            lambda _n, _p: fires.append(1),
            debounce_s=0.05,
        )
        watcher.handler.prime(tmp_path)
        watcher.notify_for_tests(target, event_type="modified")
        # Allow all staggered Windows deferred rechecks to settle without firing.
        time.sleep(1.0)
        assert fires == []
        watcher.stop()

    def test_windows_mtime_lag_still_reloads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Early modified events (mtime not flushed yet) must not drop reloads."""

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
        old = watcher.handler._signatures[str(target.resolve())]
        state = {"n": 0}

        def lagging_signature(path: Path):
            state["n"] += 1
            # First pass (immediate + 20ms retry) still sees the old signature.
            if state["n"] <= 2:
                return old
            return (old[0] + 10**6, old[1] + 1)

        with patch.object(watcher.handler, "_signature", side_effect=lagging_signature):
            watcher.notify_for_tests(target, event_type="modified")
            time.sleep(0.9)

        assert len(fires) == 1
        watcher.stop()

    def test_debounce_collapses_duplicate_windows_events(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        target = tmp_path / "app.py"
        target.write_text("print(1)\n", encoding="utf-8")
        fires: list[int] = []

        watcher = ServiceWatcher(
            "web",
            [tmp_path],
            lambda _n, _p: fires.append(1),
            debounce_s=0.12,
        )
        watcher.handler.prime(tmp_path)
        time.sleep(0.05)
        target.write_text("print(2)\n", encoding="utf-8")
        for _ in range(5):
            watcher.notify_for_tests(target, event_type="modified")
        time.sleep(0.35)
        assert len(fires) == 1
        watcher.stop()
        # Stopping twice must not leak / raise.
        watcher.stop()
        assert watcher.handler.fire_count == 1

    def test_watch_manager_stop_clears_watchers(self, tmp_path: Path) -> None:
        target = tmp_path / "app.py"
        target.write_text("x=1\n", encoding="utf-8")
        specs = [
            ServiceSpec(
                name="web",
                path=tmp_path,
                command="python -c pass",
                reload=True,
            )
        ]
        wm = WatchManager(debounce_s=0.05, log=lambda _m: None)
        wm.start(specs, on_change=lambda *_a: None, project_root=tmp_path)
        assert "web" in wm.watched_services
        wm.stop()
        assert list(wm.watched_services) == []
        wm.stop()  # idempotent

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows takeover only")
    def test_flask_debug_takeover_on_windows(self) -> None:
        assert should_takeover_native_reload(
            "python -m flask --app app:app run --debug --port 8000"
        )
