"""Regression tests for Adaptive Health Detection Engine."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from stackpilot.adapters.detect.health_probe import (
    format_health_diagnostic,
    select_working_health_endpoint,
)
from stackpilot.adapters.detect.health_routes import (
    discover_fastapi_routes,
    discover_flask_routes,
    discover_django_routes,
    discover_express_routes,
    discover_nestjs_routes,
    discover_health_path,
    rank_health_routes,
    resolve_health_endpoint,
    select_best_health_path,
)
from stackpilot.adapters.django import DjangoAdapter
from stackpilot.adapters.express import ExpressAdapter
from stackpilot.adapters.fastapi import FastAPIAdapter
from stackpilot.adapters.flask import FlaskAdapter
from stackpilot.adapters.nestjs import NestJSAdapter
from stackpilot.generator import generate_stackfile
from stackpilot.http_checker import HttpProbeResult, probe_http
from stackpilot.scanner import scan_project


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class _MultiPathHandler(BaseHTTPRequestHandler):
    """Serve configured status codes per path."""

    responses: dict[str, int] = {}

    def do_GET(self) -> None:  # noqa: N802
        code = self.responses.get(self.path, 404)
        self.send_response(code)
        self.end_headers()
        if code < 400:
            self.wfile.write(b"ok")

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _serve(responses: dict[str, int], stop: threading.Event) -> str:
    handler = type("Handler", (_MultiPathHandler,), {"responses": dict(responses)})
    server = HTTPServer(("127.0.0.1", 0), handler)
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def _shutdown() -> None:
        stop.wait()
        server.shutdown()

    threading.Thread(target=_shutdown, daemon=True).start()
    return f"http://127.0.0.1:{port}"


class TestRanking:
    def test_priority_order(self) -> None:
        ranked = rank_health_routes(
            ["/", "/ping", "/api/v1/health", "/ready", "/health", "/docs"]
        )
        assert ranked[0] == "/health"
        assert "/ready" in ranked
        assert "/ping" in ranked
        assert "/" in ranked
        assert "/docs" not in ranked

    def test_api_v1_health_beats_root(self) -> None:
        assert select_best_health_path(["/", "/api/v1/health"]) == "/api/v1/health"

    def test_ready_beats_ping(self) -> None:
        assert select_best_health_path(["/ping", "/ready"]) == "/ready"

    def test_liveness_root_preferred_when_health_also_present(self) -> None:
        # Plain /health still beats / when both are simple candidates.
        assert select_best_health_path(["/", "/health"]) == "/health"

    def test_health_wins_when_root_absent(self) -> None:
        assert select_best_health_path(["/health", "/ready"]) == "/health"

    def test_aggregate_health_handler_skipped_for_fastapi(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "main.py",
            "from fastapi import APIRouter, FastAPI\n"
            "app = FastAPI()\n"
            "router = APIRouter()\n"
            "@router.get('/')\n"
            "def health_check():\n"
            "    return {'ok': True}\n"
            "@router.get('/health')\n"
            "async def health_detailed():\n"
            "    return await get_aggregate_health()\n"
            "app.include_router(router)\n",
        )
        assert discover_health_path(tmp_path, "FastAPI") == "/"

    def test_account_status_is_not_health(self) -> None:
        ranked = rank_health_routes(["/account/status", "/", "/health/db", "/auth/me"])
        assert "/account/status" not in ranked
        assert "/auth/me" not in ranked
        assert ranked[0] == "/"

    def test_root_beats_account_status_when_no_health(self) -> None:
        assert select_best_health_path(["/account/status", "/"]) == "/"

    def test_cache_health_still_qualifies(self) -> None:
        assert "/cache/health" in rank_health_routes(["/", "/cache/health"])

    def test_root_liveness_beats_subsystem_and_db_health(self) -> None:
        assert select_best_health_path(["/", "/cache/health", "/health/db"]) == "/"
        assert select_best_health_path(["/", "/interview/health"]) == "/"

    def test_api_health_still_beats_root(self) -> None:
        assert select_best_health_path(["/", "/api/v1/health"]) == "/api/v1/health"


class TestFastAPIDiscovery:
    def test_router_prefix_root_route(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "main.py",
            "from fastapi import APIRouter, FastAPI\n"
            "app = FastAPI()\n"
            'router = APIRouter(prefix="/health")\n'
            '@router.get("/")\n'
            "def ok():\n"
            "    return {}\n"
            "app.include_router(router)\n",
        )
        routes = discover_fastapi_routes(tmp_path)
        assert "/health" in routes
        assert discover_health_path(tmp_path, "FastAPI") == "/health"

    def test_nested_prefixes(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "main.py",
            "from fastapi import APIRouter, FastAPI\n"
            "app = FastAPI()\n"
            'router = APIRouter(prefix="/health")\n'
            '@router.get("/")\n'
            "def ok():\n"
            "    return {}\n"
            'app.include_router(router, prefix="/v1")\n',
        )
        assert discover_health_path(tmp_path, "FastAPI") == "/v1/health"

    def test_multiple_routers_picks_best(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "main.py",
            "from fastapi import APIRouter, FastAPI\n"
            "app = FastAPI()\n"
            'a = APIRouter(prefix="/api")\n'
            '@a.get("/ping")\n'
            "def ping():\n"
            "    return {}\n"
            'b = APIRouter()\n'
            '@b.get("/health")\n'
            "def health():\n"
            "    return {}\n"
            "app.include_router(a)\n"
            "app.include_router(b)\n",
        )
        assert discover_health_path(tmp_path, "FastAPI") == "/health"

    def test_health_at_root(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "main.py",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            '@app.get("/")\n'
            "def root():\n"
            "    return {}\n",
        )
        assert discover_health_path(tmp_path, "FastAPI") == "/"

    def test_health_at_api_v1(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "main.py",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            '@app.get("/api/v1/health")\n'
            "def health():\n"
            "    return {}\n",
        )
        assert discover_health_path(tmp_path, "FastAPI") == "/api/v1/health"

    def test_ready_route(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "main.py",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            '@app.get("/ready")\n'
            "def ready():\n"
            "    return {}\n",
        )
        assert discover_health_path(tmp_path, "FastAPI") == "/ready"

    def test_ping_route(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "main.py",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            '@app.get("/ping")\n'
            "def ping():\n"
            "    return {}\n",
        )
        assert discover_health_path(tmp_path, "FastAPI") == "/ping"

    def test_no_health_endpoint(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "main.py",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            '@app.get("/docs")\n'
            "def docs():\n"
            "    return {}\n",
        )
        assert discover_health_path(tmp_path, "FastAPI") is None
        spec = FastAPIAdapter().generate_service(tmp_path, port=8000)
        assert spec.health == "tcp"

    def test_router_prefix_health_path(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "main.py",
            "from fastapi import APIRouter, FastAPI\n"
            "app = FastAPI()\n"
            'router = APIRouter(prefix="/api")\n'
            '@router.get("/health")\n'
            "def health():\n"
            "    return {}\n"
            "app.include_router(router)\n",
        )
        assert discover_health_path(tmp_path, "FastAPI") == "/api/health"


class TestFlaskDiscovery:
    def test_app_route_and_blueprint(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "app.py",
            "from flask import Blueprint, Flask\n"
            "app = Flask(__name__)\n"
            'bp = Blueprint("api", __name__, url_prefix="/api")\n'
            '@bp.route("/health")\n'
            "def health():\n"
            "    return 'ok'\n"
            '@app.get("/")\n'
            "def index():\n"
            "    return 'ok'\n"
            "app.register_blueprint(bp)\n",
        )
        routes = discover_flask_routes(tmp_path)
        assert "/api/health" in routes
        assert "/" in routes
        assert discover_health_path(tmp_path, "Flask") == "/api/health"
        spec = FlaskAdapter().generate_service(tmp_path, port=8000)
        assert spec.health == "http"
        assert spec.health_path == "/api/health"


class TestDjangoDiscovery:
    def test_urlpatterns_health(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "manage.py",
            "import django\n",
        )
        _write(
            tmp_path / "project" / "settings.py",
            "SECRET_KEY='x'\n",
        )
        _write(
            tmp_path / "project" / "urls.py",
            "from django.urls import path\n"
            "from django.http import HttpResponse\n"
            "def health(_r):\n"
            "    return HttpResponse('ok')\n"
            "urlpatterns = [\n"
            '    path("health/", health),\n'
            '    path("ready/", health),\n'
            "]\n",
        )
        routes = discover_django_routes(tmp_path)
        assert "/health/" in routes or "/health" in routes
        path = discover_health_path(tmp_path, "Django")
        assert path in {"/health", "/health/"}
        spec = DjangoAdapter().generate_service(tmp_path, port=8000)
        assert spec.health == "http"
        assert spec.health_path in {"/health", "/health/"}


class TestExpressDiscovery:
    def test_app_and_router_mount(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            '{"dependencies":{"express":"4.0.0"}}\n',
        )
        _write(
            tmp_path / "server.js",
            "const express = require('express');\n"
            "const app = express();\n"
            "const router = express.Router();\n"
            "router.get('/health', (req, res) => res.send('ok'));\n"
            "app.use('/api', router);\n"
            "app.get('/ping', (req, res) => res.send('ok'));\n",
        )
        routes = discover_express_routes(tmp_path)
        assert "/api/health" in routes
        assert "/ping" in routes
        assert discover_health_path(tmp_path, "Express") == "/api/health"
        spec = ExpressAdapter().generate_service(tmp_path)
        assert spec.health_path == "/api/health"


class TestNestJSDiscovery:
    def test_controller_get(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            '{"dependencies":{"@nestjs/core":"10.0.0"}}\n',
        )
        _write(
            tmp_path / "app.controller.ts",
            "import { Controller, Get } from '@nestjs/common';\n"
            "@Controller('health')\n"
            "export class HealthController {\n"
            "  @Get()\n"
            "  check() { return { ok: true }; }\n"
            "}\n",
        )
        routes = discover_nestjs_routes(tmp_path)
        assert "/health" in routes
        assert discover_health_path(tmp_path, "NestJS") == "/health"
        spec = NestJSAdapter().generate_service(tmp_path)
        assert spec.health_path == "/health"


class TestExplicitOverride:
    def test_explicit_stackfile_path_overrides_discovery(self) -> None:
        selection = resolve_health_endpoint(
            explicit_path="/custom/health",
            discovered_routes=["/health", "/ready"],
        )
        assert selection.kind == "explicit"
        assert selection.path == "/custom/health"


class TestProbeValidation:
    def test_http_200_to_399_healthy(self) -> None:
        stop = threading.Event()
        try:
            base = _serve({"/ok": 204, "/redir": 302}, stop)
            assert probe_http(f"{base}/ok").kind == "healthy"
            assert probe_http(f"{base}/redir").kind == "healthy"
        finally:
            stop.set()

    def test_404_then_second_endpoint_succeeds(self) -> None:
        stop = threading.Event()
        try:
            base = _serve({"/health": 404, "/ready": 200}, stop)
            selection, result = select_working_health_endpoint(
                base_url=base,
                discovered_routes=["/health", "/ready"],
            )
            assert selection.kind == "http"
            assert selection.path == "/ready"
            assert result is not None and result.kind == "healthy"
        finally:
            stop.set()

    def test_explicit_path_not_overridden_on_404(self) -> None:
        stop = threading.Event()
        try:
            base = _serve({"/custom": 404, "/health": 200}, stop)
            selection, result = select_working_health_endpoint(
                base_url=base,
                discovered_routes=["/health"],
                explicit_path="/custom",
            )
            assert selection.kind == "explicit"
            assert selection.path == "/custom"
            assert result is not None and result.kind == "not_found"
        finally:
            stop.set()

    def test_tcp_fallback_when_no_http_health(self) -> None:
        selection, _ = select_working_health_endpoint(
            base_url="http://127.0.0.1:1",
            discovered_routes=["/docs"],
            tcp_check=lambda: True,
            probe=lambda _url: HttpProbeResult(kind="not_found", status_code=404),
        )
        assert selection.kind == "tcp"
        assert "TCP" in selection.detail

    def test_no_health_tcp_fallback_selection(self) -> None:
        selection = resolve_health_endpoint(discovered_routes=["/docs", "/openapi.json"])
        assert selection.kind == "tcp"
        assert selection.path is None


class TestDiagnostics:
    def test_not_found_recommendation(self) -> None:
        text = format_health_diagnostic(
            configured_path="/health",
            probe=HttpProbeResult(kind="not_found", status_code=404, detail="404 Not Found"),
            discovered_routes=["/", "/docs", "/openapi.json"],
            application_running=True,
        )
        assert "Health endpoint not found" in text
        assert "Configured endpoint:" in text
        assert "/health" in text
        assert "404 Not Found" in text
        assert "Application is running." in text
        assert "Detected endpoints:" in text
        assert "Recommendation:" in text
        assert 'Use "/" as health endpoint' in text

    def test_detected_healthy_message(self) -> None:
        from stackpilot.adapters.detect.health_routes import HealthEndpointSelection

        text = format_health_diagnostic(
            configured_path=None,
            probe=HttpProbeResult(kind="healthy", status_code=200, detail="200 OK"),
            selected=HealthEndpointSelection(
                kind="http", path="/api/v1/health", detail="detected"
            ),
        )
        assert "Detected health endpoint" in text
        assert "/api/v1/health" in text
        assert "Healthy" in text

    def test_tcp_fallback_message(self) -> None:
        from stackpilot.adapters.detect.health_routes import HealthEndpointSelection

        text = format_health_diagnostic(
            configured_path=None,
            probe=None,
            selected=HealthEndpointSelection(
                kind="tcp",
                detail="no HTTP health endpoint found; TCP connection successful",
            ),
        )
        assert "No HTTP health endpoint found." in text
        assert "Using TCP health." in text


class TestGeneratorIntegration:
    def test_generated_stackfile_uses_discovered_path(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "api" / "main.py",
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            '@app.get("/ready")\n'
            "def ready():\n"
            "    return {}\n",
        )
        services = scan_project(tmp_path)
        text = generate_stackfile(services, project_root=tmp_path)
        assert 'HttpHealthCheck(url="http://127.0.0.1:8000/ready")' in text
