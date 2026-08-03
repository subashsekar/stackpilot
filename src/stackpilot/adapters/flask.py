"""Flask framework adapter."""

from __future__ import annotations

import re
from pathlib import Path

from .base import AdapterServiceSpec, FrameworkAdapter, read_text, source_mentions
from .detect.entrypoint import FlaskEntrypoint, detect_flask_entrypoint
from .detect.health_routes import discover_health_path
from .detect.package_manager import python_run_prefix
from .detect.ports import detect_preferred_port
from .detect.venv import resolve_python_executable

_ENTRY_RELATIVES: tuple[str, ...] = (
    "app.py",
    "wsgi.py",
    "application.py",
    "main.py",
    "server.py",
    "app/__init__.py",
    "src/app/__init__.py",
)

_MAIN_RUN_RE = re.compile(
    r"""if\s+__name__\s*==\s*['\"]__main__['\"]\s*:.*?\.run\s*\(""",
    re.DOTALL,
)


class FlaskAdapter(FrameworkAdapter):
    """Detect Flask apps (instance or application factory) and generate a launch command."""

    name = "Flask"
    priority = 60

    def detect(self, path: Path) -> bool:
        directory = path.expanduser()
        if not directory.is_dir():
            return False

        for relative in _ENTRY_RELATIVES:
            source = directory / Path(relative)
            if source.is_file() and _looks_like_flask(source):
                return True
        return False

    def generate_service(
        self,
        path: Path,
        *,
        port: int | None = None,
    ) -> AdapterServiceSpec:
        directory = path.expanduser()
        entry = detect_flask_entrypoint(directory)
        python = resolve_python_executable(directory)
        runner = python_run_prefix(directory, python=python)
        preferred = detect_preferred_port(directory)

        command = _build_flask_command(directory, entry, runner, python, port)
        health_path = discover_health_path(directory, self.name)
        return AdapterServiceSpec(
            framework=self.name,
            command=command,
            uses_port=True,
            health="http" if health_path is not None else "tcp",
            health_path=health_path or "/health",
            preferred_port=preferred,
            reload=True,
        )


def _build_flask_command(
    directory: Path,
    entry: object | None,
    runner: str,
    python: str,
    port: int | None,
) -> str:
    flask_entry = entry if isinstance(entry, FlaskEntrypoint) else None

    # Only emit ``python app.py`` when the file actually starts the server.
    if (
        flask_entry is not None
        and not flask_entry.is_factory
        and flask_entry.file.resolve() == (directory / "app.py").resolve()
        and runner == python
        and _has_runnable_main(flask_entry.file)
    ):
        return f"{python} app.py"

    if flask_entry is not None:
        target = flask_entry.target
    else:
        target = "app:app"

    command = f"{runner} -m flask --app {target} run --host 0.0.0.0"
    if port is not None:
        command = f"{command} --port {port}"
    return command


def _has_runnable_main(path: Path) -> bool:
    text = read_text(path)
    if not text:
        return False
    return bool(_MAIN_RUN_RE.search(text))


def _looks_like_flask(path: Path) -> bool:
    return source_mentions(
        path,
        "from flask import",
        "import flask",
        "Flask(",
        "def create_app(",
    )
