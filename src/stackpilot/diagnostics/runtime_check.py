"""Runtime integrity, orphan process, env-file, and permission diagnostics."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from ..config import ServiceSpec
from ..models import ServiceState
from ..runtime_control import read_runtime_payload
from ..status import RUNTIME_STATUS_FILE, pid_is_alive
from .models import CheckStatus, DiagnosticContext, DoctorCheck


def check_runtime_integrity(ctx: DiagnosticContext) -> None:
    """Validate ``.stackpilot/runtime.json`` when present."""

    root = _project_root(ctx)
    if root is None:
        return

    path = root / RUNTIME_STATUS_FILE
    if not path.exists():
        ctx.add(
            DoctorCheck(
                name="Runtime status integrity",
                status=CheckStatus.OK,
                detail="No runtime.json (no active or prior session)",
            )
        )
        return

    parsed = read_runtime_payload(root)
    if parsed.corrupted:
        ctx.add(
            DoctorCheck(
                name="Runtime status integrity",
                status=CheckStatus.FAIL,
                detail=f"Corrupted runtime status at {path}",
                fix="Run: stackpilot stop  (clears the corrupt file) then stackpilot run",
            )
        )
        return

    ctx.add(
        DoctorCheck(
            name="Runtime status integrity",
            status=CheckStatus.OK,
            detail="runtime.json is valid JSON",
        )
    )


def check_orphan_processes(ctx: DiagnosticContext) -> None:
    """Warn when a prior session left RUNNING PIDs after session ended."""

    root = _project_root(ctx)
    if root is None:
        return

    path = root / RUNTIME_STATUS_FILE
    if not path.exists():
        ctx.add(
            DoctorCheck(
                name="Orphan StackPilot processes",
                status=CheckStatus.OK,
                detail="No orphan processes detected.",
            )
        )
        return

    parsed = read_runtime_payload(root)
    if parsed.corrupted or parsed.missing:
        ctx.add(
            DoctorCheck(
                name="Orphan StackPilot processes",
                status=CheckStatus.OK,
                detail="No orphan processes detected.",
            )
        )
        return

    snapshot = parsed.snapshot or {}
    session_active = bool(snapshot.get("session_active"))
    live: List[str] = []

    # Orphans = RUNNING PIDs still alive while the session is marked inactive.
    if not session_active:
        for raw in parsed.raw_services:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip() or "service"
            pid = raw.get("pid")
            status = str(raw.get("status") or "").strip().lower()
            if (
                isinstance(pid, int)
                and status == ServiceState.RUNNING.value
                and pid_is_alive(pid)
            ):
                live.append(f"{name} (pid {pid})")

    live = list(dict.fromkeys(live))
    if live:
        ctx.add(
            DoctorCheck(
                name="Orphan StackPilot processes",
                status=CheckStatus.WARN,
                detail="Orphan process(es): " + ", ".join(live),
                fix="Run: stackpilot stop --force",
            )
        )
        return

    ctx.add(
        DoctorCheck(
            name="Orphan StackPilot processes",
            status=CheckStatus.OK,
            detail="No orphan processes detected.",
        )
    )


def check_env_files(ctx: DiagnosticContext) -> None:
    """Fail when a service ``env_file`` is missing or unreadable."""

    if ctx.stack is None:
        return

    specs = list(ctx.stack.services)
    configured = [s for s in specs if s.env_file]
    if not configured:
        ctx.add(
            DoctorCheck(
                name="Env files readable",
                status=CheckStatus.OK,
                detail="No env_file configured",
            )
        )
        return

    problems: List[str] = []
    for spec in configured:
        problem = _env_file_problem(spec)
        if problem is not None:
            problems.append(f"{spec.name}: {problem}")

    if problems:
        ctx.add(
            DoctorCheck(
                name="Env files readable",
                status=CheckStatus.FAIL,
                detail="; ".join(problems),
                fix="Create the env file or fix env_file= (path is relative to the service path=).",
            )
        )
        return

    ctx.add(
        DoctorCheck(
            name="Env files readable",
            status=CheckStatus.OK,
            detail=f"{len(configured)} env_file(s) readable",
        )
    )


def check_project_permissions(ctx: DiagnosticContext) -> None:
    """Warn when the project cannot write ``.stackpilot`` artifacts."""

    root = _project_root(ctx)
    if root is None:
        return

    stackpilot_dir = root / ".stackpilot"
    problems: List[str] = []

    if not os.access(root, os.R_OK):
        problems.append(f"project root not readable: {root}")
    if not os.access(root, os.W_OK):
        problems.append(f"project root not writable: {root}")

    if stackpilot_dir.exists():
        if not os.access(stackpilot_dir, os.W_OK):
            problems.append(f".stackpilot not writable: {stackpilot_dir}")
    else:
        probe = root / ".stackpilot_doctor_probe"
        try:
            probe.mkdir(exist_ok=True)
            probe.rmdir()
        except OSError as exc:
            problems.append(f"cannot create .stackpilot under {root}: {exc}")

    if ctx.stack is not None:
        for spec in ctx.stack.services:
            path = Path(spec.path)
            if path.is_dir() and not os.access(path, os.R_OK):
                problems.append(f"service path not readable: {spec.name} → {path}")

    if problems:
        ctx.add(
            DoctorCheck(
                name="Project artifact permissions",
                status=CheckStatus.WARN,
                detail="; ".join(problems),
                fix="Fix directory permissions so StackPilot can read the project and write .stackpilot/.",
            )
        )
        return

    ctx.add(
        DoctorCheck(
            name="Project artifact permissions",
            status=CheckStatus.OK,
            detail="Project and .stackpilot are writable",
        )
    )


def _project_root(ctx: DiagnosticContext) -> Optional[Path]:
    if ctx.project is not None:
        return Path(ctx.project.root)
    return None


def _env_file_problem(spec: ServiceSpec) -> Optional[str]:
    raw = (spec.env_file or "").strip()
    if not raw:
        return None
    base = Path(spec.path).expanduser().resolve()
    path = Path(raw)
    if not path.is_absolute():
        path = (base / path).resolve()
    if not path.exists():
        return f"missing env_file={raw}"
    if not path.is_file():
        return f"env_file={raw} is not a file"
    if not os.access(path, os.R_OK):
        return f"env_file={raw} is not readable"
    return None
