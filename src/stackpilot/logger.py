from __future__ import annotations

import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from .issues import DEFAULT_ISSUES_DIR, IssueTracker
from .dashboard import ascii_fallback_dx, print_safe

PrintFn = Callable[[str], None]
ClockFn = Callable[[], datetime]

# Historical parameter name; points at the Issue Tracker directory.
DEFAULT_LOGS_DIR = DEFAULT_ISSUES_DIR

# Spaces after the longest ``[name]`` so columns line up.
_STDOUT_GAP = 2

# Detect common log-level tokens at the start of a line (optionally wrapped).
_LEVEL_RE = re.compile(
    r"^(?:\[)?(?P<level>DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL|TRACE)(?:\])?"
    r"(?:\s*[:\-]\s*|\s+)",
    re.IGNORECASE,
)

# Python logging style: ``2026-07-27 11:45:09,291 | INFO | module | message``
_LEVEL_EMBEDDED_RE = re.compile(
    r"(?:^|[\s\|\[\]])(?P<level>DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL|TRACE)"
    r"(?:[\s\|\]:\-]|$)",
    re.IGNORECASE,
)

# Structured JSON logs: ``{"level": "INFO", ...}``
_JSON_LEVEL_RE = re.compile(
    r'"level"\s*:\s*"(?P<level>DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL|TRACE)"',
    re.IGNORECASE,
)

_WARN_HINT_RE = re.compile(r"\bUserWarning\b|\bDeprecationWarning\b|\bWarning\b")

_LEVEL_COLORS = {
    "DEBUG": "bright_black",
    "INFO": "cyan",
    "WARN": "yellow",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "red",
    "FATAL": "red",
    "TRACE": "bright_black",
}


