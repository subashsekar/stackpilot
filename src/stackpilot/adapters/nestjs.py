"""NestJS framework adapter."""

from __future__ import annotations

import re
from pathlib import Path

from .base import (
    AdapterServiceSpec,
    FrameworkAdapter,
    load_package_json,
    package_has_dependency,
    read_text,
)
from .detect.health_routes import discover_health_path
from .detect.package_manager import node_run_command
from .detect.ports import detect_preferred_port
from .detect.scan import iter_project_files
from .detect.scripts import prefer_node_script, script_implies_reload


class NestJSAdapter(FrameworkAdapter):
    """Detect NestJS apps via ``@nestjs/core`` in package.json."""

    name = "NestJS"
    priority = 10

    def detect(self, path: Path) -> bool:
        directory = path.expanduser()
        if not directory.is_dir():
            return False
        if not (directory / "package.json").is_file():
            return False
        return package_has_dependency(directory, "@nestjs/core")

    def generate_service(
        self,
        path: Path,
        *,
        port: int | None = None,
    ) -> AdapterServiceSpec:
        directory = path.expanduser()
        _ = port  # Bound via launch_env PORT=; do not invent CLI flags.
        script = prefer_node_script(
            directory,
            ("start:dev", "start", "dev"),
            default=None,
        )
        if script is None:
            command = _fallback_node_command(directory)
            reload = True
        else:
            command = node_run_command(directory, script)
            reload = script_implies_reload(directory, script) or script in {
                "start:dev",
                "dev",
            }

        health_path = discover_health_path(directory, self.name)
        # NestJS root ``@Get()`` → ``/`` is a business route, not a health
        # endpoint. Only /health-like paths (or Terminus) count as HTTP health.
        if health_path == "/":
            health_path = None
        if health_path is None:
            health_path = _nestjs_fallback_health_path(directory)
        preferred = detect_preferred_port(directory)
        return AdapterServiceSpec(
            framework=self.name,
            command=command,
            uses_port=True,
            health="http" if health_path is not None else "tcp",
            health_path=health_path or "/health",
            preferred_port=preferred,
            reload=reload,
        )


def _nestjs_fallback_health_path(directory: Path) -> str | None:
    """
    Prefer HTTP ``/health`` when the app clearly exposes it.

    Covers ``@nestjs/terminus`` and common HealthController layouts that the
    decorator walk may miss. Returns ``None`` when nothing indicates /health
    so callers fall back to TCP.
    """

    if package_has_dependency(directory, "@nestjs/terminus"):
        return "/health"

    for path in iter_project_files(directory, suffixes=(".ts", ".js"), max_depth=5):
        text = read_text(path)
        if not text:
            continue
        lower = text.lower()
        compact = lower.replace(" ", "")
        if "@healthcheck" in lower or "healthcheckservice" in lower:
            return "/health"
        if "healthcheckcontroller" in compact or "healthcontroller" in compact:
            return "/health"
        if "from '@nestjs/terminus'" in lower or 'from "@nestjs/terminus"' in lower:
            return "/health"
        if "@controller('health')" in lower or '@controller("health")' in lower:
            return "/health"
        if "@controller" in lower and "path:" in lower and "health" in lower:
            return "/health"
        # ``@Get('health')`` / ``@Get("/health")`` on any controller.
        if re.search(r"""@get\s*\(\s*['\"]/?health['\"]""", lower):
            return "/health"
    return None


def _fallback_node_command(directory: Path) -> str:
    data = load_package_json(directory) or {}
    main = data.get("main")
    if isinstance(main, str) and main.strip():
        return f"node {main.strip()}"
    for candidate in ("main.js", "dist/main.js", "src/main.js", "index.js"):
        if (directory / candidate).is_file():
            return f"node {candidate}"
    return "npm start"
