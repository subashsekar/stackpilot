from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from .config import ExternalDependency, ServiceSpec, Stack


class DependencyError(ValueError):
    """Base error for dependency graph problems."""


class DuplicateServiceError(DependencyError):
    """Raised when two services share the same name."""


class MissingDependencyError(DependencyError):
    """Raised when a service depends on a name that is not registered."""


class CircularDependencyError(DependencyError):
    """Raised when the dependency graph contains a cycle."""

    def __init__(self, cycle: Sequence[str]) -> None:
        self.cycle = tuple(cycle)
        super().__init__(format_cycle_path(self.cycle))


class UnknownServiceError(DependencyError):
    """Raised when a requested service name is not in the graph."""


def format_cycle_path(cycle: Sequence[str]) -> str:
    """
    Render a readable dependency cycle path.

    Example::

        gateway
        ↓

        auth
        ↓

        users
        ↓

        gateway
    """

    if not cycle:
        return ""

    parts: List[str] = []
    for index, name in enumerate(cycle):
        parts.append(name)
        if index < len(cycle) - 1:
            parts.append("↓")
            parts.append("")
    return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class DependencyGraph:
    """
    Directed graph of application services and external dependencies.

    Edge meaning: ``A -> B`` means ``A`` depends on ``B`` (``B`` must be
    ready before ``A`` starts). External dependency nodes are never started;
    they only participate in validation and display.
    """

    names: Tuple[str, ...]
    edges: Mapping[str, Tuple[str, ...]]
    specs: Mapping[str, ServiceSpec]
    external: Mapping[str, ExternalDependency] = field(default_factory=dict)

    @classmethod
    def from_services(
        cls,
        services: Sequence[ServiceSpec],
        external_dependencies: Sequence[ExternalDependency] | None = None,
    ) -> "DependencyGraph":
        """Build a graph from registered service specs and optional externals."""

        specs: Dict[str, ServiceSpec] = {}
        for spec in services:
            if spec.name in specs:
                raise DuplicateServiceError(f"Duplicate service name: {spec.name}")
            specs[spec.name] = spec

        external: Dict[str, ExternalDependency] = {}
        for dep in external_dependencies or ():
            if dep.name in specs:
                raise DuplicateServiceError(
                    f"Duplicate name shared by service and external dependency: {dep.name}"
                )
            if dep.name in external:
                raise DuplicateServiceError(
                    f"Duplicate external dependency name: {dep.name}"
                )
            external[dep.name] = dep

        # Application services may depend on other services or externals.
        # External nodes have no outbound edges.
        edges: Dict[str, Tuple[str, ...]] = {
            name: tuple(dict.fromkeys(spec.depends_on)) for name, spec in specs.items()
        }
        for name in external:
            edges.setdefault(name, ())

        names = tuple(list(specs.keys()) + list(external.keys()))
        return cls(names=names, edges=edges, specs=specs, external=external)

    @classmethod
    def from_stack(cls, stack: Stack) -> "DependencyGraph":
        """Build a graph from a full ``Stack`` (services + external deps)."""

        return cls.from_services(
            stack.services,
            stack.external_dependencies,
        )

    def is_external(self, name: str) -> bool:
        return name in self.external

    def label(self, name: str) -> str:
        """Display label for ``name`` (appends ``[external]`` when applicable)."""

        if self.is_external(name):
            return f"{name} [external]"
        return name

    def validate(self) -> None:
        """Validate dependency names and reject circular dependencies."""

        self._validate_missing()
        cycle = self.detect_cycle()
        if cycle is not None:
            raise CircularDependencyError(cycle)

    def _validate_missing(self) -> None:
        known = set(self.names)
        missing: List[Tuple[str, str]] = []
        for name, deps in self.edges.items():
            for dep in deps:
                if dep not in known:
                    missing.append((name, dep))

        if not missing:
            return

        details = ", ".join(
            f"'{service}' depends on unknown service '{dep}'"
            for service, dep in missing
        )
        raise MissingDependencyError(f"Missing dependencies: {details}")

    def detect_cycle(self) -> Optional[Tuple[str, ...]]:
        """
        DFS-based cycle detection.

        Returns a path that starts and ends on the same service when a cycle
        exists, otherwise ``None``.
        """

        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {name: WHITE for name in self.names}
        parent: Dict[str, Optional[str]] = {name: None for name in self.names}

        def dfs(node: str) -> Optional[Tuple[str, ...]]:
            color[node] = GRAY
            for dep in self.edges.get(node, ()):
                if color[dep] == GRAY:
                    return _reconstruct_cycle(parent, node, dep)
                if color[dep] == WHITE:
                    parent[dep] = node
                    found = dfs(dep)
                    if found is not None:
                        return found
            color[node] = BLACK
            return None

        for name in self.names:
            if color[name] == WHITE:
                found = dfs(name)
                if found is not None:
                    return found
        return None

    def topological_order(self) -> List[str]:
        """
        Return nodes in dependency-safe order.

        Dependencies appear before the nodes that require them. Includes
        external dependency names when they participate in the graph.
        """

        self.validate()

        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {name: WHITE for name in self.names}
        order: List[str] = []

        def dfs(node: str) -> None:
            color[node] = GRAY
            for dep in self.edges.get(node, ()):
                if color[dep] == WHITE:
                    dfs(dep)
            color[node] = BLACK
            order.append(node)

        for name in self.names:
            if color[name] == WHITE:
                dfs(name)
        return order

    def resolve_for(self, target: str) -> List[str]:
        """
        Selective startup: ``target`` plus every transitive dependency,
        ordered topologically.

        ``target`` must be an application service (not an external dependency).
        """

        if target not in self.specs:
            if target in self.external:
                raise UnknownServiceError(
                    f"'{target}' is an external dependency and cannot be started"
                )
            raise UnknownServiceError(f"Unknown service: {target}")

        self.validate()

        required: Set[str] = set()

        def collect(name: str) -> None:
            if name in required:
                return
            required.add(name)
            for dep in self.edges.get(name, ()):
                collect(dep)

        collect(target)
        return [name for name in self.topological_order() if name in required]

    def dependents(self, name: str, *, transitive: bool = True) -> List[str]:
        """
        Return application services that depend on ``name``, in start order.

        When ``transitive`` is True, includes indirect dependents.
        External dependency names are never returned (they are not started).
        """

        if name not in self.specs and name not in self.external:
            raise UnknownServiceError(f"Unknown service: {name}")

        reverse: Dict[str, List[str]] = {n: [] for n in self.names}
        for service, deps in self.edges.items():
            for dep in deps:
                reverse.setdefault(dep, []).append(service)

        found: Set[str] = set()

        def collect(node: str) -> None:
            for child in reverse.get(node, ()):
                if child in found:
                    continue
                found.add(child)
                if transitive:
                    collect(child)

        collect(name)
        return [
            n
            for n in self.topological_order()
            if n in found and n in self.specs
        ]

    def ordered_specs(
        self,
        *,
        target: Optional[str] = None,
    ) -> List[ServiceSpec]:
        """
        Return application ``ServiceSpec`` objects in start order.

        External dependency nodes are omitted — they are validated separately
        and never passed to Runner / ProcessManager.
        """

        names = self.resolve_for(target) if target is not None else self.topological_order()
        return [self.specs[name] for name in names if name in self.specs]

    def required_externals(
        self,
        *,
        target: Optional[str] = None,
    ) -> List[ExternalDependency]:
        """
        External dependencies required before starting the selected services.

        Selective: externals in the dependency closure of ``target``.
        Full stack: every external depended on by at least one application
        service being started.
        """

        if target is not None:
            names = self.resolve_for(target)
            ordered_names = names
        else:
            ordered_names = self.topological_order()
            required: Set[str] = set()
            for name, deps in self.edges.items():
                if name not in self.specs:
                    continue
                for dep in deps:
                    if dep in self.external:
                        required.add(dep)
            ordered_names = [
                n for n in ordered_names if n in self.specs or n in required
            ]

        seen: Set[str] = set()
        result: List[ExternalDependency] = []
        for name in ordered_names:
            if name in self.external and name not in seen:
                seen.add(name)
                result.append(self.external[name])
        return result

    def format_ascii_tree(self) -> str:
        """
        Render the architecture dependency tree.

        Uses root detection, Unicode connectors, status glyphs, ports, and
        framework labels. Prefer :func:`stackpilot.graph_view.format_architecture_report`
        for the full header / footer view used by ``stackpilot graph``.
        """

        self.validate()

        from .graph_view import format_dependency_tree

        return format_dependency_tree(self)


def _reconstruct_cycle(
    parent: Mapping[str, Optional[str]],
    current: str,
    back_edge_to: str,
) -> Tuple[str, ...]:
    """Build ``[start, ..., start]`` from DFS parent pointers and a back edge."""

    path: List[str] = [current]
    node: Optional[str] = current
    while node is not None and node != back_edge_to:
        node = parent.get(node)
        if node is None:
            break
        path.append(node)

    path.reverse()
    if not path or path[0] != back_edge_to:
        path.insert(0, back_edge_to)
    path.append(back_edge_to)
    return tuple(path)


def build_graph(
    services: Union[Sequence[ServiceSpec], Stack],
    external_dependencies: Sequence[ExternalDependency] | None = None,
) -> DependencyGraph:
    """Convenience constructor used by Orchestrator and CLI."""

    if isinstance(services, Stack):
        return DependencyGraph.from_stack(services)
    return DependencyGraph.from_services(services, external_dependencies)
