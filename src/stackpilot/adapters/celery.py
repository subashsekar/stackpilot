"""Celery framework adapter."""

from __future__ import annotations

from pathlib import Path

from .base import (
    AdapterServiceSpec,
    FrameworkAdapter,
    any_python_source_mentions,
    celery_broker_depends_on,
    detect_celery_app_name,
    detect_celery_broker,
    read_text,
    source_mentions,
)
from .detect.package_manager import cli_is_runnable, detect_python_package_manager
from .detect.venv import resolve_python_executable


class CeleryAdapter(FrameworkAdapter):
    """Detect Celery workers and generate a ``celery -A … worker`` command."""

    name = "Celery"
    priority = 40

    def detect(self, path: Path) -> bool:
        directory = path.expanduser()
        if not directory.is_dir():
            return False

        celery_py = directory / "celery.py"
        if celery_py.is_file() and source_mentions(celery_py, "Celery(", "celery"):
            return True

        nested = directory / directory.name / "celery.py"
        if nested.is_file() and source_mentions(nested, "Celery("):
            return True

        # Worker modules one/two levels deep (tasks.py, worker.py, …).
        has_celery = any_python_source_mentions(
            directory,
            "Celery(",
            max_depth=2,
        )
        if not has_celery:
            return False

        worker_signal = (
            directory.name.lower() in {"worker", "workers", "celery", "tasks"}
            or any_python_source_mentions(
                directory,
                "worker",
                ".worker",
                max_depth=2,
            )
            or _mentions_worker_in_celery_module(directory)
            or _has_worker_named_module(directory)
        )
        return worker_signal

    def generate_service(
        self,
        path: Path,
        *,
        port: int | None = None,
    ) -> AdapterServiceSpec:
        _ = port
        directory = path.expanduser()
        app_name = detect_celery_app_name(directory)
        broker = detect_celery_broker(directory)
        command = _celery_worker_command(directory, app_name)
        return AdapterServiceSpec(
            framework=self.name,
            command=command,
            uses_port=False,
            health="process",
            reload=False,
            depends_on=celery_broker_depends_on(broker),
        )


def _celery_worker_command(directory: Path, app_name: str) -> str:
    manager = detect_python_package_manager(directory)
    if manager == "uv" and cli_is_runnable("uv"):
        return f"uv run celery -A {app_name} worker"
    if manager == "poetry" and cli_is_runnable("poetry"):
        return f"poetry run celery -A {app_name} worker"
    if manager == "pipenv" and cli_is_runnable("pipenv"):
        return f"pipenv run celery -A {app_name} worker"

    python = resolve_python_executable(directory)
    if python != "python":
        return f"{python} -m celery -A {app_name} worker"
    return f"celery -A {app_name} worker"


def _has_worker_named_module(directory: Path) -> bool:
    for name in ("worker.py", "workers.py", "tasks.py"):
        if (directory / name).is_file() and source_mentions(
            directory / name, "Celery("
        ):
            return True
    return False


def _mentions_worker_in_celery_module(directory: Path) -> bool:
    for candidate in (
        directory / "celery.py",
        directory / directory.name / "celery.py",
    ):
        if candidate.is_file() and "worker" in read_text(candidate).lower():
            return True
    return False
