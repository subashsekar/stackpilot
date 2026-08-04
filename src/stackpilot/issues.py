"""Persistent Issue Tracker — current project state only.

Normal stdout/stderr still stream to the terminal via ``Logger``. Actionable
problems are stored under ``.stackpilot/issues/<service>.issue`` as compact
tables. An empty ``issues/`` directory means the project is healthy.

Status meaning
--------------
- ``ACTIVE`` — the problem is still open for this service.
- ``FIXED`` — the service recovered (or a new session cleared stale rows).
  FIXED rows are kept for one hour (full timestamps on disk), then removed.

The same error + file:line fingerprint is one logical issue: if it was FIXED
and reappears, that row is reactivated to ACTIVE instead of stacking
FIXED / ACTIVE duplicates. Warnings and warning stack snippets are not
recorded (console still shows them).
"""

from __future__ import annotations

import re
import sysconfig
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .utils import sanitize_service_name

ClockFn = Callable[[], datetime]

DEFAULT_ISSUES_DIR = Path(".stackpilot") / "issues"
ISSUE_RETENTION = timedelta(hours=1)
CLEANUP_INTERVAL_S = 180.0
# Keep assembling long application tracebacks (do not truncate mid-stream).
_MAX_TRACEBACK_LINES = 2000

STATUS_ACTIVE = "ACTIVE"
STATUS_FIXED = "FIXED"

_TABLE_HEADER = "TIME                 STATUS   ERROR                          FILE:LINE"
_TABLE_RULE = "--------------------------------------------------------------------------------"

_TRACEBACK_START = re.compile(r"^Traceback \(most recent call last\):\s*$")
_FILE_LINE_RE = re.compile(
    r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)'
)
_EXCEPTION_LINE = re.compile(
    # Allow bare Node ``Error:`` as well as ``ValueError:`` / ``ModuleNotFoundError:``.
    r"^(?P<type>(?:[A-Za-z_][\w.]*)?(?:Error|Exception|Exit|Warning|Fault|Timeout|Interrupt))"
    r"(?::\s*(?P<message>.*))?$"
)
_NODE_AT_FRAME = re.compile(r"^\s+at\s+")
# Framework / server frames that must never win as the "last application frame".
_FRAMEWORK_PATH_MARKERS = (
    "/uvicorn/",
    "/gunicorn/",
    "/hypercorn/",
    "/daphne/",
    "/flask/",
    "/django/",
    "/starlette/",
    "/fastapi/",
    "/celery/",
    "/click/",
    "/typer/",
)
_ERROR_LEVEL_RE = re.compile(
    r"^(?:\[)?(?P<level>ERROR|CRITICAL|FATAL)(?:\])?(?:\s*[:\-]\s*|\s+)",
    re.IGNORECASE,
)
_NON_ERROR_LEVEL_RE = re.compile(
    r"^(?:\[)?(?:DEBUG|INFO|WARN(?:ING)?|TRACE)(?:\])?(?:\s*[:\-]\s*|\s+)",
    re.IGNORECASE,
)
_JSON_LEVEL_RE = re.compile(
    r'"level"\s*:\s*"(?P<level>DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL|TRACE)"',
    re.IGNORECASE,
)
# Python logging style: ``2026-07-31 14:59:55,624 | service | INFO | message``
_EMBEDDED_LEVEL_RE = re.compile(
    r"(?:^|[\s\|\[\]])(?P<level>DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL|TRACE)"
    r"(?:[\s\|\]:\-]|$)",
    re.IGNORECASE,
)
# Django / gunicorn / uvicorn access lines without an explicit level token.
_HTTP_ACCESS_RE = re.compile(
    r'"[A-Z]+\s+\S+\s+HTTP/\d\.\d"\s+\d{3}\b'
)
# Python warnings (``path:line: UserWarning: …``) and bare ``UserWarning: …``.
_WARN_TYPE_RE = re.compile(
    r"(?:^|:\s*)(?P<type>[A-Za-z_][\w.]*(?:Warning))\s*:",
)
# ISO local timestamps on disk; legacy ``HH:MM:SS`` still accepted when reading.
_ROW_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}|\d{2}:\d{2}:\d{2})\s+"
    r"(?P<status>ACTIVE|FIXED)\s+"
    r"(?P<error>.+?)\s{2,}"
    r"(?P<loc>\S.*)?$"
)
_LEGACY_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
_ISO_TIME_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")


