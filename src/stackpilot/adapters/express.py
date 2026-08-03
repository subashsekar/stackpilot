"""Express.js framework adapter."""

from __future__ import annotations

from pathlib import Path

from .base import AdapterServiceSpec, FrameworkAdapter, load_package_json, package_has_dependency
from .detect.health_routes import discover_health_path
from .detect.package_manager import node_run_command
from .detect.ports import detect_preferred_port
from .detect.scripts import prefer_node_script, script_implies_reload


class ExpressAdapter(FrameworkAdapter):
    """Detect Express apps via ``package.json`` dependencies."""

    name = "Express"
    priority = 20

    def detect(self, path: Path) -> bool:
        directory = path.expanduser()
        if not directory.is_dir():
            return False
        if not (directory / "package.json").is_file():
            return False
        return package_has_dependency(directory, "express")

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
            ("dev", "start", "serve"),
            default=None,
        )
        if script is None:
            command = _fallback_node_command(directory)
            reload = True
        else:
            command = node_run_command(directory, script)
            reload = script_implies_reload(directory, script)

        health_path = discover_health_path(directory, self.name)
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


def _fallback_node_command(directory: Path) -> str:
    data = load_package_json(directory) or {}
    main = data.get("main")
    if isinstance(main, str) and main.strip():
        return f"node {main.strip()}"
    for candidate in ("server.js", "index.js", "app.js", "main.js"):
        if (directory / candidate).is_file():
            return f"node {candidate}"
    return "npm start"
