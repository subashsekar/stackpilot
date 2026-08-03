"""Soft validation warnings for detected / generated services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..base import package_has_dependency, read_text
from .entrypoint import detect_asgi_entrypoint
from .package_manager import detect_python_package_manager


@dataclass(frozen=True, slots=True)
class ValidationWarning:
    """Non-fatal sync warning for a discovered service."""

    service: str
    framework: str
    message: str

    def format(self) -> str:
        return f"Warning: {self.service} ({self.framework}): {self.message}"


class _ServiceLike:
    name: str
    path: Path
    framework: str


def validate_detected_services(
    services: Sequence[_ServiceLike],
) -> list[ValidationWarning]:
    """
    Validate discovered services and return warnings.

    Never raises — callers may print warnings and continue sync.
    """

    warnings: list[ValidationWarning] = []
    for service in services:
        warnings.extend(_validate_one(service))
    return warnings


def _validate_one(service: _ServiceLike) -> list[ValidationWarning]:
    directory = Path(service.path).expanduser()
    framework = service.framework
    name = service.name
    out: list[ValidationWarning] = []

    if framework == "FastAPI":
        entry = detect_asgi_entrypoint(directory)
        if entry is None:
            out.append(
                ValidationWarning(
                    name,
                    framework,
                    "FastAPI signals found but no ASGI entry module was resolved",
                )
            )
        if not _has_uvicorn(directory):
            out.append(
                ValidationWarning(
                    name,
                    framework,
                    "uvicorn not declared in project dependencies",
                )
            )

    elif framework == "Flask":
        if not _has_any_file(
            directory,
            ("app.py", "wsgi.py", "application.py", "main.py"),
        ) and not (directory / "app").is_dir():
            out.append(
                ValidationWarning(
                    name,
                    framework,
                    "Flask detected but no conventional entry module was found",
                )
            )

    elif framework == "Django":
        if not (directory / "manage.py").is_file():
            out.append(
                ValidationWarning(
                    name,
                    framework,
                    "Django detected but manage.py is missing",
                )
            )

    elif framework == "Express":
        if not (directory / "package.json").is_file():
            out.append(
                ValidationWarning(
                    name,
                    framework,
                    "Express detected but package.json is missing",
                )
            )
        elif not package_has_dependency(directory, "express"):
            out.append(
                ValidationWarning(
                    name,
                    framework,
                    "Express detected but the express dependency is missing",
                )
            )

    elif framework == "NestJS":
        if not (directory / "package.json").is_file():
            out.append(
                ValidationWarning(
                    name,
                    framework,
                    "NestJS detected but package.json is missing",
                )
            )
        elif not package_has_dependency(directory, "@nestjs/core"):
            out.append(
                ValidationWarning(
                    name,
                    framework,
                    "NestJS detected but @nestjs/core is missing",
                )
            )

    elif framework == "Celery":
        if not _mentions_celery(directory):
            out.append(
                ValidationWarning(
                    name,
                    framework,
                    "Celery detected but no Celery() construction was found",
                )
            )

    return out


def _has_uvicorn(directory: Path) -> bool:
    if package_has_dependency(directory, "uvicorn"):
        return True

    req = directory / "requirements.txt"
    if req.is_file() and "uvicorn" in read_text(req).lower():
        return True

    pyproject = directory / "pyproject.toml"
    if pyproject.is_file() and "uvicorn" in read_text(pyproject).lower():
        return True

    # Poetry / uv / pipenv projects may declare deps elsewhere — still warn
    # when nothing mentions uvicorn at all.
    manager = detect_python_package_manager(directory)
    if manager == "pipenv":
        pipfile = directory / "Pipfile"
        if pipfile.is_file() and "uvicorn" in read_text(pipfile).lower():
            return True

    # If the tree literally imports/runs uvicorn in source, treat as present.
    for path in (
        directory / "main.py",
        directory / "app.py",
        directory / "server.py",
    ):
        if path.is_file() and "uvicorn" in read_text(path).lower():
            return True
    return False


def _has_any_file(directory: Path, names: tuple[str, ...]) -> bool:
    return any((directory / name).is_file() for name in names)


def _mentions_celery(directory: Path) -> bool:
    for candidate in (
        directory / "celery.py",
        directory / "app.py",
        directory / "tasks.py",
        directory / "worker.py",
        directory / directory.name / "celery.py",
    ):
        if candidate.is_file() and "Celery(" in read_text(candidate):
            return True
    return False
