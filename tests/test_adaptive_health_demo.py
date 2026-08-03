"""Pytest coverage for the adaptive-health discovery fixture."""

from __future__ import annotations

from pathlib import Path

import pytest

from stackpilot.adapters.detect.health_routes import discover_health_path
from stackpilot.adapters.django import DjangoAdapter
from stackpilot.adapters.express import ExpressAdapter
from stackpilot.adapters.fastapi import FastAPIAdapter
from stackpilot.adapters.flask import FlaskAdapter
from stackpilot.adapters.nestjs import NestJSAdapter
from stackpilot.generator import generate_stackfile
from stackpilot.scanner import scan_project

DEMO = Path(__file__).resolve().parent / "fixtures" / "adaptive-health"

EXPECTED = {
    "api_v1_health": ("FastAPI", "/api/v1/health", "http"),
    "api_ready": ("FastAPI", "/ready", "http"),
    "api_root": ("FastAPI", "/", "http"),
    "api_none": ("FastAPI", None, "tcp"),
    "web_flask": ("Flask", "/api/health", "http"),
    "web_django": ("Django", "/health/", "http"),
    "app_express": ("Express", "/internal/health", "http"),
    "app_nestjs": ("NestJS", "/health", "http"),
}

ADAPTERS = {
    "FastAPI": FastAPIAdapter,
    "Flask": FlaskAdapter,
    "Django": DjangoAdapter,
    "Express": ExpressAdapter,
    "NestJS": NestJSAdapter,
}


@pytest.fixture(scope="module")
def services():
    found = scan_project(DEMO)
    assert found, "demo project produced no services"
    return {s.name: s for s in found}


class TestAdaptiveHealthDemo:
    def test_all_expected_services_discovered(self, services) -> None:
        assert set(EXPECTED) <= set(services)

    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_framework_and_health_path(self, services, name: str) -> None:
        framework, path, kind = EXPECTED[name]
        service = services[name]
        assert service.framework == framework

        discovered = discover_health_path(Path(service.path), framework)
        if path is None:
            assert discovered is None
        elif path in {"/health", "/health/"}:
            assert discovered in {"/health", "/health/"}
        else:
            assert discovered == path

        adapter = ADAPTERS[framework]()
        spec = adapter.generate_service(Path(service.path), port=8000)
        assert spec.health == kind
        if kind == "http":
            if path in {"/health", "/health/"}:
                assert spec.health_path in {"/health", "/health/"}
            else:
                assert spec.health_path == path

    def test_generated_stackfile_uses_discovered_endpoints(self, services) -> None:
        ordered = scan_project(DEMO)
        text = generate_stackfile(ordered, project_root=DEMO)
        assert "/api/v1/health" in text
        assert "/ready" in text
        assert "/api/health" in text
        assert "/internal/health" in text
        assert "TcpHealthCheck(" in text
        assert "HttpHealthCheck(" in text
        # Root health for api_root appears as trailing slash URL path.
        assert '800' in text  # ports assigned
