"""Out-of-band runtime session control (stop + stale session recovery)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import ServiceSpec
from .diagnostics.errors import format_cleanup_failure, format_user_error
from .diagnostics.ports import is_port_in_use
from .models import ServiceState, configured_port
from .port_detect import pids_listening_on_port
from .process_tree import signal_process_tree
from .status import (
    load_runtime_snapshot,
    pid_is_alive,
    runtime_status_path,
    save_runtime_snapshot,
)

__all__ = [
    "StaleSession",
    "StopResult",
    "clear_runtime_session",
    "detect_stale_session",
    "format_stale_session_error",
    "format_cleanup_failure",
    "read_runtime_payload",
    "resolve_session_root",
    "stop_runtime_session",
    "DEFAULT_STOP_TIMEOUT_S",
]

DEFAULT_STOP_TIMEOUT_S = 2.0


@dataclass(frozen=True, slots=True)
class StopResult:
    """Outcome of ``stackpilot stop``."""

    stopped_names: tuple[str, ...]
    already_dead: tuple[str, ...]
    message: str
    exit_code: int = 0
    remaining_pids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class StaleSession:
    """Detected leftovers from a previous StackPilot session."""

    live_services: tuple[str, ...] = ()
    occupied_ports: tuple[int, ...] = ()
    reason: str = "Previous session was not shut down cleanly."


@dataclass
class _RuntimeRead:
    """Internal parse result for ``runtime.json``."""

    missing: bool = False
    corrupted: bool = False
    snapshot: Optional[Dict[str, Any]] = None
    raw_services: List[Dict[str, Any]] = field(default_factory=list)


def read_runtime_payload(project_root: Path) -> _RuntimeRead:
    """
    Read ``.stackpilot/runtime.json`` without raising.

    Distinguishes missing vs corrupted so CLI can recover gracefully.
    """

    path = runtime_status_path(project_root)
    if not path.is_file():
        return _RuntimeRead(missing=True)

    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _RuntimeRead(corrupted=True)

    if not isinstance(data, dict):
        return _RuntimeRead(corrupted=True)

    services = data.get("services")
    raw: List[Dict[str, Any]] = []
    if isinstance(services, list):
        for item in services:
            if isinstance(item, dict):
                raw.append(item)
    elif services is not None:
        return _RuntimeRead(corrupted=True)

    return _RuntimeRead(snapshot=data, raw_services=raw)


def resolve_session_root(start: Path | None = None) -> Optional[Path]:
    """
    Locate the project root for ``stackpilot stop``.

    Prefers the nearest ``Stackfile.py`` ancestor. Falls back to an ancestor
    that owns ``.stackpilot/runtime.json`` so leftover sessions can still be
    cleared after the Stackfile was removed. Returns ``None`` when neither
    exists (caller prints ``No running StackPilot session.``).
    """

    origin = (start or Path.cwd()).expanduser().resolve()

    from .discovery import find_stackfile

    stackfile = find_stackfile(origin)
    if stackfile is not None:
        return stackfile.parent

    for directory in (origin, *origin.parents):
        if runtime_status_path(directory).is_file():
            return directory
    return None


def stop_runtime_session(
    project_root: Path,
    *,
    force: bool = False,
    timeout_s: float = DEFAULT_STOP_TIMEOUT_S,
) -> StopResult:
    """
    Terminate every process recorded in ``runtime.json``.

    Shutdown sequence:
      1. Graceful tree signal (skipped when ``force=True``)
      2. Wait up to ``timeout_s``
      3. Force-kill remaining process trees
      4. Verify PIDs exited
      5. Verify recorded listening ports released
      6. Clear ``runtime.json`` only when cleanup succeeds

    Uses process-group / Job-Object tree cleanup. Never raises.
    """

    root = Path(project_root).expanduser().resolve()
    parsed = read_runtime_payload(root)

    if parsed.missing:
        return StopResult(
            stopped_names=(),
            already_dead=(),
            message="No running StackPilot session.",
            exit_code=0,
        )

    if parsed.corrupted:
        clear_runtime_session(root)
        from .diagnostics.errors import format_corrupted_runtime

        return StopResult(
            stopped_names=(),
            already_dead=(),
            message=format_corrupted_runtime(cleared=True),
            exit_code=0,
        )

    stopped: List[str] = []
    already_dead: List[str] = []
    lines: List[str] = []
    tracked_pids: List[int] = []
    tracked_ports: List[int] = []
    grace = 0.05 if force else max(0.0, float(timeout_s))

    for raw in parsed.raw_services:
        name = str(raw.get("name") or "").strip() or "service"
        pid = raw.get("pid")
        status = str(raw.get("status") or "").strip().lower()
        port = raw.get("port")
        if isinstance(port, int) and port > 0:
            tracked_ports.append(port)

        alive = isinstance(pid, int) and pid_is_alive(pid)
        if not alive:
            # Still report services that looked running in the snapshot.
            if status == ServiceState.RUNNING.value or isinstance(pid, int):
                already_dead.append(name)
            continue

        assert isinstance(pid, int)
        tracked_pids.append(pid)
        lines.append(f"Stopping {name}...")
        try:
            if not force:
                signal_process_tree(pid, graceful=True)
                _wait_until_dead(pid, timeout_s=grace)
            if pid_is_alive(pid):
                signal_process_tree(pid, graceful=False)
                _wait_until_dead(pid, timeout_s=max(grace, DEFAULT_STOP_TIMEOUT_S))
        except Exception:
            # Best-effort: continue stopping other services.
            try:
                signal_process_tree(pid, graceful=False)
                _wait_until_dead(pid, timeout_s=DEFAULT_STOP_TIMEOUT_S)
            except Exception:
                pass
        stopped.append(name)

    # Final sweep — any still-live tracked PID gets a last force kill.
    remaining = [pid for pid in tracked_pids if pid_is_alive(pid)]
    for pid in remaining:
        try:
            signal_process_tree(pid, graceful=False)
        except Exception:
            pass
        _wait_until_dead(pid, timeout_s=DEFAULT_STOP_TIMEOUT_S)

    remaining = [pid for pid in tracked_pids if pid_is_alive(pid)]
    held_ports = _ports_still_held(tracked_ports, remaining)

    if remaining or held_ports:
        # Keep runtime.json so a follow-up stop can retry.
        message = format_cleanup_failure(remaining_pids=remaining)
        if lines:
            message = "\n".join([*lines, "", message])
        return StopResult(
            stopped_names=tuple(stopped),
            already_dead=tuple(already_dead),
            message=message,
            exit_code=1,
            remaining_pids=tuple(remaining),
        )

    clear_runtime_session(root)

    count = len(stopped)
    if count == 0 and not already_dead:
        message = "No running StackPilot session."
    elif count == 0:
        message = "No live StackPilot processes.\nCleared stale runtime status."
    else:
        summary = (
            f"Stopped {count} service{'s' if count != 1 else ''}.\n"
            "No orphan processes detected."
        )
        message = "\n".join([*lines, "", summary])

    return StopResult(
        stopped_names=tuple(stopped),
        already_dead=tuple(already_dead),
        message=message,
        exit_code=0,
        remaining_pids=(),
    )


def format_cleanup_failure(*, remaining_pids: Sequence[int]) -> str:
    """User-facing Problem / Reason / Suggested fix when stop leaves orphans."""

    from .diagnostics.errors import format_cleanup_failure as _format

    return _format(remaining_pids=remaining_pids)


def clear_runtime_session(project_root: Path) -> None:
    """Remove or neutralize ``runtime.json`` after stop / force recovery."""

    root = Path(project_root).expanduser().resolve()
    path = runtime_status_path(root)
    try:
        if path.is_file():
            # Prefer rewriting a clean inactive snapshot so status/ps stay useful.
            save_runtime_snapshot(
                root,
                {
                    "project": root.name,
                    "project_root": str(root),
                    "session_active": False,
                    "services": [],
                },
            )
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def detect_stale_session(
    project_root: Path,
    specs: Sequence[ServiceSpec] = (),
) -> Optional[StaleSession]:
    """
    Return a ``StaleSession`` when a previous StackPilot run left processes
    or still occupies configured ports recorded in ``runtime.json``.
    """

    root = Path(project_root).expanduser().resolve()
    parsed = read_runtime_payload(root)
    if parsed.missing:
        return None

    if parsed.corrupted:
        return StaleSession(
            reason="Previous session left a corrupted runtime status file.",
        )

    snapshot = load_runtime_snapshot(root)
    live: List[str] = []
    runtime_ports: List[int] = []

    services = []
    if snapshot and isinstance(snapshot.get("services"), list):
        services = [s for s in snapshot["services"] if isinstance(s, dict)]
    else:
        services = parsed.raw_services

    for raw in services:
        name = str(raw.get("name") or "").strip()
        pid = raw.get("pid")
        status = str(raw.get("status") or "").strip().lower()
        if isinstance(pid, int) and status == ServiceState.RUNNING.value:
            if pid_is_alive(pid):
                live.append(name or f"pid:{pid}")
        port = raw.get("port")
        if isinstance(port, int) and port > 0:
            runtime_ports.append(port)

    occupied: List[int] = []
    # Ports from the prior session that are still bound — likely orphans.
    for port in runtime_ports:
        if is_port_in_use(port, host="127.0.0.1") or is_port_in_use(
            port, host="0.0.0.0"
        ):
            if port not in occupied:
                occupied.append(port)

    # Also flag configured Stackfile ports that remain occupied when a prior
    # session was marked active (even if PIDs were lost).
    session_active = bool(
        (snapshot or parsed.snapshot or {}).get("session_active")
    )
    if session_active or live:
        for spec in specs:
            port = configured_port(spec)
            if port is None:
                continue
            if is_port_in_use(port, host="127.0.0.1") or is_port_in_use(
                port, host="0.0.0.0"
            ):
                if port not in occupied:
                    occupied.append(port)

    if live or occupied:
        return StaleSession(
            live_services=tuple(live),
            occupied_ports=tuple(occupied),
            reason="Previous session was not shut down cleanly.",
        )

    # No live leftovers — clear a stale active flag if present.
    if session_active and snapshot is not None:
        try:
            snapshot["session_active"] = False
            save_runtime_snapshot(root, snapshot)
        except Exception:
            pass
    return None


def format_stale_session_error(stale: StaleSession) -> str:
    """User-facing Problem / Reason / Suggested fix for a stale session."""

    extras: List[str] = []
    if stale.live_services:
        extras.append(
            "Live processes: " + ", ".join(stale.live_services)
        )
    if stale.occupied_ports:
        extras.append(
            "Occupied ports: "
            + ", ".join(str(p) for p in stale.occupied_ports)
        )
    extras.append("Or re-run with: stackpilot run --force")

    reason_l = (stale.reason or "").lower()
    if "corrupt" in reason_l:
        return format_user_error(
            problem="Corrupted runtime status",
            reason=stale.reason,
            suggested_fix="stackpilot stop",
            extra_lines=extras,
        )

    return format_user_error(
        problem="Existing StackPilot session detected.",
        reason=stale.reason,
        suggested_fix="stackpilot stop",
        extra_lines=extras,
    )


def _ports_still_held(ports: Sequence[int], remaining_pids: Sequence[int]) -> List[int]:
    """Return recorded ports that still have a live listener (orphans)."""

    held: List[int] = []
    remaining_set = {int(p) for p in remaining_pids}
    for port in ports:
        owners = pids_listening_on_port(int(port))
        if not owners:
            # Fallback bind probe when PID→port lookup is unavailable.
            if is_port_in_use(port, host="127.0.0.1") or is_port_in_use(
                port, host="0.0.0.0"
            ):
                # Only treat as held when we also failed to kill PIDs, or when
                # a listener exists with no matching remaining tracked PID
                # (true orphan listener). If all tracked PIDs are dead and a
                # foreign process took the port, do not fail stop.
                if remaining_set:
                    held.append(int(port))
            continue
        # A listener owned by a still-live tracked PID is a cleanup failure.
        if any(owner in remaining_set for owner in owners):
            held.append(int(port))
    return held


def _wait_until_dead(pid: int, *, timeout_s: float) -> None:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if not pid_is_alive(pid):
            return
        time.sleep(0.05)