@dataclass
class IssueRow:
    """One row in a ``<service>.issue`` table."""

    time: str
    status: str
    error: str
    file_line: str = "-"


@dataclass(frozen=True, slots=True)
class ParsedException:
    """Exception type, message, and last application frame from a traceback."""

    exception_type: Optional[str] = None
    exception_message: Optional[str] = None
    file_line: Optional[str] = None


@dataclass
class Issue:
    """CLI / list view of one issue row."""

    id: str
    service: str
    status: str
    first_seen: str
    fixed_at: Optional[str]
    delete_after: Optional[str]
    root_cause: str
    exception_type: Optional[str] = None
    traceback: Optional[str] = None
    exit_code: Optional[int] = None
    file_line: str = "-"


class IssueTracker:
    """
    Maintain per-service ``.issue`` tables for the current project state.

    Same error + file:line is one fingerprint: ACTIVE duplicates are kept,
    FIXED duplicates are reactivated. FIXED rows older than one hour are
    removed; empty files are deleted. Warnings are not persisted.
    """

    def __init__(
        self,
        issues_dir: Optional[Path] = None,
        *,
        clock: Optional[ClockFn] = None,
        retention: timedelta = ISSUE_RETENTION,
        cleanup_interval_s: float = CLEANUP_INTERVAL_S,
        auto_cleanup: bool = True,
    ) -> None:
        self._issues_dir = (
            Path(issues_dir)
            if issues_dir is not None
            else Path.cwd() / DEFAULT_ISSUES_DIR
        )
        self._project_root = _infer_project_root(self._issues_dir)
        self._clock: ClockFn = clock or _default_clock
        self._retention = retention
        self._cleanup_interval_s = max(1.0, float(cleanup_interval_s))
        self._lock = threading.RLock()
        self._disabled = False
        self._warn_emitted = False
        self._traceback_buffers: Dict[str, List[str]] = {}
        # Last ``path:line`` extracted from a Python traceback per service.
        self._last_locations: Dict[str, str] = {}
        # Last parsed exception (type / message / app frame) per service.
        self._last_exceptions: Dict[str, ParsedException] = {}
        # Last raw application stderr / traceback for failure UX (in-memory).
        self._last_application_output: Dict[str, str] = {}
        # Incomplete Node.js stack buffers (Error: … / at …).
        self._node_stack_buffers: Dict[str, List[str]] = {}
        self._cleanup_timer: Optional[threading.Timer] = None
        self._closed = False

        try:
            self._issues_dir.mkdir(parents=True, exist_ok=True)
        except (PermissionError, FileExistsError, OSError, IOError) as exc:
            self._disabled = True
            self._warn_failure(exc, self._issues_dir)

        if not self._disabled:
            self.cleanup()
            if auto_cleanup:
                self._schedule_cleanup()

    @property
    def issues_dir(self) -> Path:
        return self._issues_dir

    def issue_path(self, service: str) -> Path:
        """Return the ``.issue`` path for ``service``."""

        return self._issues_dir / f"{sanitize_service_name(service)}.issue"

    # Backward-compatible alias used by older call sites / tests.
    def log_path(self, service: str) -> Path:
        return self.issue_path(service)

    def last_exception(self, service: str) -> Optional[ParsedException]:
        """Return the most recently parsed traceback exception for ``service``."""

        with self._lock:
            return self._last_exceptions.get(sanitize_service_name(service))

    def last_application_output(self, service: str) -> Optional[str]:
        """Return the last captured application stderr / traceback text."""

        with self._lock:
            text = self._last_application_output.get(sanitize_service_name(service))
            return text if text else None

    def record_error(
        self,
        service: str,
        *,
        root_cause: str,
        exception_type: Optional[str] = None,
        traceback: Optional[str] = None,
        exit_code: Optional[int] = None,
        file: Optional[str] = None,
        line: Optional[int] = None,
    ) -> Optional[Issue]:
        """
        Ensure one ACTIVE row for this error + file:line fingerprint.

        - Existing ACTIVE duplicate → unchanged.
        - Existing FIXED duplicate → reactivated to ACTIVE (same row).
        - Otherwise → append a new ACTIVE row.

        When ``traceback`` is provided, file/line/error are extracted from it.
        Full tracebacks are never stored.
        """

        error, file_line = _resolve_fields(
            root_cause=root_cause,
            exception_type=exception_type,
            traceback=traceback,
            file=file,
            line=line,
            exit_code=exit_code,
            project_root=self._project_root,
        )
        if not error:
            return None

        with self._lock:
            if self._disabled:
                return None

            # Reuse the last traceback location when the caller did not supply one
            # (e.g. status ``Service crashed`` after stderr already parsed a TB).
            if file_line == "-":
                remembered = self._last_locations.get(sanitize_service_name(service))
                if remembered:
                    file_line = remembered
            elif traceback or (file and line is not None):
                self._last_locations[sanitize_service_name(service)] = file_line

            if traceback:
                parsed = parse_traceback_exception(
                    traceback,
                    project_root=self._project_root,
                )
                key = sanitize_service_name(service)
                self._last_exceptions[key] = parsed
                self._last_application_output[key] = traceback
            elif exception_type or file_line != "-":
                key = sanitize_service_name(service)
                self._last_exceptions[key] = ParsedException(
                    exception_type=exception_type,
                    exception_message=error if exception_type else None,
                    file_line=None if file_line == "-" else file_line,
                )
                if root_cause and key not in self._last_application_output:
                    self._last_application_output[key] = root_cause
            elif root_cause:
                key = sanitize_service_name(service)
                if key not in self._last_application_output:
                    self._last_application_output[key] = root_cause

            path = self.issue_path(service)
            rows = self._read_rows(path)
            match_idx = _find_fingerprint(rows, error=error, file_line=file_line)
            if match_idx is not None:
                row = rows[match_idx]
                if row.status == STATUS_ACTIVE:
                    return self._row_to_issue(service, row, index=match_idx)
                # Reactivate FIXED → ACTIVE instead of stacking FIXED + ACTIVE.
                row.status = STATUS_ACTIVE
                row.time = self._now_time()
                # Drop older FIXED copies of the same fingerprint (history noise).
                rows = [
                    r
                    for i, r in enumerate(rows)
                    if i == match_idx or r.error != error or r.file_line != file_line
                ]
                # match_idx may shift after filtering; re-locate.
                match_idx = _find_fingerprint(rows, error=error, file_line=file_line)
                assert match_idx is not None
                self._write_rows(path, rows)
                return self._row_to_issue(service, rows[match_idx], index=match_idx)

            row = IssueRow(
                time=self._now_time(),
                status=STATUS_ACTIVE,
                error=error,
                file_line=file_line,
            )
            rows.append(row)
            self._write_rows(path, rows)
            return self._row_to_issue(service, row, index=len(rows) - 1)

    def ingest_stderr(self, service: str, line: str) -> Optional[Issue]:
        """Feed one stderr line; assemble tracebacks into a single compact row."""

        text = line.rstrip("\r\n")
        if not text.strip():
            return None

        with self._lock:
            buf = self._traceback_buffers.get(service)
            if buf is not None:
                # A new traceback header ends the previous (possibly incomplete)
                # block without recording it — start fresh.
                if _TRACEBACK_START.match(text):
                    self._traceback_buffers[service] = [text]
                    return None
                buf.append(text)
                match = _EXCEPTION_LINE.match(text)
                if match and not text.startswith((" ", "\t")):
                    tb = "\n".join(buf)
                    exc_type = match.group("type")
                    message = (match.group("message") or "").strip()
                    # Warnings are not crash issues — drop incomplete TB quietly.
                    if _is_warning_type(exc_type):
                        self._traceback_buffers.pop(service, None)
                        return None
                    root = message or exc_type
                    self._traceback_buffers.pop(service, None)
                    return self.record_error(
                        service,
                        root_cause=root,
                        exception_type=exc_type,
                        traceback=tb,
                    )
                if len(buf) > _MAX_TRACEBACK_LINES:
                    # Still try to record whatever we have rather than silently
                    # dropping a long traceback mid-stream.
                    tb = "\n".join(buf)
                    self._traceback_buffers.pop(service, None)
                    return self.record_error(
                        service,
                        root_cause="Traceback truncated (too many frames)",
                        traceback=tb,
                    )
                return None

            if _TRACEBACK_START.match(text):
                self._traceback_buffers[service] = [text]
                self._node_stack_buffers.pop(service, None)
                return None

            # Extend a pending Node.js stack with ``at …`` frames.
            node_buf = self._node_stack_buffers.get(service)
            if node_buf is not None and _NODE_AT_FRAME.match(text):
                node_buf.append(text)
                key = sanitize_service_name(service)
                self._last_application_output[key] = "\n".join(node_buf)
                if len(node_buf) > _MAX_TRACEBACK_LINES:
                    self._node_stack_buffers.pop(service, None)
                return None
            if node_buf is not None and not _NODE_AT_FRAME.match(text):
                # Stack finished; keep captured output and process this line normally.
                self._node_stack_buffers.pop(service, None)

            # Prefer exception / Error lines over log-level token matching so
            # ``Error: Cannot find module`` starts a Node stack instead of a
            # single-line ERROR issue.
            exc_match = _EXCEPTION_LINE.match(text)
            if (
                exc_match
                and not text.startswith((" ", "\t"))
                and not _TRACEBACK_START.match(text)
            ):
                exc_type = exc_match.group("type")
                if not _is_warning_type(exc_type):
                    message = (exc_match.group("message") or "").strip()
                    self._node_stack_buffers[service] = [text]
                    return self.record_error(
                        service,
                        root_cause=message or exc_type,
                        exception_type=exc_type,
                        traceback=text,
                    )

            # Skip Python warnings + indented warning source snippets. Console
            # still shows them; they are not actionable Issue Tracker rows.
            if _is_non_actionable_stderr(text):
                return None

            level = _detected_log_level(text)
            if level is not None:
                if level in {"DEBUG", "INFO", "WARN", "WARNING", "TRACE"}:
                    return None
                if level in {"ERROR", "CRITICAL", "FATAL"}:
                    message = text
                    leading = _ERROR_LEVEL_RE.match(text)
                    if leading:
                        message = text[leading.end() :].lstrip() or text
                    if _is_warning_line(message):
                        return None
                    return self.record_error(service, root_cause=message)

            # Level-less stderr: keep for bare failures (``Connection refused``),
            # but never treat routine HTTP access lines as issues.
            if _HTTP_ACCESS_RE.search(text):
                return None
            return self.record_error(service, root_cause=text)

    def has_active(self, service: str) -> bool:
        """Return True when ``service`` has at least one ACTIVE issue row."""

        with self._lock:
            if self._disabled:
                return False
            rows = self._read_rows(self.issue_path(service))
            return any(row.status == STATUS_ACTIVE for row in rows)

    def mark_fixed(self, service: str) -> List[Issue]:
        """Mark every ACTIVE row for ``service`` as FIXED (1h row retention)."""

        now_t = self._now_time()
        updated: List[Issue] = []
        with self._lock:
            if self._disabled:
                return updated
            path = self.issue_path(service)
            rows = self._read_rows(path)
            if not rows:
                return updated
            changed = False
            for idx, row in enumerate(rows):
                if row.status != STATUS_ACTIVE:
                    continue
                row.status = STATUS_FIXED
                row.time = now_t
                changed = True
                updated.append(self._row_to_issue(service, row, index=idx))
            if changed:
                self._write_rows(path, rows)
            self._traceback_buffers.pop(service, None)
            self._node_stack_buffers.pop(service, None)
            key = sanitize_service_name(service)
            self._last_locations.pop(key, None)
            self._last_exceptions.pop(key, None)
            self._last_application_output.pop(key, None)
        return updated

    def cleanup(self) -> int:
        """
        Remove FIXED rows older than one hour; delete empty ``.issue`` files.

        Returns the number of files deleted.
        """

        now = self._clock()
        files_deleted = 0
        with self._lock:
            if self._disabled:
                return 0
            for path in self._issue_paths():
                rows = self._read_rows(path)
                file_mtime = _file_mtime(path, tz=now.tzinfo)
                kept: List[IssueRow] = []
                for row in rows:
                    if row.status == STATUS_FIXED and _fixed_row_expired(
                        row.time,
                        now=now,
                        retention=self._retention,
                        file_mtime=file_mtime,
                    ):
                        continue
                    kept.append(row)
                if not kept:
                    try:
                        if path.is_file():
                            path.unlink(missing_ok=True)
                            files_deleted += 1
                    except (PermissionError, OSError, IOError) as exc:
                        self._warn_failure(exc, path)
                elif len(kept) != len(rows):
                    self._write_rows(path, kept)
        return files_deleted

    def list_issues(
        self,
        *,
        service: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Issue]:
        """Return rows filtered by optional service name and/or status."""

        with self._lock:
            paths = self._issue_paths()
            items: List[Issue] = []
            for path in paths:
                svc = path.stem
                if service is not None and svc != service:
                    continue
                rows = self._read_rows(path)
                for idx, row in enumerate(rows):
                    if status is not None and row.status.upper() != status.upper():
                        continue
                    items.append(self._row_to_issue(svc, row, index=idx))
        items.sort(key=lambda i: (i.service, i.id))
        return items

    def close(self) -> None:
        """Stop periodic cleanup."""

        with self._lock:
            self._closed = True
            timer = self._cleanup_timer
            self._cleanup_timer = None
        if timer is not None:
            timer.cancel()

    def _schedule_cleanup(self) -> None:
        with self._lock:
            if self._closed or self._disabled:
                return
            timer = threading.Timer(self._cleanup_interval_s, self._cleanup_tick)
            timer.daemon = True
            self._cleanup_timer = timer
            timer.start()

    def _cleanup_tick(self) -> None:
        try:
            self.cleanup()
        finally:
            reschedule = True
            with self._lock:
                if self._closed or self._disabled:
                    reschedule = False
            if reschedule:
                self._schedule_cleanup()

    def _row_to_issue(self, service: str, row: IssueRow, *, index: int) -> Issue:
        now = self._clock()
        fixed_at = row.time if row.status == STATUS_FIXED else None
        delete_after: Optional[str] = None
        if row.status == STATUS_FIXED:
            parsed = _parse_row_timestamp(now, row.time)
            if parsed is not None:
                delete_after = (parsed + self._retention).strftime("%Y-%m-%dT%H:%M:%S")
        return Issue(
            id=f"{service}#{index + 1}",
            service=service,
            status=row.status,
            first_seen=row.time,
            fixed_at=fixed_at,
            delete_after=delete_after,
            root_cause=row.error,
            file_line=row.file_line,
        )

    def _write_rows(self, path: Path, rows: List[IssueRow]) -> None:
        if self._disabled:
            return
        if not rows:
            try:
                path.unlink(missing_ok=True)
            except (PermissionError, OSError, IOError) as exc:
                self._disabled = True
                self._warn_failure(exc, path)
            return
        try:
            if not self._issues_dir.is_dir():
                self._issues_dir.mkdir(parents=True, exist_ok=True)
            text = render_issue_table(rows)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except (PermissionError, FileExistsError, OSError, IOError) as exc:
            self._disabled = True
            self._warn_failure(exc, path)

    def _read_rows(self, path: Path) -> List[IssueRow]:
        try:
            if not path.is_file():
                return []
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, IOError, UnicodeError):
            return []
        return parse_issue_table(text)

    def _issue_paths(self) -> List[Path]:
        if self._disabled or not self._issues_dir.is_dir():
            return []
        return sorted(self._issues_dir.glob("*.issue"))

    def _now_time(self) -> str:
        # Full local timestamp so FIXED retention survives overnight.
        return self._clock().strftime("%Y-%m-%dT%H:%M:%S")

    def _warn_failure(self, exc: BaseException, path: Path) -> None:
        if self._warn_emitted:
            return
        self._warn_emitted = True
        strerror = getattr(exc, "strerror", None) or str(exc).strip() or type(exc).__name__
        from .dashboard import print_safe

        print_safe(
            "Warning:\n"
            "Unable to write service issue file:\n"
            f"{strerror}: {path.name}\n\n"
            "Continuing without issue persistence.",
            ascii_fallback=(
                "Warning:\n"
                "Unable to write service issue file:\n"
                f"{strerror}: {path.name}\n\n"
                "Continuing without issue persistence."
            ),
        )


