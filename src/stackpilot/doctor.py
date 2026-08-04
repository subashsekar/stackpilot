"""``stackpilot doctor`` — orchestrate modular diagnostics."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .diagnostics.dependency_check import check_dependencies
from .diagnostics.external_check import check_external_dependencies
from .diagnostics.health_check import check_health_configuration
from .diagnostics.models import CheckStatus, DiagnosticContext, DoctorCheck, DoctorReport
from .diagnostics.ports import check_ports
from .diagnostics.project import (
    check_inside_project,
    check_stackfile_exists,
    check_stackfile_loads,
)
from .diagnostics.python_check import (
    check_cli_available,
    check_package_import,
    check_python_version,
)
from .diagnostics.runtime_check import (
    check_env_files,
    check_orphan_processes,
    check_project_permissions,
    check_runtime_integrity,
)
from .diagnostics.service_check import check_services
from .diagnostics.summary import format_doctor_report

__all__ = [
    "CheckStatus",
    "DoctorCheck",
    "DoctorReport",
    "run_doctor",
    "format_doctor_report",
]


def run_doctor(*, start: Optional[Path] = None) -> DoctorReport:
    """
    Collect environment and project health checks for ``stackpilot doctor``.

    Individual checks live under ``stackpilot.diagnostics``; this function only
    sequences them and returns a ``DoctorReport``.
    """

    origin = (start or Path.cwd()).expanduser().resolve()
    ctx = DiagnosticContext(origin=origin)

    check_python_version(ctx)
    check_package_import(ctx)
    check_cli_available(ctx)

    if check_stackfile_exists(ctx):
        check_inside_project(ctx)
        if check_stackfile_loads(ctx):
            check_services(ctx)
            check_ports(ctx)
            check_dependencies(ctx)
            check_health_configuration(ctx)
            check_external_dependencies(ctx)
            check_env_files(ctx)
            check_runtime_integrity(ctx)
            check_orphan_processes(ctx)
            check_project_permissions(ctx)
    else:
        check_inside_project(ctx)
        check_stackfile_loads(ctx)

    return DoctorReport(checks=tuple(ctx.checks))
