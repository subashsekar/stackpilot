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
    language: str = ""
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
    cycle_names: Set[str] = field(default_factory=set)
    compact: bool = False


def collect_node_displays(
    graph: DependencyGraph,
    *,
    statuses: Mapping[str, str] | None = None,
    ports: Mapping[str, Optional[int]] | None = None,
    frameworks: Mapping[str, str] | None = None,
    languages: Mapping[str, str] | None = None,
) -> Dict[str, NodeDisplay]:
    """Build display metadata for every node in ``graph``."""

    statuses = statuses or {}
    ports = ports or {}
    frameworks = frameworks or {}
    languages = languages or {}
    nodes: Dict[str, NodeDisplay] = {}

    for name, spec in graph.specs.items():
        status = _normalize_status(statuses.get(name))
        port = ports.get(name)
        if port is None:
            port = _port_from_spec(spec)
        framework = (frameworks.get(name) or "").strip()
        if not framework:
            framework = _detect_framework_label(spec)
        language = (languages.get(name) or "").strip()
        if not language:
            language = _language_for_framework(framework, spec.command)
        nodes[name] = NodeDisplay(
            name=name,
            status=status,
            port=port,
            framework=framework,
            language=language,
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
            language="",
            external=True,
            display_name=dep.display_name or external_dependency_display_name(dep.type, name),
        )

    return nodes


def find_root_names(graph: DependencyGraph) -> List[str]:
    """
    Return application entry points (nothing else depends on them).

    External dependencies are never tree roots — they render under the
    dedicated External Infrastructure section to avoid clutter on large
    stacks. When every application has an incoming edge (should not happen
    on a valid DAG), fall back to all application names so the tree still
    renders.
    """

    depended_on: Set[str] = set()
    for deps in graph.edges.values():
        depended_on.update(deps)

    app_roots = [name for name in graph.specs if name not in depended_on]
    if app_roots:
        return app_roots
    if graph.specs:
        return list(graph.specs.keys())
    return []


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
        cycles = " -> ".join(members) if members else "Detected"

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
    languages: Mapping[str, str] | None = None,
    color: bool = False,
    unicode: bool = True,
    cycle: Sequence[str] | None = None,
) -> str:
    """
    Full professional architecture visualization.

    Separates Applications (dependency tree of app services), External
    Infrastructure (grouped once), and Connections (edge list). Never
    raises — rendering failures degrade to a minimal safe report.
    """

    try:
        nodes = collect_node_displays(
            graph,
            statuses=statuses,
            ports=ports,
            frameworks=frameworks,
            languages=languages,
        )
        cycle_names = _cycle_name_set(cycle)
        stats = compute_stats(graph, nodes, cycle=cycle)
        apps = format_applications_section(
            graph,
            nodes=nodes,
            unicode=unicode,
            cycle_names=cycle_names,
        )
        externals = format_external_infrastructure(graph, nodes=nodes, unicode=unicode)
        connections = format_connections(graph, unicode=unicode)
        startup = format_startup_order(graph, unicode=unicode)

        header = _format_header(stats, unicode=unicode)
        footer = _format_footer(stats, unicode=unicode)
        legend = _format_legend(unicode=unicode)
        sections = [header, "", apps]
        if externals:
            sections.extend(["", externals])
        if connections:
            sections.extend(["", connections])
        sections.extend(["", startup, "", legend, "", footer])
        body = "\n".join(sections)

        if color:
            return _colorize_report(body, nodes)
        return body
    except Exception:
        # Graph rendering must never crash the CLI (cp1252, deep trees, etc.).
        try:
            names = ", ".join(graph.specs.keys()) or "(none)"
            ext = ", ".join(graph.external.keys()) or "(none)"
            return (
                "StackPilot Architecture\n"
                f"Applications: {names}\n"
                f"External Infrastructure: {ext}\n"
                "Graph Generated Successfully\n"
            )
        except Exception:
            return "StackPilot Architecture\nGraph Generated Successfully\n"


def format_startup_order(graph: DependencyGraph, *, unicode: bool = True) -> str:
    """Render topological startup order for application services."""

    try:
        ordered = [
            name for name in graph.topological_order() if name in graph.specs
        ]
    except Exception:
        ordered = list(graph.specs.keys())

    if not ordered:
        return "Startup order: (none)"

    arrow = " → " if unicode else " -> "
    return "Startup order: " + arrow.join(ordered)


