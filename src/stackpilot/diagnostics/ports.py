"""Port conflict diagnostics from health-check configuration."""

from __future__ import annotations

import socket
from collections import defaultdict
from typing import DefaultDict, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from ..config import HttpHealthCheck, ServiceSpec, TcpHealthCheck
from .models import CheckStatus, DiagnosticContext, DoctorCheck

PortBinding = Tuple[str, int]  # (host, port)


def check_ports(ctx: DiagnosticContext) -> None:
    """Detect duplicate configured ports and ports already bound on the host."""

    if ctx.stack is None:
        return

    specs = list(ctx.stack.services)
    bindings = collect_port_bindings(specs)
    check_duplicate_ports(ctx, bindings)
    check_ports_in_use(ctx, bindings)


def collect_port_bindings(
    specs: Sequence[ServiceSpec],
) -> Dict[str, List[PortBinding]]:
    """
    Map service name → explicit TCP/HTTP ports from health checks.

    Only ports that appear explicitly in config are included (default HTTP
    80/443 without an explicit port are ignored to reduce noise).
    """

    result: Dict[str, List[PortBinding]] = {}
    for spec in specs:
        ports = _ports_for_spec(spec)
        if ports:
            result[spec.name] = ports
    return result


def check_duplicate_ports(
    ctx: DiagnosticContext,
    bindings: Optional[Dict[str, List[PortBinding]]] = None,
) -> None:
    """Fail when two services configure the same host:port (or same port on wildcards)."""

    if bindings is None:
        if ctx.stack is None:
            return
        bindings = collect_port_bindings(ctx.stack.services)

    if not bindings:
        ctx.add(
            DoctorCheck(
                name="Duplicate ports",
                status=CheckStatus.OK,
                detail="No explicit ports configured in health checks",
            )
        )
        return

    by_port: DefaultDict[int, List[Tuple[str, str]]] = defaultdict(list)
    for service, ports in bindings.items():
        for host, port in ports:
            by_port[port].append((service, host))

    conflicts: List[str] = []
    for port, owners in sorted(by_port.items()):
        if len(owners) < 2:
            continue
        # Same numeric port used by multiple services is a conflict for local
        # development (typical StackPilot use case binds on localhost).
        labels = ", ".join(f"{svc} ({host}:{port})" for svc, host in owners)
        conflicts.append(labels)

    if conflicts:
        ctx.add(
            DoctorCheck(
                name="Duplicate ports",
                status=CheckStatus.FAIL,
                detail="Port conflict(s): " + " | ".join(conflicts),
                fix="Give each service a unique TCP/HTTP port in health_check.",
            )
        )
        return

    ctx.add(
        DoctorCheck(
            name="Duplicate ports",
            status=CheckStatus.OK,
            detail="No duplicate ports across services",
        )
    )


def check_ports_in_use(
    ctx: DiagnosticContext,
    bindings: Optional[Dict[str, List[PortBinding]]] = None,
) -> None:
    """Warn when a configured port cannot be bound (already occupied)."""

    if bindings is None:
        if ctx.stack is None:
            return
        bindings = collect_port_bindings(ctx.stack.services)

    if not bindings:
        ctx.add(
            DoctorCheck(
                name="Ports available",
                status=CheckStatus.OK,
                detail="No explicit ports to probe",
            )
        )
        return

    occupied: List[str] = []
    seen: set[PortBinding] = set()
    for service, ports in bindings.items():
        for host, port in ports:
            key = (_normalize_host(host), port)
            if key in seen:
                continue
            seen.add(key)
            if is_port_in_use(port, host=host):
                owner_detail = _owner_detail(port)
                label = f"{service} → {host}:{port}"
                if owner_detail:
                    label = f"{label} ({owner_detail})"
                occupied.append(label)

    if occupied:
        ctx.add(
            DoctorCheck(
                name="Ports available",
                status=CheckStatus.WARN,
                detail="Port(s) already in use: " + "; ".join(occupied),
                fix="Run stackpilot stop, or change the service port.",
            )
        )
        return

    ctx.add(
        DoctorCheck(
            name="Ports available",
            status=CheckStatus.OK,
            detail="Configured ports appear free",
        )
    )


def _owner_detail(port: int) -> str:
    """Best-effort ``PID N (exe)`` summary for doctor WARN lines."""

    try:
        from ..port_detect import describe_port_owners

        owners = describe_port_owners(int(port))
    except Exception:
        return ""
    if not owners:
        return ""
    parts: List[str] = []
    for pid, label in owners[:3]:
        if label and label != "unknown":
            parts.append(f"PID {pid} {label}")
        else:
            parts.append(f"PID {pid}")
    return ", ".join(parts)


def is_port_in_use(port: int, *, host: str = "0.0.0.0") -> bool:
    """
    Return True when ``host:port`` cannot be bound.

    Cross-platform: uses a short-lived TCP bind probe (no listen backlog).
    Deliberately avoids ``SO_REUSEADDR`` so an occupied port is detected on
    Windows as well as Unix.
    """

    family, bind_host = _socket_bind_target(host)
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((bind_host, int(port)))
            return False
        except OSError:
            return True


def _ports_for_spec(spec: ServiceSpec) -> List[PortBinding]:
    health = spec.health_check
    if health is None:
        return []

    if isinstance(health, TcpHealthCheck):
        host = health.host.strip() or "127.0.0.1"
        return [(host, int(health.port))]

    if isinstance(health, HttpHealthCheck):
        binding = _http_port_binding(health.url)
        return [binding] if binding is not None else []

    return []


def _http_port_binding(url: str) -> Optional[PortBinding]:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.port is None:
        return None
    host = (parsed.hostname or "127.0.0.1").strip() or "127.0.0.1"
    return host, int(parsed.port)


def _normalize_host(host: str) -> str:
    text = host.strip().lower()
    if text in {"", "0.0.0.0", "::", "*"}:
        return "0.0.0.0"
    if text in {"localhost", "127.0.0.1", "::1"}:
        return "127.0.0.1"
    return text


def _socket_bind_target(host: str) -> Tuple[int, str]:
    normalized = _normalize_host(host)
    if normalized == "0.0.0.0":
        return socket.AF_INET, "0.0.0.0"
    if ":" in host and not host.startswith("["):
        # IPv6 literal without brackets
        return socket.AF_INET6, host
    return socket.AF_INET, normalized if normalized != "127.0.0.1" else "127.0.0.1"
