from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

from .adapters.detect.validation import validate_detected_services
from .discovery import STACKFILE_NAME
from .generator import StackfileExistsError, write_stackfile
from .scanner import ServiceInfo, scan_project


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Structured result for a completed sync operation."""

    services: list[ServiceInfo]
    output_path: Path
    overwritten: bool
    ports: tuple[int, ...]
    warnings: tuple[str, ...] = ()


def sync_project(
    *,
    project_root: Path,
    output_path: Path | None = None,
    force: bool = False,
) -> SyncResult:
    """
    Discover services beneath ``project_root`` and write a runnable
    ``Stackfile.py``.

    When the destination already exists and ``force`` is false, the user
    is prompted before overwriting.

    Soft validation warnings are printed but never abort sync.
    """

    root = project_root.expanduser().resolve()
    destination = (
        output_path.expanduser().resolve()
        if output_path is not None
        else root / STACKFILE_NAME
    )

    _echo("Scanning project...")
    services = scan_project(root)
    _print_discovered_services(services)

    warning_messages = tuple(
        warning.format() for warning in validate_detected_services(services)
    )
    for message in warning_messages:
        _echo(message)

    write_force = force
    if destination.exists() and not force:
        should_overwrite = typer.confirm(
            f"{destination.name} already exists. Overwrite?",
            default=False,
        )
        if not should_overwrite:
            raise StackfileExistsError(destination)
        write_force = True

    result = write_stackfile(
        services,
        project_root=root,
        output_path=destination,
        force=write_force,
    )

    count = len(services)
    _echo("")
    _echo(f"Generated {destination.name}")
    _echo(f"Found {count} service{'s' if count != 1 else ''}.")
    _echo("")
    _echo("Next: stackpilot run")

    return SyncResult(
        services=services,
        output_path=result.output_path,
        overwritten=result.overwritten,
        ports=result.ports,
        warnings=warning_messages,
    )


def _print_discovered_services(services: list[ServiceInfo]) -> None:
    if not services:
        _echo("No services discovered.")
        return

    for service in services:
        message = f"✓ {service.name} ({service.framework})"
        fallback = f"+ {service.name} ({service.framework})"
        _echo(message, fallback=fallback)


def _echo(message: str, *, fallback: str | None = None) -> None:
    from .dashboard import ascii_fallback_dx, print_safe

    print_safe(
        message,
        ascii_fallback=fallback or ascii_fallback_dx(message),
    )
