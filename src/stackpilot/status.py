"""Runtime status tracking and on-disk snapshot for CLI commands."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import ManagedService, ServiceState, configured_port
from .port_detect import listening_ports_for_pid, parse_port_from_command
from .dashboard import color_enabled, style_text

__all__ = [
    "ServiceRuntimeInfo",
    "RuntimeStatus",
    "RUNTIME_STATUS_FILE",
    "RUNTIME_FLUSH_INTERVAL_S",
    "STATUS_PROBE_TIMEOUT_S",
    "STATUS_PROBE_CACHE_TTL_S",
    "detect_framework",
    "detect_language",
    "derive_health",
    "format_uptime",
    "load_runtime_snapshot",
    "save_runtime_snapshot",
    "runtime_status_path",
    "format_status_report",
    "format_ps_table",
    "pid_is_alive",
    "probe_external_dependencies_status",
]

RUNTIME_STATUS_FILE = Path(".stackpilot") / "runtime.json"

# Minimum interval between unchanged runtime.json flushes (uptime refresh).
# Meaningful field changes (state / PID / status / port / issue count) write
# immediately; idle monitor polls must not hammer the disk every ~250 ms.
RUNTIME_FLUSH_INTERVAL_S = 1.5

# External dependency probes for ``stackpilot status`` (never block the table).
STATUS_PROBE_TIMEOUT_S = 0.25
STATUS_PROBE_CACHE_TTL_S = 5.0

_probe_cache: Dict[str, tuple[float, str]] = {}
_probe_cache_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class ServiceRuntimeInfo:
    """Snapshot of one service's runtime metadata."""

    name: str
    pid: Optional[int]
    status: ServiceState
    port: Optional[int]
    started_at: Optional[datetime]
    uptime: Optional[float]
    exit_code: Optional[int] = None
    framework: str = "-"
    language: str = "-"
    command: str = ""
    health: str = "stopped"


