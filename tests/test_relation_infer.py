"""Tests for universal microservice relation inference."""

from __future__ import annotations

from pathlib import Path

from stackpilot.config import ServiceSpec, Stack
from stackpilot.relation_infer import (
    fill_missing_stack_dependencies,
    infer_service_dependencies,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _spec(tmp: Path, name: str, port: int, body: str = "") -> ServiceSpec:
    root = tmp / name
    root.mkdir(parents=True, exist_ok=True)
    if body:
        _write(root / "app" / "main.py", body)
    else:
        _write(root / "app" / "main.py", "app = None\n")
    return ServiceSpec(
        name=name,
        path=root,
        command=f"uvicorn app.main:app --port {port}",
        port=port,
    )


class TestInferFromCodeRefs:
    def test_detects_service_url_attr_and_port(self, tmp_path: Path) -> None:
        auth = _spec(tmp_path, "auth_service", 8001)
        user = _spec(
            tmp_path,
            "user_service",
            8002,
            body=(
                "from shared import settings\n"
                "url = settings.auth_service_url\n"
                "fallback = 'http://localhost:8001/health'\n"
            ),
        )
        gateway = _spec(
            tmp_path,
            "gateway",
            8000,
            body=(
                "upstreams = [\n"
                "  settings.auth_service_url,\n"
                "  settings.user_service_url,\n"
                "]\n"
            ),
        )
        deps = infer_service_dependencies(
            project_root=tmp_path,
            services=[auth, user, gateway],
        )
        assert "auth_service" in deps["user_service"]
        assert set(deps["gateway"]) >= {"auth_service", "user_service"}
        assert "auth_service" not in deps or not deps.get("auth_service")


class TestInferFromCompose:
    def test_compose_depends_on(self, tmp_path: Path) -> None:
        auth = _spec(tmp_path, "auth_service", 8001)
        user = _spec(tmp_path, "user_service", 8002)
        _write(
            tmp_path / "docker-compose.yml",
            """
services:
  auth_service:
    image: auth
  user_service:
    image: user
    depends_on:
      auth_service:
        condition: service_healthy
""",
        )
        deps = infer_service_dependencies(
            project_root=tmp_path,
            services=[auth, user],
        )
        assert deps["user_service"] == ("auth_service",)


class TestFillMissingStackDependencies:
    def test_fills_empty_depends_on_only(self, tmp_path: Path) -> None:
        auth = _spec(tmp_path, "auth_service", 8001)
        user = _spec(
            tmp_path,
            "user_service",
            8002,
            body="AUTH_SERVICE_URL = 'http://auth_service:8001'\n",
        )
        stack = Stack()
        stack._services.extend(
            [
                ServiceSpec(
                    name="auth_service",
                    path=auth.path,
                    command=auth.command,
                    port=8001,
                ),
                ServiceSpec(
                    name="user_service",
                    path=user.path,
                    command=user.command,
                    port=8002,
                ),
                ServiceSpec(
                    name="gateway",
                    path=_spec(tmp_path, "gateway", 8000).path,
                    command="uvicorn app:app --port 8000",
                    port=8000,
                    depends_on=("user_service",),
                ),
            ]
        )
        filled = fill_missing_stack_dependencies(stack, project_root=tmp_path)
        by_name = {s.name: s for s in filled.services}
        assert "auth_service" in by_name["user_service"].depends_on
        # Explicit Stackfile edge preserved (not replaced).
        assert by_name["gateway"].depends_on == ("user_service",)
