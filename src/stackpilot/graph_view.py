"""Architecture graph presentation for ``stackpilot graph``.

Pure rendering helpers. Dependency validation and resolution stay in
``dependency_graph`` — this module only formats what the graph already knows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .config import ServiceSpec, external_dependency_display_name
from .dependency_graph import DependencyGraph
from .models import ServiceState

# ---------------------------------------------------------------------------
# Status / style constants
# ---------------------------------------------------------------------------

STATUS_RUNNING = ServiceState.RUNNING.value
STATUS_STOPPED = ServiceState.STOPPED.value
STATUS_STARTING = ServiceState.STARTING.value
STATUS_FAILED = ServiceState.FAILED.value
STATUS_UNKNOWN = "unknown"
STATUS_EXTERNAL = "external"

_STATUS_GLYPH: Mapping[str, str] = {
    STATUS_RUNNING: "🟢",
    STATUS_STOPPED: "🔴",
    STATUS_STARTING: "🟡",
    STATUS_FAILED: "🔴",
    STATUS_UNKNOWN: "⚪",
    STATUS_EXTERNAL: "🔵",
}

_STATUS_GLYPH_ASCII: Mapping[str, str] = {
    STATUS_RUNNING: "[*]",
    STATUS_STOPPED: "[x]",
    STATUS_STARTING: "[~]",
    STATUS_FAILED: "[x]",
    STATUS_UNKNOWN: "[ ]",
    STATUS_EXTERNAL: "[#]",
}

# Rich style names (gracefully ignored when Rich / ANSI is unavailable).
_STATUS_STYLE: Mapping[str, str] = {
    STATUS_RUNNING: "bold green",
    STATUS_STOPPED: "bold red",
    STATUS_STARTING: "bold yellow",
    STATUS_FAILED: "bold red",
    STATUS_UNKNOWN: "bright_black",
    STATUS_EXTERNAL: "bold blue",
}

_FRAMEWORK_BUCKETS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("FastAPI", ("fastapi",)),
    ("Django", ("django",)),
    ("Flask", ("flask",)),
    ("Node", ("express", "nestjs", "node")),
)

_RULE = "─" * 44
_RULE_ASCII = "-" * 44


@dataclass(frozen=True, slots=True)
class NodeDisplay:
    """Presentation metadata for one graph node."""

    name: str
    status: str
    port: Optional[int] = None
    framework: str = ""
    external: bool = False
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class ArchitectureStats:
    """Aggregate counts shown in the architecture header / footer."""

    services: int
    running: int
    stopped: int
    fastapi: int
    django: int
    flask: int
    node: int
    external: int
    depth: int
    cycles: str = "None"


@dataclass
class _RenderContext:
    graph: DependencyGraph
    nodes: Mapping[str, NodeDisplay]
    visited: Set[str] = field(default_factory=set)
    unicode: bool = True


def collect_node_displays(
    graph: DependencyGraph,
    *,
    statuses: Mapping[str, str] | None = None,
    ports: Mapping[str, Optional[int]] | None = None,
    frameworks: Mapping[str, str] | None = None,
) -> Dict[str, NodeDisplay]:
    """Build display metadata for every node in ``graph``."""

    statuses = statuses or {}
    ports = ports or {}
    frameworks = frameworks or {}
    nodes: Dict[str, NodeDisplay] = {}

    for name, spec in graph.specs.items():
        status = _normalize_status(statuses.get(name))
        port = ports.get(name)
        if port is None:
            port = _port_from_spec(spec)
        framework = (frameworks.get(name) or "").strip()
        if not framework:
            framework = _detect_framework_label(spec)
        nodes[name] = NodeDisplay(
            name=name,
            status=status,
            port=port,
            framework=framework,
            external=False,
            display_name=name,
        )

    for name, dep in graph.external.items():
        port = ports.get(name, dep.port)
        nodes[name] = NodeDisplay(
            name=name,
            status=STATUS_EXTERNAL,
            port=int(port) if port is not None else None,
            framework="",
            external=True,
            display_name=dep.display_name or external_dependency_display_name(dep.type, name),
        )

    return nodes


def find_root_names(graph: DependencyGraph) -> List[str]:
    """
    Return nodes that nothing else depends on (architecture entry points).

    Application services are listed first (registration order), then external
    roots. When every node has an incoming edge (should not happen on a valid
    DAG), fall back to application service names so the tree still renders.
    """

    depended_on: Set[str] = set()
    for deps in graph.edges.values():
        depended_on.update(deps)

    app_roots = [name for name in graph.specs if name not in depended_on]
    ext_roots = [name for name in graph.external if name not in depended_on]
    roots = app_roots + ext_roots
    if roots:
        return roots
    if graph.specs:
        return list(graph.specs.keys())
    return list(graph.names)


def dependency_depth(graph: DependencyGraph) -> int:
    """Longest dependency chain length (edges) in the DAG."""

    memo: Dict[str, int] = {}

    def depth(name: str, stack: Set[str]) -> int:
        if name in memo:
            return memo[name]
        if name in stack:
            return 0
        stack.add(name)
        children = graph.edges.get(name, ())
        value = 0 if not children else 1 + max(depth(child, stack) for child in children)
        stack.discard(name)
        memo[name] = value
        return value

    if not graph.names:
        return 0
    return max(depth(name, set()) for name in graph.names)


def compute_stats(
    graph: DependencyGraph,
    nodes: Mapping[str, NodeDisplay],
    *,
    cycle: Sequence[str] | None = None,
) -> ArchitectureStats:
    """Aggregate header / footer counters from node display metadata."""

    running = 0
    stopped = 0
    buckets = {label: 0 for label, _ in _FRAMEWORK_BUCKETS}

    for name in graph.specs:
        node = nodes[name]
        if node.status == STATUS_RUNNING:
            running += 1
        elif node.status in {STATUS_STOPPED, STATUS_FAILED, STATUS_UNKNOWN}:
            stopped += 1
        elif node.status == STATUS_STARTING:
            # Starting counts toward neither running nor stopped totals.
            pass
        else:
            stopped += 1

        key = node.framework.strip().lower()
        for label, aliases in _FRAMEWORK_BUCKETS:
            if key in aliases or key == label.lower():
                buckets[label] += 1
                break

    cycles = "None"
    if cycle:
        # Unique members excluding the repeated closing node.
        members = list(dict.fromkeys(cycle[:-1] if len(cycle) > 1 and cycle[0] == cycle[-1] else cycle))
        cycles = " → ".join(members) if members else "Detected"

    return ArchitectureStats(
        services=len(graph.specs),
        running=running,
        stopped=stopped,
        fastapi=buckets["FastAPI"],
        django=buckets["Django"],
        flask=buckets["Flask"],
        node=buckets["Node"],
        external=len(graph.external),
        depth=dependency_depth(graph),
        cycles=cycles,
    )


def format_circular_dependency(cycle: Sequence[str]) -> str:
    """Render a clear circular-dependency block for the graph command."""

    if not cycle:
        return "❌ Circular Dependency"

    lines = ["❌ Circular Dependency", ""]
    for index, name in enumerate(cycle):
        lines.append(name)
        if index < len(cycle) - 1:
            lines.append("  ↓")
    return "\n".join(lines)


def format_circular_dependency_ascii(cycle: Sequence[str]) -> str:
    """ASCII-safe variant of :func:`format_circular_dependency`."""

    if not cycle:
        return "X Circular Dependency"

    lines = ["X Circular Dependency", ""]
    for index, name in enumerate(cycle):
        lines.append(name)
        if index < len(cycle) - 1:
            lines.append("  v")
    return "\n".join(lines)


def format_architecture_report(
    graph: DependencyGraph,
    *,
    statuses: Mapping[str, str] | None = None,
    ports: Mapping[str, Optional[int]] | None = None,
    frameworks: Mapping[str, str] | None = None,
    color: bool = False,
    unicode: bool = True,
) -> str:
    """
    Full professional architecture visualization.

    Includes header stats, dependency tree, and footer. Does not validate the
    graph — callers should handle cycles separately when desired.
    """

    nodes = collect_node_displays(
        graph,
        statuses=statuses,
        ports=ports,
        frameworks=frameworks,
    )
    stats = compute_stats(graph, nodes)
    tree = format_dependency_tree(graph, nodes=nodes, unicode=unicode)

    header = _format_header(stats, unicode=unicode)
    footer = _format_footer(stats, unicode=unicode)
    body = "\n".join(part for part in (header, "", tree, "", footer) if part is not None)

    if color:
        return _colorize_report(body, nodes)
    return body


def format_dependency_tree(
    graph: DependencyGraph,
    *,
    nodes: Mapping[str, NodeDisplay] | None = None,
    unicode: bool = True,
    prefer_rich: bool = False,
) -> str:
    """Render only the dependency tree (no header / footer)."""

    if not graph.names:
        return "(no services)"

    display_nodes = nodes or collect_node_displays(graph)

    # No depends_on edges → independent roots (still status/port/framework),
    # plus a short tip pointing at Stackfile wiring / sync inference.
    if _has_no_app_edges(graph):
        return _format_ungraphed_services(graph, display_nodes, unicode=unicode)

    roots = find_root_names(graph)

    # Unicode walker is the primary renderer: it supports inter-sibling spacer
    # lines (│) that match the architecture mockup. Rich Tree is optional —
    # useful when callers want Rich's layout, but it drops those spacers and
    # can wrap deep labels once indentation exceeds the console width.
    if prefer_rich and unicode and dependency_depth(graph) <= 24:
        rendered = _render_with_rich_trees(graph, display_nodes, roots)
        if rendered is not None:
            return rendered

    ctx = _RenderContext(graph=graph, nodes=display_nodes, unicode=unicode)
    lines: List[str] = []
    for index, root in enumerate(roots):
        _append_unicode_subtree(lines, ctx, root, prefix="", is_root=True)
        if index < len(roots) - 1:
            lines.append("")
    return "\n".join(lines)


def _has_no_app_edges(graph: DependencyGraph) -> bool:
    return all(not graph.edges.get(name, ()) for name in graph.specs)


def _format_ungraphed_services(
    graph: DependencyGraph,
    nodes: Mapping[str, NodeDisplay],
    *,
    unicode: bool,
) -> str:
    tip = (
        "No depends_on edges in Stackfile.py — showing independent roots.\n"
        "Add depends_on=[...] or keep docker-compose / SERVICE_URL references "
        "so StackPilot can infer the graph (re-run: stackpilot sync --force)."
    )
    lines: List[str] = [tip, ""]

    # Forest of roots: every application service, then orphan externals.
    for name in graph.specs:
        node = nodes.get(name) or NodeDisplay(name=name, status=STATUS_UNKNOWN)
        lines.append(format_node_label(node, unicode=unicode))

    if graph.external:
        if graph.specs:
            lines.append("")
        for name in graph.external:
            node = nodes[name]
            lines.append(format_node_label(node, unicode=unicode))

    return "\n".join(lines)


def format_node_label(node: NodeDisplay, *, unicode: bool = True) -> str:
    """Single-line label: glyph, name/port, optional framework."""

    glyphs = _STATUS_GLYPH if unicode else _STATUS_GLYPH_ASCII
    glyph = glyphs.get(node.status, glyphs[STATUS_UNKNOWN])
    title = node.display_name if node.external else node.name

    parts = [f"{glyph} {title}"]
    if node.port is not None:
        parts[0] = f"{glyph} {title} (:{node.port})"

    if node.external:
        # Prefer a clean infrastructure label; keep a subtle marker for tests /
        # greppability when the display name already equals the raw name.
        if title == node.name:
            parts.append("[external]")
    elif node.framework:
        parts.append(f"[{node.framework}]")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Header / footer
# ---------------------------------------------------------------------------


def _format_header(stats: ArchitectureStats, *, unicode: bool) -> str:
    rule = _RULE if unicode else _RULE_ASCII
    return "\n".join(
        [
            "StackPilot Architecture",
            rule,
            "",
            f"Services : {stats.services}",
            f"Running  : {stats.running}",
            f"Stopped  : {stats.stopped}",
            f"FastAPI  : {stats.fastapi}",
            f"Django   : {stats.django}",
            f"Flask    : {stats.flask}",
            f"Node     : {stats.node}",
            f"External : {stats.external}",
        ]
    )


def _format_footer(stats: ArchitectureStats, *, unicode: bool) -> str:
    rule = _RULE if unicode else _RULE_ASCII
    return "\n".join(
        [
            rule,
            "",
            f"Total Services         : {stats.services}",
            f"Dependency Depth       : {stats.depth}",
            f"Circular Dependencies  : {stats.cycles}",
            "",
            "Graph Generated Successfully",
        ]
    )


# ---------------------------------------------------------------------------
# Unicode tree walk
# ---------------------------------------------------------------------------


def _visible_children(ctx: _RenderContext, name: str) -> List[str]:
    """Children to render; omit already-shown external leaves."""

    visible: List[str] = []
    for child in ctx.graph.edges.get(name, ()):
        child_node = ctx.nodes.get(child)
        if (
            child in ctx.visited
            and child_node is not None
            and child_node.external
        ):
            continue
        visible.append(child)
    return visible


def _append_unicode_subtree(
    lines: List[str],
    ctx: _RenderContext,
    name: str,
    *,
    prefix: str,
    is_root: bool,
) -> None:
    node = ctx.nodes.get(name) or NodeDisplay(name=name, status=STATUS_UNKNOWN)
    label = format_node_label(node, unicode=ctx.unicode)

    if is_root:
        lines.append(label)

    already_seen = name in ctx.visited
    ctx.visited.add(name)

    if already_seen:
        return

    children = _visible_children(ctx, name)
    if not children:
        return

    tee = "├── " if ctx.unicode else "+-- "
    elbow = "└── " if ctx.unicode else "`-- "
    pipe = "│   " if ctx.unicode else "|   "
    blank = "    "
    spacer = "│" if ctx.unicode else "|"

    for index, child in enumerate(children):
        is_last = index == len(children) - 1
        branch = elbow if is_last else tee
        child_node = ctx.nodes.get(child) or NodeDisplay(name=child, status=STATUS_UNKNOWN)
        reused = child in ctx.visited
        child_label = format_node_label(child_node, unicode=ctx.unicode)
        if reused:
            child_label = f"{child_label} ⋯"
        lines.append(f"{prefix}{branch}{child_label}")

        if reused:
            if not is_last and prefix == "":
                lines.append(f"{prefix}{spacer}")
            continue

        ctx.visited.add(child)
        if ctx.graph.edges.get(child, ()):
            _append_children_only(
                lines,
                ctx,
                child,
                prefix=f"{prefix}{blank if is_last else pipe}",
            )

        # Breathing room between top-level branches under a root.
        if not is_last and prefix == "":
            lines.append(f"{prefix}{spacer}")


def _append_children_only(
    lines: List[str],
    ctx: _RenderContext,
    name: str,
    *,
    prefix: str,
) -> None:
    """Emit children of an already-printed node (used after root / branch)."""

    children = _visible_children(ctx, name)
    tee = "├── " if ctx.unicode else "+-- "
    elbow = "└── " if ctx.unicode else "`-- "
    pipe = "│   " if ctx.unicode else "|   "
    blank = "    "
    spacer = "│" if ctx.unicode else "|"

    for index, child in enumerate(children):
        is_last = index == len(children) - 1
        branch = elbow if is_last else tee
        child_node = ctx.nodes.get(child) or NodeDisplay(name=child, status=STATUS_UNKNOWN)
        child_label = format_node_label(child_node, unicode=ctx.unicode)
        reused = child in ctx.visited
        if reused:
            child_label = f"{child_label} ⋯"
        lines.append(f"{prefix}{branch}{child_label}")
        if reused:
            if not is_last and prefix == "":
                lines.append(f"{prefix}{spacer}")
            continue
        ctx.visited.add(child)
        if ctx.graph.edges.get(child, ()):
            _append_children_only(
                lines,
                ctx,
                child,
                prefix=f"{prefix}{blank if is_last else pipe}",
            )
        if not is_last and prefix == "":
            lines.append(f"{prefix}{spacer}")


# ---------------------------------------------------------------------------
# Optional Rich Tree
# ---------------------------------------------------------------------------


def _render_with_rich_trees(
    graph: DependencyGraph,
    nodes: Mapping[str, NodeDisplay],
    roots: Sequence[str],
) -> Optional[str]:
    try:
        from rich.console import Console
        from rich.tree import Tree
        from rich.text import Text
        import io
    except ImportError:
        return None

    visited: Set[str] = set()
    buffer = io.StringIO()
    # Deep dependency chains indent far beyond a typical terminal width;
    # use a generous buffer so Rich does not wrap / drop leaf labels.
    depth = dependency_depth(graph)
    width = max(200, 8 * depth + 64)
    console = Console(
        file=buffer,
        force_terminal=False,
        color_system=None,
        width=width,
        emoji=True,
        highlight=False,
        soft_wrap=False,
    )

    for index, root in enumerate(roots):
        if index:
            console.print()
        node = nodes.get(root) or NodeDisplay(name=root, status=STATUS_UNKNOWN)
        tree = Tree(Text(format_node_label(node, unicode=True)))
        visited.add(root)
        for child in graph.edges.get(root, ()):
            _add_rich_child(tree, graph, nodes, child, visited, Tree=Tree, Text=Text)
        console.print(tree)

    text = buffer.getvalue().rstrip("\n")
    if not text:
        return None

    # If wrapping still dropped a node name, prefer the manual Unicode walk.
    for name in graph.names:
        if name not in text and (
            nodes.get(name) is None or nodes[name].display_name not in text
        ):
            return None
    return text


def _add_rich_child(
    parent,
    graph: DependencyGraph,
    nodes: Mapping[str, NodeDisplay],
    name: str,
    visited: Set[str],
    *,
    Tree,
    Text,
) -> None:
    node = nodes.get(name) or NodeDisplay(name=name, status=STATUS_UNKNOWN)
    label = format_node_label(node, unicode=True)
    if name in visited:
        if node.external:
            return
        parent.add(Text(f"{label} ⋯"))
        return

    branch = parent.add(Text(label))
    visited.add(name)
    for child in graph.edges.get(name, ()):
        child_node = nodes.get(child)
        if (
            child in visited
            and child_node is not None
            and child_node.external
        ):
            continue
        _add_rich_child(branch, graph, nodes, child, visited, Tree=Tree, Text=Text)


def _colorize_report(body: str, nodes: Mapping[str, NodeDisplay]) -> str:
    """Apply Rich markup colors when printing via a capable Console."""

    _ = nodes
    try:
        from rich.console import Console
        import io
    except ImportError:
        return body

    buffer = io.StringIO()
    console = Console(
        file=buffer,
        force_terminal=True,
        color_system="auto",
        width=120,
        emoji=True,
        highlight=False,
        soft_wrap=False,
    )
    console.print(style_architecture_text(body), end="")
    return buffer.getvalue()


def style_architecture_text(body: str):
    """Return a Rich ``Text`` with status colors applied, when Rich is installed."""

    from rich.text import Text

    glyph_to_status = {glyph: status for status, glyph in _STATUS_GLYPH.items()}
    styled = Text()
    for index, line in enumerate(body.splitlines()):
        if index:
            styled.append("\n")
        stripped = line.lstrip()
        lead = line[: len(line) - len(stripped)]
        status = None
        for glyph, st in glyph_to_status.items():
            if stripped.startswith(glyph):
                status = st
                break
        if status is None:
            if line.startswith("StackPilot Architecture"):
                styled.append(line, style="bold")
            elif line.startswith("Graph Generated Successfully"):
                styled.append(line, style="green")
            elif line.startswith("❌"):
                styled.append(line, style="bold red")
            else:
                styled.append(line)
            continue
        styled.append(lead)
        styled.append(stripped, style=_STATUS_STYLE.get(status, ""))
    return styled


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_status(value: Optional[str]) -> str:
    if not value:
        return STATUS_STOPPED
    key = str(value).strip().lower()
    if key in {
        STATUS_RUNNING,
        STATUS_STOPPED,
        STATUS_STARTING,
        STATUS_FAILED,
        STATUS_UNKNOWN,
        STATUS_EXTERNAL,
    }:
        return key
    if key in {"reachable"}:
        return STATUS_EXTERNAL
    if key in {"unreachable"}:
        return STATUS_STOPPED
    return STATUS_UNKNOWN


def _port_from_spec(spec: ServiceSpec) -> Optional[int]:
    try:
        from .port_detect import resolve_service_port

        return resolve_service_port(spec, pid=None)
    except Exception:
        return spec.port


def _detect_framework_label(spec: ServiceSpec) -> str:
    """
    Resolve a display framework label without touching discovery APIs.

    Command heuristics win first (Stackfile is authoritative for how the
    process is launched). Adapter match on ``spec.path`` is the fallback when
    the command does not identify a framework. Results are cached per resolved
    path because adapter detection may touch the filesystem.
    """

    command = (spec.command or "").lower()
    if "uvicorn" in command or "fastapi" in command:
        return "FastAPI"
    if "flask" in command:
        return "Flask"
    if "django" in command or "manage.py" in command:
        return "Django"
    if "celery" in command:
        return "Celery"
    if "nest" in command:
        return "NestJS"
    if "express" in command or "node" in command or "npm" in command:
        return "Express"

    try:
        path = Path(spec.path).expanduser().resolve()
    except OSError:
        return ""

    cache_key = str(path)
    if cache_key in _FRAMEWORK_PATH_CACHE:
        return _FRAMEWORK_PATH_CACHE[cache_key]

    label = ""
    try:
        if path.exists() and path.is_dir():
            from .adapters import default_registry

            adapter = default_registry.match(path)
            if adapter is not None and not getattr(adapter, "external", False):
                label = adapter.name
    except Exception:
        label = ""

    _FRAMEWORK_PATH_CACHE[cache_key] = label
    return label


# Resolved-path -> framework label. Adapter detection can be filesystem-heavy;
# graph rendering often revisits the same service directory.
_FRAMEWORK_PATH_CACHE: Dict[str, str] = {}