class RuntimeStatus:
    """
    Owns live runtime metadata for the current StackPilot session.

    ``Runner`` pushes updates; snapshots are also written under
    ``.stackpilot/runtime.json`` so ``stackpilot status`` / ``ps`` work
    from another terminal.
    """

    def __init__(self, *, project_root: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._by_name: Dict[str, ServiceRuntimeInfo] = {}
        self._order: List[str] = []
        self._stack_started_at: Optional[datetime] = None
        self._stack_started_mono: Optional[float] = None
        self._project_root = (
            Path(project_root).expanduser().resolve()
            if project_root is not None
            else None
        )
        self._project_name = (
            self._project_root.name if self._project_root is not None else "-"
        )
        self._session_active = False
        self._issue_tracker = None
        self._issue_states: Dict[str, ServiceState] = {}
        self._issue_count = 0
        self._last_persist_mono: float = 0.0
        self._last_persist_fingerprint: Optional[tuple] = None
        self._persist_writes = 0

    def set_issue_tracker(self, tracker) -> None:
        """Attach an IssueTracker for crash / recovery persistence hooks."""

        self._issue_tracker = tracker

    @property
    def project_root(self) -> Optional[Path]:
        return self._project_root

    @property
    def project_name(self) -> str:
        return self._project_name

    @property
    def stack_started_at(self) -> Optional[datetime]:
        return self._stack_started_at

    @property
    def startup_elapsed_s(self) -> Optional[float]:
        if self._stack_started_mono is None:
            return None
        return max(0.0, time.monotonic() - self._stack_started_mono)

    def set_project_root(self, root: Path) -> None:
        self._project_root = Path(root).expanduser().resolve()
        self._project_name = self._project_root.name

    def mark_stack_started(self, *, when: Optional[datetime] = None) -> None:
        with self._lock:
            self._stack_started_at = when or datetime.now(timezone.utc).astimezone()
            self._stack_started_mono = time.monotonic()
            self._session_active = True

    def mark_session_ended(self) -> None:
        with self._lock:
            self._session_active = False
            updated: Dict[str, ServiceRuntimeInfo] = {}
            for name, info in self._by_name.items():
                # Never invent STOPPED while a PID is still recorded — runtime
                # status must match process reality. Only flip RUNNING→STOPPED
                # when there is no live PID to contradict it.
                if info.status == ServiceState.RUNNING and info.pid is None:
                    updated[name] = ServiceRuntimeInfo(
                        name=info.name,
                        pid=None,
                        status=ServiceState.STOPPED,
                        port=info.port,
                        started_at=info.started_at,
                        uptime=None,
                        exit_code=info.exit_code,
                        framework=info.framework,
                        language=info.language,
                        command=info.command,
                        health=derive_health(ServiceState.STOPPED),
                    )
                else:
                    updated[name] = info
            self._by_name = updated
        self.persist(force=True)

    def register(
        self,
        name: str,
        *,
        port: Optional[int] = None,
        status: ServiceState = ServiceState.STOPPED,
        framework: str = "-",
        language: str = "-",
        command: str = "",
    ) -> None:
        with self._lock:
            if name not in self._by_name:
                self._order.append(name)
                fw = framework or "-"
                self._by_name[name] = ServiceRuntimeInfo(
                    name=name,
                    pid=None,
                    status=status,
                    port=port,
                    started_at=None,
                    uptime=None,
                    framework=fw,
                    language=language or detect_language(command, framework=fw),
                    command=command or "",
                    health=derive_health(status),
                )
            else:
                prev = self._by_name[name]
                new_status = prev.status
                fw = framework or prev.framework
                self._by_name[name] = ServiceRuntimeInfo(
                    name=prev.name,
                    pid=prev.pid,
                    status=new_status,
                    port=port if port is not None else prev.port,
                    started_at=prev.started_at,
                    uptime=prev.uptime,
                    exit_code=prev.exit_code,
                    framework=fw,
                    language=language
                    or prev.language
                    or detect_language(command or prev.command, framework=fw),
                    command=command or prev.command,
                    health=derive_health(new_status),
                )

    def register_specs(self, specs: Iterable) -> None:
        for spec in specs:
            cmd = str(getattr(spec, "command", "") or "")
            path = getattr(spec, "path", None)
            fw = detect_framework(cmd, path=path)
            self.register(
                spec.name,
                port=configured_port(spec),
                framework=fw,
                language=detect_language(cmd, framework=fw),
                command=cmd,
            )

    def sync_managed(self, managed: ManagedService) -> None:
        cmd = str(managed.spec.command or "")
        fw = detect_framework(cmd, path=managed.spec.path)
        info = ServiceRuntimeInfo(
            name=managed.name,
            pid=managed.pid,
            status=managed.status,
            port=managed.port,
            started_at=managed.started_at,
            uptime=managed.uptime,
            exit_code=managed.exit_code,
            framework=fw,
            language=detect_language(cmd, framework=fw),
            command=cmd,
            health=derive_health(managed.status),
        )
        with self._lock:
            if managed.name not in self._by_name:
                self._order.append(managed.name)
            self._by_name[managed.name] = info
        self._notify_issue_tracker(info)
        self.persist()

    def sync_all(self, services: Sequence[ManagedService]) -> None:
        synced: List[ServiceRuntimeInfo] = []
        for managed in services:
            cmd = str(managed.spec.command or "")
            fw = detect_framework(cmd, path=managed.spec.path)
            info = ServiceRuntimeInfo(
                name=managed.name,
                pid=managed.pid,
                status=managed.status,
                port=managed.port,
                started_at=managed.started_at,
                uptime=managed.uptime,
                exit_code=managed.exit_code,
                framework=fw,
                language=detect_language(cmd, framework=fw),
                command=cmd,
                health=derive_health(managed.status),
            )
            with self._lock:
                if managed.name not in self._by_name:
                    self._order.append(managed.name)
                self._by_name[managed.name] = info
            synced.append(info)
        for info in synced:
            self._notify_issue_tracker(info)
        self.persist()

    def _notify_issue_tracker(self, info: ServiceRuntimeInfo) -> None:
        """
        Persist crash / recovery signals without changing orchestration.

        FAILED → ensure a crash issue exists (stderr may already have one).
        FAILED → RUNNING → mark ACTIVE issues FIXED (1h delayed row removal).

        Steady RUNNING polls must not clear stderr issues while the process
        is still up — only a recovery transition marks FIXED.
        """

        tracker = self._issue_tracker
        if tracker is None:
            return
        previous = self._issue_states.get(info.name)
        self._issue_states[info.name] = info.status
        try:
            if info.status == ServiceState.FAILED and previous != ServiceState.FAILED:
                # Stderr usually already recorded the real error. Only add a
                # generic crash row when nothing ACTIVE exists yet.
                if not tracker.has_active(info.name):
                    tracker.record_error(
                        info.name,
                        root_cause="Service crashed",
                        exit_code=info.exit_code,
                    )
            elif (
                info.status == ServiceState.RUNNING
                and previous == ServiceState.FAILED
            ):
                tracker.mark_fixed(info.name)
            tracker.cleanup()
        except Exception:
            # Issue persistence must never break the runtime session.
            pass

    def get(self, name: str) -> ServiceRuntimeInfo:
        with self._lock:
            try:
                return self._by_name[name]
            except KeyError as exc:
                raise KeyError(f"Unknown service: {name}") from exc

    def services(self) -> tuple[ServiceRuntimeInfo, ...]:
        with self._lock:
            return tuple(self._by_name[n] for n in self._order if n in self._by_name)

    def running_count(self) -> int:
        return sum(1 for s in self.services() if s.status == ServiceState.RUNNING)

    def persist(self, *, force: bool = False) -> None:
        """
        Best-effort write of ``.stackpilot/runtime.json``.

        Writes immediately when meaningful service fields change (state, PID,
        status, port, issue count, session flag). Otherwise flushes at most
        once per ``RUNTIME_FLUSH_INTERVAL_S`` so monitor polls do not create
        unnecessary disk IO. ``force=True`` always writes (final session state).

        Never raises: filesystem failures are logged as a warning and the
        orchestrator continues. Running services outrank metadata durability.
        """

        root = self._project_root
        if root is None:
            return
        path = runtime_status_path(root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            issue_count = self._refresh_issue_count()
            with self._lock:
                snapshot = tuple(
                    self._by_name[n] for n in self._order if n in self._by_name
                )
                session_active = self._session_active
                fingerprint = _runtime_fingerprint(
                    session_active=session_active,
                    issue_count=issue_count,
                    services=snapshot,
                )
                now = time.monotonic()
                if not force:
                    unchanged = fingerprint == self._last_persist_fingerprint
                    within_interval = (
                        now - self._last_persist_mono
                    ) < RUNTIME_FLUSH_INTERVAL_S
                    if unchanged and within_interval:
                        return
                payload = {
                    "project": self._project_name,
                    "project_root": str(root),
                    "updated_at": datetime.now(timezone.utc).astimezone().isoformat(),
                    "session_active": session_active,
                    "services": [_service_to_dict(s) for s in snapshot],
                }
            _write_runtime_payload(path, payload)
            with self._lock:
                self._last_persist_mono = time.monotonic()
                self._last_persist_fingerprint = fingerprint
                self._persist_writes += 1
        except (PermissionError, FileExistsError, OSError, IOError) as exc:
            # Catch PermissionError / FileExistsError / IOError explicitly for
            # clarity; on modern Python they are OSError subclasses. Windows
            # sharing/locking errors surface as OSError (e.g. WinError 32).
            _warn_runtime_persist_failure(exc, path)

    def _refresh_issue_count(self) -> int:
        """Refresh cached ACTIVE issue count (no status lock held)."""

        tracker = self._issue_tracker
        if tracker is None:
            self._issue_count = 0
            return 0
        try:
            count = len(tracker.list_issues(status="ACTIVE"))
        except Exception:
            count = self._issue_count
        self._issue_count = count
        return count


def runtime_status_path(project_root: Path) -> Path:
    return Path(project_root).expanduser().resolve() / RUNTIME_STATUS_FILE


def load_runtime_snapshot(project_root: Path) -> Optional[Dict[str, Any]]:
    path = runtime_status_path(project_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # Refresh liveness for PIDs so stale sessions don't look alive.
    services = data.get("services")
    if isinstance(services, list):
        any_alive = False
        for raw in services:
            if not isinstance(raw, dict):
                continue
            pid = raw.get("pid")
            status = str(raw.get("status") or "")
            if isinstance(pid, int) and status == ServiceState.RUNNING.value:
                if pid_is_alive(pid):
                    any_alive = True
                    started = raw.get("started_at")
                    raw["uptime"] = _uptime_from_iso(started)
                    # Prefer the process's actual listen port when missing.
                    # Skip the subprocess probe when a port is already known —
                    # status/ps merge would otherwise double the cost.
                    if raw.get("port") is None:
                        live = listening_ports_for_pid(pid)
                        if live:
                            raw["port"] = live[0]
                        else:
                            raw["port"] = parse_port_from_command(
                                str(raw.get("command") or "")
                            )
                else:
                    raw["status"] = ServiceState.STOPPED.value
                    raw["pid"] = None
                    raw["uptime"] = None
            elif status == ServiceState.RUNNING.value and not isinstance(pid, int):
                raw["status"] = ServiceState.STOPPED.value
                raw["uptime"] = None
            raw["health"] = derive_health(str(raw.get("status") or ""))
        if not any_alive:
            data["session_active"] = False
    return data


def save_runtime_snapshot(project_root: Path, snapshot: Dict[str, Any]) -> None:
    """
    Persist a runtime snapshot dict to ``.stackpilot/runtime.json``.

    Best-effort only: never raises on filesystem errors.
    """

    root = Path(project_root).expanduser().resolve()
    path = runtime_status_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(snapshot)
        payload.setdefault("project", root.name)
        payload.setdefault("project_root", str(root))
        payload["updated_at"] = datetime.now(timezone.utc).astimezone().isoformat()
        services = payload.get("services")
        if isinstance(services, list):
            for raw in services:
                if isinstance(raw, dict):
                    raw["health"] = derive_health(str(raw.get("status") or ""))
        _write_runtime_payload(path, payload)
    except (PermissionError, FileExistsError, OSError, IOError) as exc:
        _warn_runtime_persist_failure(exc, path)


def derive_health(status: ServiceState | str) -> str:
    """Map a service status to a DX health label."""

    value = status.value if isinstance(status, ServiceState) else str(status or "")
    if value == ServiceState.RUNNING.value:
        return "healthy"
    if value == ServiceState.FAILED.value:
        return "unhealthy"
    if value == ServiceState.STARTING.value:
        return "starting"
    return "stopped"


def detect_framework(
    command: str | Sequence[str],
    *,
    path: Optional[Path | str] = None,
) -> str:
    """
    Detect a runtime framework label from a launch command.

    Node launchers (``npm``, ``node``, ``pnpm``, …) never return ``"-"`` —
    they resolve to ``express`` / ``nestjs`` / ``node``. Optional ``path``
    enables adapter-based NestJS vs Express disambiguation.
    """

    if isinstance(command, (list, tuple)):
        tokens = [str(p).lower() for p in command]
        text = " ".join(tokens)
    else:
        text = str(command or "").strip().lower()
        tokens = text.split()
    if not text:
        return "-"
    if "uvicorn" in tokens or "uvicorn" in text:
        return "uvicorn"
    if "gunicorn" in tokens or "gunicorn" in text:
        return "gunicorn"
    if "fastapi" in text:
        return "fastapi"
    if "flask" in tokens or "flask" in text:
        return "flask"
    if "django" in text or "manage.py" in text:
        return "django"
    if "celery" in text:
        return "celery"
    if "nest" in text:
        return "nestjs"
    if "express" in text:
        return "express"
    if any(
        token in text
        for token in ("npm", "npx", "node", "pnpm", "yarn", "bun")
    ):
        label = _adapter_framework_from_path(path)
        if label in {"nestjs", "express"}:
            return label
        return "node"
    if tokens and Path(tokens[0]).name.startswith("python"):
        return "python"
    if "python" in tokens:
        return "python"
    return "-"


def detect_language(
    command: str | Sequence[str],
    *,
    framework: str = "",
) -> str:
    """Map a command / framework to a language label for status / ps / graph."""

    fw = (framework or detect_framework(command)).strip().lower()
    if fw in {
        "fastapi",
        "flask",
        "django",
        "uvicorn",
        "gunicorn",
        "celery",
        "python",
    }:
        return "Python"
    if fw == "nestjs":
        return "TypeScript"
    if fw in {"express", "node"}:
        return "JavaScript"

    if isinstance(command, (list, tuple)):
        text = " ".join(str(p).lower() for p in command)
    else:
        text = str(command or "").strip().lower()
    if not text:
        return "-"
    if any(
        token in text
        for token in ("uvicorn", "gunicorn", "fastapi", "flask", "django", "python")
    ):
        return "Python"
    if any(
        token in text
        for token in ("npm", "npx", "node", "pnpm", "yarn", "bun", "express", "nest")
    ):
        return "JavaScript"
    return "-"


def _adapter_framework_from_path(path: Optional[Path | str]) -> str:
    """Best-effort NestJS/Express label from a service directory."""

    if path is None or path == "":
        return ""
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return ""
    if not resolved.is_dir():
        return ""
    try:
        from .adapters import default_registry

        adapter = default_registry.match(resolved)
        if adapter is None or getattr(adapter, "external", False):
            return ""
        return str(adapter.name or "").strip().lower()
    except Exception:
        return ""


def format_uptime(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def format_status_report(
    *,
    project_name: str,
    services: Sequence[Dict[str, Any]],
    session_active: bool,
    external_dependencies: Sequence[Dict[str, Any]] | None = None,
    color: bool | None = None,
) -> str:
    use_color = color_enabled(force=color)
    running = sum(1 for s in services if s.get("status") == ServiceState.RUNNING.value)
    failed = sum(1 for s in services if s.get("status") == ServiceState.FAILED.value)
    healthy = sum(
        1
        for s in services
        if str(s.get("health") or derive_health(str(s.get("status") or "")))
        == "healthy"
    )

    lines = [
        f"Project: {project_name}",
        f"Running: {running}  Healthy: {healthy}  Failed: {failed}",
        "",
    ]
    if not session_active and running == 0:
        lines.append("StackPilot is not running.")
        lines.append("Start with: stackpilot run")
        lines.append("")

    lines.append("Applications")
    lines.append("-" * 12)
    headers = (
        "SERVICE",
        "STATUS",
        "PID",
        "PORT",
        "UPTIME",
        "HEALTH",
        "FRAMEWORK",
        "LANGUAGE",
    )
    rows: List[tuple[str, ...]] = []
    for svc in services:
        status = str(svc.get("status") or "-")
        framework = str(svc.get("framework") or "-")
        language = str(
            svc.get("language")
            or detect_language(str(svc.get("command") or ""), framework=framework)
            or "-"
        )
        health = str(
            svc.get("health") or derive_health(status) or "-"
        )
        rows.append(
            (
                str(svc.get("name") or "-"),
                _colorize_status_label(status, use_color=use_color),
                "-" if svc.get("pid") is None else str(svc.get("pid")),
                "-" if svc.get("port") is None else str(svc.get("port")),
                format_uptime(svc.get("uptime")),
                _colorize_health_label(health, use_color=use_color),
                framework,
                language,
            )
        )
    lines.append(_format_table(headers, rows))

    externals = list(external_dependencies or ())
    if externals:
        lines.append("")
        lines.append("External Dependencies")
        lines.append("-" * 21)
        ext_headers = ("NAME", "TYPE", "HOST", "PORT", "STATUS")
        ext_rows: List[tuple[str, ...]] = []
        for dep in externals:
            ext_status = str(dep.get("status") or "-")
            ext_rows.append(
                (
                    str(dep.get("name") or "-"),
                    str(dep.get("type") or "-"),
                    str(dep.get("host") or "-"),
                    "-" if dep.get("port") is None else str(dep.get("port")),
                    _colorize_external_status(ext_status, use_color=use_color),
                )
            )
        lines.append(_format_table(ext_headers, ext_rows))

    return "\n".join(lines).rstrip() + "\n"


def format_ps_table(
    services: Sequence[Dict[str, Any]],
    *,
    color: bool | None = None,
) -> str:
    use_color = color_enabled(force=color)
    active = [
        s
        for s in services
        if s.get("status") == ServiceState.RUNNING.value and s.get("pid") is not None
    ]
    if not active:
        return "No active StackPilot processes.\n"
    headers = (
        "SERVICE",
        "PID",
        "PORT",
        "STATUS",
        "HEALTH",
        "FRAMEWORK",
        "LANGUAGE",
    )
    rows = []
    for s in active:
        framework = str(s.get("framework") or "-")
        language = str(
            s.get("language")
            or detect_language(str(s.get("command") or ""), framework=framework)
            or "-"
        )
        status = str(s.get("status") or "-")
        health = str(s.get("health") or derive_health(status) or "-")
        rows.append(
            (
                str(s.get("name") or "-"),
                str(s.get("pid")),
                "-" if s.get("port") is None else str(s.get("port")),
                _colorize_status_label(status, use_color=use_color),
                _colorize_health_label(health, use_color=use_color),
                framework,
                language,
            )
        )
    return _format_table(headers, rows) + "\n"


def probe_external_dependencies_status(
    deps: Sequence[Any],
    *,
    timeout_s: float = STATUS_PROBE_TIMEOUT_S,
    cache_ttl_s: float = STATUS_PROBE_CACHE_TTL_S,
) -> List[Dict[str, Any]]:
    """
    Probe external dependencies in parallel for ``stackpilot status``.

    Short timeouts + a small TTL cache keep status under ~1s even when one
    Redis/Postgres endpoint is down. Cached misses are labeled ``stale``.
    """

    from concurrent.futures import ThreadPoolExecutor, as_completed

    deps_list = list(deps)
    if not deps_list:
        return []

    results: Dict[str, Dict[str, Any]] = {}

    def _one(dep: Any) -> Dict[str, Any]:
        key = f"{getattr(dep, 'name', '')}|{getattr(dep, 'host', '')}|{getattr(dep, 'port', '')}"
        now = time.monotonic()
        with _probe_cache_lock:
            cached = _probe_cache.get(key)
        try:
            ok = _quick_external_probe(dep, timeout_s=timeout_s)
            status = "reachable" if ok else "unreachable"
            with _probe_cache_lock:
                _probe_cache[key] = (now, status)
            return {
                "name": getattr(dep, "name", "-"),
                "type": getattr(dep, "type", "-"),
                "host": getattr(dep, "host", "-"),
                "port": getattr(dep, "port", None),
                "status": status,
            }
        except Exception:
            if cached is not None and (now - cached[0]) <= cache_ttl_s * 4:
                return {
                    "name": getattr(dep, "name", "-"),
                    "type": getattr(dep, "type", "-"),
                    "host": getattr(dep, "host", "-"),
                    "port": getattr(dep, "port", None),
                    "status": f"{cached[1]} (stale)",
                }
            return {
                "name": getattr(dep, "name", "-"),
                "type": getattr(dep, "type", "-"),
                "host": getattr(dep, "host", "-"),
                "port": getattr(dep, "port", None),
                "status": "unreachable",
            }

    # Serve fresh cache hits without spawning workers.
    pending: list[Any] = []
    now = time.monotonic()
    for dep in deps_list:
        key = f"{getattr(dep, 'name', '')}|{getattr(dep, 'host', '')}|{getattr(dep, 'port', '')}"
        with _probe_cache_lock:
            cached = _probe_cache.get(key)
        if cached is not None and (now - cached[0]) <= cache_ttl_s:
            results[getattr(dep, "name", key)] = {
                "name": getattr(dep, "name", "-"),
                "type": getattr(dep, "type", "-"),
                "host": getattr(dep, "host", "-"),
                "port": getattr(dep, "port", None),
                "status": cached[1],
            }
        else:
            pending.append(dep)

    if pending:
        workers = min(len(pending), 32)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_one, dep): dep for dep in pending}
            for future in as_completed(futures):
                row = future.result()
                results[str(row.get("name") or "")] = row

    ordered: List[Dict[str, Any]] = []
    for dep in deps_list:
        name = getattr(dep, "name", "")
        ordered.append(
            results.get(
                name,
                {
                    "name": name,
                    "type": getattr(dep, "type", "-"),
                    "host": getattr(dep, "host", "-"),
                    "port": getattr(dep, "port", None),
                    "status": "unreachable",
                },
            )
        )
    return ordered


def _quick_external_probe(dep: Any, *, timeout_s: float) -> bool:
    """Single short TCP/HTTP probe for status (never uses full startup retries)."""

    from .config import HttpHealthCheck, TcpHealthCheck
    from .http_checker import check_http
    from .tcp_checker import check_tcp

    check = getattr(dep, "health_check", None)
    if isinstance(check, HttpHealthCheck) and check.url:
        return bool(check_http(check.url, request_timeout=timeout_s))
    host = getattr(dep, "host", "127.0.0.1")
    port = getattr(dep, "port", None)
    if isinstance(check, TcpHealthCheck):
        host = check.host or host
        port = check.port if check.port is not None else port
    if port is None:
        return False
    return check_tcp(str(host), int(port), connect_timeout=timeout_s)


def pid_is_alive(pid: int) -> bool:
    """Return True when ``pid`` refers to a live process (cross-platform)."""

    if pid <= 0:
        return False

    if sys.platform == "win32":
        # ``os.kill(pid, 0)`` is unreliable on Windows (often WinError 87).
        # OpenProcess can still succeed for a terminated process while any
        # handle remains open (e.g. a local ``Popen``), so check the exit
        # code: STILL_ACTIVE (259) means the process has not exited.
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            # Access denied still means the process object exists; treat as alive.
            return ctypes.GetLastError() == 5

        exit_code = wintypes.DWORD()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(
            handle, ctypes.byref(exit_code)
        )
        ctypes.windll.kernel32.CloseHandle(handle)
        if not ok:
            return True
        return int(exit_code.value) == STILL_ACTIVE

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _visible_width(cell))
    head = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = [
        "  ".join(_pad_visible(str(cell), widths[i]) for i, cell in enumerate(row))
        for row in rows
    ]
    return "\n".join([head, sep, *body]) if body else "\n".join([head, sep])


_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _visible_width(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def _pad_visible(text: str, width: int) -> str:
    pad = max(0, width - _visible_width(text))
    return text + (" " * pad)


def _colorize_status_label(status: str, *, use_color: bool) -> str:
    if not use_color:
        return status
    key = status.strip().lower()
    if key in {ServiceState.RUNNING.value, "reloading", "reload"}:
        return style_text(status, fg="green")
    if key == ServiceState.STARTING.value or key == "restarting":
        return style_text(status, fg="yellow")
    if key == ServiceState.FAILED.value:
        return style_text(status, fg="red")
    if key == ServiceState.STOPPED.value:
        return style_text(status, fg="bright_black")
    if key in {"external", "reachable"}:
        return style_text(status, fg="cyan")
    return status


def _colorize_health_label(health: str, *, use_color: bool) -> str:
    if not use_color:
        return health
    key = health.strip().lower()
    if key == "healthy":
        return style_text(health, fg="green")
    if key in {"unhealthy", "failed"}:
        return style_text(health, fg="red")
    if key in {"warning", "starting", "degraded"}:
        return style_text(health, fg="yellow")
    if key == "stopped":
        return style_text(health, fg="bright_black")
    return health


def _colorize_external_status(status: str, *, use_color: bool) -> str:
    if not use_color:
        return status
    key = status.strip().lower()
    if key.startswith("reachable"):
        return style_text(status, fg="cyan")
    if "stale" in key:
        return style_text(status, fg="yellow")
    if key.startswith("unreachable"):
        return style_text(status, fg="red")
    return style_text(status, fg="cyan")


def _service_to_dict(info: ServiceRuntimeInfo) -> Dict[str, Any]:
    status = (
        info.status.value if isinstance(info.status, ServiceState) else str(info.status)
    )
    return {
        "name": info.name,
        "pid": info.pid,
        "port": info.port,
        "status": status,
        "started_at": info.started_at.isoformat() if info.started_at else None,
        "uptime": info.uptime,
        "exit_code": info.exit_code,
        "framework": info.framework,
        "language": info.language or detect_language(info.command, framework=info.framework),
        "command": info.command,
        "health": info.health or derive_health(status),
    }


def _runtime_fingerprint(
    *,
    session_active: bool,
    issue_count: int,
    services: Sequence[ServiceRuntimeInfo],
) -> tuple:
    """Identity of fields that must trigger an immediate runtime.json write."""

    return (
        session_active,
        issue_count,
        tuple(
            (
                s.name,
                s.status.value if isinstance(s.status, ServiceState) else str(s.status),
                s.pid,
                s.port,
                s.exit_code,
                s.health,
            )
            for s in services
        ),
    )


def _uptime_from_iso(value: Any) -> Optional[float]:
    if not value or not isinstance(value, str):
        return None
    try:
        started = datetime.fromisoformat(value)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc).astimezone()
    return max(0.0, (now - started.astimezone()).total_seconds())


def _write_runtime_payload(path: Path, payload: Dict[str, Any]) -> None:
    """
    Atomically write ``runtime.json`` via a unique temp file.

    A fixed ``runtime.tmp`` name is prone to Windows sharing/permission issues
    when a stale temp file is left locked or read-only by another process.
    """

    body = json.dumps(payload, indent=2)
    fd: int | None = None
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{path.stem}.",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(body)
        os.replace(tmp_name, path)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


# Warn once per distinct failure detail so monitor-loop persist retries
# do not flood the console every poll interval.
_persist_warn_seen: set[str] = set()
_persist_warn_lock = threading.Lock()


def _concise_fs_error(exc: BaseException, path: Path) -> str:
    """Build a short ``reason: filename`` line for persistence warnings."""

    filename = getattr(exc, "filename", None)
    hint = Path(filename).name if filename else path.with_suffix(".tmp").name
    if hint.endswith(".tmp") and hint.startswith(f"{path.stem}."):
        hint = path.with_suffix(".tmp").name
    strerror = getattr(exc, "strerror", None)
    if strerror:
        return f"{strerror}: {hint}"
    text = str(exc).strip() or type(exc).__name__
    # Prefer trailing ``: path`` forms already present in OSError strings.
    if hint and hint not in text:
        return f"{text}: {hint}"
    return text


def _warn_runtime_persist_failure(exc: BaseException, path: Path) -> None:
    detail = _concise_fs_error(exc, path)
    with _persist_warn_lock:
        if detail in _persist_warn_seen:
            return
        _persist_warn_seen.add(detail)
    from .dashboard import print_safe

    print_safe(
        "Warning:\n"
        "Unable to update runtime status:\n"
        f"{detail}\n\n"
        "Continuing without runtime persistence.",
        ascii_fallback=(
            "Warning:\n"
            "Unable to update runtime status:\n"
            f"{detail}\n\n"
            "Continuing without runtime persistence."
        ),
    )
