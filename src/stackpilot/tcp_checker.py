"""TCP readiness probe using ``socket.create_connection``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple, Union

import errno
import socket

from .config import TcpHealthCheck


@dataclass(frozen=True, slots=True)
class TcpProbeResult:
    """Structured result of a single TCP connectivity probe."""

    ok: bool
    kind: str
    detail: str

    @property
    def reachable(self) -> bool:
        return self.ok


def check_tcp(host: str, port: int, *, connect_timeout: float = 2.0) -> bool:
    """
    Return True when a TCP connection to ``host:port`` succeeds.

    The connection is closed immediately after a successful handshake.
    """

    return diagnose_tcp(host, port, connect_timeout=connect_timeout).ok


def diagnose_tcp(
    host: str,
    port: int,
    *,
    connect_timeout: float = 2.0,
) -> TcpProbeResult:
    """
    Probe ``host:port`` and classify the outcome for diagnostics.

    Kinds: ``reachable``, ``timeout``, ``refused``, ``dns``, ``unreachable``.
    """

    endpoint = f"{host}:{int(port)}"
    try:
        with socket.create_connection((host, int(port)), timeout=connect_timeout):
            return TcpProbeResult(
                ok=True,
                kind="reachable",
                detail=f"{endpoint} accepts TCP connections",
            )
    except socket.timeout:
        return TcpProbeResult(
            ok=False,
            kind="timeout",
            detail=f"connection to {endpoint} timed out after {connect_timeout:g}s",
        )
    except TimeoutError:
        return TcpProbeResult(
            ok=False,
            kind="timeout",
            detail=f"connection to {endpoint} timed out after {connect_timeout:g}s",
        )
    except socket.gaierror as exc:
        return TcpProbeResult(
            ok=False,
            kind="dns",
            detail=f"host {host!r} could not be resolved ({exc})",
        )
    except ConnectionRefusedError:
        return TcpProbeResult(
            ok=False,
            kind="refused",
            detail=(
                f"connection refused at {endpoint} "
                "(nothing listening, or incorrect host/port)"
            ),
        )
    except OSError as exc:
        # Windows often surfaces refused connects as WinError 10061.
        err = getattr(exc, "errno", None)
        refused = {errno.ECONNREFUSED}
        timed_out = {errno.ETIMEDOUT}
        wsa_refused = getattr(errno, "WSAECONNREFUSED", None)
        wsa_timed = getattr(errno, "WSAETIMEDOUT", None)
        if wsa_refused is not None:
            refused.add(wsa_refused)
        if wsa_timed is not None:
            timed_out.add(wsa_timed)
        if err in refused:
            return TcpProbeResult(
                ok=False,
                kind="refused",
                detail=(
                    f"connection refused at {endpoint} "
                    "(nothing listening, or incorrect host/port)"
                ),
            )
        if err in timed_out:
            return TcpProbeResult(
                ok=False,
                kind="timeout",
                detail=f"connection to {endpoint} timed out after {connect_timeout:g}s",
            )
        return TcpProbeResult(
            ok=False,
            kind="unreachable",
            detail=f"{endpoint} unreachable ({exc})",
        )


def tcp_endpoint_from_config(
    health_check: Union[TcpHealthCheck, Mapping[str, Any]],
) -> Optional[Tuple[str, int]]:
    """Extract ``(host, port)`` from a health-check model or mapping."""

    if isinstance(health_check, TcpHealthCheck):
        host_text = health_check.host.strip()
        if not host_text:
            return None
        return host_text, int(health_check.port)

    host = health_check.get("host")
    port = health_check.get("port")
    if host is None or port is None:
        return None
    host_text = str(host).strip()
    if not host_text:
        return None
    try:
        return host_text, int(port)
    except (TypeError, ValueError):
        return None
