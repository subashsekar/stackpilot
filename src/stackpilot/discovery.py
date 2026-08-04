from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


STACKFILE_NAME = "Stackfile.py"

MISSING_STACKFILE_MESSAGE = """Problem: No Stackfile.py found.
Reason: StackPilot needs a Stackfile.py in this directory (or a parent) to know which services to run.
Suggested fix:
  stackpilot init
  stackpilot sync
  stackpilot run"""


class StackfileNotFoundError(FileNotFoundError):
    """Raised when no Stackfile.py exists in the current or parent directories."""

    def __init__(self, start: Path) -> None:
        self.start = start
        super().__init__(MISSING_STACKFILE_MESSAGE)


@dataclass(frozen=True, slots=True)
class ProjectContext:
    """Resolved StackPilot project rooted at the directory that owns Stackfile.py."""

    root: Path
    stackfile: Path


def find_stackfile(start: Optional[Path] = None) -> Optional[Path]:
    """
    Walk ``start`` and its parents looking for ``Stackfile.py``.

    Mirrors how Git discovers ``.git`` from any subdirectory.
    """

    current = (start or Path.cwd()).expanduser().resolve()
    for directory in (current, *current.parents):
        candidate = directory / STACKFILE_NAME
        if candidate.is_file():
            return candidate
    return None


def discover_project(start: Optional[Path] = None) -> ProjectContext:
    """
    Locate the nearest StackPilot project.

    Raises ``StackfileNotFoundError`` with a friendly message when none exists.
    """

    origin = (start or Path.cwd()).expanduser().resolve()
    stackfile = find_stackfile(origin)
    if stackfile is None:
        raise StackfileNotFoundError(origin)
    return ProjectContext(root=stackfile.parent, stackfile=stackfile)
