"""Per-service filesystem watcher with debounced change detection."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
    FileSystemMovedEvent,
)
from watchdog.observers import Observer

from .ignore import IgnoreMatcher

DEFAULT_DEBOUNCE_S = 0.3

# (service_name, changed_paths) — paths are absolute strings.
ChangeCallback = Callable[[str, Sequence[str]], None]
FileSignature = Tuple[int, int]  # (mtime_ns, size)

# uvicorn --reload* flags that take a following value.
_RELOAD_FLAGS_WITH_VALUE = frozenset(
    {
        "--reload-dir",
        "--reload-dirs",
        "--reload-delay",
        "--reload-include",
        "--reload-exclude",
    }
)


def _command_tokens(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, (list, tuple)):
        return [str(part) for part in command]
    text = str(command).strip()
    if not text:
        return []
    try:
        from .utils import _split_command_text

        return _split_command_text(text)
    except ValueError:
        return text.split()


def has_uvicorn_reload_flag(command: str | Sequence[str]) -> bool:
    """True when argv/command includes uvicorn-style ``--reload``."""

    for token in _command_tokens(command):
        lower = token.lower()
        if lower == "--reload" or lower.startswith("--reload="):
            return True
        if lower in _RELOAD_FLAGS_WITH_VALUE:
            return True
        if any(lower.startswith(f"{flag}=") for flag in _RELOAD_FLAGS_WITH_VALUE):
            return True
    return False


def has_django_runserver(command: str | Sequence[str]) -> bool:
    """True when ``command`` launches Django's ``runserver``."""

    tokens = _command_tokens(command)
    lowered = [t.lower() for t in tokens]
    for index, token in enumerate(lowered):
        if token != "runserver":
            continue
        if index == 0:
            return True
        prev = lowered[index - 1].replace("\\", "/").rsplit("/", 1)[-1]
        if prev == "manage.py" or prev in {"django", "django-admin"}:
            return True
    return False


def django_runserver_has_autoreload(command: str | Sequence[str]) -> bool:
    """True when Django ``runserver`` would enable StatReloader."""

    if not has_django_runserver(command):
        return False
    lowered = [t.lower() for t in _command_tokens(command)]
    return "--noreload" not in lowered


def has_flask_debug(command: str | Sequence[str]) -> bool:
    """True when ``command`` enables Flask debug / reloader."""

    tokens = _command_tokens(command)
    lowered = [t.lower() for t in tokens]
    if not lowered:
        return False
    if "flask" in lowered and "--debug" in lowered:
        return True
    joined = " ".join(lowered)
    if "flask" in lowered and (
        "flask_debug=1" in joined or "flask_env=development" in joined
    ):
        return True
    return False


def should_takeover_native_reload(command: str | Sequence[str]) -> bool:
    """
    True when StackPilot must own reload instead of the framework.

    On Windows, uvicorn ``--reload`` sends ``CTRL_C_EVENT`` to restart the
    worker; that console signal interrupts the StackPilot parent and shuts
    down the whole stack. Django ``runserver`` autoreload is also taken over
    on Windows so StackPilot can restart the full process on file changes
    (failed Django reloads can leave a dead server thread while the parent
    stays "alive"). Flask ``--debug`` is taken over for the same reason.
    """

    if sys.platform != "win32":
        return False
    if has_uvicorn_reload_flag(command):
        return True
    if django_runserver_has_autoreload(command):
        return True
    return has_flask_debug(command)


def strip_native_reload_argv(argv: Sequence[str]) -> list[str]:
    """Disable framework-native reload so StackPilot can own restarts."""

    out: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        lower = token.lower()
        if lower == "--reload" or lower.startswith("--reload="):
            continue
        if lower in _RELOAD_FLAGS_WITH_VALUE:
            skip_next = True
            continue
        if any(lower.startswith(f"{flag}=") for flag in _RELOAD_FLAGS_WITH_VALUE):
            continue
        if lower == "--debug" and "flask" in [t.lower() for t in out]:
            # Drop Flask --debug under Windows takeover.
            continue
        out.append(token)

    # Django runserver autoreloads by default; disable it for takeover.
    if django_runserver_has_autoreload(out):
        out.append("--noreload")
    return out


def has_native_reload(command: str | Sequence[str]) -> bool:
    """
    Return True when ``command`` already enables framework-native reload.

    Detects patterns such as ``uvicorn ... --reload``, Django ``runserver``,
    and ``flask --debug``.
    """

    tokens = _command_tokens(command)
    lowered = [t.lower() for t in tokens]
    if not lowered:
        return False

    if has_uvicorn_reload_flag(tokens):
        return True

    if django_runserver_has_autoreload(tokens):
        return True

    if has_flask_debug(tokens):
        return True

    return False


