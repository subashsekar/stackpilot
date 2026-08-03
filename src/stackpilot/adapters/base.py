"""Framework adapter interface and shared helpers."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Mapping, Sequence

HealthKind = Literal["http", "tcp", "process", "none"]

COMPOSE_FILENAMES: Final[tuple[str, ...]] = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)


@dataclass(frozen=True, slots=True)
class AdapterServiceSpec:
    """
    Metadata an adapter produces for Stackfile generation.

    The generator consumes this shape only — it must not contain
    framework-specific branching.
    """

    framework: str
    command: str
    uses_port: bool = False
    health: HealthKind = "none"
    health_path: str = "/health"
    fixed_port: int | None = None
    # Explicit port from .env / compose / config when known.
    preferred_port: int | None = None
    # When True, generator emits ``stack.external_dependency(...)`` instead
    # of a startable ``stack.service(...)``.
    external: bool = False
    # Stackfile ``type=`` for external dependencies (e.g. postgresql, redis).
    external_type: str | None = None
    # Emit ``reload=True`` when StackPilot should own file watching.
    reload: bool = False
    # Soft dependency hints (e.g. Celery → redis) merged when targets exist.
    depends_on: tuple[str, ...] = ()


class FrameworkAdapter(ABC):
    """
    Pluggable detector + generator for one framework or runtime.

    Subclasses register themselves on the central adapter registry.
    Detection order is controlled by ``priority`` (lower runs first).
    """

    name: str = "Generic"
    # Lower values are matched before higher ones.
    priority: int = 100
    # When True, a match is infrastructure (Postgres/Redis), not an app
    # boundary — scanners keep walking nested directories.
    external: bool = False

    @abstractmethod
    def detect(self, path: Path) -> bool:
        """Return True when ``path`` is a project of this framework."""

    @abstractmethod
    def generate_service(
        self,
        path: Path,
        *,
        port: int | None = None,
    ) -> AdapterServiceSpec:
        """Build launch metadata for a detected project directory."""


def read_text(path: Path) -> str:
    """Read a UTF-8 text file, returning ``\"\"`` on failure."""

    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def source_mentions(path: Path, *needles: str) -> bool:
    """True when any needle appears in ``path`` (case-insensitive)."""

    if not path.is_file():
        return False
    text = read_text(path).lower()
    return any(needle.lower() in text for needle in needles)


def any_python_source_mentions(
    directory: Path,
    *needles: str,
    max_depth: int = 0,
) -> bool:
    """
    Scan ``*.py`` files under ``directory`` for needles.

    ``max_depth=0`` keeps the historical shallow (direct children only)
    behaviour. Higher depths walk nested packages while skipping ignored
    trees (``.venv``, ``node_modules``, …).
    """

    if max_depth <= 0:
        try:
            children = list(directory.iterdir())
        except OSError:
            return False

        for child in children:
            if child.is_file() and child.suffix == ".py":
                if source_mentions(child, *needles):
                    return True
        return False

    from .detect.scan import iter_project_files

    for path in iter_project_files(directory, suffixes=(".py",), max_depth=max_depth):
        if source_mentions(path, *needles):
            return True
    return False


