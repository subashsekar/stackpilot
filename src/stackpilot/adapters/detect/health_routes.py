"""Adaptive HTTP health-route discovery (adapter detect layer).

Static analysis finds candidate routes; a shared ranker picks the best
health path. Framework-specific parsers live here so scanner/generator
remain framework-agnostic.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlparse

from ..base import read_text
from .scan import iter_project_files

# Higher priority = lower index. Exact path match beats suffix match.
HEALTH_ROUTE_PRIORITY: tuple[str, ...] = (
    "/health",
    "/health/",
    "/healthz",
    "/ready",
    "/readiness",
    "/live",
    "/liveness",
    "/ping",
    "/status",
    "/",
)

# Final-path segments that commonly mean "health" after a mount prefix.
_HEALTH_LEAF_SEGMENTS: frozenset[str] = frozenset(
    {
        "health",
        "healthz",
        "ready",
        "readiness",
        "live",
        "liveness",
        "ping",
    }
)

# Allowed prefixes for ``.../status`` (exact ``/status`` always allowed).
# ``/account/status`` must NOT qualify — it is a business API, not liveness.
_SAFE_STATUS_PREFIXES: frozenset[str] = frozenset(
    {
        "api",
        "apis",
        "v1",
        "v2",
        "v3",
        "internal",
        "private",
        "public",
        "system",
        "sys",
        "ops",
        "infra",
        "monitor",
        "monitoring",
        "actuator",
        "meta",
        "health",
        "healthz",
        "service",
        "svc",
        "app",
        "backend",
        "gateway",
    }
)

# API-style prefixes that keep ``.../health`` as a primary health surface.
_API_HEALTH_PREFIXES: frozenset[str] = frozenset(
    {
        "api",
        "apis",
        "v1",
        "v2",
        "v3",
        "internal",
        "private",
        "public",
        "system",
        "sys",
        "ops",
        "infra",
        "monitor",
        "monitoring",
        "actuator",
        "meta",
        "service",
        "svc",
        "app",
        "backend",
        "gateway",
    }
)

# Subsystem mounts whose ``.../health`` is secondary to process liveness ``/``.
_SUBSYSTEM_HEALTH_PREFIXES: frozenset[str] = frozenset(
    {
        "cache",
        "interview",
        "metrics",
        "queue",
        "worker",
        "storage",
        "db",
        "database",
        "redis",
        "search",
        "mail",
        "email",
        "billing",
        "payment",
        "payments",
    }
)

_BUSINESS_SEGMENTS: frozenset[str] = frozenset(
    {
        "account",
        "accounts",
        "user",
        "users",
        "auth",
        "admin",
        "login",
        "logout",
        "session",
        "sessions",
        "profile",
        "profiles",
        "me",
        "order",
        "orders",
        "payment",
        "payments",
        "billing",
        "cart",
        "password",
        "token",
        "tokens",
    }
)


@dataclass(frozen=True, slots=True)
class HealthEndpointSelection:
    """Result of adaptive health resolution for Stackfile generation / probes."""

    kind: str  # "http" | "tcp" | "explicit"
    path: str | None = None
    detail: str = ""


def normalize_route(path: str) -> str:
    """Normalize a route to a leading-slash path (preserve trailing slash)."""

    text = (path or "").strip()
    if not text:
        return "/"
    if "://" in text:
        parsed = urlparse(text)
        text = parsed.path or "/"
    if not text.startswith("/"):
        text = "/" + text
    if text != "/" and "//" in text:
        text = re.sub(r"/{2,}", "/", text)
    return text or "/"


def health_route_rank(path: str) -> int | None:
    """
    Return a sort key for ``path`` (lower is better), or ``None`` when the
    path is not a health-like candidate.

    Exact priority paths beat API leaf matches (``/api/v1/health``).
    Business routes like ``/account/status`` are rejected.
    Process liveness ``/`` beats subsystem checks (``/cache/health``,
    ``/health/db``) so startup does not depend on optional subsystems.
    """

    route = normalize_route(path)
    stripped = route if route == "/" else route.rstrip("/")
    if route == "/" or stripped == "":
        return 900

    segments = [part for part in stripped.split("/") if part]
    if not segments:
        return 900

    leaf = segments[-1].lower()
    prior = [part.lower() for part in segments[:-1]]

    # 1) Exact priority entries (ignoring trailing slash).
    for index, preferred in enumerate(HEALTH_ROUTE_PRIORITY):
        if preferred == "/":
            continue
        pref = preferred.rstrip("/")
        if stripped == pref:
            return index * 2

    # 2) Final segment is a health leaf.
    if leaf in _HEALTH_LEAF_SEGMENTS:
        if any(part in _BUSINESS_SEGMENTS for part in prior):
            return None
        try:
            index = HEALTH_ROUTE_PRIORITY.index(f"/{leaf}")
        except ValueError:
            index = 8
        # Subsystem health (/cache/health) is secondary to process liveness /.
        if prior and any(part in _SUBSYSTEM_HEALTH_PREFIXES for part in prior):
            return 920 + index
        # API-style mounts stay primary (/api/v1/health, /internal/health).
        if not prior or all(part in _API_HEALTH_PREFIXES for part in prior):
            return index * 2 + 1
        # Unknown prefix: keep as health candidate, but after exact leaves.
        return 30 + index * 2 + min(len(prior), 5)

    # 3) /status is exact-or-safe-prefix only — never /account/status.
    if leaf == "status":
        if not prior:
            return HEALTH_ROUTE_PRIORITY.index("/status") * 2
        if any(part in _BUSINESS_SEGMENTS for part in prior):
            return None
        if all(part in _SAFE_STATUS_PREFIXES for part in prior):
            return HEALTH_ROUTE_PRIORITY.index("/status") * 2 + 1
        return None

    # 4) Health mount with subpath (/health/db) — after process liveness /.
    if any(part in {"health", "healthz"} for part in segments):
        return 940 + len(segments)

    return None


def rank_health_routes(routes: Iterable[str]) -> list[str]:
    """Deduplicate and sort health-like routes by priority."""

    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw in routes:
        route = normalize_route(raw)
        if route in seen:
            continue
        rank = health_route_rank(route)
        if rank is None:
            continue
        seen.add(route)
        scored.append((rank, route))
    scored.sort(key=lambda item: (item[0], len(item[1]), item[1]))
    return [route for _, route in scored]


def select_best_health_path(routes: Iterable[str]) -> str | None:
    """Return the highest-ranked health path, or ``None`` when none qualify."""

    ranked = rank_health_routes(routes)
    return ranked[0] if ranked else None


def resolve_health_endpoint(
    *,
    explicit_path: str | None = None,
    discovered_routes: Sequence[str] = (),
) -> HealthEndpointSelection:
    """
    Priority 1: explicit Stackfile / caller path always wins.
    Priority 2: best discovered health-like route.
    Fallback: TCP when nothing qualifies.
    """

    if explicit_path is not None and str(explicit_path).strip():
        path = normalize_route(str(explicit_path))
        return HealthEndpointSelection(
            kind="explicit",
            path=path,
            detail="explicit health URL overrides discovery",
        )

    best = select_best_health_path(discovered_routes)
    if best is not None:
        return HealthEndpointSelection(
            kind="http",
            path=best,
            detail="discovered health endpoint",
        )
    return HealthEndpointSelection(
        kind="tcp",
        path=None,
        detail="no HTTP health endpoint found",
    )


def discover_routes(directory: Path, framework: str) -> list[str]:
    """Discover HTTP routes for a supported framework under ``directory``."""

    root = directory.expanduser()
    name = framework.strip().lower()
    if name == "fastapi":
        return discover_fastapi_routes(root)
    if name == "flask":
        return discover_flask_routes(root)
    if name == "django":
        return discover_django_routes(root)
    if name == "express":
        return discover_express_routes(root)
    if name == "nestjs":
        return discover_nestjs_routes(root)
    return []


def discover_health_path(directory: Path, framework: str) -> str | None:
    """Best health path for ``framework`` under ``directory``, if any."""

    return select_best_health_path(discover_routes(directory, framework))


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------


def discover_fastapi_routes(directory: Path) -> list[str]:
    routes: list[str] = []
    for path in iter_project_files(directory, suffixes=(".py",), max_depth=4):
        text = read_text(path)
        if not text:
            continue
        lower = text.lower()
        if (
            "fastapi" not in lower
            and "apirouter" not in lower
            and "@app." not in text
            and "@router." not in text
        ):
            continue
        routes.extend(_fastapi_routes_from_source(text))
    return _unique(routes)


_DEEP_HEALTH_NAME_RE = re.compile(
    r"(aggregate|detailed|downstream|readiness|deep_health|full_health)",
    re.IGNORECASE,
)


def _is_deep_health_handler(node: ast.FunctionDef | ast.AsyncFunctionDef, route_path: str) -> bool:
    """True when a /health handler looks like aggregate/readiness, not liveness."""

    path = normalize_route(route_path)
    if path not in {"/health", "/health/"} and not path.endswith("/health"):
        return False
    if _DEEP_HEALTH_NAME_RE.search(node.name or ""):
        return True
    try:
        body = ast.unparse(node)
    except Exception:
        body = ""
    return bool(_DEEP_HEALTH_NAME_RE.search(body))


def _fastapi_routes_from_source(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _fastapi_routes_regex_fallback(source)

    router_prefixes: dict[str, str] = {}
    include_prefixes: dict[str, list[str]] = {}
    routes: list[str] = []

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                prefix = _api_router_prefix(node.value)
                if prefix is not None:
                    router_prefixes[target.id] = prefix

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_attr_call(node, "include_router"):
            if not node.args:
                continue
            router_name = _name_of(node.args[0])
            if router_name is None:
                continue
            extra = _kw_str(node, "prefix") or ""
            include_prefixes.setdefault(router_name, []).append(normalize_route(extra) if extra else "")

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            mount, route_path = _fastapi_decorator_route(dec)
            if route_path is None:
                continue
            if _is_deep_health_handler(node, route_path):
                # Skip aggregate/readiness probes for startup health selection.
                continue
            if mount in {"app", "application", "api"}:
                routes.append(normalize_route(route_path))
                continue
            base = router_prefixes.get(mount, "")
            include_list = include_prefixes.get(mount)
            if include_list:
                for inc in include_list:
                    routes.append(_join_routes(inc, base, route_path))
            else:
                routes.append(_join_routes(base, route_path))

    if not routes:
        routes.extend(_fastapi_routes_regex_fallback(source))
    return routes


def _api_router_prefix(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id == "APIRouter":
        return normalize_route(_kw_str(node, "prefix") or "/")
    if isinstance(func, ast.Attribute) and func.attr == "APIRouter":
        return normalize_route(_kw_str(node, "prefix") or "/")
    return None


def _fastapi_decorator_route(dec: ast.AST) -> tuple[str, str | None]:
    """Return ``(mount_name, path)`` for ``@router.get("/x")``-style decorators."""

    if not isinstance(dec, ast.Call):
        return "", None
    func = dec.func
    if not isinstance(func, ast.Attribute):
        return "", None
    if func.attr not in {"get", "head", "api_route", "route"}:
        return "", None
    mount = _name_of(func.value) or ""
    if not dec.args:
        return mount, None
    path = _const_str(dec.args[0])
    return mount, path


def _fastapi_routes_regex_fallback(source: str) -> list[str]:
    routes: list[str] = []
    router_prefixes: dict[str, str] = {}
    for match in re.finditer(
        r"""(\w+)\s*=\s*APIRouter\s*\([^)]*prefix\s*=\s*['\"]([^'\"]*)['\"]""",
        source,
    ):
        router_prefixes[match.group(1)] = normalize_route(match.group(2))

    include_prefixes: dict[str, list[str]] = {}
    for match in re.finditer(
        r"""include_router\s*\(\s*(\w+)\s*(?:,\s*prefix\s*=\s*['\"]([^'\"]*)['\"])?""",
        source,
    ):
        name = match.group(1)
        extra = match.group(2) or ""
        include_prefixes.setdefault(name, []).append(
            normalize_route(extra) if extra else ""
        )

    for match in re.finditer(
        r"""@(\w+)\.(?:get|head|api_route|route)\s*\(\s*['\"]([^'\"]*)['\"]""",
        source,
    ):
        mount, path = match.group(1), match.group(2)
        if mount in {"app", "application", "api"}:
            routes.append(normalize_route(path))
            continue
        base = router_prefixes.get(mount, "")
        includes = include_prefixes.get(mount)
        if includes:
            for inc in includes:
                routes.append(_join_routes(inc, base, path))
        else:
            routes.append(_join_routes(base, path))
    return routes


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------


