from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

DEFAULT_HEALTH_INTERVAL_S = 1.0
DEFAULT_HEALTH_TIMEOUT_S = 30.0
DEFAULT_HEALTH_PROBE_TIMEOUT_S = 5.0
# External deps are probed before startup. Retries cover brief boot races
# (Postgres/Redis becoming reachable a few seconds after launch) while the
# overall timeout still fails fast when the service is truly offline.
DEFAULT_EXTERNAL_INTERVAL_S = 0.5
DEFAULT_EXTERNAL_TIMEOUT_S = 10.0
DEFAULT_EXTERNAL_PROBE_TIMEOUT_S = 1.0
DEFAULT_EXTERNAL_RETRIES = 5
DEFAULT_EXTERNAL_RETRY_BACKOFF: Literal["fixed", "exponential"] = "fixed"


@dataclass(frozen=True, slots=True)
class ProcessHealthCheck:
    """Health check that passes while the service subprocess is alive."""

    type: Literal["process"] = "process"
    interval: float = DEFAULT_HEALTH_INTERVAL_S
    timeout: float = DEFAULT_HEALTH_TIMEOUT_S
    probe_timeout: float = DEFAULT_HEALTH_PROBE_TIMEOUT_S


@dataclass(frozen=True, slots=True)
class HttpHealthCheck:
    """Health check that GETs ``url`` and expects HTTP 200–399."""

    url: str
    type: Literal["http"] = "http"
    interval: float = DEFAULT_HEALTH_INTERVAL_S
    timeout: float = DEFAULT_HEALTH_TIMEOUT_S
    probe_timeout: float = DEFAULT_HEALTH_PROBE_TIMEOUT_S


@dataclass(frozen=True, slots=True)
class TcpHealthCheck:
    """Health check that opens a TCP connection to ``host:port``."""

    host: str
    port: int
    type: Literal["tcp"] = "tcp"
    interval: float = DEFAULT_HEALTH_INTERVAL_S
    timeout: float = DEFAULT_HEALTH_TIMEOUT_S
    probe_timeout: float = DEFAULT_HEALTH_PROBE_TIMEOUT_S


HealthCheck = Union[ProcessHealthCheck, HttpHealthCheck, TcpHealthCheck]
HealthCheckInput = Union[HealthCheck, Mapping[str, Any]]


def _validate_http_url_scheme(url: str) -> None:
    """Reject non-http(s) schemes early when parsing health-check mappings."""

    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ValueError(f"invalid url: {exc}") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"url must use http or https scheme (got {scheme!r})")
    if not parsed.netloc:
        raise ValueError("url must include a host")


def parse_health_check(value: HealthCheckInput) -> HealthCheck:
    """
    Coerce a mapping or typed model into a concrete ``HealthCheck``.

    Dict configs remain supported for Stackfile backwards compatibility:
    ``{"type": "http", "url": "..."}``.
    """

    if isinstance(value, (ProcessHealthCheck, HttpHealthCheck, TcpHealthCheck)):
        return value
    if not isinstance(value, Mapping):
        raise TypeError(
            "health_check must be a HealthCheck model or a mapping, "
            f"got {type(value).__name__}"
        )

    raw = dict(value)
    check_type = str(raw.get("type", "process")).strip().lower() or "process"
    interval = float(raw.get("interval", DEFAULT_HEALTH_INTERVAL_S))
    timeout = float(raw.get("timeout", DEFAULT_HEALTH_TIMEOUT_S))
    probe_timeout = float(raw.get("probe_timeout", DEFAULT_HEALTH_PROBE_TIMEOUT_S))

    if check_type == "process":
        return ProcessHealthCheck(
            interval=interval,
            timeout=timeout,
            probe_timeout=probe_timeout,
        )
    if check_type == "http":
        url = raw.get("url")
        if url is None or not str(url).strip():
            raise ValueError("http health_check requires a non-empty 'url'")
        normalized = str(url).strip()
        _validate_http_url_scheme(normalized)
        return HttpHealthCheck(
            url=normalized,
            interval=interval,
            timeout=timeout,
            probe_timeout=probe_timeout,
        )
    if check_type == "tcp":
        host = raw.get("host")
        port = raw.get("port")
        if host is None or not str(host).strip():
            raise ValueError("tcp health_check requires a non-empty 'host'")
        if port is None:
            raise ValueError("tcp health_check requires 'port'")
        return TcpHealthCheck(
            host=str(host).strip(),
            port=int(port),
            interval=interval,
            timeout=timeout,
            probe_timeout=probe_timeout,
        )
    raise ValueError(f"Unknown health check type: {check_type!r}")