def render_issue_table(rows: Sequence[IssueRow]) -> str:
    """Render the compact issue table."""

    lines = [_TABLE_HEADER, _TABLE_RULE]
    for row in rows:
        error = row.error if len(row.error) >= 30 else f"{row.error:<30}"
        loc = row.file_line or "-"
        lines.append(f"{row.time:<19}   {row.status:<6}   {error}   {loc}")
    return "\n".join(lines) + "\n"


def parse_issue_table(text: str) -> List[IssueRow]:
    """Parse rows from a ``.issue`` table file."""

    rows: List[IssueRow] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("TIME") or set(line) <= {"-"}:
            continue
        match = _ROW_RE.match(line)
        if not match:
            # Fallback: split on 2+ spaces
            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) < 3:
                continue
            time_s, status, error = parts[0], parts[1], parts[2]
            loc = parts[3] if len(parts) > 3 else "-"
            if status not in {STATUS_ACTIVE, STATUS_FIXED}:
                continue
            rows.append(
                IssueRow(time=time_s, status=status, error=error.strip(), file_line=loc.strip() or "-")
            )
            continue
        loc = (match.group("loc") or "-").strip() or "-"
        rows.append(
            IssueRow(
                time=match.group("time"),
                status=match.group("status"),
                error=match.group("error").strip(),
                file_line=loc,
            )
        )
    return rows


