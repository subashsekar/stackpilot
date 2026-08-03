"""Helpers for StackPilot dependency QA tests."""

from __future__ import annotations

import os
import random
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Sequence, Tuple

from stackpilot.config import ServiceSpec, Stack
from stackpilot.dependency_graph import DependencyGraph, build_graph
from stackpilot.logger import Logger
from stackpilot.process_manager import ProcessManager
from stackpilot.utils import load_stack_from_stackfile

REPO_ROOT = Path(__file__).resolve().parents[2]
STACKPILOT_TEST_ROOT = REPO_ROOT / "tests" / "fixtures" / "stackpilot-test"
STACKFILE = STACKPILOT_TEST_ROOT / "Stackfile.py"


def load_test_stack() -> Stack:
    """Load the checked-in stackpilot-test Stackfile with absolute service paths."""

    if not STACKFILE.is_file():
        raise FileNotFoundError(
            f"Missing test fixture Stackfile: {STACKFILE}. "
            "It should ship with the repository under tests/fixtures/stackpilot-test/."
        )
    return load_stack_from_stackfile(STACKFILE)


def build_test_graph() -> DependencyGraph:
    return build_graph(load_test_stack().services)


def assert_before(order: Sequence[str], earlier: str, later: str) -> None:
    assert earlier in order, f"{earlier!r} missing from order {list(order)}"
    assert later in order, f"{later!r} missing from order {list(order)}"
    assert order.index(earlier) < order.index(later), (
        f"Expected {earlier!r} before {later!r}, got {list(order)}"
    )


def assert_constraints(
    order: Sequence[str],
    constraints: Sequence[Tuple[str, str]],
) -> None:
    for earlier, later in constraints:
        assert_before(order, earlier, later)


def parse_started_services(console_text: str) -> List[str]:
    """Extract service start order from StackPilot console output."""

    pattern = re.compile(r"^\[(?P<name>[^\]]+)\]\s+(?P=name) started\s*$", re.M)
    fallback = re.compile(r"^\[(?P<name>[^\]]+)\].*started\s*$", re.M | re.I)
    names = [m.group("name") for m in pattern.finditer(console_text)]
    if names:
        return names
    return [m.group("name") for m in fallback.finditer(console_text)]


@contextmanager
def short_sleep_env(seconds: float = 0.05) -> Iterator[None]:
    """Make stackpilot-test service scripts exit quickly during automated runs."""

    key = "STACKPILOT_SLEEP"
    previous = os.environ.get(key)
    os.environ[key] = str(seconds)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def start_ordered_once(
    specs: Sequence[ServiceSpec],
    *,
    logs_dir: Path,
) -> List[str]:
    """Start services via ProcessManager; return names started (once each)."""

    logger = Logger(logs_dir, service_names=[s.name for s in specs])
    manager = ProcessManager(logger)
    started: List[str] = []
    try:
        for spec in specs:
            manager.start(spec)
            started.append(spec.name)
        time.sleep(0.15)
    finally:
        manager.stop_all(timeout_s=2.0)
        logger.close()
    return started


def stack_with_cycle() -> Stack:
    """Clone the test stack and add users -> gateway to form a cycle."""

    base = load_test_stack()
    mutated = Stack()
    for spec in base.services:
        deps = list(spec.depends_on)
        if spec.name == "users" and "gateway" not in deps:
            deps.append("gateway")
        mutated.service(
            name=spec.name,
            path=spec.path,
            command=spec.command,
            depends_on=deps,
            health_check=spec.health_check,
        )
    return mutated


def stack_with_missing_dependency() -> Stack:
    """Clone the test stack and make auth depend on unknown rabbitmq."""

    base = load_test_stack()
    mutated = Stack()
    for spec in base.services:
        deps = list(spec.depends_on)
        if spec.name == "auth":
            deps.append("rabbitmq")
        mutated.service(
            name=spec.name,
            path=spec.path,
            command=spec.command,
            depends_on=deps,
            health_check=spec.health_check,
        )
    return mutated


def stack_with_duplicate_redis_dep() -> Stack:
    """Clone the test stack with depends_on=['redis','redis'] on payments."""

    base = load_test_stack()
    mutated = Stack()
    for spec in base.services:
        if spec.name == "payments":
            deps = ["postgres", "redis", "redis"]
        else:
            deps = list(spec.depends_on)
        mutated.service(
            name=spec.name,
            path=spec.path,
            command=spec.command,
            depends_on=deps,
            health_check=spec.health_check,
        )
    return mutated


def stack_with_independent_services() -> Stack:
    """Test stack plus metrics and docs (no dependencies)."""

    base = load_test_stack()
    mutated = Stack()
    for spec in base.services:
        mutated.service(
            name=spec.name,
            path=spec.path,
            command=spec.command,
            depends_on=list(spec.depends_on),
            health_check=spec.health_check,
        )
    mutated.service(
        name="metrics",
        path=STACKPILOT_TEST_ROOT / "redis",
        command="python main.py",
    )
    mutated.service(
        name="docs",
        path=STACKPILOT_TEST_ROOT / "email",
        command="python main.py",
    )
    return mutated


def make_fake_spec(name: str, depends_on: Sequence[str] = ()) -> ServiceSpec:
    return ServiceSpec(
        name=name,
        path=Path("."),
        command="true",
        depends_on=tuple(depends_on),
    )


def generate_random_dag(
    n: int,
    *,
    seed: int = 42,
    max_deps: int = 3,
) -> List[ServiceSpec]:
    """Build ``n`` services with a random acyclic dependency graph."""

    rng = random.Random(seed)
    names = [f"svc_{i:03d}" for i in range(n)]
    specs: List[ServiceSpec] = []
    for i, name in enumerate(names):
        if i == 0:
            deps: Tuple[str, ...] = ()
        else:
            k = rng.randint(0, min(max_deps, i))
            chosen = rng.sample(names[:i], k) if k else []
            deps = tuple(chosen)
        specs.append(make_fake_spec(name, deps))
    return specs


def measure_topo_ms(specs: Sequence[ServiceSpec], *, repeats: int = 5) -> float:
    """Return best-of-N topological sort time in milliseconds."""

    best = float("inf")
    for _ in range(repeats):
        graph = build_graph(specs)
        started = time.perf_counter()
        order = graph.topological_order()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        assert len(order) == len(specs)
        best = min(best, elapsed_ms)
    return best


def count_start_occurrences(names: Sequence[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return counts