def format_applications_section(
    graph: DependencyGraph,
    *,
    nodes: Mapping[str, NodeDisplay] | None = None,
    unicode: bool = True,
    cycle_names: Set[str] | None = None,
) -> str:
    """Render the Applications section (app-only dependency tree)."""

    tree = format_dependency_tree(
        graph,
        nodes=nodes,
        unicode=unicode,
        cycle_names=cycle_names,
    )
    return "Applications\n" + (_RULE if unicode else _RULE_ASCII) + "\n" + tree


def format_external_infrastructure(
    graph: DependencyGraph,
    *,
    nodes: Mapping[str, NodeDisplay] | None = None,
    unicode: bool = True,
) -> str:
    """Render grouped external infrastructure (once, never as tree roots)."""

    if not graph.external:
        return ""

    display_nodes = nodes or collect_node_displays(graph)
    lines = [
        "External Infrastructure",
        _RULE if unicode else _RULE_ASCII,
    ]
    for name in graph.external:
        node = display_nodes.get(name) or NodeDisplay(
            name=name, status=STATUS_EXTERNAL, external=True, display_name=name
        )
        lines.append(format_node_label(node, unicode=unicode))
    return "\n".join(lines)


def format_connections(graph: DependencyGraph, *, unicode: bool = True) -> str:
    """
    Render an explicit Connections list (app → deps).

    Keeps large graphs readable: the tree shows structure; this section
    enumerates every edge including links to external infrastructure.
    For large stacks (50+ apps) the edge list is omitted / simplified to
    reduce visual noise.
    """

    app_count = len(graph.specs)
    if app_count >= 50:
        # Simplify: show only external fan-in counts, not every edge.
        return _format_simplified_connections(graph, unicode=unicode)

    arrow = " → " if unicode else " -> "
    rows: List[str] = []
    for name in graph.specs:
        deps = list(graph.edges.get(name, ()))
        if not deps:
            continue
        labels: List[str] = []
        for dep in deps:
            if dep in graph.external:
                ext = graph.external[dep]
                labels.append(
                    ext.display_name
                    or external_dependency_display_name(ext.type, dep)
                )
            else:
                labels.append(dep)
        rows.append(f"{name}{arrow}{', '.join(labels)}")

    if not rows:
        return ""

    return "\n".join(
        [
            "Connections",
            _RULE if unicode else _RULE_ASCII,
            *rows,
        ]
    )


def _format_simplified_connections(
    graph: DependencyGraph, *, unicode: bool
) -> str:
    """Compact connections summary for 50+ service graphs."""

    ext_users: Dict[str, int] = {name: 0 for name in graph.external}
    app_edges = 0
    for name in graph.specs:
        for dep in graph.edges.get(name, ()):
            if dep in graph.external:
                ext_users[dep] = ext_users.get(dep, 0) + 1
            elif dep in graph.specs:
                app_edges += 1

    lines = [
        "Connections",
        _RULE if unicode else _RULE_ASCII,
        f"Application edges : {app_edges} (simplified for {len(graph.specs)} services)",
    ]
    for name, count in ext_users.items():
        if count <= 0:
            continue
        ext = graph.external[name]
        label = ext.display_name or external_dependency_display_name(ext.type, name)
        lines.append(f"{label} : used by {count} services")
    if len(lines) == 3 and app_edges == 0:
        return ""
    return "\n".join(lines)


def format_dependency_tree(
    graph: DependencyGraph,
    *,
    nodes: Mapping[str, NodeDisplay] | None = None,
    unicode: bool = True,
    prefer_rich: bool = False,
    cycle_names: Set[str] | None = None,
) -> str:
    """Render only the application dependency tree (no header / footer)."""

    if not graph.specs:
        if graph.external:
            return "(no application services)"
        return "(no services)"

    display_nodes = nodes or collect_node_displays(graph)
    cycles = cycle_names or set()
    compact = len(graph.specs) >= 50

    # No depends_on edges between apps → independent roots (still
    # status/port/framework). Externals live in their own section.
    if _has_no_app_edges(graph):
        return _format_ungraphed_services(graph, display_nodes, unicode=unicode)

    roots = find_root_names(graph)

    # Unicode walker is the primary renderer: it supports inter-sibling spacer
    # lines (│) that match the architecture mockup. Rich Tree is optional —
    # useful when callers want Rich's layout, but it drops those spacers and
    # can wrap deep labels once indentation exceeds the console width.
    # Skip Rich for large graphs — manual walk is faster and more compact.
    if (
        prefer_rich
        and unicode
        and not compact
        and dependency_depth(graph) <= 24
        and not cycles
    ):
        rendered = _render_with_rich_trees(graph, display_nodes, roots)
        if rendered is not None:
            return rendered

    ctx = _RenderContext(
        graph=graph,
        nodes=display_nodes,
        unicode=unicode,
        cycle_names=cycles,
        compact=compact,
    )
    lines: List[str] = []
    for index, root in enumerate(roots):
        _append_unicode_subtree(lines, ctx, root, prefix="", is_root=True)
        # Breathing room between top-level roots; tighten on large graphs.
        if index < len(roots) - 1 and not compact:
            lines.append("")
    return "\n".join(lines)


