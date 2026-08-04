"""Stack orchestration — config validation, dependency order, Runner lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from .config import Stack
from .dashboard import ascii_fallback_dx, print_safe
from .dependency_graph import (
    CircularDependencyError,
    DependencyError,
    DependencyGraph,
    build_graph,
)
from .diagnostics.errors import (
    format_circular_dependency_error,
    format_spawn_failure,
    format_user_error,
)
from .external_validation import (
    ExternalDependencyError,
    validate_external_dependencies,
)
from .issues import DEFAULT_ISSUES_DIR, IssueTracker
from .logger import Logger
from .paths import PathEscapeError, ensure_within_project
from .process_manager import ProcessManager
from .runner import Runner
from .watch_manager import WatchManager
from .watcher import DEFAULT_DEBOUNCE_S


class Orchestrator:
    """
    Top-level coordinator between CLI / ``Stack.run()`` and ``Runner``.

    Owns Stackfile-derived configuration concerns: validation, dependency
    graph construction, startup ordering, and wiring of process / watch /
    log collaborators. ``Runner`` only executes an already-ordered list of
    services.

    Lifecycle (single entry / single shutdown path)::

        validate externals
        → start services
        → start watchers
        → monitor
        → disable reload
        → stop processes
        → stop watchers
        → unbind
        → logger shutdown
    """

    def __init__(
        self,
        *,
        logs_dir: Optional[Path] = None,
        poll_interval_s: float = 0.25,
        reload_debounce_s: float = DEFAULT_DEBOUNCE_S,
        runner: Optional[Runner] = None,
    ) -> None:
        self._logs_dir = logs_dir
        self._poll_interval_s = poll_interval_s
        self._reload_debounce_s = reload_debounce_s
        self._runner = runner
        self._logger: Optional[Logger] = None
        self._graph: Optional[DependencyGraph] = None
        self._watch_manager: Optional[WatchManager] = None
        self._cleanup_done = False

    def run(
        self,
        stack: Stack,
        *,
        target: Optional[str] = None,
        project_root: Optional[Path] = None,
    ) -> int:
        """
        Validate ``stack``, resolve startup order, then execute via ``Runner``.

        ``DependencyError`` propagates to the caller (CLI maps it to exit 1).

        Runtime artifacts (``.stackpilot/issues/``, ``runtime.json``) are always
        rooted under the discovered project root (or an explicit ``project_root``),
        never under a subdirectory ``cwd``.
        """

        try:
            ordered = self._ordered_services(stack, target=target)
        except CircularDependencyError as exc:
            # Must run before ValueError — DependencyError subclasses ValueError.
            message = format_circular_dependency_error(exc.cycle)
            print_safe(message, ascii_fallback=ascii_fallback_dx(message))
            return 1
        except DependencyError as exc:
            message = format_user_error(
                problem="Dependency error",
                reason=str(exc),
                suggested_fix=(
                    "Fix depends_on= in Stackfile.py so every name refers to a "
                    "registered service, then re-run."
                ),
            )
            print_safe(message, ascii_fallback=ascii_fallback_dx(message))
            return 1
        except ValueError as exc:
            message = format_user_error(
                problem="Configuration error",
                reason=str(exc),
                suggested_fix="Add stack.service(...) entries in Stackfile.py, then re-run.",
            )
            print_safe(message, ascii_fallback=ascii_fallback_dx(message))
            return 1

        assert self._graph is not None
        try:
            validate_external_dependencies(
                self._graph,
                ordered_services=ordered,
                target=target,
            )
        except ExternalDependencyError:
            return 1

        root = self._resolve_project_root(project_root, ordered=ordered)
        try:
            self._validate_service_paths(ordered, root)
        except PathEscapeError as exc:
            message = format_user_error(
                problem="Configuration error",
                reason=str(exc),
                suggested_fix="Keep service path= and reload_dirs inside the project root.",
            )
            print_safe(message, ascii_fallback=ascii_fallback_dx(message))
            return 1
        except (OSError, ValueError) as exc:
            message = format_user_error(
                problem="Bad path",
                reason=str(exc),
                suggested_fix="Fix path= / reload_dirs in Stackfile.py. Run: stackpilot doctor",
            )
            print_safe(message, ascii_fallback=ascii_fallback_dx(message))
            return 1

        # ``logs_dir`` is the historical kwarg name for the Issue Tracker path.
        issues_dir = self._logs_dir or (root / DEFAULT_ISSUES_DIR)
        names = [spec.name for spec in ordered]
        tracker = IssueTracker(issues_dir)
        logger = Logger(
            issues_dir,
            service_names=names,
            issue_tracker=tracker,
            auto_cleanup=False,
        )
        manager = ProcessManager(
            logger,
            services=stack.services,
            external_dependencies=stack.external_dependencies,
        )
        watch_manager = WatchManager(
            debounce_s=self._reload_debounce_s,
            log=lambda message: print_safe(
                message, ascii_fallback=ascii_fallback_dx(message)
            ),
        )

        runner = self._ensure_runner()
        self._logger = logger
        self._watch_manager = watch_manager
        self._cleanup_done = False

        runner.bind(
            manager=manager,
            graph=self._graph,
            watch_manager=watch_manager,
            project_root=root,
            ordered=ordered,
            logger=logger,
        )
        # Thin persistence hook only — orchestration behaviour unchanged.
        runner._status.set_issue_tracker(tracker)

        exit_code = 1
        try:
            # Clear stale ACTIVE rows from a previous session *before* start so
            # stderr / crashes during this run stay ACTIVE (not immediately FIXED).
            for name in names:
                tracker.mark_fixed(name)

            if not runner.start_all(ordered):
                exit_code = 1
            else:
                watch_manager.start(
                    ordered,
                    runner.on_reload,
                    project_root=runner.project_root,
                )
                runner.print_watch_ready(
                    watching=bool(watch_manager.watched_services)
                )
                exit_code = runner.monitor()
        except KeyboardInterrupt:
            exit_code = self.stop()
        except (OSError, FileNotFoundError, PermissionError, ValueError) as exc:
            self._print_spawn_failure(exc, ordered, manager)
            exit_code = 1
        finally:
            if not self._cleanup_done:
                # Bound runner may hold live children from a partial start or
                # aborted spawn. Only skip process stop when nothing could have
                # been started (mathematically impossible).
                stop_procs = bool(runner is not None and runner.is_bound)
                self._finish_shutdown(stop_processes=stop_procs)
            self._logger = None
            self._graph = None
            self._watch_manager = None

        return exit_code

    def stop(self) -> int:
        """
        Shut down the running stack through the single shutdown sequence.

        Order: disable reload → stop processes → stop watchers → unbind →
        logger close.
        """

        return self._finish_shutdown(stop_processes=True)

    def _finish_shutdown(self, *, stop_processes: bool) -> int:
        """
        Run the full cleanup sequence transactionally.

        ``_cleanup_done`` is set only after every stage has been attempted so a
        Ctrl+C mid-cleanup cannot permanently skip remaining stages. Each stage
        catches ``KeyboardInterrupt`` and continues; a second interrupt forces
        aggressive process teardown on the next stop attempt.
        """

        if self._cleanup_done:
            return 0

        runner = self._runner
        logger = self._logger
        watch_manager = self._watch_manager
        code = 0
        interrupts = 0

        def _stage(action):  # noqa: ANN001
            nonlocal interrupts
            try:
                return action()
            except KeyboardInterrupt:
                interrupts += 1
                return None
            except Exception:
                return None

        try:
            # 1. Disable reload — ignore further watch callbacks.
            if runner is not None:
                _stage(runner.begin_shutdown)

            # 2. Stop processes whenever any service may have been started.
            if (
                stop_processes
                and runner is not None
                and logger is not None
                and runner.is_bound
            ):
                force = interrupts > 0
                result = _stage(
                    lambda: runner.shutdown(logger, force=force)
                )
                if isinstance(result, int):
                    code = result
                # Interrupted mid-stop: retry remaining live services forcefully.
                if interrupts and runner.is_bound:
                    result = _stage(
                        lambda: runner.shutdown(logger, force=True)
                    )
                    if isinstance(result, int):
                        code = result

            # 3. Stop watchers.
            if watch_manager is not None:
                _stage(watch_manager.stop)

            # Capture project root before unbind clears it.
            project_root = None
            if runner is not None:
                project_root = getattr(runner, "_project_root", None)
                if project_root is None:
                    try:
                        project_root = runner.status.project_root
                    except Exception:
                        project_root = None

            # 4. Unbind runtime collaborators.
            if runner is not None:
                _stage(runner.unbind)

            # 5. Clear runtime.json session after processes are down.
            if project_root is not None:
                def _clear() -> None:
                    from .runtime_control import clear_runtime_session

                    clear_runtime_session(project_root)

                _stage(_clear)

            # 6. Logger shutdown.
            if logger is not None:
                _stage(logger.close)
        finally:
            # Mark complete only after every stage has been attempted.
            self._cleanup_done = True

        if interrupts and code == 0:
            return 130
        return code

    def _print_spawn_failure(
        self,
        exc: BaseException,
        ordered: Sequence,
        manager: ProcessManager,
    ) -> None:
        service = ordered[0].name if ordered else "service"
        command = ""
        cwd: Optional[Path] = None
        for managed in manager.services():
            if managed.state.name == "FAILED" or managed.state.name == "STARTING":
                service = managed.name
                command = managed.spec.command
                cwd = managed.spec.path
                break
        else:
            if ordered:
                command = ordered[0].command
                cwd = ordered[0].path
        message = format_spawn_failure(
            service=service,
            exc=exc,
            command=str(command),
            cwd=Path(cwd) if cwd is not None else None,
        )
        print_safe(message, ascii_fallback=ascii_fallback_dx(message))

    def _ensure_runner(self) -> Runner:
        if self._runner is None:
            self._runner = Runner(
                logs_dir=self._logs_dir,
                poll_interval_s=self._poll_interval_s,
                reload_debounce_s=self._reload_debounce_s,
            )
        return self._runner

    @staticmethod
    def _resolve_project_root(
        project_root: Optional[Path],
        *,
        ordered: Sequence = (),
    ) -> Path:
        if project_root is not None:
            return Path(project_root).expanduser().resolve()

        discovered: Optional[Path] = None
        try:
            from .discovery import discover_project

            discovered = discover_project().root
        except Exception:
            discovered = None

        if discovered is not None and Orchestrator._paths_within(ordered, discovered):
            return discovered

        # API / test callers often omit project_root. Prefer a root that actually
        # contains the configured service paths so containment checks stay valid.
        inferred = Orchestrator._infer_root_from_services(ordered)
        if inferred is not None:
            return inferred

        return Path.cwd().resolve()

    @staticmethod
    def _paths_within(ordered: Sequence, root: Path) -> bool:
        if not ordered:
            return True
        try:
            for spec in ordered:
                Path(spec.path).expanduser().resolve().relative_to(root)
        except (ValueError, OSError):
            return False
        return True

    @staticmethod
    def _infer_root_from_services(ordered: Sequence) -> Optional[Path]:
        if not ordered:
            return None
        try:
            import os

            paths = [str(Path(spec.path).expanduser().resolve()) for spec in ordered]
            return Path(os.path.commonpath(paths)).resolve()
        except (ValueError, OSError):
            return None

    @staticmethod
    def _validate_service_paths(ordered: Sequence, project_root: Path) -> None:
        for spec in ordered:
            ensure_within_project(
                spec.path,
                project_root,
                label=f"service {spec.name!r} path",
            )
            for entry in spec.reload_dirs:
                path = Path(entry)
                if not path.is_absolute():
                    path = Path(spec.path) / path
                ensure_within_project(
                    path,
                    project_root,
                    label=f"service {spec.name!r} reload_dirs entry",
                )

    def _ordered_services(
        self,
        stack: Stack,
        *,
        target: Optional[str],
    ) -> Sequence:
        services = list(stack.services)
        if not services:
            raise ValueError("No services configured. Add stack.service(...).")

        try:
            graph = build_graph(stack)
            ordered = graph.ordered_specs(target=target)
        except DependencyError:
            raise

        self._graph = graph
        return ordered
