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
    format_crash_report,
    format_ready_urls,
    format_shutdown_summary,
    print_safe,
)
from .dependency_graph import DependencyGraph
from .diagnostics.errors import format_health_timeout, format_spawn_failure
from .health import Health, HealthCheckTimeout
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


def _print_reload_warning(message: str) -> None:
    """Print a yellow WARNING line for hot-reload change detection."""

    # Always emit ANSI yellow so Git Bash / Windows still show the warning.
    # Tests that monkeypatch ``print`` continue to see the raw message.
    colored = f"\033[1;33m{message}\033[0m"
    print_safe(colored, ascii_fallback=f"\033[1;33m{ascii_fallback_dx(message)}\033[0m")


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
        self._reloading_lock = threading.Lock()
        self._startup_began_mono: Optional[float] = None
        self._shutting_down = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False

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

    def begin_shutdown(self) -> None:
        """
        Disable hot reload before tearing down processes / watchers.

        Safe to call multiple times. In-flight ``on_reload`` callbacks that have
        not yet acquired their per-service lock will no-op.
        """

        self._shutting_down = True
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
        url = service_display_url(spec)
        if url:
            _safe_print(f"  → {url}", ascii_fallback=f"  -> {url}")
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
        Start services in the given order, waiting for each to become healthy.

        Returns False when a health check times out (startup aborted).
        On any failure or exception after at least one successful start,
        already-started services are stopped so no orphans remain.
        """

        manager = self._require_manager()
        print_safe(
            "Starting application services...",
            ascii_fallback="Starting application services...",
        )

        ok = True
        try:
            for spec in ordered:
                if not self.start(spec):
                    print_safe("Startup aborted.", ascii_fallback="Startup aborted.")
                    ok = False
                    break
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

        if ok:
            self._refresh_status()
            self._print_ready_urls(ordered)
            print_safe("Watching for changes...", ascii_fallback="Watching for changes...")
            print_safe("Press Ctrl+C to stop.", ascii_fallback="Press Ctrl+C to stop.")
        return ok

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
            for lock in list(self._reload_locks.values()):
                lock.acquire()
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

        for wave in self._shutdown_waves(names):
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

        elapsed = max(0.0, time.monotonic() - began)
        summary = format_shutdown_summary(
            stopped_names=stopped,
            total=total,
            shutdown_time_s=elapsed,
        )
        print_safe(summary, ascii_fallback=ascii_fallback_dx(summary))
        return 1 if failures else 130

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

        # Non-blocking acquire drops overlapping reload requests for the same
        # service (prevents double-reload storms under bursty FS events).
        if not lock.acquire(blocking=False):
            return
        try:
            if self._shutting_down or self._manager is None:
                return
            with self._reloading_lock:
                self._reloading.add(name)
            try:
                try:
                    managed = manager.get(name)
                except KeyError:
                    return
                spec = managed.spec

                display = format_changed_paths(
                    changed_paths,
                    relative_to=self._project_root or Path.cwd(),
                )
                _print_reload_warning(
                    f"WARNING StackPilot detected changes in {display}. Reloading..."
                )
                self._restart_with_health(manager, spec)

                if spec.restart_dependents and graph is not None:
                    for dep_name in graph.dependents(name, transitive=True):
                        if self._shutting_down:
                            return
                        try:
                            dep_spec = manager.get(dep_name).spec
                        except KeyError:
                            continue
                        self._restart_with_health(manager, dep_spec)
            finally:
                with self._reloading_lock:
                    self._reloading.discard(name)
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
        """Emit adaptive health diagnostics when an HTTP probe fails."""

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

        if probe.kind == "not_found" and (process_alive or tcp_ok):
            report = format_health_diagnostic(
                configured_path=configured,
                probe=probe,
                discovered_routes=discovered,
                application_running=True,
            )
            print_safe(report, ascii_fallback=ascii_fallback_dx(report))
            return

        if probe.kind == "refused":
            report = "Connection refused\n\nApplication not started."
            print_safe(report, ascii_fallback=report)
            return

        if probe.kind == "timeout":
            report = "Timeout\n\nApplication unhealthy."
            print_safe(report, ascii_fallback=report)
            return

        if probe.kind == "failed":
            report = (
                f"Detected health endpoint\n\n{configured}\n\n"
                f"Status\n\n{probe.detail}\n\nHealth failed."
            )
            print_safe(report, ascii_fallback=ascii_fallback_dx(report))
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
        report = format_startup_failure_report(
            service=name,
            cwd=cwd,
            command=command,
            python_executable=python_exe,
            comparison=comparison,
            summary=summary,
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

    def _restart_with_health(self, manager: ProcessManager, spec: ServiceSpec) -> bool:
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

        print_safe(
            f"Waiting for {spec.name}...",
            ascii_fallback=f"Waiting for {spec.name}...",
        )
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

        _safe_print(
            f"✓ {spec.name} reloaded ({elapsed:.1f}s)",
            ascii_fallback=f"+ {spec.name} reloaded ({elapsed:.1f}s)",
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
    if "express" in command or "node" in command or "npm" in command:
        # Prefer Express discovery for generic Node; Nest wins when decorators exist.
        from .adapters.detect.health_routes import discover_nestjs_routes

        if discover_nestjs_routes(Path(spec.path)):
            return "NestJS"
        return "Express"
    return ""