def extract_traceback_location(
    traceback: str,
    *,
    project_root: Optional[Path] = None,
) -> tuple[Optional[str], Optional[int]]:
    """
    Return ``(path, line)`` from the deepest application frame.

    Prefer a project-relative path (``auth/database.py``). Frames under
    ``site-packages``, ``dist-packages``, the Python stdlib, or common
    ASGI/WSGI server packages (uvicorn, gunicorn, …) are skipped.
    When no application frame exists, return ``(None, None)``.
    """

    file_name: Optional[str] = None
    line_no: Optional[int] = None
    for line in traceback.splitlines():
        match = _FILE_LINE_RE.match(line)
        if not match:
            continue
        raw_path = match.group("file")
        if not _is_application_frame(raw_path):
            continue
        file_name = format_source_path(raw_path, project_root=project_root)
        line_no = int(match.group("line"))
    return file_name, line_no


def parse_traceback_exception(
    traceback: str,
    *,
    project_root: Optional[Path] = None,
) -> ParsedException:
    """
    Extract exception type, message, and last application ``file:line``.

    Skips framework frames (e.g. uvicorn) so FILE:LINE points at application
    code such as ``app/dependencies/auth.py:12``.
    """

    exc_type: Optional[str] = None
    message: Optional[str] = None
    for tb_line_text in reversed(traceback.splitlines()):
        stripped = tb_line_text.strip()
        match = _EXCEPTION_LINE.match(stripped)
        if match and not tb_line_text.startswith((" ", "\t")):
            exc_type = match.group("type")
            message = (match.group("message") or "").strip() or None
            break

    file_name, line_no = extract_traceback_location(
        traceback,
        project_root=project_root,
    )
    file_line: Optional[str] = None
    if file_name and line_no is not None:
        file_line = f"{file_name}:{line_no}"
    elif file_name:
        file_line = file_name

    return ParsedException(
        exception_type=exc_type,
        exception_message=message,
        file_line=file_line,
    )


