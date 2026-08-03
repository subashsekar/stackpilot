"""Service name, path, and command diagnostics."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import List, Optional, Sequence

from ..config import ExternalDependency, ServiceSpec
from ..executable import is_launchable
from ..launch_env import build_child_env, resolve_service_argv
from ..utils import _split_command_text
from .models import CheckStatus, DiagnosticContext, DoctorCheck


def check_services(ctx: DiagnosticContext) -> None:
    """Validate service names, directories, and command executables."""

    if ctx.stack is None:
        return

    specs = list(ctx.stack.services)
    if not specs:
        return

    check_unique_names(ctx, specs)
    check_service_paths(ctx, specs)
    check_service_commands(ctx, specs)


def check_unique_names(
    ctx: DiagnosticContext,
    specs: Optional[List[ServiceSpec]] = None,
) -> None:
    """Fail when two services share the same name (including external deps)."""

    if specs is None:
        if ctx.stack is None:
            return
        specs = list(ctx.stack.services)

    names = [spec.name for spec in specs]
    if ctx.stack is not None:
        names.extend(dep.name for dep in ctx.stack.external_dependencies)

    counts = Counter(names)
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        ctx.add(
            DoctorCheck(
                name="Service names unique",
                status=CheckStatus.FAIL,
                detail=f"Duplicate service name(s): {', '.join(duplicates)}",
                fix="Give each stack.service(...) / external_dependency(...) a unique name.",
            )
        )
        return

    ctx.add(
        DoctorCheck(
            name="Service names unique",
            status=CheckStatus.OK,
            detail=f"{len(names)} unique name(s)",
        )
    )


def check_service_paths(
    ctx: DiagnosticContext,
    specs: Optional[List[ServiceSpec]] = None,
) -> None:
    """Fail when a service ``path`` does not exist on disk."""

    if specs is None:
        if ctx.stack is None:
            return
        specs = list(ctx.stack.services)

    missing: List[str] = []
    for spec in specs:
        path = spec.path
        if not path.exists():
            missing.append(f"{spec.name} → {path}")
        elif not path.is_dir():
            missing.append(f"{spec.name} → {path} (not a directory)")

    if missing:
        ctx.add(
            DoctorCheck(
                name="Service paths exist",
                status=CheckStatus.FAIL,
                detail="Missing or invalid service path(s): " + "; ".join(missing),
                fix="Create the service directory or fix the path= argument.",
            )
        )
        return

    ctx.add(
        DoctorCheck(
            name="Service paths exist",
            status=CheckStatus.OK,
            detail="All service directories exist",
        )
    )


def check_service_commands(
    ctx: DiagnosticContext,
    specs: Optional[List[ServiceSpec]] = None,
) -> None:
    """
    Validate that each service command is parseable and its executable exists.

    Uses the same ``build_child_env`` + ``resolve_service_argv`` path as the
    Runner so Doctor never reports "command valid" for an argv the Runner
    cannot actually spawn (including Windows ``npm.cmd`` resolution).
    """

    if specs is None:
        if ctx.stack is None:
            return
        specs = list(ctx.stack.services)

    topology_services = specs
    external: Sequence[ExternalDependency] = (
        list(ctx.stack.external_dependencies) if ctx.stack is not None else ()
    )

    problems: List[str] = []
    for spec in specs:
        problem = _command_problem(
            spec,
            services=topology_services,
            external_dependencies=external,
        )
        if problem is not None:
            problems.append(f"{spec.name}: {problem}")

    if problems:
        ctx.add(
            DoctorCheck(
                name="Service commands valid",
                status=CheckStatus.FAIL,
                detail="; ".join(problems),
                fix="Fix command= so the first token is a runnable executable on PATH "
                "(or install/unblock that tool).",
            )
        )
        return

    ctx.add(
        DoctorCheck(
            name="Service commands valid",
            status=CheckStatus.OK,
            detail="All service commands resolve to an executable",
        )
    )


def _command_problem(
    spec: ServiceSpec,
    *,
    services: Sequence[ServiceSpec] = (),
    external_dependencies: Sequence[ExternalDependency] = (),
) -> Optional[str]:
    command = (spec.command or "").strip()
    if not command:
        return "command is empty"

    cwd = Path(spec.path).expanduser().resolve()
    env = build_child_env(
        cwd,
        services=services,
        external_dependencies=external_dependencies,
    )

    try:
        argv = resolve_service_argv(command, cwd=cwd, env=env)
    except ValueError as exc:
        return str(exc)

    if not argv:
        return "command is empty"

    resolved = argv[0]
    if not Path(resolved).is_file():
        original = _original_executable(command, fallback=resolved)
        return f"executable not found: {original}"

    if not is_launchable(resolved):
        original = _original_executable(command, fallback=resolved)
        return (
            f"executable blocked by OS policy: {original} "
            f"({resolved})"
        )
    return None


def _original_executable(command: str, *, fallback: str) -> str:
    try:
        tokens = _split_command_text(command)
    except ValueError:
        return fallback
    return tokens[0] if tokens else fallback
