"""ASGI / Flask entrypoint and module-path detection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..base import read_text
from .scan import iter_project_files

# Relative entry candidates checked before a broader walk.
_FASTAPI_CANDIDATES: tuple[str, ...] = (
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

_FLASK_CANDIDATES: tuple[str, ...] = (
    "app.py",
    "wsgi.py",
    "application.py",
    "main.py",
    "server.py",
    "app/__init__.py",
    "src/app/__init__.py",
)

_ASGI_APP_RE = re.compile(
    r"""^([A-Za-z_][\w]*)\s*=\s*(?:FastAPI|Starlette)\s*\(""",
    re.MULTILINE,
)

_FLASK_APP_RE = re.compile(
    r"""^([A-Za-z_][\w]*)\s*=\s*Flask\s*\(""",
    re.MULTILINE,
)

_CREATE_APP_RE = re.compile(
    r"""^def\s+(create_app)\s*\(""",
    re.MULTILINE,
)

_FASTAPI_SIGNALS = ("FastAPI(", "APIRouter(", "from fastapi", "import fastapi", "uvicorn")
_FLASK_SIGNALS = (
    "Flask(",
    "from flask import",
    "import flask",
    "def create_app(",
)


@dataclass(frozen=True, slots=True)
class AsgiEntrypoint:
    """Resolved ASGI module target such as ``app.main:app``."""

    file: Path
    module: str
    attr: str
    app_dir: str | None = None

    @property
    def target(self) -> str:
        return f"{self.module}:{self.attr}"


@dataclass(frozen=True, slots=True)
class FlaskEntrypoint:
    """Resolved Flask module target or application factory."""

    file: Path
    module: str
    attr: str
    is_factory: bool = False

    @property
    def target(self) -> str:
        if self.is_factory:
            return f"{self.module}:{self.attr}"
        return f"{self.module}:{self.attr}"


def find_python_files(directory: Path, *, max_depth: int = 3) -> list[Path]:
    """Return project ``*.py`` files, skipping ignored trees."""

    return list(iter_project_files(directory, suffixes=(".py",), max_depth=max_depth))


def module_path_for(directory: Path, file_path: Path) -> tuple[str, str | None]:
    """
    Convert ``file_path`` into an importable module path relative to ``directory``.

    When the file lives under a top-level ``src/`` layout and ``src`` itself
    is not a package, returns ``(module, "src")`` so callers can pass
    ``--app-dir src`` to uvicorn.
    """

    root = directory.expanduser().resolve()
    resolved = file_path.expanduser().resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        stem = resolved.stem
        return stem if stem != "__init__" else resolved.parent.name, None

    parts = list(relative.parts)
    app_dir: str | None = None

    if parts and parts[0] == "src":
        src_init = root / "src" / "__init__.py"
        if not src_init.is_file():
            app_dir = "src"
            parts = parts[1:]

    if not parts:
        return "app", app_dir

    if parts[-1].endswith(".py"):
        stem = Path(parts[-1]).stem
        if stem == "__init__":
            parts = parts[:-1]
        else:
            parts = parts[:-1] + [stem]

    if not parts:
        return "app", app_dir

    return ".".join(parts), app_dir


def detect_asgi_entrypoint(
    directory: Path,
    *,
    require_fastapi: bool = True,
) -> AsgiEntrypoint | None:
    """Locate a FastAPI/ASGI entry module and attribute name."""

    root = directory.expanduser()
    for relative in _FASTAPI_CANDIDATES:
        candidate = root / relative
        if not candidate.is_file():
            continue
        text = read_text(candidate)
        if require_fastapi and not _mentions_fastapi(text):
            continue
        attr = _detect_asgi_attr(text) or "app"
        module, app_dir = module_path_for(root, candidate)
        return AsgiEntrypoint(file=candidate, module=module, attr=attr, app_dir=app_dir)

    for path in find_python_files(root, max_depth=3):
        text = read_text(path)
        if require_fastapi and not _mentions_fastapi(text):
            continue
        if not _mentions_fastapi(text) and "FastAPI(" not in text:
            continue
        attr = _detect_asgi_attr(text)
        if attr is None and "APIRouter(" not in text and "FastAPI(" not in text:
            continue
        attr = attr or "app"
        module, app_dir = module_path_for(root, path)
        return AsgiEntrypoint(file=path, module=module, attr=attr, app_dir=app_dir)

    return None


def detect_flask_entrypoint(directory: Path) -> FlaskEntrypoint | None:
    """Locate a Flask app instance or ``create_app`` factory."""

    root = directory.expanduser()
    for relative in _FLASK_CANDIDATES:
        candidate = root / relative
        if not candidate.is_file():
            continue
        text = read_text(candidate)
        if not _mentions_flask(text):
            continue
        entry = _flask_from_text(root, candidate, text)
        if entry is not None:
            return entry

    for path in find_python_files(root, max_depth=3):
        text = read_text(path)
        if not _mentions_flask(text):
            continue
        entry = _flask_from_text(root, path, text)
        if entry is not None:
            return entry

    return None


def _flask_from_text(root: Path, path: Path, text: str) -> FlaskEntrypoint | None:
    factory = _CREATE_APP_RE.search(text)
    if factory:
        module, _ = module_path_for(root, path)
        return FlaskEntrypoint(
            file=path,
            module=module,
            attr=factory.group(1),
            is_factory=True,
        )

    match = _FLASK_APP_RE.search(text)
    if match:
        module, _ = module_path_for(root, path)
        return FlaskEntrypoint(
            file=path,
            module=module,
            attr=match.group(1),
            is_factory=False,
        )

    # Import-only signal with conventional ``app`` attribute.
    if "flask" in text.lower():
        module, _ = module_path_for(root, path)
        return FlaskEntrypoint(file=path, module=module, attr="app", is_factory=False)
    return None


def _detect_asgi_attr(text: str) -> str | None:
    match = _ASGI_APP_RE.search(text)
    if match:
        return match.group(1)
    # Common alternate name.
    if re.search(r"^application\s*=\s*FastAPI\s*\(", text, re.MULTILINE):
        return "application"
    return None


def _mentions_fastapi(text: str) -> bool:
    lower = text.lower()
    if "fastapi" in lower or "uvicorn" in lower:
        return True
    return any(signal in text for signal in ("FastAPI(", "APIRouter("))


def _mentions_flask(text: str) -> bool:
    lower = text.lower()
    if "flask" in lower:
        return True
    return any(signal in text for signal in _FLASK_SIGNALS)