def format_source_path(
    file_path: str,
    *,
    project_root: Optional[Path] = None,
) -> str:
    """
    Normalize a traceback path for FILE:LINE display.

    When ``project_root`` is known, return a posix path relative to that root
    (``stackpilot-test/auth/database.py``). Relative traceback paths keep their
    folders. Absolute paths outside the project fall back to the basename.
    """

    text = (file_path or "").strip()
    if not text:
        return ""

    normalized = text.replace("\\", "/")
    candidate = Path(text)
    # On Windows, POSIX-style ``/app/x.py`` is not Path.is_absolute().
    looks_absolute = candidate.is_absolute() or normalized.startswith("/")

    if project_root is not None:
        try:
            root = Path(project_root).expanduser().resolve()
            if looks_absolute:
                resolved = candidate.expanduser().resolve()
            else:
                resolved = (root / candidate).resolve()
            return resolved.relative_to(root).as_posix()
        except (OSError, ValueError, RuntimeError):
            pass

    if not looks_absolute:
        if normalized.startswith("./"):
            return normalized[2:]
        return normalized

    return Path(normalized).name


def _infer_project_root(issues_dir: Path) -> Optional[Path]:
    """Return project root when ``issues_dir`` is ``<root>/.stackpilot/issues``."""

    try:
        resolved = Path(issues_dir).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if resolved.name == "issues" and resolved.parent.name == ".stackpilot":
        return resolved.parent.parent
    return None


