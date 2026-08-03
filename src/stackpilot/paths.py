"""Validate that runtime paths stay inside the project root."""

from __future__ import annotations

from pathlib import Path


class PathEscapeError(ValueError):
    """Raised when a configured path resolves outside the project root."""


def ensure_within_project(path: Path, project_root: Path, *, label: str = "path") -> Path:
    """
    Resolve ``path`` and require it to be ``project_root`` or a descendant.

    Returns the resolved path. Raises ``PathEscapeError`` on escape attempts.
    """

    root = Path(project_root).expanduser().resolve()
    resolved = Path(path).expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathEscapeError(
            f"{label} escapes project root: {resolved} (root={root})"
        ) from exc
    return resolved
