"""Helpers for choosing real ``package.json`` scripts (never invent names)."""

from __future__ import annotations

from pathlib import Path

from ..base import load_package_json


def prefer_node_script(
    directory: Path,
    names: tuple[str, ...],
    *,
    default: str | None = "start",
) -> str | None:
    """
    Return the first script in ``names`` that exists in ``package.json``.

    When none match, returns ``default`` only if that script exists; otherwise
    ``None`` so callers can fall back to ``node <main>`` instead of an invalid
    ``npm run …`` command.
    """

    data = load_package_json(directory) or {}
    scripts = data.get("scripts")
    available: set[str] = set()
    if isinstance(scripts, dict):
        available = {str(key) for key in scripts}

    for name in names:
        if name in available:
            return name
    if default is not None and default in available:
        return default
    return None


def script_implies_reload(directory: Path, script: str) -> bool:
    """True when the chosen script looks like a watched / HMR dev entry."""

    if script in {"dev", "start:dev", "watch"}:
        return True
    data = load_package_json(directory) or {}
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return False
    raw = scripts.get(script)
    if not isinstance(raw, str):
        return False
    lower = raw.lower()
    return any(
        token in lower
        for token in (
            "nodemon",
            "ts-node-dev",
            "tsx watch",
            "--watch",
            "nest start --watch",
            "vite",
        )
    )
