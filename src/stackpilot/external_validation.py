"""Validate external infrastructure dependencies before application startup."""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from .config import (
    DEFAULT_EXTERNAL_INTERVAL_S,
    DEFAULT_EXTERNAL_RETRIES,
    DEFAULT_EXTERNAL_TIMEOUT_S,
    ExternalDependency,
    ServiceSpec,
    TcpHealthCheck,
)
from .dashboard import ascii_fallback_dx, print_safe
from .dependency_graph import DependencyGraph
from .health import Health, HealthCheckTimeout


class ExternalDependencyError(RuntimeError):
    """Raised when a required external dependency is unreachable."""

    def __init__(self, message: str, *, dependency: ExternalDependency) -> None:
        self.dependency = dependency
        super().__init__(message)


def format_external_unavailable(
    dep: ExternalDependency,
    *,
    dependents: Sequence[str],
    elapsed_s: float = 0.0,
    attempts: int = 0,
    include_checking: bool = True,
) -> str:
    """
    Render the graceful abort message for an unreachable external dependency.

    Example::

        Checking PostgreSQL...
        ✗ PostgreSQL is not reachable.

        Problem: Dependency unavailable
        Dependency: PostgreSQL
        Host: 127.0.0.1
        Port: 5432
        Elapsed: 10.0s
        Attempts: 5/5

        Services depending on PostgreSQL:
        - auth
        - users

        Suggested fix: Start PostgreSQL (or update host/port), then re-run
        ``stackpilot run``. Verify with ``stackpilot doctor``.

        Startup aborted.
    """

    label = dep.display_name
    retries = max(1, int(getattr(dep, "retries", DEFAULT_EXTERNAL_RETRIES)))
    shown_attempts = attempts if attempts > 0 else retries
    lines: List[str] = []
    if include_checking:
        lines.append(f"Checking {label}...")
    lines.append(f"✗ {label} is not reachable.")
    lines.append("")
    lines.append("Problem: Dependency unavailable")
    lines.append(f"Dependency: {label}")
    lines.append(f"Host: {dep.host}")
    lines.append(f"Port: {dep.port}")
    lines.append(f"Elapsed: {elapsed_s:.1f}s")
    lines.append(f"Attempts: {shown_attempts}/{retries}")
    lines.append("")
    if dependents:
        lines.append(f"Services depending on {label}:")
        for name in dependents:
            lines.append(f"- {name}")
        lines.append("")
    lines.append(
        f"Suggested fix: Start {label} (or update host/port in Stackfile.py), "
        "then re-run `stackpilot run`. Verify with `stackpilot doctor`."
    )
    lines.append("")
    lines.append("Startup aborted.")
    return "\n".join(lines)


def dependents_of_external(
    graph: DependencyGraph,
    dep_name: str,
    *,
    among: Optional[Sequence[ServiceSpec]] = None,
) -> List[str]:
    """Application service names that directly depend on ``dep_name``."""

    allowed = {spec.name for spec in among} if among is not None else None
    names: List[str] = []
    for name, deps in graph.edges.items():
        if dep_name not in deps:
            continue
        if name not in graph.specs:
            continue
        if allowed is not None and name not in allowed:
            continue
        names.append(name)
    return names


def check_external_dependency(dep: ExternalDependency) -> bool:
    """Return True when the dependency passes a single health-engine probe."""

    check = dep.health_check
    if check is None:
        check = TcpHealthCheck(host=dep.host, port=dep.port)
    return Health.dispatch(check)


def _retry_sleep_s(dep: ExternalDependency, attempt: int, base_delay: float) -> float:
    """Delay before the next attempt (``attempt`` is 1-based after a failure)."""

    if base_delay <= 0:
        return 0.0
    backoff = str(getattr(dep, "retry_backoff", "fixed") or "fixed").lower()
    if backoff == "exponential":
        # attempt 1 → base, 2 → 2*base, 3 → 4*base, …
        return float(base_delay) * (2 ** max(0, attempt - 1))
    return float(base_delay)