def load_package_json(directory: Path) -> Mapping[str, object] | None:
    """Parse ``package.json`` when present; otherwise ``None``."""

    package_path = directory / "package.json"
    if not package_path.is_file():
        return None
    try:
        data = json.loads(read_text(package_path) or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def package_has_dependency(directory: Path, *names: str) -> bool:
    """True when any named package appears in dependencies or peerDeps."""

    data = load_package_json(directory)
    if data is None:
        return False

    buckets: Sequence[object] = (
        data.get("dependencies"),
        data.get("devDependencies"),
        data.get("peerDependencies"),
        data.get("optionalDependencies"),
    )
    wanted = {name.lower() for name in names}
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        for key in bucket:
            if str(key).lower() in wanted:
                return True
    return False


def compose_text(directory: Path) -> str:
    """Concatenate compose file contents found directly under ``directory``."""

    chunks: list[str] = []
    for name in COMPOSE_FILENAMES:
        path = directory / name
        if path.is_file():
            chunks.append(read_text(path))
    return "\n".join(chunks)


def compose_mentions(directory: Path, *needles: str) -> bool:
    """True when a local compose file mentions any needle."""

    text = compose_text(directory).lower()
    if not text:
        return False
    return any(needle.lower() in text for needle in needles)


def find_settings_py(directory: Path) -> Path | None:
    """Locate Django ``settings.py`` at root or up to two package levels deep."""

    direct = directory / "settings.py"
    if direct.is_file():
        return direct

    from .detect.scan import should_skip_directory

    try:
        children = list(directory.iterdir())
    except OSError:
        return None
    for child in children:
        if not child.is_dir() or should_skip_directory(child.name):
            continue
        candidate = child / "settings.py"
        if candidate.is_file():
            return candidate
        # Common layout: project/config/settings.py
        try:
            grandchildren = list(child.iterdir())
        except OSError:
            continue
        for grandchild in grandchildren:
            if not grandchild.is_dir() or should_skip_directory(grandchild.name):
                continue
            nested = grandchild / "settings.py"
            if nested.is_file():
                return nested
    return None


def find_django_wsgi_or_asgi(directory: Path) -> Path | None:
    """Locate Django ``wsgi.py`` or ``asgi.py`` near the project root."""

    from .detect.scan import should_skip_directory

    for name in ("wsgi.py", "asgi.py"):
        direct = directory / name
        if direct.is_file():
            return direct

    try:
        children = list(directory.iterdir())
    except OSError:
        return None
    for child in children:
        if not child.is_dir() or should_skip_directory(child.name):
            continue
        for name in ("wsgi.py", "asgi.py"):
            candidate = child / name
            if candidate.is_file():
                return candidate
    return None


_CELERY_APP_RE = re.compile(
    r"""(?:app\s*=\s*)?Celery\(\s*['\"]([^'\"]+)['\"]""",
    re.MULTILINE,
)

_CELERY_BROKER_RE = re.compile(
    r"""(?:broker(?:_url)?|CELERY_BROKER_URL)\s*=\s*['\"]([^'\"]+)['\"]""",
    re.IGNORECASE,
)
_CELERY_BROKER_KW_RE = re.compile(
    r"""Celery\s*\([^)]*\bbroker\s*=\s*['\"]([^'\"]+)['\"]""",
    re.IGNORECASE | re.DOTALL,
)


def detect_celery_app_name(directory: Path) -> str:
    """
    Infer the Celery ``-A`` module target.

    Prefers an importable module path for ``celery.py`` (``package`` or
    ``celery``), then an explicit ``Celery('name')`` argument when that is
    already a dotted module path, then the directory name.
    """

    root = directory.expanduser()
    candidates = _celery_source_candidates(root)

    for path in candidates:
        if not path.is_file():
            continue
        if path.name == "celery.py":
            module = _celery_module_path(root, path)
            if module:
                return module

    for path in candidates:
        if not path.is_file():
            continue
        text = read_text(path)
        match = _CELERY_APP_RE.search(text)
        if match:
            name = match.group(1).strip()
            # Dotted paths are valid ``-A`` targets; bare labels are not.
            if "." in name or name.endswith(".celery"):
                return name

    if any_python_source_mentions(root, "Celery(", max_depth=2):
        return root.name
    return root.name


def detect_celery_broker(directory: Path) -> str | None:
    """
    Return an explicit Celery broker URL when declared in source / ``.env``.

    Returns ``None`` when nothing is declared (do not invent a broker).
    """

    root = directory.expanduser()
    for name in (".env", ".env.local", ".env.development", ".env.dev"):
        path = root / name
        if not path.is_file():
            continue
        for match in _ENV_PORT_RE_BROKER.finditer(read_text(path)):
            key = match.group(1).upper()
            if key in {"CELERY_BROKER_URL", "BROKER_URL", "CELERY_BROKER"}:
                value = match.group(2).strip().strip("'\"")
                if value:
                    return value

    for path in _celery_source_candidates(root):
        if not path.is_file():
            continue
        text = read_text(path)
        match = _CELERY_BROKER_KW_RE.search(text)
        if match:
            return match.group(1).strip()
        match = _CELERY_BROKER_RE.search(text)
        if match:
            return match.group(1).strip()
    return None


def celery_broker_depends_on(broker_url: str | None) -> tuple[str, ...]:
    """Map a broker URL onto external dependency name hints."""

    if not broker_url:
        return ()
    lower = broker_url.lower()
    if lower.startswith("redis:") or "redis://" in lower or "rediss://" in lower:
        return ("redis",)
    if (
        lower.startswith("amqp:")
        or "rabbitmq" in lower
        or lower.startswith("pyamqp:")
    ):
        return ("rabbitmq",)
    return ()


_ENV_PORT_RE_BROKER = re.compile(
    r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$",
    re.MULTILINE,
)


def _celery_source_candidates(directory: Path) -> list[Path]:
    candidates = [
        directory / "celery.py",
        directory / "app.py",
        directory / "tasks.py",
        directory / "worker.py",
        directory / directory.name / "celery.py",
    ]
    from .detect.scan import iter_project_files

    for path in iter_project_files(directory, suffixes=(".py",), max_depth=2):
        if path.name in {"celery.py", "tasks.py", "worker.py", "app.py"}:
            if path not in candidates:
                candidates.append(path)
    return candidates


def _celery_module_path(directory: Path, celery_file: Path) -> str | None:
    """
    Build the ``celery -A`` module for a ``celery.py`` file.

    Root ``celery.py`` → ``celery``. Package ``pkg/celery.py`` → ``pkg``.
    """

    root = directory.expanduser().resolve()
    try:
        relative = celery_file.expanduser().resolve().relative_to(root)
    except ValueError:
        return None
    parts = list(relative.parts)
    if not parts or parts[-1] != "celery.py":
        return None
    parents = parts[:-1]
    if not parents:
        return "celery"
    return ".".join(parents)


def fastapi_module_name(directory: Path) -> str:
    """
    Prefer ``main`` over ``app`` when both FastAPI entrypoints exist.

    Prefer :func:`detect.entrypoint.detect_asgi_entrypoint` for full
    layout support; this helper remains for simple root-level modules.
    """

    from .detect.entrypoint import detect_asgi_entrypoint

    entry = detect_asgi_entrypoint(directory)
    if entry is not None:
        return entry.module

    if (directory / "main.py").is_file() and source_mentions(
        directory / "main.py", "fastapi"
    ):
        return "main"
    if (directory / "app.py").is_file():
        return "app"
    if (directory / "main.py").is_file():
        return "main"
    return "app"
