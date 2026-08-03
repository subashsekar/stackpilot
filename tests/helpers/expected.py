"""Expected outputs and constants for StackPilot dependency QA."""

from __future__ import annotations

# Full-stack topological startup order for tests/fixtures/stackpilot-test.
FULL_STARTUP_ORDER = [
    "postgres",
    "redis",
    "users",
    "email",
    "auth",
    "payments",
    "analytics",
    "notifications",
    "gateway",
]

# Selective startup: stackpilot run auth
AUTH_SUBGRAPH_ORDER = [
    "postgres",
    "redis",
    "users",
    "email",
    "auth",
]

AUTH_SUBGRAPH_EXCLUDED = [
    "payments",
    "analytics",
    "notifications",
    "gateway",
]

# Selective startup: stackpilot run gateway (entire required closure)
GATEWAY_SUBGRAPH_ORDER = list(FULL_STARTUP_ORDER)

# Cycle introduced by users -> gateway
EXPECTED_CYCLE_PATH = (
    "gateway\n"
    "↓\n"
    "\n"
    "auth\n"
    "↓\n"
    "\n"
    "users\n"
    "↓\n"
    "\n"
    "gateway"
)

# Fragments that must appear in `stackpilot graph` output for the test project.
EXPECTED_GRAPH_FRAGMENTS = [
    "gateway",
    "auth",
    "users",
    "postgres",
    "redis",
    "email",
    "payments",
    "analytics",
    "notifications",
]

# Partial-order constraints that must hold for any valid topo sort.
FULL_ORDER_CONSTRAINTS = [
    ("postgres", "users"),
    ("postgres", "payments"),
    ("postgres", "analytics"),
    ("redis", "auth"),
    ("redis", "payments"),
    ("redis", "notifications"),
    ("users", "auth"),
    ("email", "auth"),
    ("email", "notifications"),
    ("auth", "gateway"),
    ("payments", "gateway"),
    ("analytics", "gateway"),
    ("notifications", "gateway"),
]
