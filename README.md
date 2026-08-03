# StackPilot

[![CI](https://github.com/stackpilot-dev/stackpilot/actions/workflows/ci.yml/badge.svg)](https://github.com/stackpilot-dev/stackpilot/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/stackpilot.svg)](https://pypi.org/project/stackpilot/)
[![Python versions](https://img.shields.io/pypi/pyversions/stackpilot.svg)](https://pypi.org/project/stackpilot/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Typed](https://img.shields.io/badge/typing-py.typed-blue.svg)](https://mypy.readthedocs.io/en/stable/installed_packages.html)

**Local microservice orchestration without Docker.**

StackPilot starts your services in dependency order, streams logs in one
terminal, hot-reloads on file changes, and tracks actionable issues — all from
a single `Stackfile.py`.

```bash
pip install stackpilot
cd my-project
stackpilot sync
stackpilot run
```

---

## Features

- **One config file** — declare services and external dependencies in `Stackfile.py`
- **Auto-discovery** — `stackpilot sync` detects FastAPI, Flask, Django, Celery, Express, NestJS, Postgres, Redis, and more
- **Apps vs infrastructure** — only application services are started; Postgres/Redis are validated as external dependencies
- **Dependency-aware startup** — services start in topological order
- **Health checks** — HTTP, TCP, or process liveness before dependents launch
- **Hot reload** — file changes restart only the affected service
- **Issue tracker** — actionable crashes under `.stackpilot/issues/`, not endless log files
- **Doctor** — environment and config diagnostics in one command
- **No Docker required** for app processes (compose still works for databases)

---

## Installation

Requires **Python 3.10+**.

```bash
pip install stackpilot
```

Verify:

```bash
stackpilot version
# or, if Scripts is not on PATH:
python -m stackpilot version
```

### From source (contributors)

```bash
git clone https://github.com/stackpilot-dev/stackpilot.git
cd stackpilot
python -m pip install -e ".[dev]"
pytest
```

---

## Quick Start

```bash
stackpilot init          # create Stackfile.py
# edit services, or:
stackpilot sync          # auto-discover nested services
stackpilot run           # start + stream logs (Ctrl+C to stop)
```

You can also execute the Stackfile directly:

```bash
python Stackfile.py
```

Commands walk up from the current directory until they find `Stackfile.py`
(like Git finding `.git`). Runtime artifacts always land under that project
root in `.stackpilot/`.

---

## Commands

The public CLI for **v0.1.x** is frozen until **v0.2.0**.

Nine commands. One job each.

| Command | Definition |
|---------|------------|
| [`init`](#stackpilot-init) | Create a starter `Stackfile.py` |
| [`sync`](#stackpilot-sync) | Discover nested services and write `Stackfile.py` |
| [`run`](#stackpilot-run) | Start services in dependency order and stream live logs |
| [`graph`](#stackpilot-graph) | Print a professional architecture dependency graph |
| [`status`](#stackpilot-status) | Show runtime status (PID, port, uptime, health) |
| [`ps`](#stackpilot-ps) | List active StackPilot processes |
| [`issues`](#stackpilot-issues) | List actionable service issues from `.stackpilot/issues/` |
| [`doctor`](#stackpilot-doctor) | Diagnose environment, Stackfile, and service configuration |
| [`version`](#stackpilot-version) | Print the installed StackPilot version |

```bash
stackpilot --help
stackpilot <command> --help
```

---

### `stackpilot init`

**Definition:** Create a starter `Stackfile.py` in the current directory.

Use this when you are starting a new project and do not have a Stackfile yet.
It writes an empty template you can edit by hand, or overwrite later with
`stackpilot sync`.

```bash
stackpilot init
stackpilot init --force    # overwrite an existing Stackfile.py
```

| Option | Definition |
|--------|------------|
| `--force` | Overwrite an existing `Stackfile.py` without prompting |

---

### `stackpilot sync`

**Definition:** Walk nested directories, detect frameworks and infrastructure,
and generate (or overwrite) `Stackfile.py`.

Use this to bootstrap or refresh configuration from the project layout.
Application services become `stack.service(...)`. Postgres/Redis become
`stack.external_dependency(...)` and are never given a start command.

```bash
stackpilot sync
stackpilot sync --force    # overwrite Stackfile.py without prompting
```

| Option | Definition |
|--------|------------|
| `--force` | Overwrite an existing `Stackfile.py` without prompting |

Soft validation warnings (for example FastAPI without uvicorn) never abort sync.

---

### `stackpilot run`

**Definition:** Validate external dependencies, start application services in
dependency order, stream live logs, and hot-reload changed services until you
press Ctrl+C.

This is the main foreground command. There is no separate `stop` or `restart`
command in v0.1.x — stop with Ctrl+C; restart on file change is automatic while
`run` is active.

```bash
stackpilot run             # start the full stack
stackpilot run auth        # start auth and its dependencies only
```

| Argument | Definition |
|----------|------------|
| `SERVICE` (optional) | Service name to start, plus everything it depends on |

**What it does:**

1. Checks required external dependencies (Postgres/Redis) with retry + timeout
2. Starts apps in topological order and waits for health checks
3. Streams `INFO` / `WARNING` / `ERROR` lines to the terminal
4. Watches files and reloads affected services when enabled
5. On Ctrl+C: disable reload → stop process trees → stop watchers → exit `130`

Use `status` / `ps` for runtime tables and `issues` for persisted problems —
`run` itself only starts services and streams logs.

---

### `stackpilot graph`

**Definition:** Print a professional architecture visualization of application
services and external dependencies from the Stackfile.

Use this to understand startup order and `depends_on` relationships without
starting anything. Output includes live status colors when a session is active,
ports, detected frameworks, and dependency depth.

```bash
stackpilot graph
```

Example output:

```text
StackPilot Architecture
────────────────────────────────────────────

Services : 3
Running  : 0
Stopped  : 2
FastAPI  : 2
Django   : 0
Flask    : 0
Node     : 0
External : 1

🔴 gateway (:8000) [FastAPI]
└── 🔴 auth (:8001) [FastAPI]
    └── 🔵 PostgreSQL (:5432)

────────────────────────────────────────────

Total Services         : 2
Dependency Depth       : 2
Circular Dependencies  : None

Graph Generated Successfully
```

External nodes use infrastructure display names (for example `PostgreSQL`) and
are never started by StackPilot. Circular dependencies abort with a clear cycle
path.

---

### `stackpilot status`

**Definition:** Show a runtime status report for the project: applications and
external dependencies, including PID, port, uptime, and health when a session
is active.

Reads `.stackpilot/runtime.json` written by a live `stackpilot run` session
(works from another terminal). Safe to run when nothing is running — services
simply show as stopped / inactive.

```bash
stackpilot status
```

**Shows:**

- Project name and whether a run session is active
- **Applications** — name, status, PID, port, uptime, health
- **External Dependencies** — reachability-oriented rows for Postgres/Redis, etc.

---

### `stackpilot ps`

**Definition:** List active StackPilot processes in a concise table.

A shorter companion to `status`, focused on what is currently running
(name, PID, port, state).

```bash
stackpilot ps
```

---

### `stackpilot issues`

**Definition:** List actionable service problems stored under
`.stackpilot/issues/` (not full log files).

Default view is **ACTIVE** issues only. Use `--fixed` for recently fixed rows.
Pass a service name to filter to that service.

```bash
stackpilot issues              # ACTIVE issues for all services
stackpilot issues --fixed      # recently FIXED issues
stackpilot issues auth         # every issue for service "auth"
stackpilot issues auth --fixed
```

| Argument / option | Definition |
|-------------------|------------|
| `SERVICE` (optional) | Limit output to one known service name |
| `--fixed` | Show recently fixed issues instead of ACTIVE ones |

An empty `issues/` directory means the project is healthy.

---

### `stackpilot doctor`

**Definition:** Diagnose the environment, Stackfile, dependency graph, ports,
health checks, and external dependencies in one report.

Use this when sync/run fails, imports break, ports conflict, or Postgres/Redis
look misconfigured. Exit code is non-zero when errors are present.

```bash
stackpilot doctor
```

**Checks include:** Python / package import, Stackfile load, service paths and
commands, duplicate/free ports, dependency cycles, health-check config, and
external dependency reachability.

---

### `stackpilot version`

**Definition:** Print the installed StackPilot package version and exit.

```bash
stackpilot version
python -m stackpilot version
```

---

### Command cheat sheet

```bash
stackpilot init
stackpilot sync
stackpilot run
stackpilot run auth
stackpilot graph
stackpilot status
stackpilot ps
stackpilot issues
stackpilot issues --fixed
stackpilot issues auth
stackpilot doctor
stackpilot version
```

If no Stackfile is found:

```text
No Stackfile.py found.

Create one:

  stackpilot init
  stackpilot sync

Then start services:

  stackpilot run
```

---

## First Project

### 1. Layout

```text
my-project/
  auth/
    main.py          # FastAPI app
  gateway/
    main.py
```

### 2. Discover

```bash
cd my-project
stackpilot sync
```

### 3. Generated `Stackfile.py`

```python
from stackpilot import Stack, HttpHealthCheck

stack = Stack()

stack.external_dependency(
    name="postgres",
    type="postgresql",
    host="127.0.0.1",
    port=5432,
)

stack.service(
    name="auth",
    path="./auth",
    command="python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000",
    port=8000,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8000/health"),
    depends_on=["postgres"],  # external deps are validated, never started
)

stack.service(
    name="gateway",
    path="./gateway",
    command="python -m uvicorn main:app --reload --host 0.0.0.0 --port 8001",
    port=8001,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8001/health"),
    depends_on=["auth"],   # add application dependencies by hand when needed
)

stack.run()
```

StackPilot starts **application services** only. PostgreSQL, Redis, and other
infrastructure are declared as `external_dependency` entries: they appear in
`graph` / `status` / `doctor`, are TCP-validated before startup, and are
never spawned by Runner.

### 4. Run

```bash
stackpilot run
```

In another terminal:

```bash
stackpilot status
stackpilot ps
stackpilot issues
```

Ready-made samples live under [`examples/`](examples/).
Also see [`examples/external-deps/`](examples/external-deps/) for a Postgres + Redis layout.

---

## External Dependencies

StackPilot distinguishes **application services** from **external dependencies**.

| | Application Service | External Dependency |
|--|---------------------|---------------------|
| Declared with | `stack.service(...)` | `stack.external_dependency(...)` |
| Has a process command | Yes | No |
| Started by Runner / ProcessManager | Yes | **Never** |
| Validated before `run` | Health check after start | TCP reachability with **retry + timeout** before any app starts |
| Shown in | Applications (status), graph | External Dependencies (status), graph as infrastructure labels |

### Supported infrastructure

| Type | Default port | Validation |
|------|--------------|------------|
| PostgreSQL (`postgresql` / `postgres`) | `5432` | TCP connect via the Health Engine |
| Redis (`redis`) | `6379` | TCP connect via the Health Engine |

`stackpilot sync` detects Postgres/Redis directories and emits
`stack.external_dependency(...)` — never an executable `stack.service(...)`.

### How validation works

Before application startup, StackPilot probes each required external dependency
through the Health Engine (TCP preferred on the declared `host`/`port`).

Defaults (overridable per dependency):

| Setting | Default | Meaning |
|---------|---------|---------|
| `retries` | `5` | Maximum probe attempts |
| `retry_delay` | `0.5s` | Delay between attempts |
| `retry_backoff` | `fixed` | `fixed` or `exponential` |
| health-check `timeout` | `10s` | Hard deadline for the whole wait |

```python
stack.external_dependency(
    name="postgres",
    type="postgresql",
    host="127.0.0.1",
    port=5432,
    retries=5,
    retry_delay=0.5,
    retry_backoff="exponential",  # or "fixed"
)
```

**All reachable:**

```text
Checking external dependencies...

Checking PostgreSQL...
Attempt 1/5...
Connected.
✓ PostgreSQL (127.0.0.1:5432)

Checking Redis...
Attempt 1/5...
Connected.
✓ Redis (127.0.0.1:6379)

Starting application services...
```

**One unavailable (retries exhausted):**

```text
Checking external dependencies...

Checking PostgreSQL...
Attempt 1/5...
Attempt 2/5...
Attempt 3/5...
Attempt 4/5...
Attempt 5/5...
✗ PostgreSQL is not reachable.

Problem: Dependency unavailable
Dependency: PostgreSQL
Host: 127.0.0.1
Port: 5432
Elapsed: 10.0s
Attempts: 5/5

Services depending on PostgreSQL:
- auth
- users

Suggested fix: Start PostgreSQL (or update host/port in Stackfile.py), then re-run `stackpilot run`. Verify with `stackpilot doctor`.

Startup aborted.
```

No application processes are started when validation fails.

---

## Framework Support

`stackpilot sync` walks nested directories and asks the **adapter registry**
which framework matches. Soft validation warnings (for example FastAPI without
uvicorn) never abort sync.

| Framework | Detection signals | Generated command | Health |
|-----------|-------------------|-------------------|--------|
| NestJS | `package.json` + `@nestjs/core` | `<pm> run start:dev` | HTTP `/` |
| Express | `package.json` + `express` | `<pm> run dev` | HTTP `/` |
| Django | `manage.py` + settings / WSGI / ASGI | `python manage.py runserver` | HTTP `/` |
| Celery | `Celery()` / worker modules | `celery -A <app> worker` | PROCESS |
| FastAPI | `FastAPI()` / common layouts | `python -m uvicorn <module>:<attr> --reload` | HTTP `/health` |
| Flask | `Flask()` / `create_app()` | `python app.py` or `flask --app … run` | HTTP `/` |
| PostgreSQL | compose / `postgresql.conf` | `external_dependency` (TCP `5432`) — never started | TCP |
| Redis | `redis.conf` / compose | `external_dependency` (TCP `6379`) — never started | TCP |
| Generic | `main.py` / `app.py` / bare `package.json` | `python …` or `<pm> start` | PROCESS |

See [`examples/`](examples/) for minimal projects per framework.

### Package managers

**Python:** uv → Poetry → Pipenv → pip (with local `.venv` when present)

**Node:** bun → pnpm → yarn → npm (lockfile wins)

### Port detection

1. `.env` / `.env.*` keys (`PORT`, `APP_PORT`, …)
2. Compose host port mappings
3. Sequential defaults from `8000` (Postgres `5432`, Redis `6379`)

### Custom adapters

Add one adapter file and register it in the adapter registry — nothing else.
Third-party code can also build a private `AdapterRegistry`.

---

## Health Checks

Prefer typed models exported from `stackpilot`:

```python
from stackpilot import ProcessHealthCheck, HttpHealthCheck, TcpHealthCheck

HttpHealthCheck(url="http://127.0.0.1:8000/health")
TcpHealthCheck(host="127.0.0.1", port=5432)
ProcessHealthCheck()  # process must stay alive
```

Legacy dict configs (`{"type": "http", "url": "..."}`) remain supported.

Dependents wait until a service passes its health check (or the process stays
alive for process health). Failures surface in the Issue Tracker.

External dependencies default to a TCP probe on their declared `host`/`port`
and are validated **before** any application service starts.

---

## Issue Tracking

StackPilot does **not** persist normal service logs. Live `INFO` / `WARNING` /
`ERROR` lines still stream in the `stackpilot run` terminal — only actionable
problems are written under `.stackpilot/issues/`.

Each service owns one compact table file (for example `auth.issue`). Python
tracebacks are reduced to error message + project-relative `path:line`.
Duplicate ACTIVE rows are ignored.

```text
ACTIVE
  ↓  (issue resolved / service healthy)
FIXED
  ↓  (1 hour)
Row removed
  ↓  (no rows left)
Delete <service>.issue
```

An empty `issues/` folder means the project is healthy.

```bash
stackpilot issues           # ACTIVE
stackpilot issues --fixed   # recently fixed
stackpilot issues auth      # one service
```

---

## FAQ

**Do I need Docker?**  
No. StackPilot only starts application processes. Postgres/Redis are external
dependencies — start them yourself (Docker, local install, managed DB, …);
StackPilot only checks that they are reachable before launching apps.

**Where is the config file?**  
Always `Stackfile.py` — never `stackpilot.py` (that name would shadow the
package).

**Is Stackfile.py trusted code?**  
Yes. Loading a Stackfile can execute arbitrary Python, and `command=` values
are spawned as your user. Review Stackfiles like you would a Makefile. See
[SECURITY.md](SECURITY.md).

**Can I run from a subdirectory?**  
Yes. Discovery walks parents like Git. Artifacts stay under the project root.

**How do I stop everything?**  
Press Ctrl+C in the `stackpilot run` terminal. StackPilot disables hot reload,
stops process trees (dependents first), stops file watchers, clears runtime
bindings, and closes the logger. Exit code is `130`. There is no separate
`stop` / `restart` CLI command in v0.1.x.

**What happens during shutdown?**  
`disable reload → stop processes → stop watchers → unbind → logger shutdown`.
A shutdown summary lists each stopped service. Orphan children should not remain.

**What about restart / hot reload?**  
While `run` is active, file watchers restart changed services (when
`reload=True`, or when Windows takes over uvicorn/Django native reload).
Debounced callbacks are ignored once Ctrl+C begins shutdown. Limitations:

- Generator defaults do **not** set `reload=True`; FastAPI often relies on
  uvicorn `--reload` instead.
- On Windows, StackPilot strips uvicorn `--reload` / Django auto-reload and
  owns restart so `CTRL_C_EVENT` cannot tear down the whole stack.
- Reload restarts only the changed service (plus `restart_dependents` when set).

**Where did `stackpilot logs` go?**  
Live logs still stream in the `run` terminal. Persistence is the Issue Tracker
(`.stackpilot/issues/`) via `stackpilot issues` — not `.stackpilot/logs/`.

**What if a service fails to spawn?**  
Missing executables, missing directories, permission errors, invalid commands,
and port conflicts print a short **Problem / Affected service / Reason /
Suggested fix** block without a raw traceback. Run `stackpilot doctor` for
deeper checks.

**What if Postgres/Redis is down?**  
External dependency validation retries (default 5 attempts with delay/backoff)
until the configured timeout, then aborts **before** starting application
processes. The message lists host, port, elapsed time, attempts, dependents,
and the next action.

**How do I check health without reading logs?**  
`stackpilot status`, `stackpilot doctor`, and `stackpilot issues`.

---

## Known Limitations

- StackPilot starts **application processes only**. Databases and brokers must
  already be running (or reachable) as external dependencies.
- Generator defaults do **not** set `reload=True`; many frameworks rely on their
  own reload flags (`uvicorn --reload`, etc.).
- On Windows, StackPilot may strip native uvicorn/Django reload and own restarts
  so `CTRL_C_EVENT` cannot tear down the whole stack.
- Hot reload restarts the changed service (plus `restart_dependents` when set),
  not the entire graph.
- There is no separate `stop` / `restart` CLI command in v0.1.x — use Ctrl+C
  and file watchers while `run` is active.
- `Stackfile.py` is trusted code (see [SECURITY.md](SECURITY.md)).

---

## Troubleshooting

| Symptom | What to try |
|---------|-------------|
| `No Stackfile.py found` | `stackpilot init` or `stackpilot sync` from the project root |
| Sync finds nothing | Put each service in a **nested** directory (the project root itself is never a service) |
| Port already in use | Change `port=` / health URL, or free the port |
| Executable not found | Activate the project venv or fix `command=` — then `stackpilot doctor` |
| Permission denied | Check execute bits / antivirus locks on the service path |
| Invalid cwd / bad path | Fix `path=` so it exists under the project root |
| Dependency unavailable | Start Postgres/Redis (or fix host/port); wait for boot; re-run |
| Health endpoint missing | Confirm the route exists and matches `health_check=` |
| Health timeout | Check the run terminal, `stackpilot issues <name>`, then doctor |
| Service fails on start | `stackpilot issues <name>` and open the referenced `FILE:LINE` |
| Import / CLI missing | `pip install stackpilot` then `python -m stackpilot doctor` |
| Wrong Python / venv | Activate the project venv, or use uv/Poetry/Pipenv so sync emits the right runner |
| Stackfile load / config error | `stackpilot doctor` — check syntax and that `stack = Stack()` exists |
| Ctrl+C leaves orphans | Upgrade to latest 0.1.x; report if process trees remain after shutdown summary |

```bash
stackpilot doctor
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests, and the frozen CLI /
architecture rules for v0.1.x.

```bash
python -m pip install -e ".[dev]"
pytest
```

---

## Security

See [SECURITY.md](SECURITY.md) for the trusted-Stackfile threat model, supported
versions, and how to report vulnerabilities.

---

## License

[MIT](LICENSE) © StackPilot contributors