def _is_application_frame(file_path: str) -> bool:
    """True when ``file_path`` looks like project / application code."""

    text = (file_path or "").strip()
    if not text:
        return False

    # Synthetic / frozen interpreter frames.
    if text.startswith("<") or text.endswith(">"):
        return False

    normalized = text.replace("\\", "/")
    lower = normalized.lower()
    if "site-packages/" in lower or lower.endswith("/site-packages"):
        return False
    if "dist-packages/" in lower or lower.endswith("/dist-packages"):
        return False

    # Common ASGI/WSGI / framework packages — never treat as app frames even
    # when installed editable outside site-packages.
    for marker in _FRAMEWORK_PATH_MARKERS:
        if marker in lower:
            return False

    # Stdlib layouts commonly seen in tracebacks (POSIX + Windows installs).
    if re.search(r"/lib(?:64)?/python\d", lower):
        return False
    if re.search(r"/python\d+(?:\.\d+)*/lib/", lower):
        return False

    # Relative paths (e.g. ``backend/database.py``) are application code.
    candidate = Path(text)
    if not candidate.is_absolute():
        return True

    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return True

    for root in _non_application_roots():
        try:
            resolved.relative_to(root)
            return False
        except ValueError:
            continue
    return True


def _non_application_roots() -> List[Path]:
    """Stdlib and site-package roots that should not appear in FILE:LINE."""

    roots: List[Path] = []
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        raw = sysconfig.get_path(key)
        if not raw:
            continue
        try:
            roots.append(Path(raw).resolve())
        except (OSError, RuntimeError):
            continue

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: List[Path] = []
    for root in roots:
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def format_issues_report(
    issues: Sequence[Issue],
    *,
    heading: str,
    empty_message: str,
) -> str:
    """Format issues for ``stackpilot issues`` as a diagnostics table."""

    if not issues:
        return empty_message if empty_message.endswith("\n") else empty_message + "\n"

    headers = ("TIME", "SERVICE", "SEVERITY", "STATUS", "REASON", "RESOLUTION")
    rows: List[tuple[str, ...]] = []
    for issue in issues:
        reason = issue.root_cause
        location = ""
        if issue.file_line and issue.file_line != "-":
            location = f" ({issue.file_line})"
        rows.append(
            (
                _short_issue_time(issue.first_seen),
                issue.service,
                _issue_severity(issue),
                issue.status,
                f"{reason}{location}",
                _issue_resolution(issue),
            )
        )

    lines = [heading, "-" * len(heading), "", _format_issues_table(headers, rows)]
    return "\n".join(lines).rstrip() + "\n"


