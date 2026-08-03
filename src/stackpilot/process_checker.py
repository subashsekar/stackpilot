"""Process liveness probe using ``Popen.poll()``."""

from __future__ import annotations

from subprocess import Popen
from typing import Optional


def check_process(process: Optional[Popen[str]]) -> bool:
    """
    Return True when the service subprocess is still alive.

    ``poll()`` returning ``None`` means the process has not exited.
    """

    if process is None:
        return False
    return process.poll() is None
