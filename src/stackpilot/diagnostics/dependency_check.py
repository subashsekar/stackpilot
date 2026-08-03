"""Dependency graph diagnostics (reuses ``dependency_graph``)."""

from __future__ import annotations

from ..dependency_graph import (
    CircularDependencyError,
    DependencyGraph,
    DuplicateServiceError,
    MissingDependencyError,
    build_graph,
    format_cycle_path,
)
from .models import CheckStatus, DiagnosticContext, DoctorCheck


def check_dependencies(ctx: DiagnosticContext) -> None:
    """
    Validate the service dependency graph.

    Reuses ``DependencyGraph.validate()`` so doctor and runtime share one
    source of truth for missing deps and cycles.
    """

    if ctx.stack is None:
        return

    specs = list(ctx.stack.services)
    if not specs and not ctx.stack.external_dependencies:
        ctx.add(
            DoctorCheck(
                name="Dependency graph",
                status=CheckStatus.OK,
                detail="No services — dependency graph is empty",
            )
        )
        ctx.add(
            DoctorCheck(
                name="Missing dependencies",
                status=CheckStatus.OK,
                detail="No dependencies to validate",
            )
        )
        ctx.add(
            DoctorCheck(
                name="Circular dependencies",
                status=CheckStatus.OK,
                detail="No dependency cycles possible",
            )
        )
        return

    try:
        graph = build_graph(ctx.stack)
    except DuplicateServiceError as exc:
        # Unique-name check already covers this; keep graph checks informative.
        ctx.add(
            DoctorCheck(
                name="Dependency graph",
                status=CheckStatus.FAIL,
                detail=str(exc),
                fix="Give each stack.service(...) a unique name.",
            )
        )
        ctx.add(
            DoctorCheck(
                name="Missing dependencies",
                status=CheckStatus.FAIL,
                detail="Skipped — duplicate service names prevent graph build.",
                fix="Fix duplicate names, then re-run: stackpilot doctor",
            )
        )
        ctx.add(
            DoctorCheck(
                name="Circular dependencies",
                status=CheckStatus.FAIL,
                detail="Skipped — duplicate service names prevent graph build.",
                fix="Fix duplicate names, then re-run: stackpilot doctor",
            )
        )
        return

    _validate_graph(ctx, graph)


def _validate_graph(ctx: DiagnosticContext, graph: DependencyGraph) -> None:
    try:
        graph.validate()
    except MissingDependencyError as exc:
        ctx.add(
            DoctorCheck(
                name="Dependency graph",
                status=CheckStatus.FAIL,
                detail=str(exc),
                fix="Add the missing service or remove it from depends_on.",
            )
        )
        ctx.add(
            DoctorCheck(
                name="Missing dependencies",
                status=CheckStatus.FAIL,
                detail=str(exc),
                fix="Add the missing service or remove it from depends_on.",
            )
        )
        ctx.add(
            DoctorCheck(
                name="Circular dependencies",
                status=CheckStatus.WARN,
                detail="Skipped — fix missing dependencies before cycle detection.",
                fix="Resolve missing dependencies, then re-run: stackpilot doctor",
            )
        )
        return
    except CircularDependencyError as exc:
        cycle_text = format_cycle_path(exc.cycle)
        ctx.add(
            DoctorCheck(
                name="Dependency graph",
                status=CheckStatus.FAIL,
                detail=f"Circular dependency detected:\n{cycle_text}",
                fix="Break the cycle in depends_on relationships.",
            )
        )
        ctx.add(
            DoctorCheck(
                name="Missing dependencies",
                status=CheckStatus.OK,
                detail="All depends_on names resolve to registered services",
            )
        )
        ctx.add(
            DoctorCheck(
                name="Circular dependencies",
                status=CheckStatus.FAIL,
                detail=f"Cycle: {' → '.join(exc.cycle)}",
                fix="Break the cycle in depends_on relationships.",
            )
        )
        return

    ctx.add(
        DoctorCheck(
            name="Dependency graph",
            status=CheckStatus.OK,
            detail=(
                f"Valid graph with {len(graph.specs)} service(s)"
                + (
                    f" and {len(graph.external)} external dependency(ies)"
                    if graph.external
                    else ""
                )
            ),
        )
    )
    ctx.add(
        DoctorCheck(
            name="Missing dependencies",
            status=CheckStatus.OK,
            detail="All depends_on names resolve to registered services or external dependencies",
        )
    )
    ctx.add(
        DoctorCheck(
            name="Circular dependencies",
            status=CheckStatus.OK,
            detail="No circular dependencies",
        )
    )
