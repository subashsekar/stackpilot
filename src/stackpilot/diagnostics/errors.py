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
                "Free the port or change port= / the health URL, then re-run. "
                "Run: stackpilot doctor",
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