def wait_for_external_dependency(
    dep: ExternalDependency,
    *,
    timeout_s: Optional[float] = None,
    interval_s: Optional[float] = None,
    retries: Optional[int] = None,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    on_attempt: Optional[Callable[[int, int], None]] = None,
) -> float:
    """
    Retry probing ``dep`` until healthy, ``retries`` exhausted, or ``timeout_s``.

    Uses :func:`check_external_dependency` so callers/tests can stub a single
    probe. Returns elapsed seconds on success. Raises ``HealthCheckTimeout``
    when the dependency stays unreachable.

    ``on_attempt(attempt, total)`` is invoked before each probe (1-based).
    """

    import time as _time

    clock = clock or _time.monotonic
    sleep = sleep or _time.sleep
    check = dep.health_check
    if check is None:
        interval = float(
            interval_s if interval_s is not None else getattr(dep, "retry_delay", DEFAULT_EXTERNAL_INTERVAL_S)
        )
        timeout = float(timeout_s if timeout_s is not None else DEFAULT_EXTERNAL_TIMEOUT_S)
    else:
        interval = float(
            interval_s
            if interval_s is not None
            else getattr(dep, "retry_delay", check.interval)
        )
        timeout = float(timeout_s if timeout_s is not None else check.timeout)

    total = max(
        1,
        int(retries if retries is not None else getattr(dep, "retries", DEFAULT_EXTERNAL_RETRIES)),
    )

    started = clock()
    deadline = started + timeout
    last_attempt = 0
    for attempt in range(1, total + 1):
        last_attempt = attempt
        if on_attempt is not None:
            on_attempt(attempt, total)
        if check_external_dependency(dep):
            return clock() - started
        if clock() >= deadline:
            raise HealthCheckTimeout(dep.name)
        if attempt >= total:
            break
        delay = _retry_sleep_s(dep, attempt, interval)
        remaining = deadline - clock()
        if remaining <= 0:
            raise HealthCheckTimeout(dep.name)
        sleep(min(delay, remaining))

    # Exhausted retries without success (or timed out on last sleep).
    raise HealthCheckTimeout(dep.name)


def validate_external_dependencies(
    graph: DependencyGraph,
    *,
    ordered_services: Sequence[ServiceSpec],
    target: Optional[str] = None,
) -> None:
    """
    Probe required external dependencies before any application service starts.

    Retries each dependency with configurable count/delay/backoff until healthy
    or the timeout elapses. On failure, raises ``ExternalDependencyError`` with
    a user-facing abort message.
    """

    required = graph.required_externals(target=target)
    if not required:
        return

    print_safe("Checking external dependencies...", ascii_fallback="Checking external dependencies...")
    print_safe("", ascii_fallback="")

    for dep in required:
        label = dep.display_name
        endpoint = f"{dep.host}:{dep.port}"
        retries = max(1, int(getattr(dep, "retries", DEFAULT_EXTERNAL_RETRIES)))
        print_safe(f"Checking {label}...", ascii_fallback=f"Checking {label}...")

        attempts_done = 0

        def _on_attempt(attempt: int, total: int) -> None:
            nonlocal attempts_done
            attempts_done = attempt
            msg = f"Attempt {attempt}/{total}..."
            print_safe(msg, ascii_fallback=msg)

        import time as _time

        started = _time.monotonic()
        try:
            wait_for_external_dependency(dep, on_attempt=_on_attempt)
        except HealthCheckTimeout:
            elapsed = _time.monotonic() - started
            dependents = dependents_of_external(
                graph,
                dep.name,
                among=ordered_services,
            )
            message = format_external_unavailable(
                dep,
                dependents=dependents,
                elapsed_s=elapsed,
                attempts=attempts_done or retries,
                include_checking=False,
            )
            print_safe(message, ascii_fallback=ascii_fallback_dx(message))
            raise ExternalDependencyError(
                format_external_unavailable(
                    dep,
                    dependents=dependents,
                    elapsed_s=elapsed,
                    attempts=attempts_done or retries,
                ),
                dependency=dep,
            ) from None

        print_safe("Connected.", ascii_fallback="Connected.")
        print_safe(f"✓ {label} ({endpoint})", ascii_fallback=f"+ {label} ({endpoint})")
        print_safe("", ascii_fallback="")