def _short_issue_time(value: str) -> str:
    text = (value or "").strip()
    if "T" in text:
        # ISO → HH:MM:SS
        try:
            return text.split("T", 1)[1][:8]
        except Exception:
            return text
    if " " in text and len(text) >= 19:
        return text.split(" ", 1)[1][:8]
    return text or "-"


def _issue_severity(issue: Issue) -> str:
    if issue.exception_type:
        return "error"
    cause = (issue.root_cause or "").lower()
    if "warn" in cause:
        return "warning"
    if issue.exit_code not in (None, 0):
        return "error"
    return "error"


def _issue_resolution(issue: Issue) -> str:
    cause = (issue.root_cause or "").lower()
    if "connection refused" in cause or "not reachable" in cause:
        return "Start dependency; check host/port"
    if "module" in cause and "not found" in cause:
        return "Install package or fix import path"
    if "address already in use" in cause or "port" in cause and "in use" in cause:
        return "Free the port or change service port"
    if "permission" in cause:
        return "Fix file/process permissions"
    if issue.file_line and issue.file_line != "-":
        return f"Inspect {issue.file_line}"
    if issue.status == STATUS_FIXED:
        return "Resolved"
    return "See service logs / stackpilot doctor"


def _format_issues_table(
    headers: Sequence[str], rows: Sequence[Sequence[str]]
) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    # Cap reason / resolution so wide terminals stay readable.
    widths[4] = min(max(widths[4], 24), 64)
    widths[5] = min(max(widths[5], 16), 40)

    def _clip(text: str, width: int) -> str:
        raw = str(text)
        if len(raw) <= width:
            return raw.ljust(width)
        if width <= 3:
            return raw[:width]
        return raw[: width - 3] + "..."

    head = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = [
        "  ".join(_clip(cell, widths[i]) for i, cell in enumerate(row))
        for row in rows
    ]
    return "\n".join([head, sep, *body])


def _find_fingerprint(
    rows: Sequence[IssueRow],
    *,
    error: str,
    file_line: str,
) -> Optional[int]:
    """Return index of the newest row matching error + file:line, if any."""

    for idx in range(len(rows) - 1, -1, -1):
        row = rows[idx]
        if row.error == error and row.file_line == file_line:
            return idx
    return None


def _detected_log_level(text: str) -> Optional[str]:
    """
    Return a normalized log level when ``text`` carries one.

    Handles leading tokens (``INFO …``), JSON (``"level": "INFO"``), and
    embedded Python-logging styles (``… | INFO | …``).
    """

    leading = re.match(
        r"^(?:\[)?(?P<level>DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL|TRACE)"
        r"(?:\])?(?:\s*[:\-]\s*|\s+)",
        text,
        re.IGNORECASE,
    )
    if leading:
        return leading.group("level").upper()

    json_level = _JSON_LEVEL_RE.search(text)
    if json_level:
        return json_level.group("level").upper()

    embedded = _EMBEDDED_LEVEL_RE.search(text)
    if embedded:
        return embedded.group("level").upper()

    return None


