from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import typer

from . import __version__
from .dependency_graph import (
    CircularDependencyError,
    DependencyError,
    build_graph,
)
from .discovery import (
    STACKFILE_NAME,
    MISSING_STACKFILE_MESSAGE,
    StackfileNotFoundError,
    discover_project,
    find_stackfile,
)
from .doctor import format_doctor_report, run_doctor
from .diagnostics.summary import ascii_fallback_report
from .generator import EMPTY_STACKFILE, StackfileExistsError
from .graph_view import (
    format_architecture_report,
    format_circular_dependency,
    format_circular_dependency_ascii,
)
from .relation_infer import fill_missing_stack_dependencies
from .issues import (
    STATUS_ACTIVE,
    STATUS_FIXED,
    IssueTracker,
    DEFAULT_ISSUES_DIR,
    format_issues_report,
)
from .models import ServiceState
from .orchestrator import Orchestrator
from .port_detect import resolve_service_port
from .status import (
    derive_health,
    detect_framework,
    format_ps_table,
    format_status_report,
    load_runtime_snapshot,
)
from .sync import sync_project
from .utils import load_stack_from_stackfile, safe_echo

# ---------------------------------------------------------------------------
# Public CLI surface — FROZEN for v0.1.x until v0.2.0.
#
# Official commands (registration order matches ``stackpilot --help``):
#   init, sync, run, stop, graph, status, ps, issues, doctor, version
#
# This list is the public API for v0.1.0. Do NOT add, remove, or rename
# commands here without an explicit release decision. Architecture
# is likewise frozen for the v0.1.0 stabilization window.
# ---------------------------------------------------------------------------
PUBLIC_CLI_COMMANDS = (
    "init",
    "sync",
    "run",
    "stop",
    "graph",
    "status",
    "ps",
    "issues",
    "doctor",
    "version",
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "StackPilot: local microservice orchestrator.\n\n"
        "Discover services, start them in dependency order, stream logs, "
        "and track issues from a single Stackfile.py."
    ),
    epilog=(
        "Examples:\n"
        "  stackpilot sync\n"
        "  stackpilot run\n"
        "  stackpilot doctor\n\n"
        "Docs: https://github.com/stackpilot-dev/stackpilot#readme"
    ),
)


def main() -> None:
    """Console-script and ``python -m stackpilot`` entry point."""

    app()


@app.callback()
def _main() -> None:
    """StackPilot: local microservice orchestrator."""


@app.command()
def init(
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing Stackfile.py without prompting.",
    ),
) -> None:
    """Create a starter Stackfile.py in the current directory."""

    destination = Path.cwd() / STACKFILE_NAME
    if destination.exists() and not force:
        typer.secho(
            f"{STACKFILE_NAME} already exists.\n\n"
            "Use --force to overwrite, or edit the existing file.",
            err=True,
            fg="red",
        )
        raise typer.Exit(code=1)

    destination.write_text(EMPTY_STACKFILE, encoding="utf-8")
    typer.echo(f"Created {destination.resolve()}")
    typer.echo("")
    typer.echo("Next:")
    typer.echo("  stackpilot sync   # discover nested services")
    typer.echo("  stackpilot run    # start and stream logs")


@app.command()
def sync(
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite Stackfile.py without prompting.",
    ),
) -> None:
    """Discover services and write Stackfile.py."""

    # Match other CLI commands: prefer the nearest Stackfile.py ancestor.
    # When none exists yet, sync creates one under the current directory.
    stackfile = find_stackfile()
    project_root = stackfile.parent if stackfile is not None else Path.cwd()

    try:
        sync_project(project_root=project_root, force=force)
    except StackfileExistsError as e:
        typer.secho(
            f"{e}\n\n"
            "Re-run with --force to overwrite.",
            err=True,
            fg="red",
        )
        raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as e:
        typer.secho(f"Sync failed: {e}", err=True, fg="red")
        raise typer.Exit(code=1)


