"""Stackfile / project discovery diagnostics."""

from __future__ import annotations

from ..discovery import STACKFILE_NAME, ProjectContext, find_stackfile
from ..utils import load_stack_from_stackfile
from .models import CheckStatus, DiagnosticContext, DoctorCheck


def check_stackfile_exists(ctx: DiagnosticContext) -> bool:
    """Locate ``Stackfile.py`` from ``ctx.origin`` (walks parents like Git)."""

    stackfile = find_stackfile(ctx.origin)
    if stackfile is None:
        ctx.add(
            DoctorCheck(
                name="Stackfile.py exists",
                status=CheckStatus.FAIL,
                detail=f"No {STACKFILE_NAME} under {ctx.origin} or parents.",
                fix="Run: stackpilot init",
            )
        )
        ctx.project = None
        return False

    ctx.project = ProjectContext(root=stackfile.parent, stackfile=stackfile)
    ctx.add(
        DoctorCheck(
            name="Stackfile.py exists",
            status=CheckStatus.OK,
            detail=str(stackfile),
        )
    )
    return True


def check_inside_project(ctx: DiagnosticContext) -> None:
    """Note whether the starting directory is inside the discovered project."""

    if ctx.project is None:
        ctx.add(
            DoctorCheck(
                name="Inside StackPilot project",
                status=CheckStatus.FAIL,
                detail="Not inside a StackPilot project.",
                fix="Run: stackpilot init",
            )
        )
        return

    origin = ctx.origin
    try:
        origin.relative_to(ctx.project.root)
        inside = True
    except ValueError:
        inside = False

    if inside:
        ctx.add(
            DoctorCheck(
                name="Inside StackPilot project",
                status=CheckStatus.OK,
                detail=f"Project root: {ctx.project.root}",
            )
        )
        return

    ctx.add(
        DoctorCheck(
            name="Inside StackPilot project",
            status=CheckStatus.WARN,
            detail=f"Nearest project is {ctx.project.root}",
        )
    )


def check_stackfile_loads(ctx: DiagnosticContext) -> bool:
    """
    Import ``Stackfile.py`` and confirm a module-level ``Stack`` exists.

    Emits separate checks for import success and Stack object creation.
    """

    if ctx.project is None:
        ctx.add(
            DoctorCheck(
                name="Stackfile imports successfully",
                status=CheckStatus.FAIL,
                detail=f"Cannot import {STACKFILE_NAME} — file not found.",
                fix="Run: stackpilot init",
            )
        )
        ctx.add(
            DoctorCheck(
                name="Stack object created",
                status=CheckStatus.FAIL,
                detail="Skipped — no Stackfile.py.",
                fix="Run: stackpilot init",
            )
        )
        ctx.add(
            DoctorCheck(
                name="Services discovered",
                status=CheckStatus.FAIL,
                detail="Cannot discover services without Stackfile.py.",
                fix="Run: stackpilot init",
            )
        )
        return False

    stackfile = ctx.project.stackfile
    try:
        stack = load_stack_from_stackfile(stackfile)
    except AttributeError as exc:
        ctx.add(
            DoctorCheck(
                name="Stackfile imports successfully",
                status=CheckStatus.OK,
                detail=f"{stackfile.name} executed without import errors",
            )
        )
        ctx.add(
            DoctorCheck(
                name="Stack object created",
                status=CheckStatus.FAIL,
                detail=str(exc),
                fix=f"Define `stack = Stack()` in {STACKFILE_NAME}.",
            )
        )
        _services_unavailable(ctx, reason="no Stack object")
        return False
    except TypeError as exc:
        ctx.add(
            DoctorCheck(
                name="Stackfile imports successfully",
                status=CheckStatus.OK,
                detail=f"{stackfile.name} executed without import errors",
            )
        )
        ctx.add(
            DoctorCheck(
                name="Stack object created",
                status=CheckStatus.FAIL,
                detail=str(exc),
                fix=f"`stack` must be a stackpilot.Stack instance in {STACKFILE_NAME}.",
            )
        )
        _services_unavailable(ctx, reason="invalid Stack object")
        return False
    except Exception as exc:
        ctx.add(
            DoctorCheck(
                name="Stackfile imports successfully",
                status=CheckStatus.FAIL,
                detail=f"Failed to load {STACKFILE_NAME}: {exc}",
                fix=f"Fix {STACKFILE_NAME}, then re-run: stackpilot doctor",
            )
        )
        ctx.add(
            DoctorCheck(
                name="Stack object created",
                status=CheckStatus.FAIL,
                detail="Skipped — Stackfile import failed.",
                fix=f"Fix {STACKFILE_NAME}, then re-run: stackpilot doctor",
            )
        )
        _services_unavailable(ctx, reason="Stackfile import failed")
        return False

    ctx.stack = stack
    ctx.add(
        DoctorCheck(
            name="Stackfile imports successfully",
            status=CheckStatus.OK,
            detail=f"Loaded {stackfile}",
        )
    )
    ctx.add(
        DoctorCheck(
            name="Stack object created",
            status=CheckStatus.OK,
            detail="Module-level `stack` is a Stack instance",
        )
    )

    names = [spec.name for spec in stack.services]
    if not names:
        ctx.add(
            DoctorCheck(
                name="Services discovered",
                status=CheckStatus.WARN,
                detail="Stackfile.py has no services yet.",
                fix="Add stack.service(...) entries, or run: stackpilot sync",
            )
        )
    else:
        ctx.add(
            DoctorCheck(
                name="Services discovered",
                status=CheckStatus.OK,
                detail=f"{len(names)} service(s): {', '.join(names)}",
            )
        )
    return True


def _services_unavailable(ctx: DiagnosticContext, *, reason: str) -> None:
    ctx.add(
        DoctorCheck(
            name="Services discovered",
            status=CheckStatus.FAIL,
            detail=f"Cannot discover services ({reason}).",
            fix=f"Fix {STACKFILE_NAME}, then re-run: stackpilot doctor",
        )
    )
