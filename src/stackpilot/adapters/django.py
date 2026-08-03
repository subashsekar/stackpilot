"""Django framework adapter."""

from __future__ import annotations

import re
from pathlib import Path

from .base import (
    AdapterServiceSpec,
    FrameworkAdapter,
    find_django_wsgi_or_asgi,
    find_settings_py,
    read_text,
    source_mentions,
)
from .detect.health_routes import discover_health_path
from .detect.package_manager import python_run_prefix
from .detect.ports import detect_preferred_port
from .detect.venv import resolve_python_executable

_RUNSERVER_PORT_RE = re.compile(
    r"""runserver(?:\s+[^\s]*)?\s+(?:0\.0\.0\.0:|127\.0\.0\.1:|localhost:)?(\d{2,5})\b""",
    re.IGNORECASE,
)


class DjangoAdapter(FrameworkAdapter):
    """Detect Django projects via ``manage.py`` and settings / WSGI / ASGI."""

    name = "Django"
    priority = 30

    def detect(self, path: Path) -> bool:
        directory = path.expanduser()
        if not directory.is_dir():
            return False

        manage = directory / "manage.py"
        if manage.is_file():
            if find_settings_py(directory) is not None:
                return True
            if find_django_wsgi_or_asgi(directory) is not None:
                return True
            return source_mentions(manage, "django", "DJANGO_SETTINGS_MODULE")

        # Strong layout without manage.py (still warn at sync time).
        return (
            find_settings_py(directory) is not None
            and find_django_wsgi_or_asgi(directory) is not None
        )

    def generate_service(
        self,
        path: Path,
        *,
        port: int | None = None,
    ) -> AdapterServiceSpec:
        directory = path.expanduser()
        python = resolve_python_executable(directory)
        runner = python_run_prefix(directory, python=python)
        preferred = detect_preferred_port(directory) or _port_from_manage_or_settings(
            directory
        )

        if (directory / "manage.py").is_file():
            if runner == python:
                base = f"{python} manage.py runserver"
            else:
                base = f"{runner} manage.py runserver"
        else:
            base = f"{runner} -m django runserver"

        bind_port = port if port is not None else preferred
        if bind_port is None:
            command = base
        else:
            command = f"{base} 0.0.0.0:{bind_port}"

        health_path = discover_health_path(directory, self.name)
        return AdapterServiceSpec(
            framework=self.name,
            command=command,
            uses_port=True,
            health="http" if health_path is not None else "tcp",
            health_path=health_path or "/health",
            preferred_port=preferred,
            # runserver autoreloads by default; StackPilot takes over on Windows.
            reload=True,
        )


def _port_from_manage_or_settings(directory: Path) -> int | None:
    for candidate in (
        directory / "manage.py",
        find_settings_py(directory),
    ):
        if candidate is None or not candidate.is_file():
            continue
        text = read_text(candidate)
        match = _RUNSERVER_PORT_RE.search(text)
        if match:
            port = int(match.group(1))
            if 1 <= port <= 65535:
                return port
    return None
