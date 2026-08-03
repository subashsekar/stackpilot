from __future__ import annotations

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, IO, List, Optional, Sequence

from .config import ExternalDependency, ServiceSpec
from .launch_env import (
    actual_launch_plan,
    build_child_env,
    expected_launch_plan,
    resolve_service_argv,
)
from .logger import Logger
from .models import ManagedService, ServiceState
from .process_tree import WindowsProcessJob, signal_process_tree, spawn_kwargs
from .utils import iter_text_lines


class ProcessManager:
    """
    Start, stop, and restart local service subprocesses.

    Owns PID tracking and ``ServiceState`` transitions. Output pumping is
    delegated to ``Logger``. One service failing does not stop others.

    Children are launched in their own process group / session so Ctrl+C in
    the StackPilot terminal does not kill them immediately; shutdown walks
    the process tree (POSIX process groups; Windows Job Objects) so
    grandchildren are not left orphaned.
    """

    def __init__(
        self,
        logger: Logger,
        *,
        services: Sequence[ServiceSpec] = (),
        external_dependencies: Sequence[ExternalDependency] = (),
        stop_timeout_s: float = 5.0,
    ) -> None:
        self._logger = logger
        self._topology_services = tuple(services)
        self._external_dependencies = tuple(external_dependencies)
        self._stop_timeout_s = stop_timeout_s

        self._services: Dict[str, ManagedService] = {}
        self._threads: List[threading.Thread] = []
        self._pump_threads: Dict[str, List[threading.Thread]] = {}
        self._lock = threading.Lock()
        # Names currently being stopped by ``stop()`` / ``restart()``.
        # ``reap_exited`` must not claim those exits as crashes.
        self._stopping: set[str] = set()
        # Windows Job Objects keyed by service name (tree ownership).
        self._jobs: Dict[str, WindowsProcessJob] = {}

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get(self, name: str) -> ManagedService:
        try:
            return self._services[name]
        except KeyError as exc:
            raise KeyError(f"Unknown service: {name}") from exc

    def services(self) -> Sequence[ManagedService]:
        return tuple(self._services.values())

    def state_of(self, name: str) -> ServiceState:
        return self.get(name).state

    def pid_of(self, name: str) -> Optional[int]:
        return self.get(name).pid

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_all(self, specs: Sequence[ServiceSpec]) -> None:
        for spec in specs:
            self.start(spec)

    def start(self, spec: ServiceSpec) -> ManagedService:
        with self._lock:
            existing = self._services.get(spec.name)
            if existing is not None and existing.state in (
                ServiceState.STARTING,
                ServiceState.RUNNING,
            ):
                raise RuntimeError(f"Service already running: {spec.name}")

            managed = existing or ManagedService(spec=spec)
            managed.spec = spec
            managed.state = ServiceState.STARTING
            managed.exit_code = None
            managed.pid = None
            managed.process = None
            managed.clear_start()
            self._services[spec.name] = managed
            self._discard_job(spec.name)

        try:
            proc = self._spawn(spec)
            if sys.platform == "win32" and proc.pid is not None:
                # Prefer a Job Object so grandchildren die with the service.
                # If the OS refuses the job, continue and fall back to
                # ``taskkill /T`` on stop — never leave the spawn half-done.
                try:
                    job = WindowsProcessJob()
                    job.assign(proc.pid)
                    with self._lock:
                        self._jobs[spec.name] = job
                except OSError:
                    pass
        except Exception:
            try:
                if "proc" in locals() and proc is not None and proc.poll() is None:
                    if proc.pid is not None:
                        signal_process_tree(proc.pid, graceful=False)
                    else:
                        proc.kill()
            except Exception:
                pass
            with self._lock:
                managed.state = ServiceState.FAILED
                managed.clear_runtime()
                managed.clear_start()
                self._discard_job(spec.name)
            raise

        with self._lock:
            managed.process = proc
            managed.pid = proc.pid
            managed.last_pid = proc.pid
            managed.state = ServiceState.RUNNING
            managed.mark_started()

        self._start_pumps(spec.name, proc)
        return managed

    def stop(self, name: str, *, timeout_s: Optional[float] = None) -> ManagedService:
        managed = self.get(name)
        timeout = self._stop_timeout_s if timeout_s is None else timeout_s

        with self._lock:
            proc = managed.process
            job = self._jobs.get(name)
            # Already finished (STOPPED / FAILED) or never started.
            if proc is None or managed.state not in (
                ServiceState.STARTING,
                ServiceState.RUNNING,
            ):
                # If a process handle remains, only claim STOPPED when the OS
                # confirms exit. Never invent STOPPED for a live process.
                if proc is not None and proc.poll() is None:
                    raise RuntimeError(
                        f"Failed to stop service {name!r}: process still alive "
                        f"(pid={proc.pid})"
                    )
                if managed.state in (ServiceState.STARTING, ServiceState.RUNNING):
                    managed.state = ServiceState.STOPPED
                managed.clear_runtime()
                self._discard_job(name)
                return managed
            self._stopping.add(name)

        try:
            self._terminate_process(proc, job=job, timeout_s=timeout)
            # Drain stdout/stderr so the final traceback is never truncated.
            self.wait_for_output(name, timeout_s=min(2.0, max(timeout, 0.1)))

            with self._lock:
                code = proc.poll()
                if code is None:
                    # Graceful + force already attempted in _terminate_process.
                    # One last force kill before declaring failure.
                    try:
                        if proc.pid is not None:
                            signal_process_tree(proc.pid, graceful=False)
                        else:
                            proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass
                    code = proc.poll()

                if code is None:
                    # Do NOT mark STOPPED or clear runtime — state must match OS.
                    raise RuntimeError(
                        f"Failed to stop service {name!r}: process still alive "
                        f"(pid={proc.pid})"
                    )

                managed.exit_code = code
                managed.clear_runtime()
                # Intentional stop — verified dead by poll().
                managed.state = ServiceState.STOPPED
                self._discard_job(name)
        finally:
            with self._lock:
                self._stopping.discard(name)

        self._prune_completed_threads()
        return managed

    def stop_all(self, *, timeout_s: Optional[float] = None) -> None:
        """Stop every registered service concurrently (cleanup / test helper)."""

        names = [s.name for s in self.services()]
        if not names:
            return

        def _stop_one(name: str) -> None:
            try:
                self.stop(name, timeout_s=timeout_s)
            except Exception:
                pass

        workers = min(len(names), 32)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_stop_one, names))
        self._prune_completed_threads()

    def restart(self, name: str) -> ManagedService:
        managed = self.get(name)
        spec = managed.spec
        if managed.state in (ServiceState.STARTING, ServiceState.RUNNING):
            self.stop(name)
        return self.start(spec)

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def reap_exited(self) -> Dict[str, ManagedService]:
        """
        Detect services that exited on their own.

        Non-zero exits become FAILED; zero exits become STOPPED. Returns only
        newly FAILED services. Does not stop or otherwise affect remaining
        services.
        """

        newly_failed: Dict[str, ManagedService] = {}

        with self._lock:
            for managed in self._services.values():
                if managed.name in self._stopping:
                    continue
                if managed.state != ServiceState.RUNNING or managed.process is None:
                    continue

                code = managed.process.poll()
                if code is None:
                    continue

                managed.exit_code = code
                managed.last_pid = managed.pid or managed.last_pid
                managed.clear_runtime()
                # Drop job ownership without TerminateJobObject so natural
                # exits are not force-killed mid-reap; closing still applies
                # KILL_ON_JOB_CLOSE for any leftover grandchildren.
                self._discard_job(managed.name)
                if code == 0:
                    managed.state = ServiceState.STOPPED
                else:
                    managed.state = ServiceState.FAILED
                    newly_failed[managed.name] = managed

        self._prune_completed_threads()
        return newly_failed

    def wait_for_output(self, name: str, *, timeout_s: float = 2.0) -> None:
        """
        Block until stdout/stderr pump threads for ``name`` finish.

        Call after the child has exited (or been stopped) so the full
        traceback is ingested before diagnostics run. Never truncates
        streams — pumps read until EOF.
        """

        with self._lock:
            threads = list(self._pump_threads.get(name, ()))
        deadline = time.monotonic() + max(0.0, timeout_s)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        self._prune_completed_threads()

    def all_finished(self) -> bool:
        """True when every registered service is STOPPED or FAILED."""

        if not self._services:
            return True
        with self._lock:
            if self._stopping:
                return False
            return all(
                s.state in (ServiceState.STOPPED, ServiceState.FAILED)
                for s in self._services.values()
            )

    def launch_comparison(self, name: str):
        """Compare the last StackPilot spawn for ``name`` to the expected plan."""

        managed = self.get(name)
        if not managed.launch_argv or not managed.launch_cwd or managed.launch_env is None:
            expected = expected_launch_plan(
                managed.spec,
                services=self._topology_services,
                external_dependencies=self._external_dependencies,
            )
            return None, expected
        actual = actual_launch_plan(
            managed.spec,
            argv=managed.launch_argv,
            cwd=Path(managed.launch_cwd),
            env=managed.launch_env,
        )
        expected = expected_launch_plan(
            managed.spec,
            services=self._topology_services,
            external_dependencies=self._external_dependencies,
        )
        from .launch_env import compare_launch_plans

        return compare_launch_plans(actual, expected), expected

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _spawn(self, spec: ServiceSpec) -> subprocess.Popen[str]:
        cwd = Path(spec.path).expanduser().resolve()
        env = build_child_env(
            cwd,
            services=self._topology_services,
            external_dependencies=self._external_dependencies,
        )
        argv = resolve_service_argv(spec.command, cwd=cwd, env=env)

        with self._lock:
            managed = self._services[spec.name]
            managed.launch_cwd = str(cwd)
            managed.launch_argv = tuple(argv)
            managed.launch_env = dict(env)

        kwargs: dict = {
            "args": argv,
            "cwd": str(cwd),
            "env": env,
            "shell": False,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            **spawn_kwargs(),
        }
        return subprocess.Popen(**kwargs)

    def _start_pumps(self, service_name: str, proc) -> None:
        self._prune_completed_threads()
        threads: List[threading.Thread] = []
        if proc.stdout is not None:
            t_out = threading.Thread(
                target=self._pump_stdout,
                args=(service_name, proc.stdout),
                name=f"stackpilot-stdout-{service_name}",
                daemon=True,
            )
            t_out.start()
            threads.append(t_out)

        if proc.stderr is not None:
            t_err = threading.Thread(
                target=self._pump_stderr,
                args=(service_name, proc.stderr),
                name=f"stackpilot-stderr-{service_name}",
                daemon=True,
            )
            t_err.start()
            threads.append(t_err)

        with self._lock:
            self._threads.extend(threads)
            self._pump_threads[service_name] = list(threads)

    def _prune_completed_threads(self) -> None:
        """Drop finished pump thread refs so long sessions stay bounded."""

        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            stale: List[str] = []
            for name, threads in self._pump_threads.items():
                alive = [t for t in threads if t.is_alive()]
                if alive:
                    self._pump_threads[name] = alive
                else:
                    stale.append(name)
            for name in stale:
                self._pump_threads.pop(name, None)

    def _pump_stdout(self, service_name: str, stream: IO[str]) -> None:
        try:
            for line in iter_text_lines(stream):
                self._logger.stdout(service_name, line)
        except Exception:
            # Pumping must never crash the manager / CLI.
            pass

    def _pump_stderr(self, service_name: str, stream: IO[str]) -> None:
        try:
            for line in iter_text_lines(stream):
                self._logger.stderr(service_name, line)
                self._logger.error_file(service_name, line)
        except Exception:
            pass

    def _terminate_process(
        self,
        proc,
        *,
        job: Optional[WindowsProcessJob],
        timeout_s: float,
    ) -> None:
        if proc.poll() is not None:
            if job is not None:
                job.terminate()
            return

        pid = proc.pid
        if pid is None:
            if job is not None:
                job.terminate()
            return

        # Graceful signal first so services can shut down cleanly.
        # On Windows with a Job Object, terminate the root process only;
        # never send CTRL_BREAK_EVENT (it can interrupt our own console).
        # Near-zero timeout (force path) skips the graceful wait.
        if timeout_s > 0:
            if sys.platform == "win32":
                try:
                    proc.terminate()
                except Exception:
                    pass
            else:
                signal_process_tree(pid, graceful=True)

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)

        # Force-kill the tree. Prefer the Job Object on Windows so
        # grandchildren die even if the root already exited. Always follow
        # with a process-tree signal so orphans cannot survive a missed job
        # membership (belt-and-suspenders; no-ops when already dead).
        if job is not None:
            job.terminate()
        signal_process_tree(pid, graceful=False)

        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass

    def _discard_job(self, name: str) -> None:
        job = self._jobs.pop(name, None)
        if job is not None:
            # close() applies KILL_ON_JOB_CLOSE for any leftover members.
            try:
                job.close()
            except Exception:
                pass


# Re-export for tests.
__all__ = ["ProcessManager", "signal_process_tree"]