@app.command()
def run(
    service: Optional[str] = typer.Argument(
        None,
        help="Optional service name. Starts that service and its dependencies.",
        metavar="SERVICE",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Clear a stale StackPilot session before starting.",
    ),
) -> None:
    """
    Start services and stream live logs.

    Shows startup progress, then watches for changes until Ctrl+C.
    Use ``stackpilot status`` / ``ps`` for runtime metadata.

    \b
    Examples:
      stackpilot run
      stackpilot run auth
      stackpilot run --force
    """

    project = _require_project()
    stack = _load_stack(project.stackfile)

    from .runtime_control import (
        clear_runtime_session,
        detect_stale_session,
        format_stale_session_error,
        stop_runtime_session,
    )

    try:
        stale = detect_stale_session(project.root, stack.services)
    except Exception:
        stale = None

    if stale is not None:
        if force:
            try:
                stop_runtime_session(project.root)
            except Exception:
                clear_runtime_session(project.root)
        else:
            typer.secho(format_stale_session_error(stale), err=True, fg="red")
            raise typer.Exit(code=1)

    try:
        code = Orchestrator().run(
            stack,
            target=service,
            project_root=project.root,
        )
    except DependencyError as e:
        _fail_dependency(e)
    except ValueError as e:
        from .diagnostics.errors import format_spawn_failure, format_user_error
        from .paths import PathEscapeError

        if isinstance(e, PathEscapeError):
            message = format_user_error(
                problem="Configuration error",
                reason=str(e),
                suggested_fix="Keep service path= and reload_dirs inside the project root.",
            )
        else:
            message = format_spawn_failure(service=service or "stack", exc=e)
        typer.secho(message, err=True, fg="red")
        raise typer.Exit(code=1)
    except (OSError, FileNotFoundError, PermissionError) as e:
        from .diagnostics.errors import format_spawn_failure

        message = format_spawn_failure(service=service or "stack", exc=e)
        typer.secho(message, err=True, fg="red")
        raise typer.Exit(code=1)
    raise typer.Exit(code=code)


@app.command()
def stop() -> None:
    """Stop every service started by StackPilot."""

    project = _require_project()
    from .runtime_control import stop_runtime_session

    try:
        result = stop_runtime_session(project.root)
    except Exception as e:
        typer.secho(
            f"Failed to stop StackPilot session: {e}",
            err=True,
            fg="red",
        )
        raise typer.Exit(code=1)

    safe_echo(
        result.message,
        ascii_fallback=result.message.replace("✓", "+"),
    )
    raise typer.Exit(code=result.exit_code)


@app.command()
def graph() -> None:
    """Print a professional architecture dependency visualization."""

    project = _require_project()
    stack = fill_missing_stack_dependencies(
        _load_stack(project.stackfile),
        project_root=project.root,
    )
    try:
        dep_graph = build_graph(stack)
        dep_graph.validate()
    except CircularDependencyError as e:
        safe_echo(
            format_circular_dependency(e.cycle),
            err=True,
            fg="red",
            ascii_fallback=format_circular_dependency_ascii(e.cycle),
        )
        raise typer.Exit(code=1)
    except DependencyError as e:
        _fail_dependency(e)

    snapshot = load_runtime_snapshot(project.root)
    statuses, ports, frameworks = _graph_display_maps(stack, snapshot)
    report = format_architecture_report(
        dep_graph,
        statuses=statuses,
        ports=ports,
        frameworks=frameworks,
        unicode=True,
    )
    ascii_report = format_architecture_report(
        dep_graph,
        statuses=statuses,
        ports=ports,
        frameworks=frameworks,
        unicode=False,
    )
    _print_architecture_report(report, ascii_fallback=ascii_report)


