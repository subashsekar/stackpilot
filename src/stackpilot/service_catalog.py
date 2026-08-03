"""Optional service-catalog helpers (``services.json`` / ``services.yaml``).

Used by Stackfile generation to preserve orchestrator ``depends_on`` edges that
filesystem scanning cannot infer. Pure data loading — no discovery changes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

# Catalog external type / name -> StackPilot external_dependency name.
_EXTERNAL_NAME_ALIASES: Mapping[str, str] = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "pgsql": "postgres",
    "pg": "postgres",
    "redis": "redis",
    "mongodb": "mongodb",
    "mongo": "mongodb",
    "rabbitmq": "rabbitmq",
    "amqp": "rabbitmq",
}


def load_catalog_dependencies(
    project_root: Path,
    *,
    known_services: Sequence[str] | None = None,
    known_externals: Sequence[str] | None = None,
) -> Dict[str, Tuple[str, ...]]:
    """
    Return ``service_name -> depends_on`` from a nearby service catalog.

    Looks for ``services.json`` / ``services.yaml`` under ``project_root`` and
    one level of child directories (monorepo layouts like
    ``enterprise-test-platform/services.yaml``).

    Only keeps dependency names that exist in ``known_services`` /
    ``known_externals`` when those sets are provided. Catalog ``external``
    entries (e.g. ``postgresql``) are mapped onto registered external
    dependency names (e.g. ``postgres``).
    """

    entries = _load_catalog_entries(project_root)
    if not entries:
        return {}

    known_svc = set(known_services or ())
    known_ext = set(known_externals or ())
    restrict = bool(known_svc or known_ext)

    result: Dict[str, Tuple[str, ...]] = {}
    for name, app_deps, externals in entries:
        deps: List[str] = []
        for dep in app_deps:
            if restrict and dep not in known_svc and dep not in known_ext:
                continue
            if dep not in deps:
                deps.append(dep)
        for ext in externals:
            mapped = _map_external_name(ext, known_ext if restrict else None)
            if mapped and mapped not in deps:
                deps.append(mapped)
        if deps:
            result[name] = tuple(deps)
    return result


def _map_external_name(
    catalog_name: str,
    known_externals: set[str] | None,
) -> str | None:
    key = str(catalog_name or "").strip().lower()
    if not key:
        return None
    preferred = _EXTERNAL_NAME_ALIASES.get(key, key)
    if known_externals is None:
        return preferred
    if preferred in known_externals:
        return preferred
    if key in known_externals:
        return key
    # Match by type alias against registered names (postgres / redis).
    for registered in known_externals:
        if _EXTERNAL_NAME_ALIASES.get(registered.lower(), registered.lower()) == preferred:
            return registered
    return None


def _load_catalog_entries(
    project_root: Path,
) -> List[Tuple[str, Tuple[str, ...], Tuple[str, ...]]]:
    root = project_root.expanduser().resolve()
    for path in _iter_catalog_paths(root):
        entries = _parse_catalog_file(path)
        if entries:
            return entries
    return []


def _iter_catalog_paths(root: Path) -> List[Path]:
    names = ("services.json", "services.yaml", "services.yml")
    ordered: List[Path] = []
    for name in names:
        ordered.append(root / name)
    try:
        children = sorted(
            (p for p in root.iterdir() if p.is_dir()),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        children = []
    for child in children:
        for name in names:
            ordered.append(child / name)
    # Prefer JSON (stdlib) before YAML when both exist at the same level.
    return [path for path in ordered if path.is_file()]


def _parse_catalog_file(
    path: Path,
) -> List[Tuple[str, Tuple[str, ...], Tuple[str, ...]]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    if path.suffix.lower() == ".json":
        return _parse_catalog_json(text)
    return _parse_catalog_yaml(text)


def _parse_catalog_json(
    text: str,
) -> List[Tuple[str, Tuple[str, ...], Tuple[str, ...]]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []

    return _entries_from_services_payload(payload)


def _entries_from_services_payload(
    payload: object,
) -> List[Tuple[str, Tuple[str, ...], Tuple[str, ...]]]:
    """
    Accept catalog shapes:

    - ``{"services": [ {"name": ...}, ... ]}`` (list)
    - ``{"services": { "auth": {"depends_on": [...]}, ... }}`` (map)
    - top-level list or map of the same
    """

    raw_services: object
    if isinstance(payload, dict):
        if "services" in payload:
            raw_services = payload.get("services")
        else:
            raw_services = payload
    else:
        raw_services = payload

    entries: List[Tuple[str, Tuple[str, ...], Tuple[str, ...]]] = []

    if isinstance(raw_services, list):
        for item in raw_services:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            depends = _normalize_depends(item.get("depends_on"))
            externals = _string_tuple(item.get("external"))
            entries.append((name, depends, externals))
        return entries

    if isinstance(raw_services, dict):
        for key, item in raw_services.items():
            name = str(key).strip()
            if not name or name in {"version", "networks", "volumes"}:
                continue
            if isinstance(item, dict):
                # Skip accidental compose-like roots without depends_on/external.
                depends = _normalize_depends(item.get("depends_on"))
                externals = _string_tuple(item.get("external"))
                # Allow ``name`` override inside the map value.
                nested_name = str(item.get("name") or "").strip()
                if nested_name:
                    name = nested_name
                entries.append((name, depends, externals))
            elif item is None:
                entries.append((name, (), ()))
        return entries

    return entries


def _normalize_depends(value: object) -> Tuple[str, ...]:
    """Support list, scalar, and compose-style map ``depends_on``."""

    if value is None:
        return ()
    if isinstance(value, Mapping):
        return tuple(
            str(key).strip()
            for key in value.keys()
            if str(key).strip()
            and str(key).strip().lower()
            not in {"condition", "restart", "required"}
        )
    return _string_tuple(value)

def _parse_catalog_yaml(
    text: str,
) -> List[Tuple[str, Tuple[str, ...], Tuple[str, ...]]]:
    """
    Best-effort YAML catalog parse without requiring PyYAML.

    Supports the flat orchestrator catalog shape used by enterprise fixtures.
    """

    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None

    if yaml is not None:
        try:
            payload = yaml.safe_load(text)
        except Exception:
            payload = None
        if payload is not None:
            entries = _entries_from_services_payload(payload)
            if entries:
                return entries

    return _parse_catalog_yaml_lite(text)


def _parse_catalog_yaml_lite(
    text: str,
) -> List[Tuple[str, Tuple[str, ...], Tuple[str, ...]]]:
    """Minimal line parser for list and map catalog blocks."""

    entries: List[Tuple[str, Tuple[str, ...], Tuple[str, ...]]] = []
    current_name: str | None = None
    depends: List[str] = []
    externals: List[str] = []
    in_services_map = False
    services_indent: int | None = None

    name_re = re.compile(r"^\s*-\s+name:\s*(\S+)\s*$")
    map_service_re = re.compile(r"^(\s*)([A-Za-z0-9][A-Za-z0-9_-]*)\s*:\s*(?:#.*)?$")
    depends_re = re.compile(r"^\s*depends_on:\s*(.*)\s*$")
    external_re = re.compile(r"^\s*external:\s*(.*)\s*$")

    def flush() -> None:
        nonlocal current_name, depends, externals
        if current_name:
            entries.append((current_name, tuple(depends), tuple(externals)))
        current_name = None
        depends = []
        externals = []

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue

        if re.match(r"^services\s*:\s*$", line):
            in_services_map = True
            services_indent = len(line) - len(line.lstrip(" "))
            flush()
            continue

        match = name_re.match(line)
        if match:
            flush()
            in_services_map = False
            current_name = match.group(1).strip().strip("\"'")
            continue

        if in_services_map:
            match = map_service_re.match(line)
            if match:
                indent = len(match.group(1))
                if services_indent is None or indent > services_indent:
                    # New map key under services:
                    if current_name is None or indent <= (
                        services_indent + 2 if services_indent is not None else indent
                    ):
                        key = match.group(2)
                        if key not in {"depends_on", "external", "name"}:
                            flush()
                            current_name = key
                            continue

        if current_name is None:
            continue
        match = depends_re.match(line)
        if match:
            depends = list(_parse_yaml_list(match.group(1)))
            continue
        match = external_re.match(line)
        if match:
            externals = list(_parse_yaml_list(match.group(1)))
            continue
    flush()
    return entries

def _parse_yaml_list(value: str) -> Tuple[str, ...]:
    text = value.strip()
    if not text or text == "[]":
        return ()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return ()
        return tuple(
            part.strip().strip("\"'")
            for part in inner.split(",")
            if part.strip().strip("\"'")
        )
    return (text.strip("\"'"),)


def _string_tuple(value: object) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()