class Logger:
    """
    Console logging for managed services, with Issue Tracker persistence.

    Console lines look like::

        12:41:22 [gateway] INFO Gateway started
        12:41:24 [payments] ERROR Connection refused

    Normal logs are streamed to the terminal only. Actionable stderr errors
    are recorded under ``.stackpilot/issues/`` via :class:`IssueTracker`.
    """

    def __init__(
        self,
        issues_dir: Optional[Path] = None,
        *,
        service_names: Optional[Sequence[str]] = None,
        print_fn: Optional[PrintFn] = None,
        clock: Optional[ClockFn] = None,
        color: Optional[bool] = None,
        issue_tracker: Optional[IssueTracker] = None,
        auto_cleanup: bool = True,
    ) -> None:
        self._issues_dir = (
            Path(issues_dir)
            if issues_dir is not None
            else Path.cwd() / DEFAULT_ISSUES_DIR
        )
        self._print_fn: PrintFn = print_fn or (lambda s: print(s, flush=True))
        self._clock: ClockFn = clock or datetime.now
        self._lock = threading.Lock()
        self._console_enabled = True
        self._buffering = False
        self._buffer: list[str] = []
        self._name_width = 0
        self._color = _resolve_color(color)
        if issue_tracker is not None:
            self._tracker = issue_tracker
        else:
            self._tracker = IssueTracker(
                self._issues_dir,
                clock=self._clock,
                auto_cleanup=auto_cleanup,
            )
        self._issues_dir = self._tracker.issues_dir
        if service_names:
            self.set_service_names(service_names)

    @property
    def issues_dir(self) -> Path:
        return self._issues_dir

    @property
    def logs_dir(self) -> Path:
        """Deprecated alias for :attr:`issues_dir`."""

        return self._issues_dir

    @property
    def issue_tracker(self) -> IssueTracker:
        return self._tracker

    def set_service_names(self, names: Sequence[str]) -> None:
        """Set names used to align ``[service]`` prefixes on stdout."""

        self._name_width = max((len(n) for n in names), default=0)

    def set_console_enabled(self, enabled: bool) -> None:
        """Toggle terminal output (keeps shutdown messages readable)."""

        with self._lock:
            self._console_enabled = enabled
            if not enabled:
                self._buffer.clear()
                self._buffering = False

    def begin_startup_buffer(self) -> None:
        """Hold console log lines until run startup messages finish."""

        with self._lock:
            self._buffering = True
            self._buffer.clear()

    def flush_startup_buffer(self) -> None:
        """Print buffered startup logs and resume live streaming."""

        with self._lock:
            lines = list(self._buffer)
            self._buffer.clear()
            self._buffering = False
        for line in lines:
            print_safe(
                line,
                ascii_fallback=ascii_fallback_dx(line),
                print_fn=self._print_fn,
            )

    def log_path(self, service: str) -> Path:
        """Return the per-service ``.issue`` path for crash-report display."""

        return self._tracker.issue_path(service)

    def stdout(self, service: str, line: str) -> None:
        level, message = detect_log_level(line, default="INFO")
        with self._lock:
            enabled = self._console_enabled
        if not enabled:
            return
        self._emit_console(self._format_console(service, level, message))

    def stderr(self, service: str, line: str) -> None:
        level, message = detect_log_level(line, default="ERROR")
        with self._lock:
            enabled = self._console_enabled
        if not enabled:
            return
        self._emit_console(self._format_console(service, level, message))

    def _emit_console(self, formatted: str) -> None:
        with self._lock:
            if not self._console_enabled:
                return
            if self._buffering:
                self._buffer.append(formatted)
                return
        print_safe(
            formatted,
            ascii_fallback=ascii_fallback_dx(formatted),
            print_fn=self._print_fn,
        )

    def error_file(self, service: str, line: str) -> None:
        """
        Persist an actionable stderr record via the Issue Tracker.

        Console streaming is handled by :meth:`stderr`. This method only
        updates ``.stackpilot/issues/`` (no duplicate ACTIVE issues).
        """

        self._tracker.ingest_stderr(service, line)

    def format_line(
        self,
        service: str,
        message: str,
        *,
        level: Optional[str] = None,
        default_level: str = "INFO",
    ) -> str:
        """Format one console log line (public for tests / tooling)."""

        if level is None:
            level, message = detect_log_level(message, default=default_level)
        return self._format_console(service, level, message)

    def _format_console(self, service: str, level: str, message: str) -> str:
        ts = self._clock().strftime("%H:%M:%S")
        width = max(self._name_width, len(service))
        pad = width - len(service) + _STDOUT_GAP
        level_token = level.upper()
        if self._color:
            level_token = _colorize_level(level_token)
            service_token = _style(f"[{service}]", fg="bright_blue")
        else:
            service_token = f"[{service}]"
        return f"{ts} {service_token}{' ' * pad}{level_token} {message}"

    def close(self) -> None:
        self._tracker.close()


def detect_log_level(line: str, *, default: str = "INFO") -> Tuple[str, str]:
    """
    Extract a log level from ``line`` when present; otherwise use ``default``.

    Returns ``(LEVEL, message)`` where message has the level prefix stripped
    when the level was a leading token. Embedded levels (Python logging / JSON)
    keep the full line as the message.
    """

    text = line.rstrip("\r\n")
    match = _LEVEL_RE.match(text)
    if match:
        level = match.group("level").upper()
        if level == "WARNING":
            level = "WARN"
        message = text[match.end() :].lstrip()
        return level, message if message else text

    json_level = _JSON_LEVEL_RE.search(text)
    if json_level:
        level = json_level.group("level").upper()
        if level == "WARNING":
            level = "WARN"
        return level, text

    embedded = _LEVEL_EMBEDDED_RE.search(text)
    if embedded:
        level = embedded.group("level").upper()
        if level == "WARNING":
            level = "WARN"
        return level, text

    if default.upper() == "ERROR" and _WARN_HINT_RE.search(text):
        return "WARN", text

    return default.upper(), text

def _colorize_level(level: str) -> str:
    fg = _LEVEL_COLORS.get(level)
    return _style(level, fg=fg) if fg else level


def _style(text: str, *, fg: Optional[str] = None) -> str:
    try:
        import click

        return click.style(text, fg=fg) if fg else text
    except Exception:
        return text


def _resolve_color(color: bool | None) -> bool:
    if color is not None:
        return color
    stream = sys.stdout
    isatty = getattr(stream, "isatty", lambda: False)
    return bool(isatty())