def _cycle_name_set(cycle: Sequence[str] | None) -> Set[str]:
    if not cycle:
        return set()
    return {str(name) for name in cycle if name}


def _has_no_app_edges(graph: DependencyGraph) -> bool:
    """True when no application depends on another application."""

    for name in graph.specs:
        for dep in graph.edges.get(name, ()):
            if dep in graph.specs:
                return False
    return True


def _format_ungraphed_services(
    graph: DependencyGraph,
    nodes: Mapping[str, NodeDisplay],
    *,
    unicode: bool,
) -> str:
    tip = (
        "No depends_on edges in Stackfile.py - showing independent roots.\n"
        "Add depends_on=[...] or keep docker-compose / SERVICE_URL references "
        "so StackPilot can infer the graph (re-run: stackpilot sync --force)."
    )
    lines: List[str] = [tip, ""]

    # Forest of application roots only — externals render in their section.
    for name in graph.specs:
        node = nodes.get(name) or NodeDisplay(name=name, status=STATUS_UNKNOWN)
        lines.append(format_node_label(node, unicode=unicode))

    return "\n".join(lines)


def format_node_label(
    node: NodeDisplay,
    *,
    unicode: bool = True,
    in_cycle: bool = False,
) -> str:
    """Single-line label: glyph, name/port, optional framework / language / cycle."""

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
    else:
        meta: List[str] = []
        if node.framework:
            meta.append(node.framework)
        if node.language:
            meta.append(node.language)
        if meta:
            sep = " · " if unicode else " / "
            parts.append(f"[{sep.join(meta)}]")

    if in_cycle:
        parts.append("[CYCLE]" if not unicode else "⚠ CYCLE")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Header / footer
# ---------------------------------------------------------------------------


def _format_header(stats: ArchitectureStats, *, unicode: bool) -> str:
    rule = _RULE if unicode else _RULE_ASCII
    lines = [
        "StackPilot Architecture",
        rule,
        "",
        f"Services : {stats.services}",
        f"Running  : {stats.running}",
        f"Stopped  : {stats.stopped}",
    ]
    # Omit zero framework buckets — less noise on mixed / small stacks.
    if stats.fastapi:
        lines.append(f"FastAPI  : {stats.fastapi}")
    if stats.django:
        lines.append(f"Django   : {stats.django}")
    if stats.flask:
        lines.append(f"Flask    : {stats.flask}")
    if stats.node:
        lines.append(f"Node     : {stats.node}")
    if stats.external:
        lines.append(f"External : {stats.external}")
    return "\n".join(lines)


def _format_legend(*, unicode: bool) -> str:
    """Status glyph legend for the architecture report."""

    rule = _RULE if unicode else _RULE_ASCII
    glyphs = _STATUS_GLYPH if unicode else _STATUS_GLYPH_ASCII
    rows = [
        f"{glyphs[STATUS_RUNNING]}  Running",
        f"{glyphs[STATUS_STARTING]}  Starting / Reload",
        f"{glyphs[STATUS_STOPPED]}  Stopped / Failed",
        f"{glyphs[STATUS_EXTERNAL]}  External",
        f"{glyphs[STATUS_UNKNOWN]}  Unknown",
    ]
    return "\n".join(["Legend", rule, *rows])


def _format_footer(stats: ArchitectureStats, *, unicode: bool) -> str:
    rule = _RULE if unicode else _RULE_ASCII
    cycles = stats.cycles
    if unicode and " -> " in cycles:
        cycles = cycles.replace(" -> ", " → ")
    return "\n".join(
        [
            rule,
            "",
            f"Dependency Depth       : {stats.depth}",
            f"Circular Dependencies  : {cycles}",
            "",
            "Graph Generated Successfully",
        ]
    )


# ---------------------------------------------------------------------------
# Unicode tree walk
# ---------------------------------------------------------------------------