def format_changed_paths(
    paths: Sequence[str | Path],
    *,
    relative_to: Path | None = None,
) -> str:
    """Format changed paths as ``'folder/file.py'`` for reload messages."""

    labels: List[str] = []
    root = relative_to.expanduser().resolve() if relative_to is not None else None
    for raw in paths:
        path = Path(raw)
        label: str
        if root is not None:
            try:
                label = path.resolve().relative_to(root).as_posix()
            except (ValueError, OSError):
                # Fall back to parent/name so we still show a folder when possible.
                try:
                    label = f"{path.parent.name}/{path.name}"
                except Exception:
                    label = path.name
        else:
            try:
                label = f"{path.parent.name}/{path.name}"
            except Exception:
                label = path.name
        if label not in labels:
            labels.append(label)
    if not labels:
        return "'<unknown>'"
    return ", ".join(f"'{label}'" for label in labels)


class ServiceWatcher:
    """
    Watch one service's directories and invoke ``on_change`` after debounce.

    Detects create, modify, delete, and rename events. Ignored paths never
    schedule a restart.
    """

    def __init__(
        self,
        service_name: str,
        watch_dirs: Sequence[Path],
        on_change: ChangeCallback,
        *,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        ignore: Optional[IgnoreMatcher] = None,
        observer: Optional[Observer] = None,
    ) -> None:
        self.service_name = service_name
        self._watch_dirs = [Path(d).expanduser().resolve() for d in watch_dirs]
        self._on_change = on_change
        self._debounce_s = max(0.0, float(debounce_s))
        self._ignore = ignore
        self._observer = observer or Observer()
        self._owns_observer = observer is None
        self._handler = _DebouncedHandler(
            service_name=service_name,
            on_change=on_change,
            debounce_s=self._debounce_s,
            ignore=ignore,
        )
        self._started = False

    @property
    def watch_dirs(self) -> Sequence[Path]:
        return tuple(self._watch_dirs)

    @property
    def handler(self) -> "_DebouncedHandler":
        """Expose the event handler for unit tests."""

        return self._handler

    def start(self) -> None:
        if self._started:
            return
        for directory in self._watch_dirs:
            directory.mkdir(parents=True, exist_ok=True)
            self._handler.prime(directory)
            self._observer.schedule(self._handler, str(directory), recursive=True)
        if self._owns_observer:
            self._observer.start()
        self._started = True

    def stop(self) -> None:
        self._handler.cancel()
        if not self._started:
            return
        if self._owns_observer:
            self._observer.stop()
            try:
                self._observer.unschedule_all()
            except Exception:
                pass
            self._observer.join(timeout=5.0)
        self._started = False
        # Drop the change callback so a late debounce fire cannot restart work.
        self._on_change = None
        self._handler._on_change = lambda *_args, **_kwargs: None

    def notify_for_tests(self, path: Path, *, event_type: str = "modified") -> None:
        """Synthesize a filesystem event (used by unit tests)."""

        self._handler.handle_path(path, event_type=event_type)


