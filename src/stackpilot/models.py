from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from subprocess import Popen
from typing import Optional

from .config import ServiceSpec
from .port_detect import resolve_service_port


class ServiceState(str, Enum):
    """Lifecycle state of a managed service process."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"


@dataclass(slots=True)
class ManagedService:
    """Runtime record for a service under ProcessManager control."""

    spec: ServiceSpec
    state: ServiceState = ServiceState.STOPPED
    pid: Optional[int] = None
    last_pid: Optional[int] = None
    exit_code: Optional[int] = None
    process: Optional[Popen[str]] = field(default=None, repr=False, compare=False)
    started_at: Optional[datetime] = None
    _started_mono: Optional[float] = field(default=None, repr=False, compare=False)
    # Last successful spawn plan (for startup / crash diagnostics).
    launch_cwd: Optional[str] = None
    launch_argv: Optional[tuple[str, ...]] = None
    launch_env: Optional[dict[str, str]] = field(
        default=None, repr=False, compare=False
    )

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def status(self) -> ServiceState:
        """Alias for ``state`` (DX / status API)."""

        return self.state

    @property
    def port(self) -> Optional[int]:
        """Resolved port: live socket, health check, explicit port, or command."""

        return resolve_service_port(self.spec, pid=self.pid)

    @property
    def uptime(self) -> Optional[float]:
        """Seconds since the process started, while starting/running."""

        if self._started_mono is None:
            return None
        if self.state not in (ServiceState.STARTING, ServiceState.RUNNING):
            return None
        return max(0.0, time.monotonic() - self._started_mono)

    def mark_started(self, *, when: Optional[datetime] = None) -> None:
        """Record start time when a process becomes live."""

        self.started_at = when or datetime.now(timezone.utc).astimezone()
        self._started_mono = time.monotonic()

    def clear_start(self) -> None:
        """Clear start timestamps before a new spawn attempt."""

        self.started_at = None
        self._started_mono = None
        self.launch_cwd = None
        self.launch_argv = None
        self.launch_env = None

    def clear_runtime(self) -> None:
        """Clear live process fields after stop or crash (keeps ``started_at``)."""

        self.pid = None
        self.process = None
        self._started_mono = None
        # Keep launch_* for post-exit diagnostics until the next start().


def configured_port(spec: ServiceSpec) -> Optional[int]:
    """
    Return the service port for DX display (no live PID lookup).

    Prefer ``resolve_service_port(spec, pid=...)`` when a PID is known.
    """

    return resolve_service_port(spec, pid=None)
