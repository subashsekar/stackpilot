"""Health Check Engine — wait until a service becomes healthy."""

from __future__ import annotations

import time
from subprocess import Popen
from typing import Callable, Mapping, Optional, Union
from urllib.parse import urlparse

from .config import (
    DEFAULT_HEALTH_INTERVAL_S,
    DEFAULT_HEALTH_PROBE_TIMEOUT_S,
    DEFAULT_HEALTH_TIMEOUT_S,
    HealthCheck,
    HealthCheckInput,
    HttpHealthCheck,
    ProcessHealthCheck,
    TcpHealthCheck,
    coerce_health_check,
    parse_health_check,
)
from .http_checker import check_http
from .port_detect import pid_tree_owns_port
from .process_checker import check_process
from .tcp_checker import check_tcp

# Re-export defaults under historical names used by tests / docs.
DEFAULT_INTERVAL_S = DEFAULT_HEALTH_INTERVAL_S
DEFAULT_TIMEOUT_S = DEFAULT_HEALTH_TIMEOUT_S
DEFAULT_PROBE_TIMEOUT_S = DEFAULT_HEALTH_PROBE_TIMEOUT_S


class HealthCheckError(Exception):
    """Base error for health-check failures."""


class HealthCheckTimeout(HealthCheckError):
    """Raised when a service does not become healthy before the deadline."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"{name} failed health check")


class PortOwnershipError(HealthCheckError):
    """Raised when a foreign process owns the service listen port."""

    def __init__(self, name: str, port: int) -> None:
        self.name = name
        self.port = int(port)
        super().__init__(
            f"{name}: port {self.port} is already owned by another process"
        )


class Health:
    """
    Dispatch and poll health checks until success or timeout.

    Checkers live in dedicated modules; this class owns retry / timeout
    orchestration only. HTTP/TCP success is gated on process-tree port
    ownership so a foreign listener cannot produce a false healthy state.
    """

    @classmethod
    def wait_until_healthy(
        cls,
        name: str,
        health_check: Optional[HealthCheckInput],
        *,
        process: Optional[Popen[str]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> float:
        """
        Block until ``health_check`` succeeds for ``name``.

        Returns elapsed seconds. Raises ``HealthCheckTimeout`` on failure.
        Raises ``PortOwnershipError`` when another process owns the listen port.
        When ``health_check`` is ``None``, defaults to a process liveness check.
        Accepts typed ``HealthCheck`` models or legacy mapping configs.
        """

        cfg = cls._normalize(health_check)
        interval = float(cfg.interval)
        timeout_s = float(cfg.timeout)

        started = clock()
        deadline = started + timeout_s

        while True:
            ownership = cls._check_port_ownership(cfg, process=process)
            if ownership is False:
                port = cls._configured_port(cfg)
                raise PortOwnershipError(name, int(port or 0))

            # Fail fast when the child already exited — waiting out the full
            # health timeout only delays diagnostics and does not help.
            if process is not None:
                if process.pid is None or process.poll() is not None:
                    raise HealthCheckTimeout(name)

            # When ownership is None (nothing detected listening yet, or
            # listener PID mapping unavailable), still probe HTTP/TCP.
            # Connection refused keeps us waiting; a foreign owner that
            # detection *can* see is rejected above as False. Skipping
            # probes on None previously starved readiness on CI hosts
            # where /proc|ss|lsof briefly miss a just-bound socket.

            if cls.dispatch(cfg, process=process):
                # Re-verify ownership after a successful probe so a race with
                # a foreign binder cannot slip through as healthy.
                ownership_after = cls._check_port_ownership(cfg, process=process)
                if ownership_after is False:
                    port = cls._configured_port(cfg)
                    raise PortOwnershipError(name, int(port or 0))
                if ownership_after is True or not cls._requires_port_ownership(cfg):
                    return clock() - started
                # Probe succeeded and no foreign owner was confirmed
                # (ownership_after is None). Accept healthy: requiring a
                # positive ownership map here leaves services stuck when
                # port→PID backends are unavailable, even though the
                # endpoint already answered.
                return clock() - started
            if cls.timeout(deadline, clock=clock):
                raise HealthCheckTimeout(name)
            cls.retry(interval, sleep=sleep)

    @classmethod
    def dispatch(
        cls,
        health_check: Union[HealthCheck, Mapping],
        *,
        process: Optional[Popen[str]] = None,
    ) -> bool:
        """Run a single health-check attempt. Returns True when healthy."""

        try:
            cfg = (
                health_check
                if isinstance(
                    health_check,
                    (ProcessHealthCheck, HttpHealthCheck, TcpHealthCheck),
                )
                else parse_health_check(health_check)
            )
        except ValueError as exc:
            raise HealthCheckError(str(exc)) from exc

        probe_timeout = float(cfg.probe_timeout)

        if isinstance(cfg, HttpHealthCheck):
            if not cfg.url:
                return False
            return check_http(cfg.url, request_timeout=probe_timeout)

        if isinstance(cfg, TcpHealthCheck):
            return check_tcp(cfg.host, cfg.port, connect_timeout=probe_timeout)

        if isinstance(cfg, ProcessHealthCheck):
            return check_process(process)

        raise HealthCheckError(f"Unknown health check type: {type(cfg)!r}")

    @classmethod
    def timeout(
        cls,
        deadline: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> bool:
        """Return True when ``clock()`` has reached or passed ``deadline``."""

        return clock() >= deadline

    @classmethod
    def retry(
        cls,
        interval: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Wait ``interval`` seconds before the next probe attempt."""

        sleep(max(0.0, float(interval)))

    @classmethod
    def _normalize(
        cls,
        health_check: Optional[HealthCheckInput],
    ) -> HealthCheck:
        if health_check is None:
            return ProcessHealthCheck(
                interval=DEFAULT_INTERVAL_S,
                timeout=DEFAULT_TIMEOUT_S,
            )
        return coerce_health_check(health_check)  # type: ignore[return-value]

    @classmethod
    def _requires_port_ownership(cls, cfg: HealthCheck) -> bool:
        return isinstance(cfg, (HttpHealthCheck, TcpHealthCheck))

    @classmethod
    def _configured_port(cls, cfg: HealthCheck) -> Optional[int]:
        if isinstance(cfg, TcpHealthCheck):
            return int(cfg.port)
        if isinstance(cfg, HttpHealthCheck):
            text = (cfg.url or "").strip()
            if not text:
                return None
            try:
                parsed = urlparse(text)
            except ValueError:
                return None
            if parsed.port is not None:
                return int(parsed.port)
            if parsed.scheme == "https":
                return 443
            if parsed.scheme == "http":
                return 80
        return None

    @classmethod
    def _check_port_ownership(
        cls,
        cfg: HealthCheck,
        *,
        process: Optional[Popen[str]],
    ) -> Optional[bool]:
        """
        ``True`` / ``False`` / ``None`` from :func:`pid_tree_owns_port`.

        Returns ``True`` (skip gate) for process checks or when PID/port
        cannot be resolved — callers still require ownership for HTTP/TCP
        when a port is known.
        """

        if not cls._requires_port_ownership(cfg):
            return True
        if process is None or process.pid is None:
            return True
        port = cls._configured_port(cfg)
        if port is None:
            return True
        return pid_tree_owns_port(int(process.pid), int(port))
