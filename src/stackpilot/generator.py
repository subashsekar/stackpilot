"""Stackfile.py generator.

Framework-specific launch commands and health defaults come from adapters.
This module only assigns ports and renders Stackfile text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, Sequence

from .adapters import AdapterServiceSpec, default_registry
from .config import ExternalDependency, ServiceSpec
from .discovery import STACKFILE_NAME
from .relation_infer import resolve_dependency_map
from .service_catalog import load_catalog_dependencies

BASE_PORT: Final[int] = 8000


class StackServiceLike(Protocol):
    """Minimal service shape required to render a Stackfile entry."""

    name: str
    path: Path
    framework: str


class StackfileExistsError(FileExistsError):
    """Raised when Stackfile.py already exists and ``force`` was not set."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"{path.name} already exists. Use --force to overwrite."
        )


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Result of writing a generated Stackfile.py."""

    output_path: Path
    content: str
    overwritten: bool
    ports: tuple[int, ...]


EMPTY_STACKFILE = """\
from stackpilot import Stack

stack = Stack()

# Register local application services:
#
# stack.service(
#     name="gateway",
#     path="./gateway",
#     command="uvicorn main:app --host 0.0.0.0 --port 8000",
# )
#
# Register external infrastructure (validated, never started):
#
# stack.external_dependency(
#     name="postgres",
#     type="postgresql",
#     host="127.0.0.1",
#     port=5432,
# )

