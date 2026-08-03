"""Adaptive health validation: probe ranked candidates, then TCP fallback."""

from __future__ import annotations

from typing import Callable, Sequence
from urllib.parse import urljoin, urlparse

from ...http_checker import HttpProbeResult, probe_http
from .health_routes import (
    HealthEndpointSelection,
    normalize_route,
    rank_health_routes,
    resolve_health_endpoint,
    select_best_health_path,
)


def join_base_url(base_url: str, path: str) -> str:
    """Join an origin (or base URL) with a health path."""

    base = (base_url or "").rstrip("/") + "/"
    route = normalize_route(path)
    if route == "/":
        return base.rstrip("/") + "/"
    return urljoin(base, route.lstrip("/"))


def select_working_health_endpoint(
    *,
    base_url: str,
    discovered_routes: Sequence[str] = (),
    explicit_path: str | None = None,
    request_timeout: float = 2.0,
    tcp_check: Callable[[], bool] | None = None,
    probe: Callable[[str], HttpProbeResult] | None = None,
) -> tuple[HealthEndpointSelection, HttpProbeResult | None]:
    """
    Validate health candidates with one HTTP probe each.

    Priority 1: explicit path only (never overridden).
    Priority 2: ranked discovered routes; 404 continues to the next.
    Fallback: TCP when provided and HTTP yields nothing usable.
    """

    do_probe = probe or (lambda url: probe_http(url, request_timeout=request_timeout))
    selection = resolve_health_endpoint(
        explicit_path=explicit_path,
        discovered_routes=discovered_routes,
    )

    if selection.kind == "explicit" and selection.path is not None:
        result = do_probe(join_base_url(base_url, selection.path))
        return selection, result

    candidates = rank_health_routes(discovered_routes)
    last: HttpProbeResult | None = None
    for path in candidates:
        result = do_probe(join_base_url(base_url, path))
        last = result
        if result.kind == "healthy":
            return (
                HealthEndpointSelection(
                    kind="http",
                    path=path,
                    detail="detected health endpoint",
                ),
                result,
            )
        if result.kind == "failed":
            # Endpoint exists; health failed — stop searching.
            return (
                HealthEndpointSelection(
                    kind="http",
                    path=path,
                    detail="endpoint exists; health failed",
                ),
                result,
            )
        if result.kind == "refused":
            return (
                HealthEndpointSelection(
                    kind="http",
                    path=path,
                    detail="application not started",
                ),
                result,
            )
        if result.kind == "timeout":
            return (
                HealthEndpointSelection(
                    kind="http",
                    path=path,
                    detail="application unhealthy",
                ),
                result,
            )
        # not_found / other → try next candidate

    if tcp_check is not None and tcp_check():
        return (
            HealthEndpointSelection(
                kind="tcp",
                path=None,
                detail="no HTTP health endpoint found; TCP connection successful",
            ),
            last,
        )

    return (
        HealthEndpointSelection(
            kind="tcp" if not candidates else "http",
            path=candidates[0] if candidates else None,
            detail="no HTTP health endpoint found",
        ),
        last,
    )


def format_health_diagnostic(
    *,
    configured_path: str | None,
    probe: HttpProbeResult | None,
    discovered_routes: Sequence[str] = (),
    selected: HealthEndpointSelection | None = None,
    application_running: bool = False,
) -> str:
    """Build developer-facing health diagnostic text."""

    lines: list[str] = []

    if (
        selected is not None
        and selected.kind == "http"
        and selected.path
        and probe is not None
        and probe.kind == "healthy"
    ):
        lines.extend(
            [
                "Detected health endpoint",
                "",
                selected.path,
                "",
                "Status",
                "",
                probe.detail or "200 OK",
                "",
                "Healthy",
            ]
        )
        return "\n".join(lines)

    if selected is not None and selected.kind == "tcp" and "TCP" in (
        selected.detail or ""
    ):
        lines.extend(
            [
                "No HTTP health endpoint found.",
                "",
                "TCP connection successful.",
                "",
                "Using TCP health.",
            ]
        )
        return "\n".join(lines)

    lines.append("Health endpoint not found")
    lines.append("")
    if configured_path:
        lines.append("Configured endpoint:")
        lines.append(normalize_route(configured_path))
        lines.append("")
    if probe is not None:
        lines.append("HTTP:")
        lines.append(probe.detail or probe.kind)
        lines.append("")
    if application_running:
        lines.append("Application is running.")
        lines.append("")
    if discovered_routes:
        lines.append("Detected endpoints:")
        lines.append("")
        for route in discovered_routes:
            lines.append(normalize_route(route))
        lines.append("")
        recommendation = select_best_health_path(discovered_routes)
        if recommendation:
            lines.append("Recommendation:")
            lines.append("")
            lines.append(f'Use "{recommendation}" as health endpoint')
            lines.append("or specify health_url explicitly.")
    return "\n".join(lines)


def origin_from_url(url: str) -> str:
    """Return ``scheme://host:port`` for an HTTP health URL."""

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return f"{parsed.scheme}://{parsed.netloc}"


def path_from_url(url: str) -> str:
    """Return the path component of ``url`` (default ``/``)."""

    parsed = urlparse(url)
    return normalize_route(parsed.path or "/")
