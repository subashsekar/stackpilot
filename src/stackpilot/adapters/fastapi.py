"""FastAPI framework adapter."""

from __future__ import annotations

from pathlib import Path

from .base import AdapterServiceSpec, FrameworkAdapter, source_mentions
from .detect.entrypoint import detect_asgi_entrypoint
from .detect.health_routes import discover_health_path
from .detect.package_manager import python_run_prefix
from .detect.ports import detect_preferred_port
from .detect.venv import resolve_python_executable

# Layouts developers commonly use for FastAPI services.
_ENTRY_RELATIVES: tuple[str, ...] = (
    "main.py",
    "app.py",
    "server.py",
    "api.py",
    "application.py",
    "app/main.py",
    "src/main.py",
    "api/main.py",
    "src/app/main.py",
    "app/__init__.py",
    "src/app/__init__.py",
)


class FastAPIAdapter(FrameworkAdapter):
    """Detect FastAPI apps and generate a uvicorn launch command."""

    name = "FastAPI"
    priority = 50

    def detect(self, path: Path) -> bool:
        directory = path.expanduser()
        if not directory.is_dir():
            return False

        for relative in _ENTRY_RELATIVES:
            source = directory / Path(relative)
            if source.is_file() and _looks_like_fastapi(source):
                return True
        return False

    def generate_service(
        self,
        path: Path,
        *,
        port: int | None = None,
    ) -> AdapterServiceSpec:
        directory = path.expanduser()
        entry = detect_asgi_entrypoint(directory)
        if entry is None:
            module = "app"
            attr = "app"
            app_dir = None
        else:
            module = entry.module
            attr = entry.attr
            app_dir = entry.app_dir

        python = resolve_python_executable(directory)
        runner = python_run_prefix(directory, python=python)
        target = f"{module}:{attr}"
        preferred = detect_preferred_port(directory)

        # Dev stacks expect live reload; Windows takeover strips --reload at spawn.
        parts = [runner, "-m", "uvicorn", target, "--reload"]
        if app_dir:
            parts.extend(["--app-dir", app_dir])
        parts.extend(["--host", "0.0.0.0"])
        # Only inject --port when Sync assigned/detected one — never invent here.
        if port is not None:
            parts.extend(["--port", str(port)])

        health_path = discover_health_path(directory, self.name)
        return AdapterServiceSpec(
            framework=self.name,
            command=" ".join(parts),
            uses_port=True,
            health="http" if health_path is not None else "tcp",
            health_path=health_path or "/health",
            preferred_port=preferred,
            # Native uvicorn --reload; StackPilot reload=True for Windows takeover
            # coordination and when native reload is stripped.
            reload=True,
        )


def _looks_like_fastapi(path: Path) -> bool:
    return source_mentions(
        path,
        "fastapi",
        "FastAPI(",
        "APIRouter(",
        "uvicorn",
    )