def _visible_children(ctx: _RenderContext, name: str) -> List[str]:
    """
    Application children to render under ``name``.

    External dependencies are omitted from the tree — they appear once in
    the External Infrastructure section and in Connections, which keeps
    large (50+) graphs readable.
    """

    visible: List[str] = []
    for child in ctx.graph.edges.get(name, ()):
        child_node = ctx.nodes.get(child)
        if child_node is not None and child_node.external:
            continue
        if child in ctx.graph.external:
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
    label = format_node_label(
        node,
        unicode=ctx.unicode,
        in_cycle=name in ctx.cycle_names,
    )

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
        child_label = format_node_label(
            child_node,
            unicode=ctx.unicode,
            in_cycle=child in ctx.cycle_names,
        )
        if reused:
            marker = " ⋯" if ctx.unicode else " ..."
            child_label = f"{child_label}{marker}"
        lines.append(f"{prefix}{branch}{child_label}")

        if reused:
            if not is_last and prefix == "" and not ctx.compact:
                lines.append(f"{prefix}{spacer}")
            continue

        ctx.visited.add(child)
        if _visible_children(ctx, child):
            _append_children_only(
                lines,
                ctx,
                child,
                prefix=f"{prefix}{blank if is_last else pipe}",
            )

        # Breathing room between top-level branches under a root.
        # Compact mode (50+ services) skips spacers to reduce clutter.
        if not is_last and prefix == "" and not ctx.compact:
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
        child_label = format_node_label(
            child_node,
            unicode=ctx.unicode,
            in_cycle=child in ctx.cycle_names,
        )
        reused = child in ctx.visited
        if reused:
            marker = " ⋯" if ctx.unicode else " ..."
            child_label = f"{child_label}{marker}"
        lines.append(f"{prefix}{branch}{child_label}")
        if reused:
            if not is_last and prefix == "" and not ctx.compact:
                lines.append(f"{prefix}{spacer}")
            continue
        ctx.visited.add(child)
        if _visible_children(ctx, child):
            _append_children_only(
                lines,
                ctx,
                child,
                prefix=f"{prefix}{blank if is_last else pipe}",
            )
        if not is_last and prefix == "" and not ctx.compact:
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
            child_node = nodes.get(child)
            if child_node is not None and child_node.external:
                continue
            if child in graph.external:
                continue
            _add_rich_child(tree, graph, nodes, child, visited, Tree=Tree, Text=Text)
        console.print(tree)

    text = buffer.getvalue().rstrip("\n")
    if not text:
        return None

    # If wrapping still dropped an application name, prefer the Unicode walk.
    for name in graph.specs:
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
    if node.external or name in graph.external:
        return
    label = format_node_label(node, unicode=True)
    if name in visited:
        parent.add(Text(f"{label} ..."))
        return

    branch = parent.add(Text(label))
    visited.add(name)
    for child in graph.edges.get(name, ()):
        child_node = nodes.get(child)
        if child_node is not None and child_node.external:
            continue
        if child in graph.external:
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

    Explicit command tokens win first (Stackfile is authoritative for how the
    process is launched). Ambiguous Node launchers (``npm run start:dev``,
    ``node main.js``, …) must not be hard-coded as Express — NestJS commonly
    ships that shape. Adapter match on ``spec.path`` resolves those cases.
    Results are cached per resolved path because adapter detection may touch
    the filesystem.
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
    # Explicit ``express`` only — bare ``npm`` / ``node`` is ambiguous.
    if "express" in command:
        return "Express"

    # Ambiguous Node launchers (``npm run start:dev``, ``node main.js``, …):
    # prefer NestJS/Express from the adapter registry so Nest is not mislabeled
    # Express. Ignore non-Node adapter hits (e.g. monorepo root matching Celery).
    if any(
        token in command
        for token in ("npm", "npx", "node", "pnpm", "yarn", "bun")
    ):
        label = _adapter_framework_label(spec.path)
        if label in {"NestJS", "Express"}:
            return label
        return "Express"

    label = _adapter_framework_label(spec.path)
    if label:
        return label
    return ""


def _language_for_framework(framework: str, command: str = "") -> str:
    key = (framework or "").strip().lower()
    if key in {"fastapi", "flask", "django", "celery", "uvicorn", "gunicorn", "python"}:
        return "Python"
    if key == "nestjs":
        return "TypeScript"
    if key in {"express", "node"}:
        return "JavaScript"
    try:
        from .status import detect_language

        return detect_language(command, framework=framework)
    except Exception:
        return ""


def _adapter_framework_label(service_path: str | Path) -> str:
    """Return the matched application adapter name for ``service_path``, if any."""

    try:
        path = Path(service_path).expanduser().resolve()
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