stack.run()
"""


def assign_ports(
    services: Sequence[StackServiceLike],
    *,
    start_port: int = BASE_PORT,
) -> list[int]:
    """
    Return a port for each service.

    Priority per service:
    1. Adapter ``fixed_port`` (Postgres / Redis from conf/compose)
    2. Adapter ``preferred_port`` from ``.env`` / compose / source / scripts
    3. Unique coordination ports starting at ``start_port`` only when the
       service uses a port and nothing was declared in the project
    """

    ports: list[int] = []
    next_port = start_port
    used: set[int] = set()

    for service in services:
        spec = _adapter_spec(service, port=None)
        if (
            spec is not None
            and spec.fixed_port is not None
            and spec.fixed_port not in used
        ):
            port = spec.fixed_port
        elif (
            spec is not None
            and spec.preferred_port is not None
            and spec.preferred_port not in used
        ):
            port = spec.preferred_port
        else:
            while next_port in used:
                next_port += 1
            port = next_port
            next_port += 1
        used.add(port)
        ports.append(port)

    if len(ports) != len(services):
        raise ValueError("Port assignment length mismatch")
    return ports


def build_command(service: StackServiceLike, port: int) -> str:
    """Build a launch command for a discovered service on ``port``."""

    spec = _adapter_spec(service, port=port)
    if spec is None:
        return "python main.py"
    return spec.command


def generate_stackfile(
    services: Sequence[StackServiceLike],
    *,
    project_root: Path,
    start_port: int = BASE_PORT,
) -> str:
    """
    Render a complete runnable ``Stackfile.py``.

    Application services receive ``stack.service(...)``. PostgreSQL / Redis
    (and other adapters marked ``external``) receive
    ``stack.external_dependency(...)`` — never started by StackPilot.

    When a nearby ``services.json`` / ``services.yaml`` catalog exists, its
    ``depends_on`` entries are preferred. Otherwise StackPilot infers relations
    from ``docker-compose`` and inter-service URL / name references so
    ``stackpilot graph`` and startup order reflect the architecture.
    """

    root = project_root.expanduser().resolve()
    ports = assign_ports(services, start_port=start_port)
    specs = [
        _adapter_spec(service, port=ports[index]) for index, service in enumerate(services)
    ]

    known_services = [
        service.name
        for service, spec in zip(services, specs, strict=True)
        if spec is None or not spec.external
    ]
    known_externals = [
        service.name
        for service, spec in zip(services, specs, strict=True)
        if spec is not None and spec.external
    ]
    catalog_deps = load_catalog_dependencies(
        root,
        known_services=known_services,
        known_externals=known_externals,
    )

    temp_specs: list[ServiceSpec] = []
    external_models: list[ExternalDependency] = []
    for index, service in enumerate(services):
        spec = specs[index]
        port = ports[index]
        if spec is not None and spec.external:
            external_models.append(
                ExternalDependency(
                    name=service.name,
                    type=spec.external_type or service.framework.lower(),
                    host="127.0.0.1",
                    port=port,
                )
            )
        else:
            temp_specs.append(
                ServiceSpec(
                    name=service.name,
                    path=Path(service.path),
                    command="",
                    port=port,
                )
            )

    dependency_map = resolve_dependency_map(
        project_root=root,
        services=temp_specs,
        external_dependencies=external_models,
        catalog=catalog_deps,
    )

    # Merge adapter soft hints (e.g. Celery broker → redis) when targets exist.
    known_all = set(known_services) | set(known_externals)
    external_aliases = {
        "redis": "redis",
        "cache": "redis",
        "postgres": "postgres",
        "postgresql": "postgres",
        "db": "postgres",
        "rabbitmq": "rabbitmq",
        "amqp": "rabbitmq",
    }
    for service, spec in zip(services, specs, strict=True):
        if spec is None or spec.external or not spec.depends_on:
            continue
        existing = list(dependency_map.get(service.name, ()))
        for dep in spec.depends_on:
            resolved = _resolve_adapter_dep(dep, known_all, known_externals, external_aliases)
            if resolved and resolved != service.name and resolved not in existing:
                existing.append(resolved)
        if existing:
            dependency_map[service.name] = tuple(existing)

    needs_http = any(
        spec is not None and not spec.external and spec.health == "http"
        for spec in specs
    )
    needs_tcp = any(
        spec is not None and not spec.external and spec.health == "tcp"
        for spec in specs
    )

    imports = ["Stack"]
    if needs_http:
        imports.append("HttpHealthCheck")
    if needs_tcp:
        imports.append("TcpHealthCheck")

    lines = [
        f"from stackpilot import {', '.join(imports)}",
        "",
        "stack = Stack()",
        "",
    ]

    for index, service in enumerate(services):
        relative_path = _format_relative_service_path(root, Path(service.path))
        spec = specs[index]
        port = ports[index]

        if spec is not None and spec.external:
            dep_type = spec.external_type or service.framework.lower()
            lines.append("stack.external_dependency(")
            lines.append(f"    name={_format_py_string(service.name)},")
            lines.append(f"    type={_format_py_string(dep_type)},")
            lines.append(f"    host={_format_py_string('127.0.0.1')},")
            lines.append(f"    port={port},")
            lines.append(")")
        else:
            command = (
                spec.command if spec is not None else build_command(service, port)
            )
            emit_port = spec is None or spec.uses_port or spec.fixed_port is not None

            lines.append("stack.service(")
            lines.append(f"    name={_format_py_string(service.name)},")
            lines.append(f"    path={_format_py_string(relative_path)},")
            lines.append(f"    command={_format_py_string(command)},")
            if emit_port:
                lines.append(f"    port={port},")
            if spec is not None and spec.reload:
                lines.append("    reload=True,")
            health_line = _format_health_line(spec, port)
            if health_line is not None:
                lines.append(health_line)
            deps = dependency_map.get(service.name, ())
            if deps:
                rendered = ", ".join(_format_py_string(dep) for dep in deps)
                lines.append(f"    depends_on=[{rendered}],")
            lines.append(")")

        if index < len(services) - 1:
            lines.append("")

    if services:
        lines.append("")

    lines.extend(["stack.run()", ""])
    return "\n".join(lines)


def write_stackfile(
    services: Sequence[StackServiceLike],
    *,
    project_root: Path,
    output_path: Path | None = None,
    force: bool = False,
    start_port: int = BASE_PORT,
) -> WriteResult:
    """
    Generate and write ``Stackfile.py``.

    Raises ``StackfileExistsError`` when the destination exists and
    ``force`` is false.
    """

    root = project_root.expanduser().resolve()
    destination = (
        output_path.expanduser().resolve()
        if output_path is not None
        else root / STACKFILE_NAME
    )

    overwritten = destination.exists()
    if overwritten and not force:
        raise StackfileExistsError(destination)

    content = generate_stackfile(
        services,
        project_root=root,
        start_port=start_port,
    )
    destination.write_text(content, encoding="utf-8")

    ports = tuple(assign_ports(services, start_port=start_port))
    return WriteResult(
        output_path=destination,
        content=content,
        overwritten=overwritten,
        ports=ports,
    )


def _adapter_spec(
    service: StackServiceLike,
    *,
    port: int | None,
) -> AdapterServiceSpec | None:
    return default_registry.generate_service(
        Path(service.path),
        port=port,
        framework=service.framework,
    )


def _resolve_adapter_dep(
    dep: str,
    known_all: set[str],
    known_externals: list[str],
    aliases: dict[str, str],
) -> str | None:
    """Map adapter dependency hints onto registered service / external names."""

    key = str(dep or "").strip()
    if not key:
        return None
    if key in known_all:
        return key
    kind = aliases.get(key.lower(), key.lower())
    for registered in known_externals:
        reg_kind = aliases.get(registered.lower(), registered.lower())
        if registered == key or reg_kind == kind or registered.lower() == kind:
            return registered
    for name in known_all:
        if aliases.get(name.lower(), name.lower()) == kind:
            return name
    return None


def _format_health_line(spec: AdapterServiceSpec | None, port: int) -> str | None:
    if spec is None or spec.external or spec.health in {"none", "process"}:
        return None
    if spec.health == "http":
        url = f"http://127.0.0.1:{port}{spec.health_path}"
        return f"    health_check=HttpHealthCheck(url={_format_py_string(url)}),"
    if spec.health == "tcp":
        return (
            f"    health_check=TcpHealthCheck("
            f"host={_format_py_string('127.0.0.1')}, port={port}),"
        )
    return None


def _format_py_string(value: str) -> str:
    """
    Render ``value`` as a double-quoted Python string literal.

    Centralized escaping for every generated Stackfile string field. Handles
    quotes, backslashes, newlines, tabs, unicode, and Windows paths so the
    output is always valid Python source (never raw concatenation).
    """

    # json.dumps always emits double-quoted strings with correct escapes;
    # the result is a valid Python string literal for these values.
    return json.dumps(str(value), ensure_ascii=False)


def _format_relative_service_path(project_root: Path, service_path: Path) -> str:
    resolved = service_path.expanduser().resolve()
    relative_path = resolved.relative_to(project_root).as_posix()
    return f"./{relative_path}"