def _is_warning_type(exc_type: Optional[str]) -> bool:
    return bool(exc_type) and str(exc_type).endswith("Warning")


def _is_warning_line(text: str) -> bool:
    """True for Python warning headers (``path:line: UserWarning: …``)."""

    return _WARN_TYPE_RE.search(text) is not None


def _is_non_actionable_stderr(text: str) -> bool:
    """
    Skip noise that should not become Issue Tracker rows.

    - Python ``UserWarning`` / ``DeprecationWarning`` / … headers
    - Indented warning source snippets (``  validated_self = …``)
    - Explicit non-error log levels already filtered elsewhere
    - Flask / Werkzeug informational startup banners
    """

    if text.startswith((" ", "\t")):
        return True
    if _is_warning_line(text):
        return True
    try:
        from .logger import is_framework_info_line

        if is_framework_info_line(text):
            return True
    except Exception:
        pass
    return False


def _resolve_fields(
    *,
    root_cause: str,
    exception_type: Optional[str],
    traceback: Optional[str],
    file: Optional[str],
    line: Optional[int],
    exit_code: Optional[int],
    project_root: Optional[Path] = None,
) -> tuple[str, str]:
    error = (root_cause or "").strip()
    file_name = (
        format_source_path(file, project_root=project_root) if file else None
    )
    line_no = line

    if traceback:
        tb_file, tb_line = extract_traceback_location(
            traceback,
            project_root=project_root,
        )
        if tb_file:
            file_name = tb_file
        if tb_line is not None:
            line_no = tb_line
        # Prefer message from the final exception line when present.
        for tb_line_text in reversed(traceback.splitlines()):
            match = _EXCEPTION_LINE.match(tb_line_text.strip())
            if match and not tb_line_text.startswith(" "):
                message = (match.group("message") or "").strip()
                error = message or match.group("type") or error
                if not exception_type:
                    exception_type = match.group("type")
                break

    if not error:
        if exception_type:
            error = exception_type
        elif exit_code is not None:
            error = f"Service crashed (exit code {exit_code})"
        else:
            return "", "-"

    if file_name and line_no is not None:
        file_line = f"{file_name}:{line_no}"
    elif file_name:
        file_line = file_name
    else:
        file_line = "-"
    return error, file_line


def _parse_row_timestamp(now: datetime, time_s: str) -> Optional[datetime]:
    """Parse a row TIME value (ISO preferred; legacy ``HH:MM:SS`` supported)."""

    text = (time_s or "").strip()
    if not text:
        return None

    for fmt in _ISO_TIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if now.tzinfo is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=now.tzinfo)
        return dt

    return _combine_time(now, text)


def _fixed_row_expired(
    time_s: str,
    *,
    now: datetime,
    retention: timedelta,
    file_mtime: Optional[datetime],
) -> bool:
    """True when a FIXED row should be removed."""

    fixed_at = _parse_row_timestamp(now, time_s)
    if fixed_at is not None and (now - fixed_at) >= retention:
        return True

    # Legacy ``HH:MM:SS`` ages wrap every day, so a row fixed yesterday at
    # 15:00 looks "20 minutes old" when checked today at 15:20. Trust the
    # file mtime as a backstop for those old tables.
    if (
        _LEGACY_TIME_RE.match((time_s or "").strip())
        and file_mtime is not None
        and (now - file_mtime) >= retention
    ):
        return True

    return False


def _file_mtime(path: Path, *, tz: Optional[tzinfo]) -> Optional[datetime]:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    if tz is not None:
        return datetime.fromtimestamp(stamp, tz=tz)
    return datetime.fromtimestamp(stamp)


def _combine_time(now: datetime, time_s: str) -> Optional[datetime]:
    try:
        hour, minute, second = (int(p) for p in time_s.split(":"))
    except ValueError:
        return None
    dt = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    # If TIME is ahead of now (crossed midnight), treat it as yesterday.
    if dt - now > timedelta(minutes=5):
        dt -= timedelta(days=1)
    return dt


def _default_clock() -> datetime:
    return datetime.now(timezone.utc).astimezone()