def discover_flask_routes(directory: Path) -> list[str]:
    routes: list[str] = []
    for path in iter_project_files(directory, suffixes=(".py",), max_depth=4):
        text = read_text(path)
        if not text:
            continue
        lower = text.lower()
        if "flask" not in lower and "blueprint" not in lower:
            continue
        routes.extend(_flask_routes_from_source(text))
    return _unique(routes)


def _flask_routes_from_source(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _flask_routes_regex(source)

    bp_prefixes: dict[str, str] = {}
    routes: list[str] = []

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                prefix = _blueprint_prefix(node.value)
                if prefix is not None:
                    bp_prefixes[target.id] = prefix

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            mount, route_path = _flask_decorator_route(dec)
            if route_path is None:
                continue
            if mount in {"app", "application"}:
                routes.append(normalize_route(route_path))
            else:
                routes.append(_join_routes(bp_prefixes.get(mount, ""), route_path))

    if not routes:
        routes.extend(_flask_routes_regex(source))
    return routes


def _blueprint_prefix(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    is_bp = (isinstance(func, ast.Name) and func.id == "Blueprint") or (
        isinstance(func, ast.Attribute) and func.attr == "Blueprint"
    )
    if not is_bp:
        return None
    return normalize_route(_kw_str(node, "url_prefix") or "/")


def _flask_decorator_route(dec: ast.AST) -> tuple[str, str | None]:
    if not isinstance(dec, ast.Call):
        return "", None
    func = dec.func
    if not isinstance(func, ast.Attribute):
        return "", None
    if func.attr not in {"route", "get", "post", "put", "delete", "patch", "head"}:
        return "", None
    # Health discovery cares about GET-like surfaces; keep route/get/head.
    if func.attr not in {"route", "get", "head"}:
        return "", None
    mount = _name_of(func.value) or ""
    if not dec.args:
        return mount, None
    return mount, _const_str(dec.args[0])


def _flask_routes_regex(source: str) -> list[str]:
    routes: list[str] = []
    bp_prefixes: dict[str, str] = {}
    for match in re.finditer(
        r"""(\w+)\s*=\s*Blueprint\s*\([^)]*url_prefix\s*=\s*['\"]([^'\"]*)['\"]""",
        source,
    ):
        bp_prefixes[match.group(1)] = normalize_route(match.group(2))
    for match in re.finditer(
        r"""@(\w+)\.(?:route|get|head)\s*\(\s*['\"]([^'\"]*)['\"]""",
        source,
    ):
        mount, path = match.group(1), match.group(2)
        if mount in {"app", "application"}:
            routes.append(normalize_route(path))
        else:
            routes.append(_join_routes(bp_prefixes.get(mount, ""), path))
    return routes


# ---------------------------------------------------------------------------
# Django
# ---------------------------------------------------------------------------


def discover_django_routes(directory: Path) -> list[str]:
    routes: list[str] = []
    for path in iter_project_files(directory, suffixes=(".py",), max_depth=4):
        text = read_text(path)
        if not text or "urlpatterns" not in text:
            continue
        routes.extend(
            _django_routes_from_source(text, directory=directory, seen=set())
        )
    return _unique(routes)


def _django_routes_from_source(
    source: str,
    *,
    directory: Path,
    seen: set[str],
    prefix: str = "",
) -> list[str]:
    routes: list[str] = []
    # path("health/", ...), re_path(r"^ready/?$", ...)
    for match in re.finditer(
        r"""(?:path|re_path|url)\s*\(\s*[r]?['\"]([^'\"]*)['\"]""",
        source,
    ):
        raw = match.group(1)
        route = _django_pattern_to_path(raw)
        if route is None:
            continue
        routes.append(_join_routes(prefix, route))

    # include("app.urls") — shallow follow when module file exists.
    for match in re.finditer(
        r"""(?:path|re_path|url)\s*\(\s*[r]?['\"]([^'\"]*)['\"]\s*,\s*include\s*\(\s*['\"]([^'\"]+)['\"]""",
        source,
    ):
        child_prefix = _django_pattern_to_path(match.group(1)) or ""
        module = match.group(2)
        child = _load_django_urls_module(directory, module, seen)
        if child:
            routes.extend(
                _django_routes_from_source(
                    child,
                    directory=directory,
                    seen=seen,
                    prefix=_join_routes(prefix, child_prefix),
                )
            )
    return routes


def _django_pattern_to_path(pattern: str) -> str | None:
    text = pattern.strip()
    if not text:
        return "/"
    # Strip common regex anchors / optional slash.
    text = text.lstrip("^").rstrip("$")
    text = re.sub(r"\?\s*$", "", text)
    text = text.replace("\\Z", "").replace("\\z", "")
    # Drop named converters: <str:name> → ignore dynamic-only routes for health
    if "<" in text and ">" in text:
        # Keep prefix before converter if present: "api/<slug>/" → skip
        # Static health paths rarely use converters.
        if re.search(r"<[^>]+>", text):
            # Allow trailing converter-free static segments only when whole path
            # has no converters.
            return None
    text = text.replace("\\/", "/")
    if text.endswith("?"):
        text = text[:-1]
    if text.endswith("/?"):
        text = text[:-2] + "/"
    return normalize_route(text)


def _load_django_urls_module(
    directory: Path,
    dotted: str,
    seen: set[str],
) -> str | None:
    if dotted in seen:
        return None
    seen.add(dotted)
    parts = dotted.split(".")
    # Try package path relative to service root and one level up.
    candidates = [
        directory.joinpath(*parts).with_suffix(".py"),
        directory.joinpath(*parts[:-1], parts[-1] + ".py")
        if len(parts) >= 1
        else None,
    ]
    if len(parts) >= 2:
        candidates.append(directory.joinpath(*parts[:-1], "urls.py"))
    for candidate in candidates:
        if candidate is None:
            continue
        if candidate.is_file():
            return read_text(candidate)
    # Search by filename urls.py inside matching package folder.
    pkg = directory.joinpath(*parts[:-1]) if len(parts) > 1 else directory / parts[0]
    urls = pkg / "urls.py"
    if urls.is_file():
        return read_text(urls)
    return None


# ---------------------------------------------------------------------------
# Express
# ---------------------------------------------------------------------------


def discover_express_routes(directory: Path) -> list[str]:
    routes: list[str] = []
    for path in iter_project_files(
        directory, suffixes=(".js", ".mjs", ".cjs", ".ts"), max_depth=4
    ):
        text = read_text(path)
        if not text:
            continue
        if "express" not in text.lower() and ".get(" not in text and ".use(" not in text:
            continue
        routes.extend(_express_routes_from_source(text))
    return _unique(routes)


def _express_routes_from_source(source: str) -> list[str]:
    routes: list[str] = []
    # router / app mount prefixes: app.use('/api', router)
    mounts: list[tuple[str, str]] = []
    for match in re.finditer(
        r"""(\w+)\.use\s*\(\s*['\"]([^'\"]*)['\"]\s*,\s*(\w+)""",
        source,
    ):
        mounts.append((match.group(3), normalize_route(match.group(2))))

    router_names = {name for name, _ in mounts}
    router_names.update(
        m.group(1)
        for m in re.finditer(
            r"""(\w+)\s*=\s*(?:express\.)?Router\s*\(""",
            source,
        )
    )

    for match in re.finditer(
        r"""(\w+)\.(?:get|head|all)\s*\(\s*['\"]([^'\"]*)['\"]""",
        source,
    ):
        mount, path = match.group(1), match.group(2)
        prefixes = [p for name, p in mounts if name == mount]
        if prefixes:
            for prefix in prefixes:
                routes.append(_join_routes(prefix, path))
        elif mount in router_names:
            routes.append(normalize_route(path))
        else:
            # app.get(...)
            routes.append(normalize_route(path))
    return routes


# ---------------------------------------------------------------------------
# NestJS
# ---------------------------------------------------------------------------


def discover_nestjs_routes(directory: Path) -> list[str]:
    routes: list[str] = []
    for path in iter_project_files(
        directory, suffixes=(".ts", ".js"), max_depth=5
    ):
        text = read_text(path)
        if not text:
            continue
        if "@Controller" not in text and "@Get" not in text:
            continue
        routes.extend(_nestjs_routes_from_source(text))
    return _unique(routes)


def _nestjs_routes_from_source(source: str) -> list[str]:
    routes: list[str] = []
    controller_positions = [
        (m.start(), normalize_route(m.group(1) or "/"))
        for m in re.finditer(
            r"""@Controller\s*\(\s*(?:['\"]([^'\"]*)['\"])?\s*\)""",
            source,
        )
    ]
    get_matches = list(
        re.finditer(
            r"""@Get\s*\(\s*(?:['\"]([^'\"]*)['\"])?\s*\)""",
            source,
        )
    )
    if not get_matches:
        # Controller alone with no @Get — treat controller path as route.
        return [ctrl for _, ctrl in controller_positions]

    for match in get_matches:
        get_path = match.group(1) if match.group(1) is not None else ""
        prefix = "/"
        for pos, ctrl in controller_positions:
            if pos < match.start():
                prefix = ctrl
            else:
                break
        routes.append(_join_routes(prefix, get_path or "/"))
    return routes


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _join_routes(*parts: str) -> str:
    pieces: list[str] = []
    for part in parts:
        text = (part or "").strip()
        if not text or text == "/":
            continue
        pieces.append(text.strip("/"))
    if not pieces:
        return "/"
    return normalize_route("/" + "/".join(pieces))


def _unique(routes: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in routes:
        route = normalize_route(raw)
        if route in seen:
            continue
        seen.add(route)
        out.append(route)
    return out


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _kw_str(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == name:
            return _const_str(kw.value)
    return None


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_attr_call(node: ast.Call, attr: str) -> bool:
    return isinstance(node.func, ast.Attribute) and node.func.attr == attr
