"""External dependency diagnostics for ``stackpilot doctor``."""



from __future__ import annotations



from pathlib import Path

from typing import List, Optional



from ..config import (

    DEFAULT_HEALTH_PROBE_TIMEOUT_S,

    ExternalDependency,

    ServiceSpec,

    TcpHealthCheck,

)

from ..tcp_checker import TcpProbeResult, diagnose_tcp

from .health_check import validate_health_check

from .models import CheckStatus, DiagnosticContext, DoctorCheck



_KIND_LABELS = {

    "reachable": "reachable",

    "timeout": "timeout",

    "refused": "unreachable (connection refused — check host/port)",

    "dns": "incorrect host (DNS lookup failed)",

    "unreachable": "unreachable",

}





def check_external_dependencies(ctx: DiagnosticContext) -> None:

    """

    Validate external dependency configuration and reachability.



    Configuration failures are FAIL. Unreachable infrastructure is FAIL with

    clear diagnostics (reachable / unreachable / timeout / incorrect host or

    port). External dependencies are never started by StackPilot.

    """



    if ctx.stack is None:

        return



    deps = list(ctx.stack.external_dependencies)

    if not deps:

        ctx.add(

            DoctorCheck(

                name="External dependencies",

                status=CheckStatus.OK,

                detail="No external dependencies configured",

            )

        )

        return



    config_problems: List[str] = []

    for dep in deps:

        problem = _validate_external_config(dep)

        if problem is not None:

            config_problems.append(f"{dep.name}: {problem}")



    if config_problems:

        ctx.add(

            DoctorCheck(

                name="External dependency configuration",

                status=CheckStatus.FAIL,

                detail="; ".join(config_problems),

                fix="Fix stack.external_dependency(...) name/type/host/port/health_check.",

            )

        )

        return



    ctx.add(

        DoctorCheck(

            name="External dependency configuration",

            status=CheckStatus.OK,

            detail=f"{len(deps)} external dependency(ies) look valid",

        )

    )



    failures: List[str] = []

    for dep in deps:

        result = diagnose_external_dependency(dep)

        if result.ok:

            continue

        label = _KIND_LABELS.get(result.kind, result.kind)

        failures.append(

            f"{dep.display_name} ({dep.host}:{dep.port}): {label} — {result.detail}"

        )



    if failures:

        ctx.add(

            DoctorCheck(

                name="External dependencies reachable",

                status=CheckStatus.FAIL,

                detail="; ".join(failures),

                fix=(

                    "Start the infrastructure outside StackPilot (or fix host/port), "

                    "then re-run doctor. StackPilot never starts external dependencies."

                ),

            )

        )

        return



    ctx.add(

        DoctorCheck(

            name="External dependencies reachable",

            status=CheckStatus.OK,

            detail=f"All {len(deps)} external dependency(ies) reachable",

        )

    )





def diagnose_external_dependency(dep: ExternalDependency) -> TcpProbeResult:

    """

    Probe one external dependency and return a classified TCP result.



    Prefer TCP connectivity via the existing checker. Non-TCP health checks

    fall back to a TCP probe on the declared host/port so doctor still

    produces host/port diagnostics.

    """



    check = dep.health_check

    host = dep.host

    port = int(dep.port)

    timeout = float(DEFAULT_HEALTH_PROBE_TIMEOUT_S)

    if isinstance(check, TcpHealthCheck):

        host = check.host or host

        port = int(check.port)

        timeout = float(check.probe_timeout)

    return diagnose_tcp(host, port, connect_timeout=timeout)





def _validate_external_config(dep: ExternalDependency) -> Optional[str]:

    if not str(dep.name or "").strip():

        return "name is empty"

    if not str(dep.type or "").strip():

        return "type is empty"

    if not str(dep.host or "").strip():

        return "host is empty"

    port = int(dep.port)

    if port < 1 or port > 65535:

        return f"port must be in 1..65535 (got {port})"



    health = dep.health_check

    if health is None:

        return None



    shim = ServiceSpec(

        name=dep.name,

        path=Path("."),

        command="true",

        health_check=health,

    )

    return validate_health_check(shim)


