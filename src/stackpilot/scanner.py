"""Project scanner — discovers candidate service directories.

Framework detection is delegated entirely to the adapter registry.
This module only walks the tree and records matching adapter results.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .adapters import FrameworkAdapter, default_registry

# Keep a stable string union for type checkers / docs. Adapters may also
# introduce custom names via registry.register().
Framework = str

IGNORED_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".github",
        ".idea",
        ".vscode",
        ".venv",
        "venv",
        "env",
        "virtualenv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "dist",
        "build",
        ".stackpilot",
    }
)

_CANONICAL_EXTERNAL_NAMES: Final[dict[str, str]] = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "redis": "redis",
}


@dataclass(frozen=True, slots=True)
class ServiceInfo:
    """A microservice discovered by scanning a project tree."""

    name: str
    path: Path
    framework: Framework


# Backward-compatible alias for earlier Day drafts.
DetectedService = ServiceInfo


def detect_framework(path: Path) -> Framework | None:
    """
    Detect the framework used by a service directory.

    Returns ``None`` when no registered adapter matches the directory.
    """

    return default_registry.detect_framework(path)


def scan_directory(path: Path) -> ServiceInfo | None:
    """Inspect a single directory and return service metadata when recognized."""

    directory = path.expanduser().resolve()
    adapter = default_registry.match(directory)
    if adapter is None:
        return None

    return ServiceInfo(
        name=directory.name,
        path=directory,
        framework=adapter.name,
    )


def scan_project(root: Path) -> list[ServiceInfo]:
    """
    Recursively discover microservice directories under ``root``.

    The project root itself is never emitted as a service; only nested
    directories are considered. Ignored directories are skipped entirely.

    Application matches prune descendants so inner packages (for example
    ``app/`` inside a FastAPI service) are not emitted as separate services.

    External matches (Postgres / Redis) do **not** prune. Monorepo roots
    often ship ``docker-compose.yml`` with Postgres while nesting real apps
    underneath; those apps must still be discovered.
    """

    project_root = root.expanduser().resolve()
    if not project_root.is_dir():
        raise NotADirectoryError(f"Project root is not a directory: {project_root}")

    detected: list[ServiceInfo] = []
    # Resolved path keys — guarantees finite traversal under symlink cycles
    # (A→B, B→A) and nested link loops.
    visited: set[str] = set()

    def _walk(directory: Path) -> None:
        try:
            resolved_key = str(directory.resolve())
        except OSError:
            return
        if resolved_key in visited:
            return
        visited.add(resolved_key)

        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name.lower())
        except OSError:
            return

        for child in children:
            # is_dir() follows symlinks; visited keys prevent infinite recursion.
            if not child.is_dir():
                continue
            if child.name in IGNORED_DIRECTORY_NAMES:
                continue

            adapter = default_registry.match(child)
            if adapter is not None:
                try:
                    service_path = child.resolve()
                except OSError:
                    continue
                detected.append(
                    ServiceInfo(
                        name=child.name,
                        path=service_path,
                        framework=adapter.name,
                    )
                )
                if adapter.external:
                    # Infrastructure tags the folder but does not own the tree.
                    _walk(child)
                continue

            _walk(child)

    _walk(project_root)
    return _finalize_detected_services(detected)


def _finalize_detected_services(services: list[ServiceInfo]) -> list[ServiceInfo]:
    """
    Clean up external matches that sit above nested discoveries.

    - Drop an external when a descendant is the same infrastructure type
      (dedicated ``postgres/`` wins over compose-at-monorepo-root).
    - Rename remaining ancestor externals to canonical names (``postgres``,
      ``redis``) so Stackfiles do not use the monorepo folder name.
    """

    if not services:
        return services

    resolved = [(service, service.path.resolve()) for service in services]
    kept: list[ServiceInfo] = []

    for service, path in resolved:
        adapter = default_registry.get(service.framework)
        if adapter is not None and adapter.external:
            same_type_descendant = any(
                other.framework == service.framework
                and other_path != path
                and _is_under(other_path, path)
                for other, other_path in resolved
            )
            if same_type_descendant:
                continue
        kept.append(service)

    resolved_kept = [(service, service.path.resolve()) for service in kept]
    used_names: set[str] = set()
    finalized: list[ServiceInfo] = []

    for service, path in resolved_kept:
        adapter = default_registry.get(service.framework)
        name = service.name
        if adapter is not None and adapter.external:
            has_descendant = any(
                other_path != path and _is_under(other_path, path)
                for _, other_path in resolved_kept
            )
            if has_descendant:
                name = _unique_name(
                    _canonical_external_name(adapter),
                    used_names,
                )
        used_names.add(name)
        finalized.append(
            ServiceInfo(name=name, path=service.path, framework=service.framework)
        )

    finalized.sort(key=lambda service: (service.name.lower(), service.path.as_posix()))
    return finalized


def _canonical_external_name(adapter: FrameworkAdapter) -> str:
    key = adapter.name.lower()
    return _CANONICAL_EXTERNAL_NAMES.get(key, key)


def _unique_name(base: str, used: set[str]) -> str:
    if base not in used:
        return base
    index = 2
    while f"{base}-{index}" in used:
        index += 1
    return f"{base}-{index}"


def _is_under(path: Path, parent: Path) -> bool:
    try:
        return path.is_relative_to(parent)
    except AttributeError:  # pragma: no cover - Python < 3.9
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False


def _iter_candidate_directories(root: Path) -> list[Path]:
    """Return nested directories under ``root``, skipping ignored names."""

    candidates: list[Path] = []
    visited: set[str] = set()

    def _walk(directory: Path) -> None:
        try:
            resolved_key = str(directory.resolve())
        except OSError:
            return
        if resolved_key in visited:
            return
        visited.add(resolved_key)

        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name.lower())
        except OSError:
            return

        for child in children:
            if not child.is_dir():
                continue
            if child.name in IGNORED_DIRECTORY_NAMES:
                continue

            candidates.append(child)
            _walk(child)

    _walk(root)
    return candidates
