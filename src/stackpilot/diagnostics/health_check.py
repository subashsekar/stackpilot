"""Health-check configuration diagnostics."""

from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlparse

from ..config import (
    HttpHealthCheck,
    ProcessHealthCheck,
    ServiceSpec,
    TcpHealthCheck,
    parse_health_check,
)
from .models import CheckStatus, DiagnosticContext, DoctorCheck


def check_health_configuration(ctx: DiagnosticContext) -> None:
    """Validate health_check models already attached to each service."""

    if ctx.stack is None:
        return

    specs = list(ctx.stack.services)
    problems: List[str] = []
    configured = 0

    for spec in specs:
        if spec.health_check is None:
            continue
        configured += 1
        problem = validate_health_check(spec)
        if problem is not None:
            problems.append(f"{spec.name}: {problem}")

    if problems:
        ctx.add(
            DoctorCheck(
                name="Health check configuration",
                status=CheckStatus.FAIL,
                detail="; ".join(problems),
                fix="Fix health_check= values (type, url/host/port, timeouts).",
            )
        )
        return

    if configured == 0:
        ctx.add(
            DoctorCheck(
                name="Health check configuration",
                status=CheckStatus.OK,
                detail="No health checks configured (optional)",
            )
        )
        return

    ctx.add(
        DoctorCheck(
            name="Health check configuration",
            status=CheckStatus.OK,
            detail=f"{configured} health check(s) look valid",
        )
    )


def validate_health_check(spec: ServiceSpec) -> Optional[str]:
    """
    Return an error message when ``spec.health_check`` is invalid, else None.

    Reuses ``parse_health_check`` for mapping round-trips and applies shared
    numeric / URL / port rules so runtime and doctor stay aligned.
    """

    health = spec.health_check
    if health is None:
        return None

    try:
        parsed = parse_health_check(health)
    except (TypeError, ValueError) as exc:
        return str(exc)

    timing_error = _validate_timing(
        interval=parsed.interval,
        timeout=parsed.timeout,
        probe_timeout=parsed.probe_timeout,
    )
    if timing_error is not None:
        return timing_error

    if isinstance(parsed, ProcessHealthCheck):
        return None

    if isinstance(parsed, HttpHealthCheck):
        return _validate_http(parsed)

    if isinstance(parsed, TcpHealthCheck):
        return _validate_tcp(parsed)

    return f"unsupported health check type: {type(parsed).__name__}"


def _validate_timing(
    *,
    interval: float,
    timeout: float,
    probe_timeout: float,
) -> Optional[str]:
    if interval <= 0:
        return f"interval must be > 0 (got {interval})"
    if timeout <= 0:
        return f"timeout must be > 0 (got {timeout})"
    if probe_timeout <= 0:
        return f"probe_timeout must be > 0 (got {probe_timeout})"
    if interval > timeout:
        return f"interval ({interval}) should not exceed timeout ({timeout})"
    return None


def _validate_http(check: HttpHealthCheck) -> Optional[str]:
    url = check.url.strip()
    if not url:
        return "http health_check requires a non-empty 'url'"
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return f"invalid url: {exc}"
    if parsed.scheme not in {"http", "https"}:
        return f"url must use http or https scheme (got {parsed.scheme!r})"
    if not parsed.netloc:
        return "url must include a host"
    return None


def _validate_tcp(check: TcpHealthCheck) -> Optional[str]:
    host = check.host.strip()
    if not host:
        return "tcp health_check requires a non-empty 'host'"
    port = int(check.port)
    if port < 1 or port > 65535:
        return f"tcp port must be in 1..65535 (got {port})"
    return None
