"""Tests for optional services.json / services.yaml catalog loading."""

from __future__ import annotations

from pathlib import Path

from stackpilot.generator import generate_stackfile
from stackpilot.scanner import ServiceInfo
from stackpilot.service_catalog import load_catalog_dependencies


def _fastapi(tmp: Path, name: str) -> ServiceInfo:
    path = tmp / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n",
        encoding="utf-8",
    )
    return ServiceInfo(name=name, path=path.resolve(), framework="FastAPI")


class TestCatalogDependencies:
    def test_loads_json_depends_on_and_postgres_alias(self, tmp_path: Path) -> None:
        (tmp_path / "services.json").write_text(
            """
            {
              "services": [
                {"name": "auth", "depends_on": [], "external": ["postgresql"]},
                {"name": "gateway", "depends_on": ["auth"], "external": []}
              ]
            }
            """,
            encoding="utf-8",
        )
        deps = load_catalog_dependencies(
            tmp_path,
            known_services=["auth", "gateway"],
            known_externals=["postgres"],
        )
        assert deps["auth"] == ("postgres",)
        assert deps["gateway"] == ("auth",)

    def test_nested_monorepo_catalog(self, tmp_path: Path) -> None:
        platform = tmp_path / "enterprise-test-platform"
        platform.mkdir()
        (platform / "services.json").write_text(
            '{"services": [{"name": "user_service", "depends_on": ["auth_service"]}]}',
            encoding="utf-8",
        )
        deps = load_catalog_dependencies(
            tmp_path,
            known_services=["auth_service", "user_service"],
            known_externals=[],
        )
        assert deps["user_service"] == ("auth_service",)

    def test_generate_stackfile_emits_depends_on(self, tmp_path: Path) -> None:
        auth = _fastapi(tmp_path, "auth")
        gateway = _fastapi(tmp_path, "gateway")
        (tmp_path / "services.json").write_text(
            """
            {
              "services": [
                {"name": "auth", "depends_on": []},
                {"name": "gateway", "depends_on": ["auth"]}
              ]
            }
            """,
            encoding="utf-8",
        )
        text = generate_stackfile([auth, gateway], project_root=tmp_path)
        assert 'name="gateway"' in text
        assert 'depends_on=["auth"]' in text
