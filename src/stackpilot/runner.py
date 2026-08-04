"""Service execution — start / stop / restart / monitor an ordered service list."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urlparse

from .adapters.detect.health_probe import (
    format_health_diagnostic,
    path_from_url,
)
from .adapters.detect.health_routes import discover_routes
from .config import HttpHealthCheck, ServiceSpec, Stack
from .dashboard import (
    ascii_fallback_dx,
    color_enabled,
    format_application_logs_banner,
    format_runtime_summary,
    format_ready_urls,
    format_shutdown_summary,
    format_wave_header,
    format_crash_report,
    print_safe,
    style_text,
)
from .dependency_graph import DependencyGraph
from .diagnostics.errors import (
    format_cleanup_failure,
    format_health_http_failure,
    format_health_timeout,
    format_port_already_in_use,
    format_spawn_failure,
)
from .health import Health, HealthCheckTimeout, PortOwnershipError
from .http_checker import probe_http
from .launch_env import (
    TracebackSummary,
    compare_launch_plans,
    expected_launch_plan,
    format_startup_failure_report,
    actual_launch_plan,
)
from .logger import Logger
from .models import ServiceState
from .port_detect import service_display_url
from .process_manager import ProcessManager
from .status import RuntimeStatus
from .tcp_checker import check_tcp
from .watch_manager import WatchManager
from .watcher import DEFAULT_DEBOUNCE_S, format_changed_paths


def _safe_print(message: str, *, ascii_fallback: str) -> None:
    """Print ``message``, falling back when the console encoding cannot."""

    print_safe(message, ascii_fallback=ascii_fallback)


def _print_reload_detected(paths_display: str) -> None:
    """Print the change-detection block for hot reload (TTY-aware color)."""

    header = "Detected change:"
    if color_enabled():
        header = style_text(header, fg="yellow", bold=True)
    print_safe(header, ascii_fallback=ascii_fallback_dx("Detected change:"))
    for raw in paths_display.split(", "):
        path = raw.strip().strip("'\"")
        if not path:
            continue
        print_safe(path, ascii_fallback=path)


class Runner:
    """
    Coordinate execution of already-ordered services.

    Does not load Stackfiles, validate configuration, or build dependency
    graphs. Those concerns belong to ``Orchestrator``. ``run(stack)`` remains
    as a thin public entry that delegates to ``Orchestrator`` for backwards
    compatibility.
    """

    def __init__(
        self,
        *,
        logs_dir: Optional[Path] = None,
        poll_interval_s: float = 0.25,
        reload_debounce_s: float = DEFAULT_DEBOUNCE_S,
    ) -> None:
        self._logs_dir = logs_dir
        self._poll_interval_s = poll_interval_s
        self._reload_debounce_s = reload_debounce_s

        self._manager: Optional[ProcessManager] = None
        self._graph: Optional[DependencyGraph] = None
        self._watch_manager: Optional[WatchManager] = None
        self._logger: Optional[Logger] = None
        self._status = RuntimeStatus()
        self._project_root: Optional[Path] = None
        self._ordered: tuple[ServiceSpec, ...] = ()
        self._reload_locks: dict[str, threading.Lock] = {}
        self._reload_gate = threading.Lock()
        self._reloading: set[str] = set()
        self._reload_pending: set[str] = set()
        self._reloading_lock = threading.Lock()
        self._startup_began_mono: Optional[float] = None
        self._shutting_down = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False
        self._in_reload_cycle = False

    # ------------------------------------------------------------------
    # Public lifecycle (Orchestrator entry + compat)
    # ------------------------------------------------------------------

    def run(self, stack: Stack, *, target: Optional[str] = None) -> int:
        """
        Backwards-compatible entry point.

        Delegates to ``Orchestrator`` so callers that still construct
        ``Runner`` directly keep the previous behaviour. Prefer
        ``Orchestrator().run(stack)`` for new code (including the CLI).
        """

        from .orchestrator import Orchestrator

        return Orchestrator(
            logs_dir=self._logs_dir,
            poll_interval_s=self._poll_interval_s,
            reload_debounce_s=self._reload_debounce_s,
            runner=self,
        ).run(stack, target=target, project_root=self._project_root)

    def bind(
        self,
        *,
        manager: ProcessManager,
        graph: Optional[DependencyGraph],
        watch_manager: WatchManager,
        project_root: Path,
        ordered: Sequence[ServiceSpec],
        logger: Optional[Logger] = None,
    ) -> None:
        """Attach runtime collaborators created by ``Orchestrator``."""

        self._manager = manager
        self._graph = graph
        self._watch_manager = watch_manager
        self._logger = logger
        self._project_root = project_root
        self._ordered = tuple(ordered)
        self._reload_locks = {spec.name: threading.Lock() for spec in ordered}
        self._status = RuntimeStatus(project_root=project_root)
        self._status.register_specs(ordered)
        self._status.mark_stack_started()
        self._status.persist()
        self._startup_began_mono = time.monotonic()
        self._shutting_down = False
        self._shutdown_started = False

    def unbind(self) -> None:
        """Clear runtime collaborators after a run completes."""

        try:
            self._status.mark_session_ended()
        except Exception:
            pass
        self._manager = None
        self._graph = None
        self._watch_manager = None
        self._logger = None
        self._project_root = None
        self._ordered = ()
        self._reload_locks = {}
        self._startup_began_mono = None
        with self._reloading_lock:
            self._reloading.clear()
            self._reload_pending.clear()
        self._in_reload_cycle = False

    def begin_shutdown(self) -> None:
        """
        Disable hot reload before tearing down processes / watchers.

        Safe to call multiple times. In-flight ``on_reload`` callbacks that have
        not yet acquired their per-service lock will no-op.
        """

        self._shutting_down = True
        with self._reloading_lock:
            self._reload_pending.clear()
        watch_manager = self._watch_manager
        if watch_manager is not None:
            # Cancel pending debounce timers so callbacks cannot fire mid-stop.
            for name in list(watch_manager.watched_services):
                watcher = watch_manager.get_watcher(name)
                if watcher is not None:
                    try:
                        watcher.handler.cancel()
                    except Exception:
                        pass

    @property
    def is_bound(self) -> bool:
        return self._manager is not None

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    @property
    def project_root(self) -> Path:
        return self._project_root or Path.cwd()

    @property
    def status(self) -> RuntimeStatus:
        """Live runtime metadata for the current session."""

        return self._status

    # ------------------------------------------------------------------
    # Per-service execution
    # ------------------------------------------------------------------

    def start(self, spec: ServiceSpec) -> bool:
        """
        Start one service and wait until it is healthy.

        Returns False when the health check times out (caller decides abort).
        """

        manager = self._require_manager()
        print_safe(f"Starting {spec.name}...", ascii_fallback=f"Starting {spec.name}...")
        try:
            managed = manager.start(spec)
        except (OSError, FileNotFoundError, PermissionError, ValueError) as exc:
            message = format_spawn_failure(
                service=spec.name,
                exc=exc,
                command=str(spec.command),
                cwd=Path(spec.path),
            )
            print_safe(message, ascii_fallback=ascii_fallback_dx(message))
            self._status.sync_managed(manager.get(spec.name))
            return False
        self._status.sync_managed(managed)
        print_safe(f"Waiting for {spec.name}...", ascii_fallback=f"Waiting for {spec.name}...")
        try:
            elapsed = Health.wait_until_healthy(
                spec.name,
                spec.health_check,
                process=managed.process,
            )
        except PortOwnershipError as exc:
            friendly = format_port_already_in_use(
                port=exc.port,
                service=spec.name,
            )
            print_safe(friendly, ascii_fallback=ascii_fallback_dx(friendly))
            self._status.sync_managed(manager.get(spec.name))
            return False
        except HealthCheckTimeout:
            # Drain stderr/stdout until EOF so the full traceback is visible
            # before we print diagnostics or tear siblings down.
            self._await_service_output(manager, spec.name, managed.process)
            _safe_print(
                f"❌ {spec.name} failed health check",
                ascii_fallback=f"X {spec.name} failed health check",
            )
            timeout_s = 0.0
            health_url = ""
            if spec.health_check is not None:
                timeout_s = float(getattr(spec.health_check, "timeout", 0.0) or 0.0)
                if isinstance(spec.health_check, HttpHealthCheck):
                    health_url = spec.health_check.url
            try:
                friendly = format_health_timeout(
                    service=spec.name,
                    health_url=health_url,
                    timeout_s=timeout_s,
                )
                print_safe(friendly, ascii_fallback=ascii_fallback_dx(friendly))
            except Exception:
                pass
            try:
                self._print_health_failure(spec, process=managed.process)
            except Exception:
                pass
            try:
                self._print_startup_failure(spec.name)
            except Exception:
                pass
            self._status.sync_managed(manager.get(spec.name))
            return False

        _safe_print(
            f"✓ {spec.name} healthy ({elapsed:.1f}s)",
            ascii_fallback=f"+ {spec.name} healthy ({elapsed:.1f}s)",
        )
        self._status.sync_managed(manager.get(spec.name))
        return True

    def stop(self, name: str) -> None:
        """Stop one service by name."""

        managed = self._require_manager().stop(name)
        self._status.sync_managed(managed)

    def restart(self, service: str | ServiceSpec) -> bool:
        """Restart one service and wait for its health check."""

        manager = self._require_manager()
        if isinstance(service, ServiceSpec):
            spec = service
        else:
            try:
                spec = manager.get(service).spec
            except KeyError:
                return False
        return self._restart_with_health(manager, spec)

    def start_all(self, ordered: Sequence[ServiceSpec]) -> bool:
        """
        Start services in dependency-safe parallel waves.

        Independent services (no mutual depends_on edges among the remaining
        set) start concurrently. Dependents never start until their
        dependencies in earlier waves are healthy.

        Returns False when a health check times out or spawn fails (startup
        aborted). On any failure after at least one successful start,
        already-started services are stopped so no orphans remain.
        """

        manager = self._require_manager()
        logger = self._logger
        if logger is not None:
            logger.begin_startup_buffer()

        print_safe(
            "Starting application services...",
            ascii_fallback="Starting application services...",
        )

        by_name = {spec.name: spec for spec in ordered}
        ok = True
        try:
            waves = self._startup_waves([spec.name for spec in ordered])
            show_waves = len(waves) > 1 or len(ordered) > 1
            for wave_index, wave_names in enumerate(waves, start=1):
                wave_specs = [by_name[name] for name in wave_names if name in by_name]
                if not wave_specs:
                    continue
                if show_waves:
                    banner = format_wave_header(
                        wave_index, [spec.name for spec in wave_specs]
                    )
                    print_safe(banner, ascii_fallback=ascii_fallback_dx(banner))
                started, failed = self._start_wave(wave_specs)
                if failed:
                    print_safe("Startup aborted.", ascii_fallback="Startup aborted.")
                    ok = False
                    break
                del started  # reserved for future DX timing
        except BaseException:
            ok = False
            raise
        finally:
            if not ok:
                # Any abort path (health timeout, spawn error, KeyboardInterrupt,
                # dependency/init failure) must tear down siblings that started.
                try:
                    self._stop_started(manager)
                except Exception:
                    pass
                self._flush_startup_logs(banner=False)

        if ok:
            self._refresh_status()
            self._print_startup_summary(ordered)
            self._print_ready_urls(ordered)
            self._flush_startup_logs(banner=True)
        return ok
    def print_watch_ready(self, *, watching: bool = True) -> None:
        """
        Print the post-startup watch banner.

        Called by ``Orchestrator`` only after ``WatchManager.start`` so the
        message cannot appear before observers are live. When no watchers
        were started, only the Ctrl+C hint is shown.
        """

        if watching:
            print_safe(
                "Watching for changes...",
                ascii_fallback="Watching for changes...",
            )
        print_safe("Press Ctrl+C to stop.", ascii_fallback="Press Ctrl+C to stop.")

    def _print_startup_summary(self, ordered: Sequence[ServiceSpec]) -> None:
        """Print a one-line runtime summary after a successful start."""

        began = self._startup_began_mono
        if began is None:
            return
        elapsed = max(0.0, time.monotonic() - began)
        total = len(ordered)
        running = self._status.running_count()
        summary = format_runtime_summary(
            started=running,
            total=total,
            startup_time_s=elapsed,
        )
        print_safe(summary, ascii_fallback=ascii_fallback_dx(summary))

    def _flush_startup_logs(self, *, banner: bool) -> None:
        """Flush buffered application logs after orchestration messages."""

        logger = self._logger
        if logger is None:
            return
        try:
            if banner:
                block = format_application_logs_banner()
                print_safe(block, ascii_fallback=ascii_fallback_dx(block))
            logger.flush_startup_buffer()
        except Exception:
            pass

    def _print_ready_urls(self, ordered: Sequence[ServiceSpec]) -> None:
        """Print a compact list of service URLs after a successful startup."""

        entries: list[tuple[str, str]] = []
        for spec in ordered:
            url = service_display_url(spec)
            if url:
                entries.append((spec.name, url))
        block = format_ready_urls(entries)
        if block:
            print_safe(block, ascii_fallback=ascii_fallback_dx(block))

    def monitor(self) -> int:
        """
        Watch running services until Ctrl+C or every process has finished.

        A single service crash marks that service FAILED, prints a crash
        notice, and does **not** tear down the rest of the stack, stop log
        pumps, stop file watchers, or end the monitor loop. Hot reloads run
        concurrently via WatchManager callbacks and do not stop siblings.

        The loop only returns when every registered service is STOPPED or
        FAILED (and no reload is in progress).
        """

        manager = self._require_manager()
        any_failed = False

        while True:
            try:
                newly_failed = manager.reap_exited()
            except Exception:
                newly_failed = {}

            if newly_failed:
                # Let stderr pumps finish ingesting the full traceback before we
                # sync FAILED → issue tracker / print diagnostics.
                for name in newly_failed:
                    try:
                        manager.wait_for_output(name, timeout_s=2.0)
                    except Exception:
                        pass
                time.sleep(min(0.05, self._poll_interval_s))

            for name, managed in newly_failed.items():
                # Ignore exits caused by an in-progress intentional reload:
                # restart() stops then starts under ProcessManager, so a
                # transient STOPPED state is expected. Only report crashes
                # for services that remain FAILED after reap.
                any_failed = True
                try:
                    self._status.sync_managed(managed)
                except Exception:
                    pass
                try:
                    self._print_crash_report(name, managed.exit_code)
                except Exception:
                    pass
                try:
                    # Environment / exception diagnostics (report only).
                    self._print_startup_failure(name)
                except Exception:
                    pass

            try:
                self._refresh_status()
            except Exception:
                pass

            # Keep monitoring while any service is still live — a crash must
            # never terminate the orchestration session on its own.
            if manager.all_finished():
                with self._reloading_lock:
                    reloading = bool(self._reloading)
                if not reloading:
                    return 1 if any_failed else 0

            time.sleep(self._poll_interval_s)

    def shutdown(self, logger: Logger, *, force: bool = False) -> int:
        """
        Stop all services in dependency-safe parallel waves; return Ctrl+C code.

        Re-entrant: a second call (e.g. after Ctrl+C mid-cleanup) continues
        stopping any services that are still live instead of no-oping.
        ``force=True`` uses a near-zero graceful timeout (immediate kill).
        """

        with self._shutdown_lock:
            first_entry = not self._shutdown_started
            self._shutdown_started = True

        self.begin_shutdown()
        manager = self._require_manager()

        if first_entry:
            # Keep shutdown UX clean (child KeyboardInterrupt noise stays in log files).
            logger.set_console_enabled(False)
            # Wait briefly for any in-flight reload to release its lock so we do
            # not stop a service mid-restart (orphan / double-stop races).
            # Bound the wait: an unbounded acquire() hangs shutdown (and CI) when
            # a reload is blocked inside health polling.
            for lock in list(self._reload_locks.values()):
                acquired = lock.acquire(timeout=5.0)
                if acquired:
                    lock.release()

        began = time.monotonic()
        # Only target services that are still live (supports re-entrant resume).
        live_states = (ServiceState.STARTING, ServiceState.RUNNING)
        names = [
            managed.name
            for managed in manager.services()
            if managed.state in live_states
        ]
        names.reverse()
        if not names:
            # Nothing live — re-entrant / natural-exit path. Do not emit the
            # Ctrl+C shutdown banner when processes already exited cleanly.
            return 130

        total = len([m for m in manager.services()])
        stopped: list[str] = []
        failures: list[str] = []
        stop_timeout = 0.05 if force else None

        waves = self._shutdown_waves(names)
        show_waves = len(waves) > 1
        for wave_index, wave in enumerate(waves, start=1):
            if show_waves and first_entry:
                banner = format_wave_header(wave_index, wave)
                print_safe(banner, ascii_fallback=ascii_fallback_dx(banner))
            wave_stopped, wave_failed = self._stop_wave(
                manager, wave, timeout_s=stop_timeout
            )
            stopped.extend(wave_stopped)
            failures.extend(wave_failed)

        if failures:
            msg = (
                "Shutdown failure: could not verify process exit for: "
                + ", ".join(failures)
            )
            print_safe(msg, ascii_fallback=ascii_fallback_dx(msg))

        # Final orphan sweep: force-kill anything still alive, then verify.
        remaining_pids: list[int] = []
        for managed in manager.services():
            proc = managed.process
            pid = managed.pid or managed.last_pid
            if proc is not None and proc.poll() is None and proc.pid is not None:
                try:
                    from .process_tree import signal_process_tree

                    signal_process_tree(proc.pid, graceful=False)
                    try:
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass
                except Exception:
                    pass
            check_pid = managed.pid or managed.last_pid or (proc.pid if proc else None)
            if isinstance(check_pid, int):
                from .status import pid_is_alive

                if pid_is_alive(check_pid):
                    remaining_pids.append(check_pid)

        if remaining_pids or failures:
            orphan_msg = format_cleanup_failure(remaining_pids=remaining_pids)
            print_safe(orphan_msg, ascii_fallback=ascii_fallback_dx(orphan_msg))

        # Drain pumps / prune threads so shutdown leaves no leaked I/O workers.
        try:
            manager._prune_completed_threads()
        except Exception:
            pass

        elapsed = max(0.0, time.monotonic() - began)
        summary = format_shutdown_summary(
            stopped_names=stopped,
            total=total,
            shutdown_time_s=elapsed,
        )
        print_safe(summary, ascii_fallback=ascii_fallback_dx(summary))
        return 1 if (failures or remaining_pids) else 130

    def verify_cleanup(self) -> dict[str, object]:
        """
        Post-shutdown integrity snapshot for tests and doctor tooling.

        Returns a dict describing leftover processes, pump threads, and watchers.
        Never raises.
        """

        result: dict[str, object] = {
            "orphan_pids": [],
            "alive_pump_threads": 0,
            "watched_services": [],
            "ok": True,
        }
        try:
            manager = self._manager
            if manager is not None:
                orphans: list[int] = []
                for managed in manager.services():
                    pid = managed.pid or managed.last_pid
                    if isinstance(pid, int):
                        from .status import pid_is_alive

                        if pid_is_alive(pid):
                            orphans.append(pid)
                try:
                    manager._prune_completed_threads()
                except Exception:
                    pass
                alive_pumps = sum(1 for t in manager._threads if t.is_alive())
                result["orphan_pids"] = orphans
                result["alive_pump_threads"] = alive_pumps
            watch = self._watch_manager
            if watch is not None:
                result["watched_services"] = list(watch.watched_services)
            result["ok"] = (
                not result["orphan_pids"]
                and int(result["alive_pump_threads"]) == 0
                and not result["watched_services"]
            )
        except Exception:
            result["ok"] = False
        return result

    def on_reload(self, name: str, changed_paths: Sequence[str] = ()) -> None:
        """
        Handle a debounced filesystem change for ``name``.

        Watcher never restarts services directly — it calls this callback,
        which delegates to ``restart``. Other services keep running.
        Failures are isolated to the reloading service (and optionally its
        dependents).
        """

        if self._shutting_down:
            return

        manager = self._manager
        graph = self._graph
        if manager is None:
            return

        lock = self._reload_locks.get(name)
        if lock is None:
            lock = self._reload_gate

        # Non-blocking acquire coalesces overlapping reload requests: mark
        # pending and run exactly one follow-up after the in-flight reload.
        if not lock.acquire(blocking=False):
            with self._reloading_lock:
                self._reload_pending.add(name)
            return
        try:
            pending_paths: Sequence[str] = changed_paths
            while True:
                if self._shutting_down or self._manager is None:
                    return
                with self._reloading_lock:
                    self._reloading.add(name)
                    self._reload_pending.discard(name)
                try:
                    try:
                        managed = manager.get(name)
                    except KeyError:
                        return
                    spec = managed.spec

                    display = format_changed_paths(
                        pending_paths,
                        relative_to=self._project_root or Path.cwd(),
                    )
                    _print_reload_detected(display)
                    began = time.monotonic()
                    self._in_reload_cycle = True
                    try:
                        ok = self._restart_with_health(manager, spec)

                        if ok and spec.restart_dependents and graph is not None:
                            for dep_name in graph.dependents(name, transitive=True):
                                if self._shutting_down:
                                    return
                                try:
                                    dep_spec = manager.get(dep_name).spec
                                except KeyError:
                                    continue
                                if not self._restart_with_health(manager, dep_spec):
                                    ok = False
                                    break

                        if ok:
                            elapsed = max(0.0, time.monotonic() - began)
                            _safe_print(
                                f"✓ Reloaded in {elapsed:.1f}s",
                                ascii_fallback=f"+ Reloaded in {elapsed:.1f}s",
                            )
                    finally:
                        self._in_reload_cycle = False
                finally:
                    with self._reloading_lock:
                        self._reloading.discard(name)
                        again = name in self._reload_pending
                        if again:
                            self._reload_pending.discard(name)
                if not again or self._shutting_down:
                    return
                pending_paths = ()
        finally:
            lock.release()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_manager(self) -> ProcessManager:
        if self._manager is None:
            raise RuntimeError("Runner is not bound; call Orchestrator.run() first")
        return self._manager

    def _refresh_status(self) -> None:
        manager = self._manager
        if manager is None:
            return
        self._status.sync_all(manager.services())

    def _print_crash_report(self, name: str, exit_code: Optional[int]) -> None:
        logger = self._logger
        if logger is not None:
            log_path = logger.log_path(name)
        else:
            base = self._logs_dir or (self.project_root / ".stackpilot" / "issues")
            log_path = Path(base) / f"{name}.issue"
        report = format_crash_report(
            service=name,
            exit_code=exit_code,
            log_path=log_path,
        )
        print_safe(report, ascii_fallback=ascii_fallback_dx(report))

    def _print_health_failure(self, spec: ServiceSpec, *, process) -> None:
        """Emit Problem / Reason / Suggested fix for HTTP health probe failures."""

        check = spec.health_check
        if not isinstance(check, HttpHealthCheck):
            return

        url = (check.url or "").strip()
        if not url:
            return

        from .adapters.detect.health_routes import HealthEndpointSelection

        probe = probe_http(url, request_timeout=float(check.probe_timeout))
        process_alive = process is not None and process.poll() is None
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port
        tcp_ok = False
        if port is not None:
            tcp_ok = check_tcp(host, int(port), connect_timeout=0.5)

        framework = _guess_framework(spec)
        discovered = discover_routes(Path(spec.path), framework) if framework else []
        configured = path_from_url(url)

        # Timeout is already covered by format_health_timeout in the caller.
        if probe.kind == "timeout":
            return

        if probe.kind in {"not_found", "failed", "refused"}:
            report = format_health_http_failure(
                service=spec.name,
                health_url=url,
                kind=probe.kind,
                detail=probe.detail or "",
                configured_path=configured,
                discovered_routes=discovered,
            )
            print_safe(report, ascii_fallback=ascii_fallback_dx(report))
            # Keep adaptive route hints as secondary application guidance.
            if probe.kind == "not_found" and (process_alive or tcp_ok) and discovered:
                extra = format_health_diagnostic(
                    configured_path=configured,
                    probe=probe,
                    discovered_routes=discovered,
                    application_running=True,
                )
                print_safe(extra, ascii_fallback=ascii_fallback_dx(extra))
            return

        if not discovered and tcp_ok:
            report = format_health_diagnostic(
                configured_path=configured,
                probe=probe,
                selected=HealthEndpointSelection(
                    kind="tcp",
                    detail="no HTTP health endpoint found; TCP connection successful",
                ),
                application_running=process_alive,
            )
            print_safe(report, ascii_fallback=ascii_fallback_dx(report))

    def _print_startup_failure(self, name: str) -> None:
        """Emit the Application startup failed diagnostic block."""

        manager = self._manager
        if manager is None:
            return
        try:
            managed = manager.get(name)
        except KeyError:
            return

        # Prefer health diagnostics when the process is still alive — the
        # generic "Application startup failed" block is for crash/import cases.
        if managed.process is not None and managed.process.poll() is None:
            if isinstance(managed.spec.health_check, HttpHealthCheck):
                return

        spec = managed.spec
        cwd = Path(managed.launch_cwd) if managed.launch_cwd else Path(spec.path)
        argv = managed.launch_argv or ()
        command = spec.command
        python_exe = argv[0] if argv else ""

        comparison = None
        if managed.launch_argv and managed.launch_cwd and managed.launch_env is not None:
            actual = actual_launch_plan(
                spec,
                argv=managed.launch_argv,
                cwd=Path(managed.launch_cwd),
                env=managed.launch_env,
            )
            expected = expected_launch_plan(spec)
            comparison = compare_launch_plans(actual, expected)

        summary = self._traceback_summary_for(name)
        application_output = None
        logger = self._logger
        if logger is not None:
            try:
                application_output = logger.issue_tracker.last_application_output(name)
            except Exception:
                application_output = None
        report = format_startup_failure_report(
            service=name,
            cwd=cwd,
            command=command,
            python_executable=python_exe,
            comparison=comparison,
            summary=summary,
            application_output=application_output,
        )
        print_safe(report, ascii_fallback=ascii_fallback_dx(report))

    def _traceback_summary_for(self, name: str) -> Optional[TracebackSummary]:
        logger = self._logger
        if logger is None:
            return None
        parsed = logger.issue_tracker.last_exception(name)
        if parsed is None:
            return None
        return TracebackSummary(
            exception_type=parsed.exception_type,
            exception_message=parsed.exception_message,
            file_line=parsed.file_line,
        )

    def _await_service_output(
        self,
        manager: ProcessManager,
        name: str,
        process,
    ) -> None:
        """Wait for the child to exit (if needed) then drain log pumps."""

        if process is not None:
            try:
                # Process may already be dead (ModuleNotFoundError at import).
                process.wait(timeout=2.0)
            except Exception:
                pass
        try:
            manager.wait_for_output(name, timeout_s=2.0)
        except Exception:
            pass
        # Flush buffered startup logs so the complete traceback hits the console
        # before the diagnostic block.
        logger = self._logger
        if logger is not None:
            try:
                logger.flush_startup_buffer()
            except Exception:
                pass

    def _stop_started(self, manager: ProcessManager) -> None:
        """Stop already-started services in reverse-dependency parallel waves."""

        names = [managed.name for managed in manager.services()]
        names.reverse()
        for wave in self._shutdown_waves(names):
            self._stop_wave(manager, wave)

    def _startup_waves(self, names: Sequence[str]) -> list[list[str]]:
        """
        Partition ``names`` into start waves that respect dependencies.

        Within a wave, no member depends on another member still waiting to
        start, so all may start concurrently. Across waves, dependencies always
        become healthy before their dependents (User → Auth → Gateway).
        """

        remaining = set(names)
        if not remaining:
            return []

        graph = self._graph
        order_index = {name: index for index, name in enumerate(names)}
        waves: list[list[str]] = []

        while remaining:
            if graph is None:
                wave = sorted(remaining, key=lambda n: order_index.get(n, 0))
                waves.append(wave)
                break

            can_start: list[str] = []
            for name in remaining:
                deps = [
                    dep
                    for dep in graph.edges.get(name, ())
                    if dep in remaining and dep in graph.specs
                ]
                if not deps:
                    can_start.append(name)

            if not can_start:
                # Cycle / incomplete graph: peel one name in display order.
                can_start = [min(remaining, key=lambda n: order_index.get(n, 0))]

            can_start.sort(key=lambda n: order_index.get(n, 0))
            waves.append(can_start)
            remaining.difference_update(can_start)

        return waves

    def _start_wave(
        self,
        specs: Sequence[ServiceSpec],
    ) -> tuple[list[str], list[str]]:
        """
        Start ``specs`` concurrently (spawn + health wait).

        Returns ``(started, failed)`` preserving wave display order.
        """

        if not specs:
            return [], []

        if len(specs) == 1:
            ok = self.start(specs[0])
            if ok:
                return [specs[0].name], []
            return [], [specs[0].name]

        results: dict[str, bool] = {}

        def _start_one(spec: ServiceSpec) -> tuple[str, bool]:
            return spec.name, self.start(spec)

        workers = min(len(specs), 32)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_start_one, spec) for spec in specs]
            for future in as_completed(futures):
                name, ok = future.result()
                results[name] = ok

        started: list[str] = []
        failed: list[str] = []
        for spec in specs:
            if results.get(spec.name, False):
                started.append(spec.name)
            else:
                failed.append(spec.name)
        return started, failed

    def _shutdown_waves(self, names: Sequence[str]) -> list[list[str]]:
        """
        Partition ``names`` into stop waves that respect dependencies.

        Within a wave, no remaining service depends on another in the wave, so
        all members may stop concurrently. Across waves, dependents always
        finish before their dependencies (Gateway → Auth → User).
        """

        remaining = set(names)
        if not remaining:
            return []

        graph = self._graph
        order_index = {name: index for index, name in enumerate(names)}
        waves: list[list[str]] = []

        while remaining:
            if graph is None:
                wave = sorted(remaining, key=lambda n: order_index.get(n, 0))
                waves.append(wave)
                break

            can_stop: list[str] = []
            for name in remaining:
                depended_on = False
                for other in remaining:
                    if other == name:
                        continue
                    if name in graph.edges.get(other, ()):
                        depended_on = True
                        break
                if not depended_on:
                    can_stop.append(name)

            if not can_stop:
                # Cycle / incomplete graph: peel one name in display order.
                can_stop = [min(remaining, key=lambda n: order_index.get(n, 0))]

            can_stop.sort(key=lambda n: order_index.get(n, 0))
            waves.append(can_stop)
            remaining.difference_update(can_stop)

        return waves

    def _stop_wave(
        self,
        manager: ProcessManager,
        names: Sequence[str],
        *,
        timeout_s: float | None = None,
    ) -> tuple[list[str], list[str]]:
        """
        Stop ``names`` concurrently.

        Returns ``(stopped, failed)`` where ``failed`` are services whose
        process could not be confirmed dead.
        """

        if not names:
            return [], []

        stopped: list[str] = []
        failed: list[str] = []

        def _stop_one(name: str) -> tuple[str, object | None, bool]:
            try:
                if timeout_s is None:
                    managed = manager.stop(name)
                else:
                    managed = manager.stop(name, timeout_s=timeout_s)
                return name, managed, False
            except Exception:
                return name, None, True

        if len(names) == 1:
            name, managed, is_fail = _stop_one(names[0])
            if managed is not None:
                try:
                    self._status.sync_managed(managed)  # type: ignore[arg-type]
                except Exception:
                    pass
            if is_fail:
                failed.append(name)
            else:
                stopped.append(name)
            return stopped, failed

        results: dict[str, tuple[object | None, bool]] = {}

        workers = min(len(names), 32)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_stop_one, name) for name in names]
            for future in as_completed(futures):
                name, managed, is_fail = future.result()
                results[name] = (managed, is_fail)

        for name in names:
            managed, is_fail = results.get(name, (None, True))
            if managed is not None:
                try:
                    self._status.sync_managed(managed)  # type: ignore[arg-type]
                except Exception:
                    pass
            if is_fail:
                failed.append(name)
            else:
                stopped.append(name)
        return stopped, failed

    def _restart_with_health(
        self,
        manager: ProcessManager,
        spec: ServiceSpec,
    ) -> bool:
        """Gracefully restart one service and wait for its health check."""

        print_safe(
            f"Reloading {spec.name}...",
            ascii_fallback=f"Reloading {spec.name}...",
        )
        try:
            managed = manager.restart(spec.name)
            self._status.sync_managed(managed)
        except Exception as exc:
            _safe_print(
                f"❌ {spec.name} reload failed: {exc}",
                ascii_fallback=f"X {spec.name} reload failed: {exc}",
            )
            return False

        try:
            elapsed = Health.wait_until_healthy(
                spec.name,
                spec.health_check,
                process=managed.process,
            )
        except HealthCheckTimeout:
            _safe_print(
                f"❌ {spec.name} failed health check after reload",
                ascii_fallback=f"X {spec.name} failed health check after reload",
            )
            self._status.sync_managed(manager.get(spec.name))
            return False
        except Exception as exc:
            _safe_print(
                f"❌ {spec.name} reload failed: {exc}",
                ascii_fallback=f"X {spec.name} reload failed: {exc}",
            )
            return False

        # Watch-driven reloads print a single cycle summary in ``on_reload``.
        if not self._in_reload_cycle:
            _safe_print(
                f"✓ Reloaded in {elapsed:.1f}s",
                ascii_fallback=f"+ Reloaded in {elapsed:.1f}s",
            )
        self._status.sync_managed(manager.get(spec.name))
        return True


def _guess_framework(spec: ServiceSpec) -> str:
    command = (spec.command or "").lower()
    if "uvicorn" in command or "fastapi" in command:
        return "FastAPI"
    if "flask" in command:
        return "Flask"
    if "django" in command or "manage.py" in command:
        return "Django"
    if "nest" in command:
        return "NestJS"
    if "express" in command:
        return "Express"
    # Ambiguous Node launchers (``npm run start:dev``, ``node …``): prefer
    # NestJS/Express from the adapter registry so Nest is not mislabeled.
    if any(token in command for token in ("npm", "npx", "node", "pnpm", "yarn", "bun")):
        try:
            from .adapters import default_registry

            adapter = default_registry.match(Path(spec.path))
            if adapter is not None and adapter.name in {"NestJS", "Express"}:
                return adapter.name
        except Exception:
            pass
        return "Express"
    return ""
