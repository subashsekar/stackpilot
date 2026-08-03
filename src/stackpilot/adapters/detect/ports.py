"""Port detection from project configuration and source — never invented."""

from __future__ import annotations

import re
from pathlib import Path

from ..base import COMPOSE_FILENAMES, load_package_json, read_text
from .scan import iter_project_files

_ENV_PORT_KEYS = (
    "PORT",
    "APP_PORT",
    "HTTP_PORT",
    "SERVER_PORT",
    "UVICORN_PORT",
    "FLASK_RUN_PORT",
    "DJANGO_PORT",
)

_ENV_PORT_RE = re.compile(
    r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]?(\d{2,5})['\"]?\s*$",
    re.MULTILINE,
)

_COMPOSE_PORTS_RE = re.compile(
    r"""ports\s*:\s*(?:-\s*['\"]?(?:\d+:)?(\d{2,5})['\"]?|\[\s*['\"]?(?:\d+:)?(\d{2,5}))""",
    re.IGNORECASE,
)

_COMPOSE_PORT_LINE_RE = re.compile(
    r"""['\"]?(?:127\.0\.0\.1:|0\.0\.0\.0:|localhost:)?(?:\d+:)?(\d{2,5})['\"]?""",
)

_SIMPLE_PORT_ASSIGN_RE = re.compile(
    r"""(?:port|PORT)\s*[:=]\s*['\"]?(\d{2,5})['\"]?""",
)

# Typed / annotated assignments: ``PORT: int = 8001``
_ANNOTATED_PORT_RE = re.compile(
    r"""^(?:export\s+)?PORT\s*(?::[^=]+)?=\s*['\"]?(\d{2,5})['\"]?\s*(?:#.*)?$""",
    re.MULTILINE | re.IGNORECASE,
)

# uvicorn.run(..., port=8080) / app.run(port=5000) / runserver ...:8000
_UVICORN_RUN_PORT_RE = re.compile(
    r"""uvicorn\.run\s*\([^)]*\bport\s*=\s*(\d{2,5})""",
    re.IGNORECASE | re.DOTALL,
)
_FLASK_RUN_PORT_RE = re.compile(
    r"""\.run\s*\([^)]*\bport\s*=\s*(\d{2,5})""",
    re.IGNORECASE | re.DOTALL,
)
_CLI_PORT_FLAG_RE = re.compile(
    r"""(?:--port|-p)(?:=|\s+)(\d{2,5})\b""",
    re.IGNORECASE,
)
_HOST_PORT_RE = re.compile(
    r"""(?:0\.0\.0\.0|127\.0\.0\.1|localhost):(\d{2,5})\b""",
    re.IGNORECASE,
)

# process.env.PORT || 3000 / ?? 8080 / PORT || Number(3000)
_NODE_PORT_FALLBACK_RE = re.compile(
    r"""process\.env\.PORT\s*(?:\?\?|\|\|)\s*(?:Number\s*\(\s*)?(\d{2,5})""",
    re.IGNORECASE,
)
# listen(3000 / .listen(PORT / app.listen(8080
_NODE_LISTEN_RE = re.compile(
    r"""\.listen\s*\(\s*(\d{2,5})\b""",
    re.IGNORECASE,
)
# const port = 3000 / let PORT = 8080
_NODE_CONST_PORT_RE = re.compile(
    r"""(?:const|let|var)\s+(?:port|PORT)\s*=\s*(?:Number\s*\(\s*)?(\d{2,5})""",
)

_POSTGRES_CONF_PORT_RE = re.compile(
    r"""^\s*port\s*=\s*(\d{2,5})\s*(?:#.*)?$""",
    re.MULTILINE | re.IGNORECASE,
)
_REDIS_CONF_PORT_RE = re.compile(
    r"""^\s*port\s+(\d{2,5})\s*(?:#.*)?$""",
    re.MULTILINE | re.IGNORECASE,
)

_CONFIG_FILENAMES = (
    "uvicorn.cfg",
    "gunicorn.conf.py",
    "config.py",
    "settings.py",
    "main.py",
    "app.py",
    "server.py",
    "server.js",
    "index.js",
    "main.js",
    "main.ts",
    "app.js",
    "app.ts",
)

_LAUNCH_SCRIPT_NAMES = (
    "Procfile",
    "Procfile.dev",
    "Makefile",
    "justfile",
    "start.sh",
    "run.sh",
    "dev.sh",
    "serve.sh",
)


def detect_preferred_port(directory: Path) -> int | None:
    """
    Infer an explicit service port from project config / source.

    Checks ``.env``, compose host mappings, launch scripts, ``package.json``,
    then common framework source snippets. Returns ``None`` when nothing is
    explicit — callers must not invent a port.
    """

    root = directory.expanduser()
    for candidate in _env_files(root):
        port = _port_from_env_text(read_text(candidate))
        if port is not None:
            return port

    for name in COMPOSE_FILENAMES:
        compose = root / name
        if compose.is_file():
            port = _port_from_compose_text(
                read_text(compose),
                service_hint=root.name,
            )
            if port is not None:
                return port

    for path in _launch_script_candidates(root):
        port = _port_from_command_text(read_text(path))
        if port is not None:
            return port

    package_port = _port_from_package_json(root)
    if package_port is not None:
        return package_port

    for path in _config_candidates(root):
        port = _port_from_source_text(read_text(path))
        if port is not None:
            return port

    return None


