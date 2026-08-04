"""Format doctor reports with colored symbols, grouped sections, and a summary."""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

import click

from .models import CheckStatus, DoctorCheck, DoctorReport
from ..dashboard import color_enabled

# Ordered section headers → check names that belong in each section.
_SECTION_ORDER: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "Environment",
        (
            "Python version",
            "Package import",
            "CLI executable available",
            "Project artifact permissions",
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
        "Runtime",
        (
            "Runtime status integrity",
            "Orphan StackPilot processes",
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
            "Env files readable",
        ),
    ),
)

# Concise healthy labels (category → display name).
_CONCISE_LABELS: Tuple[Tuple[str, str], ...] = (
    ("Environment", "Python"),
    ("Configuration", "Stackfile"),
    ("Dependencies", "Dependencies"),
    ("Health Checks", "Health"),
    ("Ports", "Ports"),
    ("External Dependencies", "External"),
    ("Runtime", "Runtime"),
)


def format_doctor_report(report: DoctorReport, *, color: bool | None = None) -> str:
    """
    Render a doctor report.

    Healthy runs stay concise (category checklist + pass count). Warnings and
    errors expand only the affected sections with full detail.
    """

    use_color = color_enabled(force=color)
    ok_mark, fail_mark, warn_mark = _marks(use_color)

    if report.error_count == 0 and report.warning_count == 0:
        return _format_healthy_report(report, ok_mark=ok_mark, use_color=use_color)

    return _format_detailed_report(
        report,
        ok_mark=ok_mark,
        fail_mark=fail_mark,
        warn_mark=warn_mark,
        use_color=use_color,
    )


def _format_healthy_report(
    report: DoctorReport,
    *,
    ok_mark: str,
    use_color: bool,
) -> str:
    by_name: Dict[str, List[DoctorCheck]] = {}
    for check in report.checks:
        by_name.setdefault(check.name, []).append(check)

    seen: set[str] = set()
    lines: List[str] = ["StackPilot doctor", ""]
    for section, label in _CONCISE_LABELS:
        names = dict(_SECTION_ORDER).get(section, ())
        section_checks = _collect_section(by_name, names, seen)
        if not section_checks:
            continue
        lines.append(f"{ok_mark} {label}")

    leftovers = [c for c in report.checks if c.name not in seen]
    if leftovers:
        lines.append(f"{ok_mark} Other")

    lines.append("")
    count_line = f"{report.passed_count} checks passed"
    lines.append(click.style(count_line, fg="green") if use_color else count_line)
    lines.append("")
    good = "Everything looks good."
    lines.append(click.style(good, fg="green") if use_color else good)
    lines.append("")
    lines.append("Next: stackpilot run")
    return "\n".join(lines).rstrip() + "\n"


def _format_detailed_report(
    report: DoctorReport,
    *,
    ok_mark: str,
    fail_mark: str,
    warn_mark: str,
    use_color: bool,
) -> str:
    lines: List[str] = ["StackPilot doctor", ""]
    by_name: Dict[str, List[DoctorCheck]] = {}
    for check in report.checks:
        by_name.setdefault(check.name, []).append(check)

    seen: set[str] = set()
    for section, names in _SECTION_ORDER:
        section_checks = _collect_section(by_name, names, seen)
        if not section_checks:
            continue
        has_problem = any(c.status != CheckStatus.OK for c in section_checks)
        if not has_problem:
            # Collapse healthy sections to one line.
            label = dict(_CONCISE_LABELS).get(section, section)
            lines.append(f"{ok_mark} {label}")
            continue
        lines.append(_section_header(section, use_color=use_color))
        for check in section_checks:
            if check.status == CheckStatus.OK:
                lines.append(f"{ok_mark} {check.name}")
            else:
                lines.extend(_format_check(check, ok_mark, fail_mark, warn_mark))
        lines.append("")

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
    # Skip blank OK details — they add noise without information.
    detail = (check.detail or "").strip()
    if detail and not (
        check.status == CheckStatus.OK and detail.lower() in {"ok", "passed", "pass"}
    ):
        for detail_line in detail.splitlines():
            if detail_line.strip():
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
        f"{label('Passed', 'green')}: {passed}  "
        f"{label('Warnings', 'yellow')}: {warnings}  "
        f"{label('Errors', 'red')}: {errors}",
        "",
    ]

    if errors == 0 and warnings == 0:
        good = "Everything looks good."
        lines.append(click.style(good, fg="green") if use_color else good)
        lines.append("")
        lines.append("Next: stackpilot run")
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
        .replace("—", "-")
    )
