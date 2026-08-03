"""Python runtime diagnostics."""

from __future__ import annotations

import sys

from .. import __version__
from ..executable import resolve_executable
from .models import CheckStatus, DiagnosticContext, DoctorCheck


def check_python_version(ctx: DiagnosticContext) -> None:
    """Require Python 3.10+."""

    version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    if sys.version_info < (3, 10):
        ctx.add(
            DoctorCheck(
                name="Python version",
                status=CheckStatus.FAIL,
                detail=f"Python {version} is too old (need >= 3.10).",
                fix="Install Python 3.10 or newer.",
            )
        )
        return

    ctx.add(
        DoctorCheck(
            name="Python version",
            status=CheckStatus.OK,
            detail=f"Python {version}",
        )
    )


def check_package_import(ctx: DiagnosticContext) -> None:
    """Verify the installed ``stackpilot`` package imports cleanly."""

    try:
        import stackpilot  # noqa: F401

        ctx.add(
            DoctorCheck(
                name="Package import",
                status=CheckStatus.OK,
                detail=f"stackpilot {__version__} imports correctly",
            )
        )
    except Exception as exc:  # pragma: no cover - defensive
        ctx.add(
            DoctorCheck(
                name="Package import",
                status=CheckStatus.FAIL,
                detail=f"Failed to import stackpilot: {exc}",
                fix="Run: python -m pip install -e .",
            )
        )


def check_cli_available(ctx: DiagnosticContext) -> None:
    """Warn when the ``stackpilot`` console script is missing from PATH."""

    exe = resolve_executable("stackpilot")
    if exe:
        ctx.add(
            DoctorCheck(
                name="CLI executable available",
                status=CheckStatus.OK,
                detail=f"Found on PATH: {exe}",
            )
        )
        return

    ctx.add(
        DoctorCheck(
            name="CLI executable available",
            status=CheckStatus.WARN,
            detail="'stackpilot' is not on PATH.",
            fix=(
                "Use: python -m stackpilot <command>\n"
                "Or add your Python Scripts directory to PATH.\n"
                "Reinstall: python -m pip install -e ."
            ),
        )
    )