def detect_infra_port(directory: Path, *, kind: str) -> int | None:
    """
    Detect a PostgreSQL / Redis listen port from conf or compose.

    ``kind`` is ``postgres`` or ``redis``. Returns ``None`` when the project
    does not declare a port (do not invent one).
    """

    root = directory.expanduser()
    key = kind.lower().strip()

    if key in {"postgres", "postgresql", "pgsql", "pg"}:
        conf = root / "postgresql.conf"
        if conf.is_file():
            match = _POSTGRES_CONF_PORT_RE.search(read_text(conf))
            if match:
                return _valid_port(int(match.group(1)))
        for name in COMPOSE_FILENAMES:
            compose = root / name
            if compose.is_file():
                port = _compose_service_host_port(
                    read_text(compose),
                    service_keys=("postgres", "postgresql", "db", "pgsql"),
                )
                if port is not None:
                    return port
        return None

    if key in {"redis", "cache"}:
        conf = root / "redis.conf"
        if conf.is_file():
            match = _REDIS_CONF_PORT_RE.search(read_text(conf))
            if match:
                return _valid_port(int(match.group(1)))
        for name in COMPOSE_FILENAMES:
            compose = root / name
            if compose.is_file():
                port = _compose_service_host_port(
                    read_text(compose),
                    service_keys=("redis", "cache"),
                )
                if port is not None:
                    return port
        return None

    return None


def _config_candidates(root: Path) -> list[Path]:
    """Return likely config / entry files that declare an app listen port."""

    found: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen or not path.is_file():
            return
        seen.add(resolved)
        found.append(path)

    for filename in _CONFIG_FILENAMES:
        _add(root / filename)

    for relative in (
        "app/core/config.py",
        "app/config.py",
        "app/settings.py",
        "core/config.py",
        "src/config.py",
        "src/settings.py",
        "src/app/core/config.py",
        "src/main.ts",
        "src/main.js",
        "src/index.ts",
        "src/index.js",
        "src/server.js",
        "src/app.js",
    ):
        _add(root / relative)

    # Shallow walk for common NestJS / Express entry files.
    for path in iter_project_files(
        root,
        suffixes=(".py", ".js", ".ts", ".mjs", ".cjs"),
        max_depth=2,
    ):
        name = path.name.lower()
        if name in {
            "main.py",
            "app.py",
            "server.py",
            "server.js",
            "index.js",
            "main.js",
            "main.ts",
            "app.js",
            "app.ts",
            "settings.py",
            "config.py",
        }:
            _add(path)

    return found


def _launch_script_candidates(root: Path) -> list[Path]:
    found: list[Path] = []
    for name in _LAUNCH_SCRIPT_NAMES:
        path = root / name
        if path.is_file():
            found.append(path)
    try:
        for child in sorted(root.iterdir()):
            if not child.is_file():
                continue
            lower = child.name.lower()
            if lower.endswith((".sh", ".bash", ".ps1", ".cmd", ".bat")):
                if child not in found:
                    found.append(child)
    except OSError:
        pass
    return found


def _env_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for name in (".env", ".env.local", ".env.development", ".env.dev"):
        path = root / name
        if path.is_file():
            files.append(path)
    try:
        for child in sorted(root.iterdir()):
            if child.is_file() and child.name.startswith(".env.") and child not in files:
                files.append(child)
    except OSError:
        pass
    return files


def _port_from_env_text(text: str) -> int | None:
    if not text:
        return None
    found: dict[str, int] = {}
    for match in _ENV_PORT_RE.finditer(text):
        key = match.group(1).upper()
        value = int(match.group(2))
        if _valid_port(value) is not None:
            found[key] = value
    for key in _ENV_PORT_KEYS:
        if key in found:
            return found[key]
    return None


def _port_from_package_json(root: Path) -> int | None:
    data = load_package_json(root)
    if data is None:
        return None

    config = data.get("config")
    if isinstance(config, dict):
        for key in ("port", "PORT", "http_port"):
            raw = config.get(key)
            port = _coerce_port(raw)
            if port is not None:
                return port

    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        for name in ("dev", "start", "start:dev", "serve", "preview"):
            raw = scripts.get(name)
            if isinstance(raw, str):
                port = _port_from_command_text(raw)
                if port is not None:
                    return port
        for raw in scripts.values():
            if isinstance(raw, str):
                port = _port_from_command_text(raw)
                if port is not None:
                    return port

    main = data.get("main")
    if isinstance(main, str) and main:
        candidate = root / main
        if candidate.is_file():
            port = _port_from_source_text(read_text(candidate))
            if port is not None:
                return port
    return None