def coerce_health_check(
    value: HealthCheckInput | None,
) -> HealthCheck | None:
    """Normalize optional health-check input; ``None`` stays ``None``."""

    if value is None:
        return None
    return parse_health_check(value)


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """Configuration needed to start a single local service."""

    name: str
    path: Path
    command: str = ""
    depends_on: Tuple[str, ...] = field(default_factory=tuple)
    health_check: Optional[HealthCheck] = None
    reload: bool = False
    reload_dirs: Tuple[str, ...] = field(default_factory=tuple)
    restart_dependents: bool = False
    # Optional display / DX port when health_check does not expose one.
    port: Optional[int] = None

    def __post_init__(self) -> None:
        # Accept legacy mapping configs when tests / callers build ServiceSpec
        # directly instead of going through ``Stack.service()``.
        if self.health_check is None:
            return
        if isinstance(
            self.health_check,
            (ProcessHealthCheck, HttpHealthCheck, TcpHealthCheck),
        ):
            return
        if isinstance(self.health_check, Mapping):
            object.__setattr__(
                self,
                "health_check",
                parse_health_check(self.health_check),
            )
            return
        raise TypeError(
            "health_check must be a HealthCheck model or mapping, "
            f"got {type(self.health_check).__name__}"
        )


@dataclass(frozen=True, slots=True)
class ExternalDependency:
    """
    External infrastructure that StackPilot never starts.

    Declared so application services can ``depends_on`` it and so startup /
    doctor can validate reachability (typically TCP) before launching apps.
    """

    name: str
    type: str
    host: str
    port: int
    health_check: Optional[HealthCheck] = None
    retries: int = DEFAULT_EXTERNAL_RETRIES
    retry_delay: float = DEFAULT_EXTERNAL_INTERVAL_S
    retry_backoff: Literal["fixed", "exponential"] = DEFAULT_EXTERNAL_RETRY_BACKOFF

    def __post_init__(self) -> None:
        retries = max(1, int(self.retries))
        if retries != self.retries:
            object.__setattr__(self, "retries", retries)
        delay = max(0.0, float(self.retry_delay))
        if delay != self.retry_delay:
            object.__setattr__(self, "retry_delay", delay)
        backoff = str(self.retry_backoff or "fixed").strip().lower()
        if backoff not in {"fixed", "exponential"}:
            raise ValueError(
                "retry_backoff must be 'fixed' or 'exponential', "
                f"got {self.retry_backoff!r}"
            )
        if backoff != self.retry_backoff:
            object.__setattr__(self, "retry_backoff", backoff)  # type: ignore[arg-type]

        if self.health_check is None:
            object.__setattr__(
                self,
                "health_check",
                TcpHealthCheck(
                    host=str(self.host).strip(),
                    port=int(self.port),
                    interval=float(self.retry_delay),
                    timeout=DEFAULT_EXTERNAL_TIMEOUT_S,
                    probe_timeout=DEFAULT_EXTERNAL_PROBE_TIMEOUT_S,
                ),
            )
            return
        if isinstance(
            self.health_check,
            (ProcessHealthCheck, HttpHealthCheck, TcpHealthCheck),
        ):
            return
        if isinstance(self.health_check, Mapping):
            object.__setattr__(
                self,
                "health_check",
                parse_health_check(self.health_check),
            )
            return
        raise TypeError(
            "health_check must be a HealthCheck model or mapping, "
            f"got {type(self.health_check).__name__}"
        )

    @property
    def display_name(self) -> str:
        """Human-readable label for CLI messages (e.g. PostgreSQL, Redis)."""

        return external_dependency_display_name(self.type, self.name)


_EXTERNAL_TYPE_LABELS: Mapping[str, str] = {
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
    "pgsql": "PostgreSQL",
    "pg": "PostgreSQL",
    "redis": "Redis",
    "mongodb": "MongoDB",
    "mongo": "MongoDB",
    "rabbitmq": "RabbitMQ",
    "amqp": "RabbitMQ",
}


