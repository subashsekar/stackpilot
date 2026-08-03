"""Virtual environment detection."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_VENV_DIR_NAMES = (".venv", "venv", "virtualenv", "env")


def detect_venv_dir(directory: Path, *, walk_parents: bool = True) -> Path | None:
    """
    Locate a virtualenv for ``directory``.

    Prefers a local ``.venv`` / ``venv`` / ``virtualenv`` / ``env``. When
    ``walk_parents`` is true (default), also searches ancestors up to the
    nearest project root — matching monorepos where services live under
    ``services/<name>`` and the editable install / venv sits at the repo root.
    """

    root = directory.expanduser().resolve()
    for folder in _venv_search_roots(root, walk_parents=walk_parents):
        found = _venv_in(folder)
        if found is not None:
            return found
    return None


def resolve_python_executable(directory: Path) -> str:
    """
    Return the best Python executable token for generated commands.

    Prefers a service-local venv interpreter as a forward-slash relative
    path. When only a monorepo/parent venv exists, returns bare ``python``
    so launch-time resolution (which walks parents) can bind the absolute
    interpreter — avoiding fragile ``../`` paths that Windows CreateProcess
    cannot launch, and absolute ``C:\\...`` paths that break Stackfile.py
    string literals. Falls back to ``python`` when no venv is present.
    """

    root = directory.expanduser().resolve()
    venv = detect_venv_dir(root)
    if venv is None:
        return "python"

    exe = _venv_python_path(venv)
    if exe is None or not exe.is_file():
        return "python"

    resolved = exe.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        # Parent/monorepo venv — resolve at spawn via bare launcher.
        return "python"

    # Forward slashes keep Stackfile.py readable on all platforms.
    return relative.as_posix()


def _venv_search_roots(directory: Path, *, walk_parents: bool) -> list[Path]:
    roots = [directory]
    if not walk_parents:
        return roots
    for parent in directory.parents:
        roots.append(parent)
        if _is_project_root(parent):
            break
    return roots


def _venv_in(directory: Path) -> Path | None:
    for name in _VENV_DIR_NAMES:
        candidate = directory / name
        if candidate.is_dir() and _looks_like_venv(candidate):
            return candidate
    return None


def _is_project_root(directory: Path) -> bool:
    """Stop upward search after checking a monorepo / VCS / Stackfile root."""

    return (
        (directory / "Stackfile.py").is_file()
        or (directory / ".git").exists()
        or (directory / "uv.lock").is_file()
        or (directory / "poetry.lock").is_file()
        or (directory / "Pipfile").is_file()
    )


def _looks_like_venv(path: Path) -> bool:
    if (path / "pyvenv.cfg").is_file():
        return True
    if (path / "bin" / "python").is_file() or (path / "bin" / "python3").is_file():
        return True
    if (path / "Scripts" / "python.exe").is_file():
        return True
    return False


def _venv_python_path(venv: Path) -> Path | None:
    if os.name == "nt" or sys.platform.startswith("win"):
        scripts = venv / "Scripts" / "python.exe"
        if scripts.is_file():
            return scripts
    unix = venv / "bin" / "python"
    if unix.is_file():
        return unix
    unix3 = venv / "bin" / "python3"
    if unix3.is_file():
        return unix3
    # Cross-platform fallback when generating on the "other" OS layout.
    for candidate in (
        venv / "Scripts" / "python.exe",
        venv / "bin" / "python",
        venv / "bin" / "python3",
    ):
        if candidate.is_file():
            return candidate
    return None