def _port_from_compose_text(text: str, *, service_hint: str | None = None) -> int | None:
    if not text:
        return None

    if service_hint:
        port = _compose_service_host_port(text, service_keys=(service_hint,))
        if port is not None:
            return port

    # Prefer host→container mappings like "8000:8000" — take the host side.
    host_map = re.finditer(
        r"""['\"]?(\d{2,5}):(\d{2,5})['\"]?""",
        text,
    )
    for match in host_map:
        host_port = int(match.group(1))
        if _valid_port(host_port) is not None and host_port not in {22, 53}:
            if host_port >= 80:
                return host_port

    for match in _COMPOSE_PORTS_RE.finditer(text):
        raw = match.group(1) or match.group(2)
        if raw:
            port = _valid_port(int(raw))
            if port is not None:
                return port

    in_ports = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ports:"):
            in_ports = True
            continue
        if in_ports:
            if stripped and not stripped.startswith("-") and not stripped.startswith("#"):
                if ":" in stripped and not stripped.startswith("'") and not stripped.startswith('"'):
                    if not stripped.startswith("-"):
                        in_ports = False
                        continue
            match = _COMPOSE_PORT_LINE_RE.search(stripped)
            if match:
                port = _valid_port(int(match.group(1)))
                if port is not None:
                    return port
    return None


def _compose_service_host_port(
    text: str,
    *,
    service_keys: tuple[str, ...],
) -> int | None:
    """Extract the host port for a named compose service when present."""

    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None

    if yaml is not None:
        try:
            payload = yaml.safe_load(text)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            services = payload.get("services")
            if isinstance(services, dict):
                wanted = {key.lower() for key in service_keys}
                for name, body in services.items():
                    if str(name).lower() not in wanted:
                        continue
                    if not isinstance(body, dict):
                        continue
                    ports = body.get("ports")
                    port = _first_host_port(ports)
                    if port is not None:
                        return port

    # Text fallback: find ``service:`` then a nearby ports mapping.
    lowered_keys = tuple(key.lower() for key in service_keys)
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_-]*)\s*:\s*(?:#.*)?$", stripped)
        if not match:
            continue
        if match.group(1).lower() not in lowered_keys:
            continue
        # Scan following indented lines for ports.
        base_indent = len(raw) - len(raw.lstrip(" "))
        for follow in lines[index + 1 : index + 40]:
            if not follow.strip() or follow.lstrip().startswith("#"):
                continue
            indent = len(follow) - len(follow.lstrip(" "))
            if indent <= base_indent:
                break
            port_match = re.search(
                r"""['\"]?(\d{2,5}):(\d{2,5})['\"]?""",
                follow,
            )
            if port_match:
                return _valid_port(int(port_match.group(1)))
    return None


def _first_host_port(ports: object) -> int | None:
    if isinstance(ports, list):
        for item in ports:
            if isinstance(item, int):
                return _valid_port(item)
            if isinstance(item, str):
                match = re.search(r"(\d{2,5})\s*:\s*(\d{2,5})", item)
                if match:
                    return _valid_port(int(match.group(1)))
                match = re.search(r"(\d{2,5})", item)
                if match:
                    return _valid_port(int(match.group(1)))
            if isinstance(item, dict):
                published = item.get("published") or item.get("host")
                port = _coerce_port(published)
                if port is not None:
                    return port
    return None


def _port_from_command_text(text: str) -> int | None:
    if not text:
        return None
    match = _CLI_PORT_FLAG_RE.search(text)
    if match:
        return _valid_port(int(match.group(1)))
    match = _HOST_PORT_RE.search(text)
    if match:
        return _valid_port(int(match.group(1)))
    match = _ENV_PORT_RE.search(text)
    if match and match.group(1).upper() in _ENV_PORT_KEYS:
        return _valid_port(int(match.group(2)))
    # Inline PORT=8123 prefix.
    match = re.search(r"""\bPORT=(\d{2,5})\b""", text)
    if match:
        return _valid_port(int(match.group(1)))
    return None


def _port_from_source_text(text: str) -> int | None:
    if not text:
        return None

    match = _ANNOTATED_PORT_RE.search(text)
    if match:
        return _valid_port(int(match.group(1)))

    match = _UVICORN_RUN_PORT_RE.search(text)
    if match:
        return _valid_port(int(match.group(1)))

    match = _FLASK_RUN_PORT_RE.search(text)
    if match:
        return _valid_port(int(match.group(1)))

    match = _NODE_PORT_FALLBACK_RE.search(text)
    if match:
        return _valid_port(int(match.group(1)))

    match = _NODE_LISTEN_RE.search(text)
    if match:
        return _valid_port(int(match.group(1)))

    match = _NODE_CONST_PORT_RE.search(text)
    if match:
        return _valid_port(int(match.group(1)))

    match = _CLI_PORT_FLAG_RE.search(text)
    if match:
        return _valid_port(int(match.group(1)))

    match = _HOST_PORT_RE.search(text)
    if match:
        return _valid_port(int(match.group(1)))

    match = _SIMPLE_PORT_ASSIGN_RE.search(text)
    if match:
        return _valid_port(int(match.group(1)))

    return None


def _coerce_port(value: object) -> int | None:
    if value is None:
        return None
    try:
        return _valid_port(int(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _valid_port(value: int) -> int | None:
    if 1 <= value <= 65535:
        return value
    return None
