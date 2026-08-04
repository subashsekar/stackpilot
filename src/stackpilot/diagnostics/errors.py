"""User-facing diagnosis helpers for common run-time failures."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence


def format_user_error(
    *,
    problem: str,
    reason: str,
    suggested_fix: str,
    service: Optional[str] = None,
    extra_lines: Optional[Sequence[str]] = None,
) -> str:
    """
    Build a consistent CLI error block.

    Always includes Problem / Reason / Suggested fix. Optionally names the
    affected service. Never includes a traceback.
    """

    lines = [
        f"Problem: {problem}",
    ]
    if service:
        lines.append(f"Affected service: {service}")
    lines.append(f"Reason: {reason}")
    lines.append(f"Suggested fix: {suggested_fix}")
    if extra_lines:
        lines.append("")
        lines.extend(extra_lines)
    return "\n".join(lines)


def format_spawn_failure(
    *,
    service: str,
    exc: BaseException,
    command: str = "",
    cwd: Optional[Path] = None,
) -> str:
    """
    Build a friendly multi-line diagnosis for a failed subprocess spawn.

    Never includes a traceback — callers should print this and exit cleanly.
    """

    problem, reason, action = classify_spawn_error(exc, command=command, cwd=cwd)
    extra: list[str] = []
    if command.strip():
        extra.append(f"Command: {command.strip()}")
    if cwd is not None:
        extra.append(f"Working directory: {cwd}")
    return format_user_error(
        problem=problem,
        reason=reason,
        suggested_fix=action,
        service=service,
        extra_lines=extra or None,
    )


def classify_spawn_error(
    exc: BaseException,
    *,
    command: str = "",
    cwd: Optional[Path] = None,
) -> tuple[str, str, str]:
    """Return (problem, reason, suggested_fix)."""

    if isinstance(exc, FileNotFoundError):
        missing = _missing_path_hint(exc, cwd=cwd)
        if missing is not None and (
            cwd is not None and missing == Path(cwd).expanduser().resolve()
        ):
            return (
                "Invalid working directory",
                f"path={missing} is missing or not a directory.",
                "Create the directory or fix path= in Stackfile.py, then re-run.",
            )
        # filename may point at the executable rather than cwd.
        if missing is not None and not missing.exists() and (
            cwd is None or missing != Path(cwd).expanduser().resolve()
        ):
            # Distinguish missing cwd vs missing executable when filename is set.
            filename = getattr(exc, "filename", None)
            if filename and cwd is not None:
                try:
                    cwd_resolved = Path(cwd).expanduser().resolve()
                except OSError:
                    cwd_resolved = Path(str(cwd))
                if not cwd_resolved.is_dir():
                    return (
                        "Invalid working directory",
                        f"path={cwd_resolved} is missing or not a directory.",
                        "Create the directory or fix path= in Stackfile.py, then re-run.",
                    )
        exe_name = _first_token(command) or "the configured executable"
        return (
            "Executable not found",
            f"The command binary {exe_name!r} was not found on PATH (or the path is wrong).",
            "Install the tool, activate the correct venv, or fix command= in Stackfile.py. "
            "Run: stackpilot doctor",
        )

    if isinstance(exc, PermissionError):
        return (
            "Permission denied",
            "The process lacks permission to execute the command or access the working directory.",
            "Check file permissions / antivirus locks, then re-run. Run: stackpilot doctor",
        )

    if isinstance(exc, NotADirectoryError):
        return (
            "Invalid working directory",
            f"path={cwd} exists but is not a directory." if cwd else "Invalid service path.",
            "Fix path= in Stackfile.py so it points at a service directory.",
        )

    if isinstance(exc, OSError):
        errno = getattr(exc, "errno", None)
        message = str(exc).lower()
        if errno in {48, 98, 10048} or "address already in use" in message:
            return (
                "Port already in use",
                "Another process is already bound to the service port.",
                "stackpilot stop\nor\nchange the service port",
            )
        if "winerror 267" in message or "not a directory" in message:
            return (
                "Invalid working directory",
                f"path={cwd} is invalid." if cwd else "Invalid service path.",
                "Fix path= in Stackfile.py so it points at a service directory.",
            )
        return (
            "Operating system rejected the spawn",
            str(exc) or type(exc).__name__,
            "Fix the command/path in Stackfile.py. Run: stackpilot doctor",
        )

    if isinstance(exc, ValueError):
        text = str(exc) or "command= could not be parsed."
        lower = text.lower()
        if "empty" in lower:
            return (
                "Invalid command",
                text,
                "Set a non-empty command= in Stackfile.py. Run: stackpilot doctor",
            )
        if "escapes project root" in lower or "outside" in lower:
            return (
                "Configuration error",
                text,
                "Keep service path= and reload_dirs inside the project root.",
            )
        return (
            "Invalid command",
            text,
            "Fix command= in Stackfile.py (quoted arguments, empty command). "
            "Run: stackpilot doctor",
        )

    return (
        "Service failed to start",
        str(exc) or type(exc).__name__,
        "Inspect the details above and run: stackpilot doctor",
    )


def format_port_already_in_use(
    *,
    port: int,
    service: Optional[str] = None,
    owners: Optional[Sequence[tuple[int, str]]] = None,
) -> str:
    """
    Friendly block when a foreign process already owns the service port.

    When ``owners`` is omitted, looks up listening PIDs and executable names
    best-effort (never raises).
    """

    port_i = int(port)
    if owners is None:
        try:
            from ..port_detect import describe_port_owners

            owners = describe_port_owners(port_i)
        except Exception:
            owners = ()

    if service:
        reason = f'Service "{service}" requires {port_i}.'
    else:
        reason = f"Port {port_i} is required but already occupied."

    lines = [
        "Problem: Port already in use",
    ]
    if service:
        lines.append(f"Affected service: {service}")
    lines.append(f"Reason: {reason}")
    lines.append("Current owner:")
    if owners:
        for pid, label in owners:
            lines.append(f"PID {int(pid)}")
            if label and label != "unknown":
                lines.append(str(label))
            else:
                lines.append("(executable unknown)")
    else:
        lines.append("(could not determine owning process)")
    lines.append(
        "Suggested fix: stackpilot stop\n"
        "\n"
        "or\n"
        "\n"
        "change the service port"
    )
    return "\n".join(lines)


def format_circular_dependency_error(cycle: Sequence[str]) -> str:
    """Friendly block for a circular service dependency (run / CLI path)."""

    if not cycle:
        reason = "A circular dependency was found in the service graph."
    else:
        lines: list[str] = [""]
        for index, name in enumerate(cycle):
            lines.append(str(name))
            if index < len(cycle) - 1:
                lines.append(" ↓")
        reason = "\n".join(lines)
    return format_user_error(
        problem="Circular dependency detected",
        reason=reason,
        suggested_fix="Remove one dependency to break the cycle.",
    )


def format_cleanup_failure(*, remaining_pids: Sequence[int]) -> str:
    """User-facing Problem / Reason / Suggested fix when stop leaves orphans."""

    if remaining_pids:
        pid_lines = "\n".join(str(int(pid)) for pid in remaining_pids)
        reason = f"The following PIDs remain alive:\n{pid_lines}"
    else:
        reason = "One or more service listening ports were not released."
    return format_user_error(
        problem="Unable to terminate all services.",
        reason=reason,
        suggested_fix=(
            "Run stackpilot stop --force\nor terminate the listed processes manually."
        ),
    )


def format_health_timeout(
    *,
    service: str,
    health_url: str = "",
    timeout_s: float = 0.0,
) -> str:
    """Friendly block when a service health check never becomes ready."""

    reason = (
        f"Service did not become healthy within {timeout_s:.0f}s."
        if timeout_s > 0
        else "Service did not become healthy before the configured timeout."
    )
    if health_url:
        reason = f"{reason} Endpoint: {health_url}"
        problem = "Health endpoint missing or unhealthy"
        fix = (
            "Confirm the process is listening, the health path exists, and "
            "health_check= matches it. Run: stackpilot doctor"
        )
    else:
        problem = "Health timeout"
        fix = (
            "Check the service command and logs in the run terminal, then "
            "run: stackpilot doctor / stackpilot issues"
        )
    return format_user_error(
        problem=problem,
        reason=reason,
        suggested_fix=fix,
        service=service,
    )


def format_health_http_failure(
    *,
    service: str,
    health_url: str,
    kind: str,
    detail: str = "",
    configured_path: str = "",
    discovered_routes: Sequence[str] = (),
) -> str:
    """
    Problem / Reason / Suggested fix for a single HTTP health probe outcome.

    Covers 404 (not_found), 5xx (failed), timeout, and connection refused.
    Never includes a traceback.
    """

    url = (health_url or "").strip() or "(unknown)"
    path = (configured_path or "").strip()
    probe_detail = (detail or "").strip()

    if kind == "not_found":
        problem = "Health endpoint not found"
        reason = f"HTTP 404 for {url}."
        if path:
            reason = f"{reason} Configured path: {path}."
        if discovered_routes:
            routes = ", ".join(str(r) for r in discovered_routes[:8])
            fix = (
                f"Update health_check= to a real path (detected: {routes}). "
                "Or run: stackpilot doctor"
            )
        else:
            fix = (
                "Confirm the health path exists and matches health_check= "
                "in Stackfile.py. Run: stackpilot doctor"
            )
    elif kind == "failed":
        problem = "Health check failed"
        status = probe_detail or "HTTP 5xx"
        reason = f"{status} from {url}."
        fix = (
            "Inspect application logs for the server error, fix the failure, "
            "then re-run. Run: stackpilot issues"
        )
    elif kind == "timeout":
        problem = "Health check timed out"
        reason = f"No timely response from {url}."
        fix = (
            "Confirm the process is listening and not blocked. "
            "Increase health timeout or fix the application. Run: stackpilot doctor"
        )
    elif kind == "refused":
        problem = "Connection refused"
        reason = f"Nothing accepted connections for {url}."
        fix = (
            "Confirm the process started and the port matches health_check=. "
            "Run: stackpilot doctor / stackpilot issues"
        )
    else:
        problem = "Health check failed"
        reason = probe_detail or f"Probe of {url} did not succeed."
        fix = "Inspect service logs, then run: stackpilot doctor"

    return format_user_error(
        problem=problem,
        reason=reason,
        suggested_fix=fix,
        service=service,
    )


def format_external_timeout(
    *,
    label: str,
    host: str,
    port: int,
    timeout_s: float,
) -> str:
    """Friendly note used when external validation exhausts its retry window."""

    return format_user_error(
        problem="Dependency unavailable",
        reason=(
            f"{label} did not become reachable within {timeout_s:.0f}s "
            f"({host}:{port})."
        ),
        suggested_fix=(
            f"Start {label} (or fix host/port), then re-run `stackpilot run`. "
            "Verify with `stackpilot doctor`."
        ),
    )


def format_corrupted_runtime(*, cleared: bool = True) -> str:
    """Friendly block when ``.stackpilot/runtime.json`` cannot be parsed."""

    reason = (
        "``.stackpilot/runtime.json`` exists but is not valid JSON / schema."
    )
    fix = (
        "Runtime status was cleared. Re-run `stackpilot run` (or "
        "`stackpilot stop --force` if processes are still live)."
        if cleared
        else "Delete `.stackpilot/runtime.json` or run `stackpilot stop --force`, "
        "then re-run."
    )
    return format_user_error(
        problem="Corrupted runtime status",
        reason=reason,
        suggested_fix=fix,
    )


def format_missing_stackfile() -> str:
    """Friendly block when no Stackfile.py can be discovered."""

    from ..discovery import MISSING_STACKFILE_MESSAGE

    return MISSING_STACKFILE_MESSAGE


def _first_token(command: str) -> str:
    text = (command or "").strip()
    if not text:
        return ""
    if text[0] in {'"', "'"}:
        quote = text[0]
        end = text.find(quote, 1)
        if end > 0:
            return text[1:end]
    return text.split()[0]


def _missing_path_hint(exc: FileNotFoundError, *, cwd: Optional[Path]) -> Optional[Path]:
    filename = getattr(exc, "filename", None)
    if filename:
        try:
            return Path(filename).expanduser().resolve()
        except OSError:
            return Path(str(filename))
    if cwd is not None:
        try:
            path = Path(cwd).expanduser().resolve()
        except OSError:
            return Path(str(cwd))
        if not path.is_dir():
            return path
    return None