@app.command()
def status() -> None:
    """Show runtime status (PID, port, uptime, health)."""

    project = _require_project()
    stack = _load_stack(project.stackfile)
    snapshot = load_runtime_snapshot(project.root)
    rows = _merge_status_rows(stack.services, snapshot)
    session_active = bool(snapshot and snapshot.get("session_active"))
    externals = _external_status_rows(stack.external_dependencies)
    text = format_status_report(
        project_name=project.root.name,
        services=rows,
        session_active=session_active,
        external_dependencies=externals,
    )
    typer.echo(text, nl=False)


@app.command("ps")
def ps_cmd() -> None:
    """List active StackPilot processes."""

    project = _require_project()
    stack = _load_stack(project.stackfile)
    snapshot = load_runtime_snapshot(project.root)
    rows = _merge_status_rows(stack.services, snapshot)
    typer.echo(format_ps_table(rows), nl=False)


@app.command()
def issues(
    service: Optional[str] = typer.Argument(
        None,
        help="Optional service name. Lists every issue for that service.",
        metavar="SERVICE",
    ),
    fixed: bool = typer.Option(
        False,
        "--fixed",
        help="Show recently fixed issues instead of ACTIVE ones.",
    ),
) -> None:
    """
    List service issues from .stackpilot/issues/.

    Default: ACTIVE issues only. Use ``--fixed`` for recently fixed issues.

    \b
    Examples:
      stackpilot issues
      stackpilot issues --fixed
      stackpilot issues auth
    """

    project = _require_project()
    stack = _load_stack(project.stackfile)
    if service is not None:
        known = {spec.name for spec in stack.services}
        if service not in known:
            available = ", ".join(sorted(known)) or "(none)"
            typer.secho(
                f"Unknown service: '{service}'.\n"
                f"Available: {available}",
                err=True,
                fg="red",
            )
            raise typer.Exit(code=1)

    tracker = IssueTracker(
        project.root / DEFAULT_ISSUES_DIR,
        auto_cleanup=False,
    )
    try:
        tracker.cleanup()
        if service is not None:
            items = tracker.list_issues(service=service)
            if fixed:
                items = [i for i in items if i.status == STATUS_FIXED]
            heading = f"ISSUES ({service})"
            empty = f"No issues for service '{service}'."
        elif fixed:
            items = tracker.list_issues(status=STATUS_FIXED)
            heading = "FIXED ISSUES"
            empty = "No recently fixed issues."
        else:
            items = tracker.list_issues(status=STATUS_ACTIVE)
            heading = "ACTIVE ISSUES"
            empty = "✓ No active service issues."
        text = format_issues_report(
            items,
            heading=heading,
            empty_message=empty,
        )
        safe_echo(text, ascii_fallback=text.replace("✓", "+"))
    finally:
        tracker.close()


@app.command()
def doctor() -> None:
    """Diagnose environment, Stackfile, and service configuration."""

    report = run_doctor()
    text = format_doctor_report(report)
    safe_echo(text, ascii_fallback=ascii_fallback_report(text))
    if not report.ok:
        raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """Print the installed StackPilot version."""

    typer.echo(__version__)