def external_dependency_display_name(dep_type: str, name: str = "") -> str:
    """Map an external dependency type (or name) to a display label."""

    key = str(dep_type or "").strip().lower()
    if key in _EXTERNAL_TYPE_LABELS:
        return _EXTERNAL_TYPE_LABELS[key]
    name_key = str(name or "").strip().lower()
    if name_key in _EXTERNAL_TYPE_LABELS:
        return _EXTERNAL_TYPE_LABELS[name_key]
    label = str(dep_type or name or "dependency").strip()
    return label[:1].upper() + label[1:] if label else "dependency"


class Stack:
    """
    Developer-defined configuration.

    A ``Stackfile.py`` file should create a ``stack = Stack()`` instance,
    add services via ``stack.service(...)``, then call ``stack.run()``.

    When ``Stackfile.py`` is executed as a script (``python Stackfile.py``),
    ``run()`` starts the stack via ``Orchestrator``. When the file is imported
    by the CLI, ``run()`` only records intent and does not start processes.
    """

    def __init__(self) -> None:
        self._services: list[ServiceSpec] = []
        self._external_dependencies: list[ExternalDependency] = []
        self._run_requested: bool = False

    def service(
        self,
        *,
        name: str,
        path: str | Path,
        command: str = "",
        depends_on: Sequence[str] | None = None,
        health_check: HealthCheckInput | None = None,
        reload: bool = False,
        reload_dirs: Sequence[str] | None = None,
        restart_dependents: bool = False,
        port: int | None = None,
    ) -> "Stack":
        service_path = Path(path).expanduser()
        deps = tuple(depends_on) if depends_on else ()
        check = coerce_health_check(health_check)
        dirs = tuple(str(d) for d in reload_dirs) if reload_dirs else ()
        self._services.append(
            ServiceSpec(
                name=name,
                path=service_path,
                command=command,
                depends_on=deps,
                health_check=check,
                reload=bool(reload),
                reload_dirs=dirs,
                restart_dependents=bool(restart_dependents),
                port=int(port) if port is not None else None,
            )
        )
        return self

    def external_dependency(
        self,
        *,
        name: str,
        type: str,
        host: str = "127.0.0.1",
        port: int,
        health_check: HealthCheckInput | None = None,
        retries: int = DEFAULT_EXTERNAL_RETRIES,
        retry_delay: float = DEFAULT_EXTERNAL_INTERVAL_S,
        retry_backoff: Literal["fixed", "exponential"] = DEFAULT_EXTERNAL_RETRY_BACKOFF,
    ) -> "Stack":
        """
        Register infrastructure that StackPilot validates but never starts.

        Application services may list ``name`` in ``depends_on``. Prefer TCP
        health checks (default when ``health_check`` is omitted).

        ``retries`` / ``retry_delay`` / ``retry_backoff`` control the pre-start
        reachability probe (fixed or exponential backoff between attempts).
        """

        check = coerce_health_check(health_check)
        self._external_dependencies.append(
            ExternalDependency(
                name=str(name).strip(),
                type=str(type).strip(),
                host=str(host).strip() or "127.0.0.1",
                port=int(port),
                health_check=check,
                retries=int(retries),
                retry_delay=float(retry_delay),
                retry_backoff=retry_backoff,
            )
        )
        return self

    def run(self) -> "Stack":
        """
        Mark the stack as executable.

        If called from a ``__main__`` module (``python Stackfile.py``), start
        services immediately via ``Orchestrator``. CLI import paths only set
        the flag — ``stackpilot run`` owns process startup.
        """

        self._run_requested = True
        try:
            caller = sys._getframe(1)
        except ValueError:
            return self

        if caller.f_globals.get("__name__") != "__main__":
            return self

        from .orchestrator import Orchestrator
        from .utils import materialize_stack_for_project

        stackfile = caller.f_globals.get("__file__")
        root = (
            Path(stackfile).expanduser().resolve().parent
            if stackfile
            else Path.cwd()
        )
        resolved = materialize_stack_for_project(self, root)
        raise SystemExit(Orchestrator().run(resolved, project_root=root))

    @property
    def services(self) -> Sequence[ServiceSpec]:
        return tuple(self._services)

    @property
    def external_dependencies(self) -> Sequence[ExternalDependency]:
        return tuple(self._external_dependencies)

    @property
    def run_requested(self) -> bool:
        return self._run_requested
