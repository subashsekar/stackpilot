"""Shared filesystem scanning with ignore rules."""

from __future__ import annotations

from pathlib import Path
from typing import Final, Iterator

# Reuse the same ignore set as the project scanner so adapter walks
# never descend into dependency / cache / VCS trees.
IGNORED_SCAN_NAMES: Final[frozenset[str]] = frozenset(
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
        ".tox",
        ".eggs",
        ".ruff_cache",
        "site-packages",
    }
)


def should_skip_directory(name: str) -> bool:
    """True when a directory name must not be scanned."""

    return name in IGNORED_SCAN_NAMES or name.startswith(".")


def iter_project_files(
    directory: Path,
    *,
    suffixes: tuple[str, ...] | None = None,
    max_depth: int = 4,
) -> Iterator[Path]:
    """
    Yield files under ``directory`` up to ``max_depth``.

    Skips ignored dependency / VCS / cache directories.
    """

    root = directory.expanduser()
    if not root.is_dir():
        return

    wanted = {s.lower() for s in suffixes} if suffixes else None

    def _walk(current: Path, depth: int) -> Iterator[Path]:
        if depth > max_depth:
            return
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return

        for child in children:
            try:
                is_dir = child.is_dir()
                is_file = child.is_file()
            except OSError:
                continue

            if is_dir:
                if should_skip_directory(child.name):
                    continue
                yield from _walk(child, depth + 1)
                continue

            if not is_file:
                continue
            if wanted is not None and child.suffix.lower() not in wanted:
                continue
            yield child

    yield from _walk(root, 0)
