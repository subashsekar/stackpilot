"""Port auto-detection for status / ps / dashboard display."""

from __future__ import annotations

from pathlib import Path

from stackpilot.config import HttpHealthCheck, ServiceSpec, TcpHealthCheck
from stackpilot.models import configured_port
from stackpilot.port_detect import (
    parse_port_from_command,
    resolve_service_port,
    service_display_url,
)


def test_parse_port_from_uvicorn_flag() -> None:
    assert parse_port_from_command("uvicorn main:app --port 8001") == 8001
    assert parse_port_from_command("uvicorn main:app --port=9000") == 9000
    assert parse_port_from_command(["uvicorn", "main:app", "-p", "8080"]) == 8080


def test_parse_port_from_host_binding() -> None:
    assert parse_port_from_command("python manage.py runserver 0.0.0.0:8000") == 8000
    assert parse_port_from_command("flask run --host 127.0.0.1:5001") == 5001
    assert parse_port_from_command("something localhost:3000") == 3000


def test_parse_port_from_env_style() -> None:
    assert parse_port_from_command("PORT=8123 python app.py") == 8123
    assert parse_port_from_command("env PORT 7000 npm start") == 7000


def test_parse_port_missing() -> None:
    assert parse_port_from_command("python main.py") is None
    assert parse_port_from_command("") is None


def test_resolve_prefers_health_then_explicit_then_command(tmp_path: Path) -> None:
    assert (
        resolve_service_port(
            ServiceSpec(
                name="a",
                path=tmp_path,
                command="uvicorn main:app --port 8001",
                health_check=TcpHealthCheck(host="127.0.0.1", port=9000),
            )
        )
        == 9000
    )
    assert (
        resolve_service_port(
            ServiceSpec(
                name="b",
                path=tmp_path,
                command="uvicorn main:app --port 8001",
                port=8111,
            )
        )
        == 8111
    )
    assert (
        resolve_service_port(
            ServiceSpec(
                name="c",
                path=tmp_path,
                command="uvicorn main:app --port 8001",
            )
        )
        == 8001
    )
    assert (
        resolve_service_port(
            ServiceSpec(
                name="d",
                path=tmp_path,
                command="python main.py",
                health_check=HttpHealthCheck(url="http://127.0.0.1:8088/health"),
            )
        )
        == 8088
    )


def test_configured_port_uses_command_when_no_health(tmp_path: Path) -> None:
    assert (
        configured_port(
            ServiceSpec(
                name="gw",
                path=tmp_path,
                command="npm start -- --port 3000",
            )
        )
        == 3000
    )


def test_service_display_url_from_http_health(tmp_path: Path) -> None:
    assert (
        service_display_url(
            ServiceSpec(
                name="gateway",
                path=tmp_path,
                command="python manage.py runserver 0.0.0.0:8001",
                health_check=HttpHealthCheck(url="http://127.0.0.1:8001/health"),
            )
        )
        == "http://127.0.0.1:8001"
    )


def test_service_display_url_from_port(tmp_path: Path) -> None:
    assert (
        service_display_url(
            ServiceSpec(
                name="api",
                path=tmp_path,
                command="python app.py",
                port=9000,
            )
        )
        == "http://127.0.0.1:9000"
    )
