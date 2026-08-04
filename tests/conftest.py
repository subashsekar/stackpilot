"""Shared pytest hooks for deterministic CI (cleanup + hang diagnostics)."""

from __future__ import annotations

import faulthandler
import os
import signal
import sys
import warnings
from typing import Iterator

import pytest

# Dump all thread stacks on hang / SIGTERM so CI logs show where pytest stuck.
faulthandler.enable(all_threads=True)
_register = getattr(faulthandler, "register", None)
if callable(_register) and hasattr(signal, "SIGTERM"):
    try:
        _register(signal.SIGTERM, all_threads=True, chain=True)
    except (RuntimeError, ValueError, OSError, AttributeError):
        # Registration can fail under some Windows / embedded runners.
        pass


@pytest.fixture(scope="session", autouse=True)
def _ci_session_guards() -> Iterator[None]:
    """Enable hang-friendly environment for the whole pytest session."""

    os.environ.setdefault("PYTHONFAULTHANDLER", "1")
    yield


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Surface a clear note when the suite was interrupted (e.g. SIGTERM → 143)."""

    del session  # unused; hook signature fixed by pytest
    if exitstatus in {pytest.ExitCode.INTERRUPTED, 2}:
        warnings.warn(
            "pytest session interrupted — check for hung subprocesses, "
            "Observer joins, or shutdown lock waits (CI exit 143 is often SIGTERM).",
            UserWarning,
            stacklevel=1,
        )
    if isinstance(exitstatus, int) and exitstatus < 0:
        sys.stderr.write(
            f"\n[stackpilot-ci] pytest exited with negative status {exitstatus}; "
            "process likely received a fatal signal.\n"
        )