def _merge_status_rows(specs, snapshot: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_name: Dict[str, Dict[str, Any]] = {}
    if snapshot and isinstance(snapshot.get("services"), list):
        for raw in snapshot["services"]:
            if isinstance(raw, dict) and raw.get("name"):
                by_name[str(raw["name"])] = dict(raw)

    rows: List[Dict[str, Any]] = []
    for spec in specs:
        runtime = by_name.pop(spec.name, None)
        if runtime is not None:
            runtime.setdefault("framework", detect_framework(spec.command))
            pid = runtime.get("pid")
            use_pid = (
                pid
                if isinstance(pid, int)
                and runtime.get("status") == ServiceState.RUNNING.value
                else None
            )
            resolved = resolve_service_port(spec, pid=use_pid)
            if resolved is not None:
                runtime["port"] = resolved
            runtime["health"] = derive_health(
                str(runtime.get("status") or ServiceState.STOPPED.value)
            )
            rows.append(runtime)
            continue
        rows.append(
            {
                "name": spec.name,
                "pid": None,
                "port": resolve_service_port(spec, pid=None),
                "status": ServiceState.STOPPED.value,
                "uptime": None,
                "framework": detect_framework(spec.command),
                "command": str(spec.command or ""),
                "exit_code": None,
                "started_at": None,
                "health": derive_health(ServiceState.STOPPED),
            }
        )
    # Preserve any leftover runtime-only entries.
    rows.extend(by_name.values())
    return rows


def _graph_display_maps(stack, snapshot: Optional[Dict[str, Any]]):
    """Build status / port / framework maps for architecture rendering."""

    rows = _merge_status_rows(stack.services, snapshot)
    statuses: Dict[str, str] = {}
    ports: Dict[str, Any] = {}
    frameworks: Dict[str, str] = {}

    for row in rows:
        name = str(row.get("name") or "")
        if not name:
            continue
        statuses[name] = str(row.get("status") or ServiceState.STOPPED.value)
        if row.get("port") is not None:
            ports[name] = row.get("port")
        fw = _pretty_framework(str(row.get("framework") or ""))
        if fw:
            frameworks[name] = fw

    return statuses, ports, frameworks


def _pretty_framework(value: str) -> str:
    key = value.strip().lower()
    mapping = {
        "fastapi": "FastAPI",
        "uvicorn": "FastAPI",
        "gunicorn": "FastAPI",
        "flask": "Flask",
        "django": "Django",
        "express": "Express",
        "nestjs": "NestJS",
        "celery": "Celery",
        "python": "",
        "-": "",
    }
    if key in mapping:
        return mapping[key]
    if not value or value == "-":
        return ""
    return value[:1].upper() + value[1:]


def _print_architecture_report(report: str, *, ascii_fallback: str) -> None:
    """Print the architecture report with Rich colors when available."""

    try:
        from rich.console import Console

        from .graph_view import style_architecture_text

        Console(emoji=True, highlight=False, soft_wrap=False).print(
            style_architecture_text(report)
        )
    except (ImportError, UnicodeEncodeError, OSError):
        safe_echo(report, ascii_fallback=ascii_fallback)


def _external_status_rows(deps) -> List[Dict[str, Any]]:
    """Build status rows for external dependencies (live TCP probe)."""

    from .external_validation import check_external_dependency

    rows: List[Dict[str, Any]] = []
    for dep in deps:
        reachable = check_external_dependency(dep)
        rows.append(
            {
                "name": dep.name,
                "type": dep.type,
                "host": dep.host,
                "port": dep.port,
                "status": "reachable" if reachable else "unreachable",
            }
        )
    return rows


def _require_project():
    try:
        return discover_project()
    except StackfileNotFoundError:
        typer.secho(MISSING_STACKFILE_MESSAGE, err=True, fg="red")
        raise typer.Exit(code=1)


def _load_stack(stackfile: Path):
    try:
        return load_stack_from_stackfile(stackfile)
    except Exception as e:
        typer.secho(
            f"Failed to load {STACKFILE_NAME}: {e}\n\n"
            "Run: stackpilot doctor",
            err=True,
            fg="red",
        )
        raise typer.Exit(code=1)


def _load_project_stack():
    project = _require_project()
    return _load_stack(project.stackfile)


def _fail_dependency(error: DependencyError) -> None:
    if isinstance(error, CircularDependencyError):
        safe_echo(
            format_circular_dependency(error.cycle),
            err=True,
            fg="red",
            ascii_fallback=format_circular_dependency_ascii(error.cycle),
        )
        raise typer.Exit(code=1)
    safe_echo(
        str(error),
        err=True,
        fg="red",
        ascii_fallback=str(error).replace("↓", "v"),
    )
    raise typer.Exit(code=1)


if __name__ == "__main__":
    main()
