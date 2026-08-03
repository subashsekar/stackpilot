"""Package manager detection for Python and Node projects."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from ...executable import cli_is_runnable
from ..base import read_text

PythonPackageManager = Literal["uv", "poetry", "pipenv", "pip"]
NodePackageManager = Literal["bun", "pnpm", "yarn", "npm"]

__all__ = [
    "NodePackageManager",
    "PythonPackageManager",
    "cli_is_runnable",
    "detect_node_package_manager",
    "detect_python_package_manager",
    "node_run_command",
    "python_run_prefix",
]


def detect_python_package_manager(directory: Path) -> PythonPackageManager:
    """
    Prefer the package manager already used by the project.

    Order: uv lock → Poetry lock/config → Pipenv → pip markers → pip.
    """

    root = directory.expanduser()
    if (root / "uv.lock").is_file():
        return "uv"
    if (root / "poetry.lock").is_file():
        return "poetry"

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = read_text(pyproject).lower()
        if "[tool.uv" in text or "tool.uv]" in text:
            return "uv"
        if "[tool.poetry" in text:
            return "poetry"

    if (root / "Pipfile").is_file() or (root / "Pipfile.lock").is_file():
        return "pipenv"

    if (root / "requirements.txt").is_file() or (root / "requirements").is_dir():
        return "pip"

    if pyproject.is_file():
        return "pip"

    return "pip"


def detect_node_package_manager(directory: Path) -> NodePackageManager:
    """
    Prefer the Node package manager already used by the project.

    Lockfile order: bun → pnpm → yarn → npm → npm fallback.
    """

    root = directory.expanduser()
    if (root / "bun.lockb").is_file() or (root / "bun.lock").is_file():
        return "bun"
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "package-lock.json").is_file() or (root / "npm-shrinkwrap.json").is_file():
        return "npm"
    return "npm"


def python_run_prefix(directory: Path, python: str = "python") -> str:
    """
    Return a command prefix that runs via the project package manager.

    Managed projects (uv / Poetry / Pipenv) use the tool's runner when that
    CLI is actually runnable so the correct environment is selected. If the
    manager binary is missing or blocked by OS policy, fall back to the
    resolved interpreter (venv path or ``python``). Plain ``pip`` projects
    always return the resolved interpreter.
    """

    manager = detect_python_package_manager(directory)
    if manager == "uv" and cli_is_runnable("uv"):
        return "uv run python"
    if manager == "poetry" and cli_is_runnable("poetry"):
        return "poetry run python"
    if manager == "pipenv" and cli_is_runnable("pipenv"):
        return "pipenv run python"
    return python


def node_run_command(directory: Path, script: str) -> str:
    """Build ``<pm> run <script>`` using the detected Node package manager."""

    manager = detect_node_package_manager(directory)
    if manager == "yarn":
        # yarn classic: `yarn run <script>` or `yarn <script>`
        return f"yarn {script}"
    if manager == "bun":
        return f"bun run {script}"
    if manager == "pnpm":
        return f"pnpm run {script}"
    # npm accepts the short form for the conventional start script.
    if script == "start":
        return "npm start"
    return f"npm run {script}"
