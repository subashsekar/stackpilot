"""Format doctor reports with colored symbols, grouped sections, and a summary."""

from __future__ import annotations

import sys
from typing import Dict, List, Sequence, Tuple

import click

from .models import CheckStatus, DoctorCheck, DoctorReport

# Ordered section headers → check names that belong in each section.
_SECTION_ORDER: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "Environment",
        (
            "Python version",
            "Package import",
            "CLI executable available",
        ),
    ),
    (
        "Dependencies",
        (
            "Missing dependencies",
            "Circular dependencies",
            "Dependency graph",
        ),
    ),
    (
        "Ports",
        (
            "Duplicate ports",
            "Ports available",
        ),
    ),
    (
        "Health Checks",
        ("Health check configuration",),
    ),
    (
        "External Dependencies",
        (
            "External dependencies",
            "External dependency configuration",
            "External dependencies reachable",
        ),
    ),
    (
        "Configuration",
        (
            "Stackfile.py exists",
            "Inside StackPilot project",
            "Stackfile imports successfully",
            "Stack object created",
            "Services discovered",
            "Service names unique",
            "Service paths exist",
            "Service commands valid",
        ),
    ),
)


def format_doctor_report(report: DoctorReport, *, color: bool | None = None) -> str:
    """
    Render a full doctor report with grouped sections.

    Sections: Environment, Configuration, Dependencies, Ports, Health Checks.
    Symbols: ``✓`` success (green), ``✗`` error (red), ``!`` warning (yellow).
    """

    use_color = _resolve_color(color)
    ok_mark, fail_mark, warn_mark = _marks(use_color)

    lines: List[str] = ["StackPilot doctor", ""]
    by_name: Dict[str, List[DoctorCheck]] = {}
    for check in report.checks:
        by_name.setdefault(check.name, []).append(check)

    seen: set[str] = set()
    for section, names in _SECTION_ORDER:
        section_checks = _collect_section(by_name, names, seen)
        if not section_checks:
            continue
        lines.append(_section_header(section, use_color=use_color))
        for check in section_checks:
            lines.extend(_format_check(check, ok_mark, fail_mark, warn_mark))
        lines.append("")

    # Any checks not mapped to a known section (future-proof).
    leftovers = [c for c in report.checks if c.name not in seen]
    if leftovers:
        lines.append(_section_header("Other", use_color=use_color))
        for check in leftovers:
            lines.extend(_format_check(check, ok_mark, fail_mark, warn_mark))
        lines.append("")

    lines.extend(_summary_lines(report, use_color=use_color))
    return "\n".join(lines).rstrip() + "\n"


def _collect_section(
    by_name: Dict[str, List[DoctorCheck]],
    names: Sequence[str],
    seen: set[str],
) -> List[DoctorCheck]:
    out: List[DoctorCheck] = []
    for name in names:
        for check in by_name.get(name, ()):
            out.append(check)
            seen.add(name)
    return out


def _section_header(title: str, *, use_color: bool) -> str:
    if use_color:
        return click.style(title, fg="bright_white", bold=True)
    return title


def _format_check(
    check: DoctorCheck,
    ok_mark: str,
    fail_mark: str,
    warn_mark: str,
) -> List[str]:
    mark = {
        CheckStatus.OK: ok_mark,
        CheckStatus.FAIL: fail_mark,
        CheckStatus.WARN: warn_mark,
    }[check.status]
    lines = [f"{mark} {check.name}"]
    for detail_line in check.detail.splitlines() or [""]:
        lines.append(f"  {detail_line}")
    if check.fix and check.status != CheckStatus.OK:
        fix_lines = check.fix.splitlines()
        lines.append(f"  Fix: {fix_lines[0]}")
        for extra in fix_lines[1:]:
            lines.append(f"       {extra}")
    return lines


def _summary_lines(report: DoctorReport, *, use_color: bool) -> List[str]:
    passed = report.passed_count
    warnings = report.warning_count
    errors = report.error_count

    def label(text: str, fg: str) -> str:
        return click.style(text, fg=fg) if use_color else text

    lines = [
        f"{label('Checks Passed', 'green')}: {passed}",
        f"{label('Warnings', 'yellow')}: {warnings}",
        f"{label('Errors', 'red')}: {errors}",
        "",
    ]

    if errors == 0:
        good = "Everything looks good."
        lines.append(click.style(good, fg="green") if use_color else good)
        lines.append("")
        lines.append("Run:")
        lines.append("")
        lines.append("stackpilot run")
    return lines


def _marks(use_color: bool) -> Tuple[str, str, str]:
    ok_s, fail_s, warn_s = "✓", "✗", "!"
    if not use_color:
        return ok_s, fail_s, warn_s
    return (
        click.style(ok_s, fg="green"),
        click.style(fail_s, fg="red"),
        click.style(warn_s, fg="yellow"),
    )


def ascii_fallback_report(text: str) -> str:
    """Replace Unicode doctor marks when the console encoding cannot print them."""

    return (
        text.replace("✓", "[OK]")
        .replace("✗", "[FAIL]")
        .replace("→", "->")
    )


def _resolve_color(color: bool | None) -> bool:
    if color is not None:
        return color
    stream = sys.stdout
    isatty = getattr(stream, "isatty", lambda: False)
    return bool(isatty())
