"""Generic fallback adapter for unrecognized runnable projects."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .base import AdapterServiceSpec, FrameworkAdapter, load_package_json
from .detect.package_manager import (
    detect_node_package_manager,
    node_run_command,
    python_run_prefix,
)
from .detect.ports import detect_preferred_port
from .detect.venv import resolve_python_executable


class GenericAdapter(FrameworkAdapter):
    """
    Catch-all adapter for plain Python or Node entrypoints.

    Matched last so specialized adapters always win.
    """

    name = "Generic"
    priority = 1000

    def detect(self, path: Path) -> bool:
        directory = path.expanduser()
        if not directory.is_dir():
            return False

        if (directory / "run.py").is_file():
            return True
        if (directory / "main.py").is_file():
            return True
        if (directory / "app.py").is_file():
            return True
        if (directory / "server.py").is_file():
            return True
        # Bare Node project without Express/NestJS still counts as a service.
        if load_package_json(directory) is not None:
            return True
        return False

    def generate_service(
        self,
        path: Path,
        *,
        port: int | None = None,
    ) -> AdapterServiceSpec:
        directory = path.expanduser()
        _ = port
        python = resolve_python_executable(directory)
        runner = python_run_prefix(directory, python=python)
        preferred = detect_preferred_port(directory)

        script: str | None = None
        for name in ("run.py", "main.py", "app.py", "server.py"):
            if (directory / name).is_file():
                script = name
                break

        if script is not None:
            command = f"{runner} {script}" if runner != python else f"{python} {script}"
        elif (package := load_package_json(directory)) is not None:
            command = node_run_command(directory, _generic_node_script(package))
        else:
            command = f"{python} main.py"

        return AdapterServiceSpec(
            framework=self.name,
            command=command,
            uses_port=False,
            health="process",
            preferred_port=preferred,
        )


def _generic_node_script(package: Mapping[str, object]) -> str:
    """Choose a sensible generic Node entry script."""

    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        return "start"

    available = {str(key) for key in scripts}
    if _looks_like_frontend_package(package) and "dev" in available:
        # Frontend dev servers often own API proxying/HMR, so prefer dev.
        return "dev"
    for name in ("start", "serve", "preview", "dev"):
        if name in available:
            return name
    return "start"


def _looks_like_frontend_package(package: Mapping[str, object]) -> bool:
    """Heuristic for browser-oriented Node apps handled by the generic adapter."""

    name = str(package.get("name", "")).lower()
    if any(token in name for token in ("frontend", "web", "client", "ui")):
        return True

    deps: set[str] = set()
    for bucket_name in (
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
    ):
        bucket = package.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        deps.update(str(key).lower() for key in bucket)

    frontend_markers = {
        "vite",
        "react",
        "react-dom",
        "next",
        "vue",
        "nuxt",
        "svelte",
        "sveltekit",
        "@sveltejs/kit",
        "@angular/core",
        "@remix-run/dev",
        "@remix-run/react",
        "solid-js",
        "astro",
    }
    return any(dep in frontend_markers for dep in deps)