class _DebouncedHandler(FileSystemEventHandler):
    """Accumulate filesystem events and fire once after a quiet period."""

    def __init__(
        self,
        *,
        service_name: str,
        on_change: ChangeCallback,
        debounce_s: float,
        ignore: Optional[IgnoreMatcher],
    ) -> None:
        super().__init__()
        self._service_name = service_name
        self._on_change = on_change
        self._debounce_s = debounce_s
        self._ignore = ignore
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._pending = False
        self._pending_paths: Set[str] = set()
        self._signatures: Dict[str, FileSignature] = {}
        # Windows: paths waiting on a deferred mtime/size recheck after an
        # early "modified" notification arrived before the writer flushed.
        self._recheck_scheduled: Set[str] = set()
        self._recheck_timers: List[threading.Timer] = []
        # Attempt index per path for staggered Windows rechecks.
        self._recheck_attempts: Dict[str, int] = {}
        self.fire_count = 0

    # Staggered delays (seconds) for Windows early-notify rechecks. A single
    # short sleep is not enough for some editors / AV flushes; giving up too
    # early silently drops real reloads.
    _WIN_RECHECK_DELAYS_S: Tuple[float, ...] = (0.08, 0.22, 0.45)

    def on_created(self, event: FileSystemEvent) -> None:
        self._consider(event.src_path, event_type="created", is_directory=event.is_directory)

    def on_modified(self, event: FileSystemEvent) -> None:
        # Directory "modified" events are noisy on some platforms (especially
        # Windows) when a child file changes; only act on file modifications.
        if event.is_directory:
            return
        self._consider(event.src_path, event_type="modified", is_directory=False)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._consider(event.src_path, event_type="deleted", is_directory=event.is_directory)

    def on_moved(self, event: FileSystemMovedEvent) -> None:
        self._consider(event.src_path, event_type="moved", is_directory=event.is_directory)
        dest = getattr(event, "dest_path", None)
        if dest:
            self._consider(dest, event_type="moved", is_directory=event.is_directory)

    def handle_path(self, path: Path | str, *, event_type: str = "modified") -> None:
        """Test helper: feed a path through the same ignore + debounce path."""

        self._consider(str(path), event_type=event_type, is_directory=False)

    def prime(self, root: Path) -> None:
        """Capture a baseline of existing files so open/focus noise won't reload."""

        try:
            walk_root = str(root)
        except OSError:
            return

        # Prune ignored directories while walking so large trees (node_modules,
        # .git, .venv) never get scanned. rglob alone still visits every file.
        for dirpath, dirnames, filenames in os.walk(walk_root, topdown=True):
            keep: List[str] = []
            for name in dirnames:
                child = Path(dirpath) / name
                if self._should_ignore(child):
                    continue
                # Fast path for well-known junk even without an IgnoreMatcher.
                if name.lower() in {
                    ".git",
                    "__pycache__",
                    ".pytest_cache",
                    ".mypy_cache",
                    "node_modules",
                    ".venv",
                    "venv",
                    ".logs",
                    ".stackpilot",
                    "dist",
                    "build",
                }:
                    continue
                keep.append(name)
            dirnames[:] = keep

            for name in filenames:
                path = Path(dirpath) / name
                if self._should_ignore(path):
                    continue
                if not path.is_file():
                    continue
                sig = self._signature(path)
                if sig is not None:
                    try:
                        key = str(path.resolve())
                    except OSError:
                        key = str(path)
                    self._signatures[key] = sig

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            for timer in self._recheck_timers:
                try:
                    timer.cancel()
                except Exception:
                    pass
            self._recheck_timers.clear()
            self._recheck_scheduled.clear()
            self._recheck_attempts.clear()
            self._pending = False
            self._pending_paths.clear()

    def _consider(
        self,
        raw_path: str,
        *,
        event_type: str,
        is_directory: bool,
    ) -> None:
        if event_type in {"opened", "closed", "closed_no_write"}:
            return
        if is_directory:
            if event_type == "deleted":
                self._forget_tree(Path(raw_path))
                self._schedule(str(Path(raw_path)))
            return
        path = Path(raw_path)
        if self._should_ignore(path):
            return
        if not self._is_real_change(path, event_type=event_type):
            # Windows ReadDirectoryChangesW often notifies before the writer
            # flushes the new mtime/size. A single 20ms re-stat drops real
            # edits; defer staggered rechecks instead of discarding the event.
            if (
                sys.platform == "win32"
                and event_type == "modified"
                and not is_directory
            ):
                self._schedule_recheck(path)
            return
        # Real change observed — cancel any pending recheck bookkeeping.
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        with self._lock:
            self._recheck_attempts.pop(key, None)
        self._schedule(str(path))

    def _schedule_recheck(self, path: Path) -> None:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        with self._lock:
            if key in self._recheck_scheduled:
                return
            attempt = int(self._recheck_attempts.get(key, 0))
            if attempt >= len(self._WIN_RECHECK_DELAYS_S):
                self._recheck_attempts.pop(key, None)
                return
            delay = self._WIN_RECHECK_DELAYS_S[attempt]
            self._recheck_attempts[key] = attempt + 1
            self._recheck_scheduled.add(key)

            def _recheck() -> None:
                with self._lock:
                    self._recheck_scheduled.discard(key)
                    try:
                        self._recheck_timers.remove(timer)
                    except ValueError:
                        pass
                # Re-enter consider; if signature still matches, another
                # staggered attempt is scheduled until delays are exhausted.
                self._consider(
                    str(path),
                    event_type="modified",
                    is_directory=False,
                )

            timer = threading.Timer(delay, _recheck)
            timer.daemon = True
            self._recheck_timers.append(timer)
            timer.start()

    def _is_real_change(self, path: Path, *, event_type: str) -> bool:
        key = str(path.resolve())
        if event_type == "deleted":
            existed = key in self._signatures
            self._forget_tree(path)
            return existed

        sig = self._signature(path)
        if event_type == "moved":
            # Rename source may already be gone (atomic replace / save-as).
            if sig is None:
                existed = key in self._signatures
                self._forget_tree(path)
                return existed
            self._signatures[key] = sig
            # Dest of an atomic replace / rename always counts — even when
            # content length matches the previous file (editor rewrite).
            return True

        if sig is None:
            return False

        old = self._signatures.get(key)
        self._signatures[key] = sig
        if event_type == "created":
            # Genuinely new path, or recreate after delete. An identical
            # signature to the primed baseline is open/focus noise.
            return old is None or old != sig
        if event_type == "modified":
            if old != sig:
                return True
            if sys.platform == "win32" and old is not None:
                # Brief re-stat for writers that flush within a few ms.
                # Unchanged results fall through to a deferred recheck in
                # ``_consider`` so late flushes still trigger reload.
                time.sleep(0.02)
                sig2 = self._signature(path)
                if sig2 is None:
                    return False
                self._signatures[key] = sig2
                return sig2 != old
            return False
        return True

    def _forget_tree(self, path: Path) -> None:
        """Drop signatures for ``path`` and every recorded descendant."""

        try:
            target = str(path.resolve())
        except OSError:
            target = str(path)

        # Prefix match avoids resolving every signature key on large trees.
        # Compare case-insensitively on Windows where path casing can drift.
        if sys.platform == "win32":
            target_key = target.casefold()
            prefix = target_key if target_key.endswith("\\") else target_key + "\\"
            # Also accept forward-slash variants from mixed path styles.
            prefix_alt = prefix.replace("\\", "/")
            target_alt = target_key.replace("\\", "/")
            drop: List[str] = []
            for key in self._signatures:
                folded = key.casefold()
                folded_slash = folded.replace("\\", "/")
                if (
                    folded == target_key
                    or folded_slash == target_alt
                    or folded.startswith(prefix)
                    or folded_slash.startswith(prefix_alt)
                ):
                    drop.append(key)
            for key in drop:
                self._signatures.pop(key, None)
            return

        prefix = target if target.endswith(os.sep) else target + os.sep
        drop = [
            key
            for key in self._signatures
            if key == target or key.startswith(prefix)
        ]
        for key in drop:
            self._signatures.pop(key, None)

    def _signature(self, path: Path) -> Optional[FileSignature]:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (int(stat.st_mtime_ns), int(stat.st_size))

    def _should_ignore(self, path: Path) -> bool:
        # Editing the ignore file should not restart the service.
        if path.name == ".stackpilotignore":
            return True

        # Editor / OS transient save artifacts (VS Code, Cursor, PyCharm,
        # vim, emacs). Ignoring them prevents duplicate reload storms during
        # atomic replace (write-temp → rename) and rapid multi-save.
        name = path.name
        lower_name = name.lower()
        if (
            lower_name.endswith(".tmp")
            or lower_name.endswith(".temp")
            or lower_name.endswith(".swp")
            or lower_name.endswith(".swo")
            or lower_name.endswith("~")
            or name.startswith(".#")
            or name.endswith("---jb_tmp___")
            or name.endswith("---jb_old___")
            or "___jb_tmp___" in name
            or "___jb_old___" in name
            or lower_name.endswith(".partial")
        ):
            return True

        if self._ignore is not None:
            return self._ignore.ignored(path)

        # Fallback: skip common junk when no matcher was provided.
        parts = {p.lower() for p in path.parts}
        for junk in (
            ".git",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            "node_modules",
            ".venv",
            ".logs",
            ".stackpilot",
            "dist",
            "build",
        ):
            if junk in parts:
                return True
        if path.suffix.lower() in {".pyc", ".pyo", ".log"}:
            return True
        return False

    def _schedule(self, path: str) -> None:
        with self._lock:
            self._pending = True
            self._pending_paths.add(path)
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(self._debounce_s, self._fire)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _fire(self) -> None:
        with self._lock:
            if not self._pending:
                return
            self._pending = False
            self._timer = None
            paths = tuple(sorted(self._pending_paths))
            self._pending_paths.clear()
            self.fire_count += 1
        try:
            self._on_change(self._service_name, paths)
        except Exception as exc:
            # Never let callback failures kill the observer thread, but do not
            # swallow them silently — that looks like "reload never happens".
            try:
                from .dashboard import ascii_fallback_dx, print_safe

                msg = (
                    f"stackpilot: reload callback failed for "
                    f"{self._service_name!r}: {exc}"
                )
                print_safe(msg, ascii_fallback=ascii_fallback_dx(msg))
            except Exception:
                pass
