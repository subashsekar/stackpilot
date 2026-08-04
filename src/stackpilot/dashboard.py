"""Developer-facing dashboard and summary formatting for StackPilot."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence, TextIO

PrintFn = Callable[[str], None]


def format_shutdown_summary(
    *,
    stopped_names: Sequence[str],
    total: int,
    shutdown_time_s: float,
) -> str:
    """Build the Ctrl+C shutdown summary block."""

    lines = [
        "",
        "Stopping StackPilot...",
    ]
    for name in stopped_names:
        lines.append(f"✓ {name} stopped")

    lines.extend(
        [
            "",
            "Summary:",
            f"  Services stopped: {len(stopped_names)}/{total}",
            f"  Shutdown time: {shutdown_time_s:.1f}s",
        ]
    )
    return "\n".join(lines)


def format_crash_report(
    *,
    service: str,
    exit_code: Optional[int],
    log_path: Path,
) -> str:
    """Build a crash notice that keeps the orchestration session alive."""

    code = "-" if exit_code is None else str(exit_code)
    display_path = _display_log_path(log_path)
    return "\n".join(
        [
            "",
            f"❌ {service} exited (exit {code})",
            f"  Issue: {display_path}",
            "  Remaining services continue running.",
        ]
    )


def format_ready_urls(
    entries: Sequence[tuple[str, str]],
) -> str:
    """Build the post-startup service URL block."""

    if not entries:
        return ""
    width = max(len(name) for name, _url in entries)
    lines = ["", "Services ready:"]
    for name, url in entries:
        lines.append(f"  {name.ljust(width)}  {url}")
    return "\n".join(lines)


def format_runtime_summary(
    *,
    started: int,
    total: int,
    startup_time_s: float,
) -> str:
    """Compact post-startup runtime summary (counts + elapsed)."""

    return f"✓ Started {started}/{total} services in {startup_time_s:.1f}s"


def format_application_logs_banner() -> str:
    """Separator printed once before flushing buffered startup logs."""

    return "\n".join(
        [
            "",
            "-" * 52,
            "",
            "Application Logs",
            "",
        ]
    )


def format_wave_header(wave_number: int, names: Sequence[str]) -> str:
    """Dependency-aware startup / shutdown wave banner."""

    lines = ["", f"Wave {wave_number}"]
    lines.extend(names)
    return "\n".join(lines)


def color_enabled(
    stream: Optional[TextIO] = None,
    *,
    force: Optional[bool] = None,
) -> bool:
    """
    Decide whether ANSI / Click colors should be used.

    Respects ``NO_COLOR``, ``FORCE_COLOR`` / ``CLICOLOR_FORCE``, ``CI``,
    and TTY detection (including Windows consoles).
    """

    if force is not None:
        return force
    if os.environ.get("NO_COLOR", "").strip():
        return False
    if os.environ.get("FORCE_COLOR", "").strip() or os.environ.get(
        "CLICOLOR_FORCE", ""
    ).strip():
        return True
    if os.environ.get("CI", "").strip():
        return False
    target = stream if stream is not None else sys.stdout
    isatty = getattr(target, "isatty", lambda: False)
    try:
        return bool(isatty())
    except Exception:
        return False


def style_text(text: str, *, fg: Optional[str] = None, bold: bool = False) -> str:
    """Apply Click color when available; otherwise return ``text`` unchanged."""

    if not fg and not bold:
        return text
    try:
        import click

        return click.style(text, fg=fg, bold=bold)
    except Exception:
        return text


def print_safe(
    message: str,
    *,
    ascii_fallback: Optional[str] = None,
    print_fn: Optional[PrintFn] = None,
) -> None:
    """Print Unicode-friendly DX messages with an ASCII fallback.

    Never raises ``UnicodeEncodeError`` on restricted Windows consoles.
    Prefers an encoding probe (like ``safe_echo``) before writing, then
    falls back again if the sink still rejects the payload.
    """

    emit = print_fn or (lambda s: print(s, flush=True))
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    fallback = ascii_fallback
    if fallback is None:
        fallback = ascii_fallback_dx(message)
        try:
            fallback.encode(encoding)
        except UnicodeEncodeError:
            fallback = message.encode(encoding, errors="replace").decode(encoding)

    text = message
    try:
        message.encode(encoding)
    except UnicodeEncodeError:
        text = fallback

    try:
        emit(text)
    except UnicodeEncodeError:
        emit(fallback)


def ascii_fallback_dx(text: str) -> str:
    """Replace Unicode DX marks for consoles that cannot encode them."""

    return (
        text.replace("❌", "X")
        .replace("✗", "X")
        .replace("✓", "+")
        .replace("→", "->")
        .replace("—", "-")
        .replace("–", "-")
        .replace("…", "...")
        .replace("⋯", "...")
        .replace("⚠", "!")
        .replace("·", "-")
        .replace("•", "*")
        .replace("━", "-")
        .replace("─", "-")
        .replace("│", "|")
        .replace("├", "+")
        .replace("└", "`")
        .replace("↓", "v")
        .replace("🟢", "[*]")
        .replace("🔴", "[x]")
        .replace("🟡", "[~]")
        .replace("⚪", "[ ]")
        .replace("🔵", "[#]")
    )


def _display_log_path(path: Path) -> str:
    """Prefer a project-root-relative path (``.stackpilot/...``) for display."""

    resolved = path.expanduser().resolve()
    parts = resolved.parts
    for index, part in enumerate(parts):
        if part == ".stackpilot":
            return "/".join(parts[index:])
    try:
        return str(resolved.relative_to(Path.cwd().resolve())).replace("\\", "/")
    except (OSError, ValueError):
        return str(resolved).replace("\\", "/")
