"""Manage per-service hot-reload watchers."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .config import ServiceSpec
from .ignore import IgnoreMatcher
from .paths import ensure_within_project
from .watcher import (
    DEFAULT_DEBOUNCE_S,
    ChangeCallback,
    ServiceWatcher,
    has_native_reload,
    should_takeover_native_reload,
)
from .dashboard import ascii_fallback_dx, print_safe


class WatchManager:
    """
    Create and tear down ``ServiceWatcher`` instances for reloadable services.

    Lifecycle ownership stays with the Runner: this class only decides which
    services get a watcher and forwards debounced change notifications.
    """

    def __init__(
        self,
        *,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._debounce_s = debounce_s
        self._log = log or (
            lambda message: print_safe(
                message, ascii_fallback=ascii_fallback_dx(message)
            )
        )
        self._watchers: Dict[str, ServiceWatcher] = {}
        self._on_change: Optional[ChangeCallback] = None

    @property
    def watched_services(self) -> Sequence[str]:
        return tuple(self._watchers.keys())

    def get_watcher(self, name: str) -> Optional[ServiceWatcher]:
        return self._watchers.get(name)

    def start(
        self,
        specs: Sequence[ServiceSpec],
        on_change: ChangeCallback,
        *,
        project_root: Optional[Path] = None,
    ) -> None:
        """
        Start a watcher for reloadable services.

        Watches when ``reload=True``, or when Windows must take over uvicorn
        ``--reload`` (native reload would Ctrl+C the whole stack). Elsewhere,
        services that already use framework-native reload are skipped.
        """

        self.stop()
        self._on_change = on_change
        root = (project_root or Path.cwd()).expanduser().resolve()

        for spec in specs:
            takeover = should_takeover_native_reload(spec.command)
            if not spec.reload and not takeover:
                continue

            if has_native_reload(spec.command) and not takeover:
                self._log("Native reload enabled. Skipping StackPilot watcher.")
                continue

            if takeover:
                self._log(
                    f"{spec.name}: StackPilot reload enabled "
                    "(Windows: framework-native reload disabled for this stack)."
                )

            watch_dirs = resolve_reload_dirs(spec, project_root=root)
            ignore_root = watch_dirs[0] if watch_dirs else (spec.path.resolve())
            # Prefer project-level .stackpilotignore when available.
            matcher_root = root if (root / ".stackpilotignore").is_file() else ignore_root
            ignore = IgnoreMatcher(matcher_root)

            watcher = ServiceWatcher(
                spec.name,
                watch_dirs,
                on_change,
                debounce_s=self._debounce_s,
                ignore=ignore,
            )
            watcher.start()
            self._watchers[spec.name] = watcher

    def stop(self) -> None:
        for watcher in list(self._watchers.values()):
            try:
                watcher.stop()
            except Exception:
                pass
        self._watchers.clear()
        self._on_change = None


def resolve_reload_dirs(
    spec: ServiceSpec,
    *,
    project_root: Optional[Path] = None,
) -> List[Path]:
    """
    Resolve directories to watch for ``spec``.

    Empty ``reload_dirs`` means watch ``spec.path``. Relative entries are
    resolved against ``spec.path``. When ``project_root`` is provided, every
    resolved directory must stay inside that root.
    """

    base = spec.path.expanduser().resolve()
    if project_root is not None:
        ensure_within_project(base, project_root, label=f"service {spec.name!r} path")

    if not spec.reload_dirs:
        dirs = [base]
    else:
        resolved: List[Path] = []
        for entry in spec.reload_dirs:
            path = Path(entry).expanduser()
            if not path.is_absolute():
                path = base / path
            resolved.append(path.resolve())
        dirs = resolved or [base]

    if project_root is not None:
        return [
            ensure_within_project(
                path,
                project_root,
                label=f"service {spec.name!r} reload dir",
            )
            for path in dirs
        ]
    return dirs
