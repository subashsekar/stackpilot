"""Infer microservice ``depends_on`` edges from project artifacts.

Universal relation discovery for projects that lack an explicit Stackfile
``depends_on`` or ``services.yaml`` catalog. Sources (merged, de-duplicated):

1. ``docker-compose*.yml`` ``depends_on`` blocks
2. Inter-service URL / name references inside each service directory
   (``AUTH_SERVICE_URL``, ``auth_service_url``, ``http://auth_service:8001``,
   ``localhost:<peer-port>``, etc.)
3. External infrastructure references (``REDIS_URL``, ``redis://``,
   ``postgresql://``, ``mongodb://``, ``amqp://``, compose aliases, default
   infra ports) mapped onto registered ``external_dependency`` names

Does not change discovery or adapter matching — read-only filesystem scan.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple

from .config import ExternalDependency, ServiceSpec, Stack
from .port_detect import resolve_service_port

_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".github",
        ".idea",
        ".vscode",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".stackpilot",
        ".microstack",
        "dist",
        "build",
        "htmlcov",
        ".tox",
        "migrations",
        "alembic",
    }
)

_SCAN_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".env",
        ".yml",
        ".yaml",
        ".json",
        ".toml",
        ".ini",
        ".cfg",
        ".txt",
    }
)

_COMPOSE_DEP_SKIP: frozenset[str] = frozenset(
    {
        "condition",
        "restart",
        "required",
        "capabilities",
        "preferences",
    }
)

_MAX_FILE_BYTES = 400_000

# docker-compose service key -> optional StackPilot external name
_COMPOSE_EXTERNAL_ALIASES: Mapping[str, str] = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "db": "postgres",
    "redis": "redis",
    "cache": "redis",
    "mongodb": "mongodb",
    "mongo": "mongodb",
    "rabbitmq": "rabbitmq",
    "amqp": "rabbitmq",
}

# URI scheme / type hint -> canonical external type key
_EXTERNAL_SCHEME_TYPES: Mapping[str, str] = {
    "redis": "redis",
    "rediss": "redis",
    "postgres": "postgres",
    "postgresql": "postgres",
    "mongodb": "mongodb",
    "mongo": "mongodb",
    "amqp": "rabbitmq",
    "amqps": "rabbitmq",
}

# Well-known env / settings keys that imply an external dependency type.
_EXTERNAL_ENV_KEYS: Mapping[str, str] = {
    "REDIS_URL": "redis",
    "REDIS_URI": "redis",
    "CACHE_URL": "redis",
    "CACHE_URI": "redis",
    "POSTGRES_URL": "postgres",
    "POSTGRES_URI": "postgres",
    "POSTGRESQL_URL": "postgres",
    "POSTGRESQL_URI": "postgres",
    "PG_URL": "postgres",
    "DATABASE_URL": "postgres",
    "DB_URL": "postgres",
    "MONGODB_URL": "mongodb",
    "MONGODB_URI": "mongodb",
    "MONGO_URL": "mongodb",
    "MONGO_URI": "mongodb",
    "AMQP_URL": "rabbitmq",
    "AMQP_URI": "rabbitmq",
    "RABBITMQ_URL": "rabbitmq",
    "RABBITMQ_URI": "rabbitmq",
    "BROKER_URL": "rabbitmq",
    "CELERY_BROKER_URL": "rabbitmq",
}

# Default TCP ports for infrastructure when an external of that type exists.
_EXTERNAL_DEFAULT_PORTS: Mapping[int, str] = {
    6379: "redis",
    5432: "postgres",
    27017: "mongodb",
    5672: "rabbitmq",
}


def infer_service_dependencies(
    *,
    project_root: Path,
    services: Sequence[ServiceSpec],
    external_dependencies: Sequence[ExternalDependency] | None = None,
) -> Dict[str, Tuple[str, ...]]:
    """
    Return ``service_name -> depends_on`` inferred from the project tree.

    Only names present in ``services`` / ``external_dependencies`` are kept.
    """

    if not services:
        return {}

    root = project_root.expanduser().resolve()
    externals = list(external_dependencies or ())
    known_services = {spec.name: spec for spec in services}
    known_external_names = {dep.name for dep in externals}
    known_all = set(known_services) | known_external_names
    type_to_external = _external_type_index(externals)

    port_to_service = _port_index(services)
    # Include default infra ports when those externals are registered.
    for port, type_key in _EXTERNAL_DEFAULT_PORTS.items():
        mapped = type_to_external.get(type_key)
        if mapped and port not in port_to_service:
            port_to_service[port] = mapped

    aliases = _build_alias_index(known_services)
    aliases.update(_build_external_alias_index(externals, type_to_external))

    result: Dict[str, List[str]] = {name: [] for name in known_services}
    compose_edges: Dict[str, List[str]] = {name: [] for name in known_services}

    # --- docker-compose depends_on (preferred for startup order) ----------
    for compose_path in _iter_compose_files(root):
        for name, deps in _parse_compose_depends_on(compose_path).items():
            if name not in known_services:
                continue
            for dep in deps:
                mapped = _map_compose_dep(dep, known_all, known_external_names)
                if mapped and mapped != name:
                    _add_dep(compose_edges, name, mapped)
                    _add_dep(result, name, mapped)

    # --- per-service code / config scan (skip edges that create cycles) ---
    for spec in services:
        found = _scan_service_directory(
            spec,
            aliases=aliases,
            port_to_service=port_to_service,
            self_name=spec.name,
            known_external_names=known_external_names,
            type_to_external=type_to_external,
        )
        for dep in found:
            if dep not in known_all or dep == spec.name:
                continue
            if _reaches(result, dep, spec.name):
                # Adding spec -> dep would close a cycle; prefer existing edge.
                continue
            _add_dep(result, spec.name, dep)

    return {
        name: tuple(deps)
        for name, deps in result.items()
        if deps
    }


def resolve_dependency_map(
    *,
    project_root: Path,
    services: Sequence[ServiceSpec],
    external_dependencies: Sequence[ExternalDependency] | None = None,
    catalog: Mapping[str, Tuple[str, ...]] | None = None,
) -> Dict[str, Tuple[str, ...]]:
    """
    Merge catalog dependencies with inferred relations.

    Catalog entries win ordering for shared keys; inferred edges are appended.
    """

    from .service_catalog import load_catalog_dependencies

    externals = list(external_dependencies or ())
    known_services = [spec.name for spec in services]
    known_externals = [dep.name for dep in externals]

    catalog_deps = dict(catalog) if catalog is not None else load_catalog_dependencies(
        project_root,
        known_services=known_services,
        known_externals=known_externals,
    )
    inferred = infer_service_dependencies(
        project_root=project_root,
        services=services,
        external_dependencies=externals,
    )

    merged: Dict[str, List[str]] = {}
    for source in (catalog_deps, inferred):
        for name, deps in source.items():
            bucket = merged.setdefault(name, [])
            for dep in deps:
                if dep not in bucket:
                    bucket.append(dep)
    return {name: tuple(deps) for name, deps in merged.items() if deps}


def fill_missing_stack_dependencies(
    stack: Stack,
    *,
    project_root: Path,
) -> Stack:
    """
    Return a shallow-copied stack whose empty ``depends_on`` lists are filled
    from catalog + inferred relations. Explicit Stackfile edges are preserved;
    missing *external* dependency edges (redis/postgres/mongodb/rabbitmq) are
    appended when inferred.
    """

    root = project_root.expanduser().resolve()
    resolved_services: List[ServiceSpec] = []
    for spec in stack.services:
        path = Path(spec.path)
        if not path.is_absolute():
            path = (root / path).resolve()
        resolved_services.append(replace(spec, path=path))

    resolved = resolve_dependency_map(
        project_root=root,
        services=resolved_services,
        external_dependencies=stack.external_dependencies,
    )
    known_externals = {dep.name for dep in stack.external_dependencies}

    filled = Stack()
    for spec in stack.services:
        inferred = resolved.get(spec.name, ())
        if spec.depends_on:
            # Preserve explicit Stackfile edges; append any missing *external*
            # deps so graph / run / doctor see redis/postgres/mongodb/rabbitmq.
            deps = list(spec.depends_on)
            for dep in inferred:
                if dep in known_externals and dep not in deps:
                    deps.append(dep)
        else:
            deps = list(inferred)
        filled._services.append(replace(spec, depends_on=tuple(deps)))
    for dep in stack.external_dependencies:
        filled._external_dependencies.append(dep)
    if stack.run_requested:
        filled._run_requested = True
    return filled


# ---------------------------------------------------------------------------
# Alias / port indexes
# ---------------------------------------------------------------------------


def _port_index(services: Sequence[ServiceSpec]) -> Dict[int, str]:
    index: Dict[int, str] = {}
    for spec in services:
        port = resolve_service_port(spec, pid=None)
        if port is None:
            continue
        index.setdefault(int(port), spec.name)
    return index


def _normalize_external_type(dep_type: str) -> str:
    key = str(dep_type or "").strip().lower()
    if key in _EXTERNAL_SCHEME_TYPES:
        return _EXTERNAL_SCHEME_TYPES[key]
    return _COMPOSE_EXTERNAL_ALIASES.get(key, key)


def _external_type_index(
    externals: Sequence[ExternalDependency],
) -> Dict[str, str]:
    """Map canonical type key (redis/postgres/...) -> registered external name."""

    index: Dict[str, str] = {}
    for dep in externals:
        type_key = _normalize_external_type(dep.type)
        if type_key and type_key not in index:
            index[type_key] = dep.name
        # Name itself often matches the type (redis, postgres, ...).
        name_key = _normalize_external_type(dep.name)
        if name_key and name_key not in index:
            index[name_key] = dep.name
    return index


def _build_alias_index(services: Mapping[str, ServiceSpec]) -> Dict[str, str]:
    """
    Map normalized tokens -> canonical service name.

    Examples for ``auth_service``:
    ``auth_service``, ``auth-service``, ``auth_service_url``,
    ``AUTH_SERVICE``, ``AUTH_SERVICE_URL``, ``auth``.
    """

    aliases: Dict[str, str] = {}
    for name in services:
        tokens = _alias_tokens(name)
        for token in tokens:
            # Prefer exact / longer names if collisions arise.
            existing = aliases.get(token)
            if existing is None or len(name) >= len(existing):
                aliases[token] = name
    return aliases


def _build_external_alias_index(
    externals: Sequence[ExternalDependency],
    type_to_external: Mapping[str, str],
) -> Dict[str, str]:
    """Map env keys / host tokens / type aliases -> external dependency name."""

    aliases: Dict[str, str] = {}
    for dep in externals:
        for token in _alias_tokens(dep.name):
            aliases.setdefault(token, dep.name)
        type_key = _normalize_external_type(dep.type)
        for alias_name, target_type in _COMPOSE_EXTERNAL_ALIASES.items():
            if target_type == type_key or target_type == dep.name:
                aliases.setdefault(alias_name, dep.name)
                aliases.setdefault(alias_name.upper(), dep.name)

    for env_key, type_key in _EXTERNAL_ENV_KEYS.items():
        mapped = type_to_external.get(type_key)
        if not mapped:
            continue
        aliases.setdefault(env_key, mapped)
        aliases.setdefault(env_key.lower(), mapped)
    return aliases


def _alias_tokens(name: str) -> Set[str]:
    raw = name.strip()
    lower = raw.lower()
    dashed = lower.replace("_", "-")
    underscored = lower.replace("-", "_")
    compact = re.sub(r"[^a-z0-9]", "", lower)
    tokens = {lower, dashed, underscored, compact, raw}

    # Drop common suffixes for short forms: auth_service -> auth
    for suffix in ("_service", "-service", "_svc", "-svc", "_api", "-api", "_app", "-app"):
        if underscored.endswith(suffix.replace("-", "_")):
            stem = underscored[: -len(suffix.replace("-", "_"))]
            if stem and stem not in {"service", "api", "app"}:
                tokens.add(stem)
                tokens.add(stem.replace("_", "-"))

    # Env / settings style
    env = underscored.upper()
    tokens.add(env)
    tokens.add(f"{env}_URL")
    tokens.add(f"{env}_SERVICE_URL")
    tokens.add(f"{underscored}_url")
    tokens.add(f"{underscored}_service_url")
    if underscored.endswith("_service"):
        stem = underscored[: -len("_service")]
        tokens.add(f"{stem.upper()}_SERVICE_URL")
        tokens.add(f"{stem.upper()}_URL")
        tokens.add(f"{stem}_service_url")
        tokens.add(f"{stem}_url")
    return {t for t in tokens if t}


# ---------------------------------------------------------------------------
# Directory scan
# ---------------------------------------------------------------------------


def _scan_service_directory(
    spec: ServiceSpec,
    *,
    aliases: Mapping[str, str],
    port_to_service: Mapping[int, str],
    self_name: str,
    known_external_names: Set[str] | None = None,
    type_to_external: Mapping[str, str] | None = None,
) -> List[str]:
    root = Path(spec.path).expanduser()
    try:
        root = root.resolve()
    except OSError:
        return []
    if not root.is_dir():
        return []

    found: List[str] = []
    seen: Set[str] = set()
    externals = known_external_names or set()
    type_map = type_to_external or {}

    for path in _iter_scan_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if len(text) > _MAX_FILE_BYTES:
            text = text[:_MAX_FILE_BYTES]
        for dep in _find_refs_in_text(
            text,
            aliases=aliases,
            port_to_service=port_to_service,
            self_name=self_name,
            known_external_names=externals,
            type_to_external=type_map,
        ):
            if dep not in seen:
                seen.add(dep)
                found.append(dep)
    return found


def _iter_scan_files(root: Path) -> Iterable[Path]:
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            children = list(directory.iterdir())
        except OSError:
            continue
        for child in children:
            name = child.name
            if child.is_dir():
                if name in _SKIP_DIR_NAMES or name.startswith("."):
                    continue
                # Skip test trees — they create noisy false positives.
                if name in {"tests", "test", "__tests__", "spec"}:
                    continue
                stack.append(child)
                continue
            if not child.is_file():
                continue
            suffix = child.suffix.lower()
            if suffix in _SCAN_SUFFIXES or child.name.startswith(".env"):
                yield child


_URL_PORT_RE = re.compile(
    r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0|host\.docker\.internal):(\d{2,5})\b",
    re.IGNORECASE,
)
_HOST_NAME_RE = re.compile(
    r"https?://([A-Za-z0-9][A-Za-z0-9._-]{1,63})(?::\d{2,5})?\b",
    re.IGNORECASE,
)
_EXTERNAL_URI_RE = re.compile(
    r"\b(redis|rediss|postgres|postgresql|mongodb|mongo|amqp|amqps)://"
    r"(?:[^/\s\"']+@)?([A-Za-z0-9][A-Za-z0-9._-]{0,63})?(?::(\d{2,5}))?",
    re.IGNORECASE,
)
_ENV_KEY_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:SERVICE)?_?URL)\b",
)
_ATTR_RE = re.compile(
    r"\b([a-z][a-z0-9_]*(?:_service)?_url)\b",
    re.IGNORECASE,
)


def _find_refs_in_text(
    text: str,
    *,
    aliases: Mapping[str, str],
    port_to_service: Mapping[int, str],
    self_name: str,
    known_external_names: Set[str] | None = None,
    type_to_external: Mapping[str, str] | None = None,
) -> List[str]:
    found: List[str] = []
    seen: Set[str] = set()
    externals = known_external_names or set()
    type_map = type_to_external or {}

    def add(name: Optional[str]) -> None:
        if not name or name == self_name or name in seen:
            return
        seen.add(name)
        found.append(name)

    def add_external_type(type_key: Optional[str]) -> None:
        if not type_key:
            return
        mapped = type_map.get(type_key) or aliases.get(type_key)
        if mapped and mapped in externals:
            add(mapped)

    for match in _URL_PORT_RE.finditer(text):
        port = int(match.group(1))
        add(port_to_service.get(port))

    for match in _HOST_NAME_RE.finditer(text):
        host = match.group(1).lower()
        add(aliases.get(host))
        add(aliases.get(host.replace("-", "_")))
        add(aliases.get(host.replace("_", "-")))

    for match in _EXTERNAL_URI_RE.finditer(text):
        scheme = match.group(1).lower()
        host = (match.group(2) or "").lower()
        port_raw = match.group(3)
        add_external_type(_EXTERNAL_SCHEME_TYPES.get(scheme))
        if host:
            add(aliases.get(host))
            add(aliases.get(host.replace("-", "_")))
            mapped = _COMPOSE_EXTERNAL_ALIASES.get(host)
            if mapped:
                add_external_type(mapped)
        if port_raw:
            add(port_to_service.get(int(port_raw)))

    for match in _ENV_KEY_RE.finditer(text):
        key = match.group(1)
        add(aliases.get(key))
        add(aliases.get(key.lower()))
        type_hint = _EXTERNAL_ENV_KEYS.get(key) or _EXTERNAL_ENV_KEYS.get(key.upper())
        if type_hint:
            # DATABASE_URL / BROKER_URL are ambiguous — only trust the key when
            # a matching scheme already appears in the same text, or when the
            # key is type-specific (REDIS_URL, MONGODB_URL, ...).
            if key.upper() in {"DATABASE_URL", "DB_URL", "BROKER_URL", "CELERY_BROKER_URL"}:
                if type_hint == "postgres" and not re.search(
                    r"\b(?:postgres|postgresql)://", text, re.IGNORECASE
                ):
                    continue
                if type_hint == "rabbitmq" and not re.search(
                    r"\b(?:amqp|amqps|redis|rediss)://", text, re.IGNORECASE
                ):
                    continue
                # Broker may be redis:// — scheme handler already added it.
                if type_hint == "rabbitmq" and re.search(
                    r"\b(?:redis|rediss)://", text, re.IGNORECASE
                ):
                    continue
            add_external_type(type_hint)

    for match in _ATTR_RE.finditer(text):
        attr = match.group(1).lower()
        add(aliases.get(attr))

    # Bare service-name tokens (word boundaries) — keep conservative:
    # only match underscored / dashed full names for apps; allow bare
    # external names (redis, postgres, mongodb, rabbitmq) when registered.
    for name, canonical in aliases.items():
        if canonical == self_name:
            continue
        is_external = canonical in externals
        if not is_external and "_" not in name and "-" not in name:
            continue
        if name.isupper() and name.endswith("_URL"):
            continue
        # Skip very short stems for externals to reduce false positives in
        # comments ("db", "pg") — require the registered name or a compose alias.
        if is_external and len(name) < 4 and name.lower() not in {
            "redis",
            "mongo",
            "amqp",
            "cache",
        }:
            continue
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text):
            add(canonical)

    return found


# ---------------------------------------------------------------------------
# docker-compose
# ---------------------------------------------------------------------------


def _iter_compose_files(root: Path) -> List[Path]:
    names = (
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    )
    found: List[Path] = []
    for name in names:
        path = root / name
        if path.is_file():
            found.append(path)
    # Also accept compose under one nested project folder.
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        children = []
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        for name in names:
            path = child / name
            if path.is_file():
                found.append(path)
    return found


def _parse_compose_depends_on(path: Path) -> Dict[str, List[str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}

    # Prefer PyYAML when available; fall back to indentation parser.
    try:
        import yaml  # type: ignore

        payload = yaml.safe_load(text)
        if isinstance(payload, dict):
            return _compose_depends_from_mapping(payload)
    except Exception:
        pass

    return _compose_depends_from_text(text)


def _compose_depends_from_mapping(payload: Mapping[str, object]) -> Dict[str, List[str]]:
    services = payload.get("services")
    if not isinstance(services, Mapping):
        return {}
    result: Dict[str, List[str]] = {}
    for name, body in services.items():
        if not isinstance(body, Mapping):
            continue
        raw = body.get("depends_on")
        deps: List[str] = []
        if isinstance(raw, Mapping):
            deps = [str(key) for key in raw.keys()]
        elif isinstance(raw, list):
            deps = [str(item) for item in raw]
        if deps:
            result[str(name)] = deps
    return result


def _compose_depends_from_text(text: str) -> Dict[str, List[str]]:
    """Indentation-based parser for common compose ``depends_on`` shapes."""

    lines = text.splitlines()
    in_services = False
    services_indent: Optional[int] = None
    current: Optional[str] = None
    current_indent = 0
    result: Dict[str, List[str]] = {}
    collecting = False
    collect_indent = 0

    service_re = re.compile(r"^(\s*)([A-Za-z0-9][A-Za-z0-9_-]*)\s*:\s*(?:#.*)?$")
    depends_re = re.compile(r"^(\s*)depends_on\s*:\s*(.*)$")

    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))

        if re.match(r"^services\s*:", raw):
            in_services = True
            services_indent = indent
            current = None
            collecting = False
            continue

        if not in_services:
            continue

        if services_indent is not None and indent <= services_indent and not raw.startswith(" "):
            # Left services section.
            break

        match = depends_re.match(raw)
        if match and current is not None:
            collecting = True
            collect_indent = indent
            inline = match.group(2).strip()
            if inline.startswith("[") and inline.endswith("]"):
                inner = inline[1:-1].strip()
                if inner:
                    result.setdefault(current, [])
                    for part in inner.split(","):
                        dep = part.strip().strip("\"'")
                        if dep:
                            result[current].append(dep)
                collecting = False
            continue

        if collecting and current is not None:
            if indent <= collect_indent:
                collecting = False
            else:
                # Only direct children of depends_on (ignore nested
                # ``condition: service_healthy`` keys).
                if indent > collect_indent + 2:
                    continue
                item = raw.strip()
                if item.startswith("- "):
                    item = item[2:].strip()
                dep_name = item.split(":", 1)[0].strip().strip("\"'")
                if (
                    dep_name
                    and not dep_name.startswith("#")
                    and dep_name.lower() not in _COMPOSE_DEP_SKIP
                ):
                    result.setdefault(current, []).append(dep_name)
                continue

        match = service_re.match(raw)
        if match:
            svc_indent = len(match.group(1))
            # Top-level service keys sit one indent under ``services:``.
            if services_indent is None or svc_indent > services_indent:
                # Nested keys (build, environment, ...) look similar — only
                # treat as a service when indent is exactly services+2 spaces
                # (common) or when we have no current / indent shrinks to svc level.
                if current is None or svc_indent <= current_indent:
                    name = match.group(2)
                    if name in {
                        "build",
                        "image",
                        "environment",
                        "env_file",
                        "volumes",
                        "networks",
                        "ports",
                        "expose",
                        "healthcheck",
                        "deploy",
                        "logging",
                        "restart",
                        "container_name",
                        "command",
                        "depends_on",
                        "profiles",
                        "labels",
                        "x-common-env",
                        "x-service-base",
                        "x-logging",
                    }:
                        continue
                    current = name
                    current_indent = svc_indent
                    collecting = False
    return result


def _map_compose_dep(
    dep: str,
    known_all: Set[str],
    known_externals: Set[str],
) -> Optional[str]:
    name = str(dep).strip()
    if not name:
        return None
    if name in known_all:
        return name
    alias = _COMPOSE_EXTERNAL_ALIASES.get(name.lower())
    if alias and alias in known_externals:
        return alias
    if alias and alias in known_all:
        return alias
    # Ignore one-shot migrate / utility containers.
    if name.lower() in {"migrate", "migration", "init", "seed"}:
        return None
    return None


def _add_dep(result: MutableMapping[str, List[str]], name: str, dep: str) -> None:
    bucket = result.setdefault(name, [])
    if dep not in bucket:
        bucket.append(dep)


def _reaches(
    edges: Mapping[str, Sequence[str]],
    start: str,
    target: str,
) -> bool:
    """True when ``target`` is reachable from ``start`` following ``edges``."""

    stack = [start]
    seen: Set[str] = set()
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(edges.get(node, ()))
    return False
