# Changelog

All notable changes to StackPilot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Configurable external-dependency retries (`retries`, `retry_delay`,
  `retry_backoff=fixed|exponential`) with progress lines
  (`Checking…` / `Attempt n/m…` / `Connected.`).
- Richer dependency-unavailable messages (host, port, elapsed time, attempts,
  dependents, suggested fix).
- Consistent CLI error blocks: **Problem / Affected service / Reason /
  Suggested fix** for spawn failures, health timeouts, bad paths, and
  configuration errors.
- Integration and packaging regression tests (wheel install, console script,
  `python -m stackpilot`, README/example smoke checks).
- CI matrix expanded to Python 3.10–3.13 on Ubuntu, Windows, and macOS;
  package job runs pytest artifacts through `twine check`, wheel install,
  and CLI verification (console script, doctor, and `python -m stackpilot`).
- `examples/minimal/` runnable single-service sample.
- `FAQ.md` index linking to the README FAQ / troubleshooting sections.
- README Known Limitations section; expanded Troubleshooting / FAQ for
  Ctrl+C, health checks, external deps, hot reload, and the issues workflow.

### Changed

- Default external probe window: interval `0.5s`, timeout `10s`, `5` retries
  (still overridable per dependency / health_check).
- Display labels for MongoDB / RabbitMQ external dependency types.

### Fixed

- Spawn errors (including invalid commands) are converted to friendly CLI
  messages inside Runner / Orchestrator instead of raw tracebacks.

## [0.1.0] - 2026-07-31

### Added

- Typed health-check models: `ProcessHealthCheck`, `HttpHealthCheck`, `TcpHealthCheck`
  (legacy dict configs still accepted).
- Public API exports: `Stack`, `ServiceSpec`, `HealthCheck`, `Runner`, and health models.
- `Stack.run()` starts the stack via `Runner` when `Stackfile.py` is executed as
  `__main__` (`python Stackfile.py`); CLI import paths remain non-starting.
- Recursive process-tree cleanup on stop (Unix process groups; Windows
  Job Objects + `taskkill /T` fallback).
- Packaging: `LICENSE`, `CHANGELOG.md`, `CONTRIBUTING.md`, GitHub Actions pytest workflow.
- After a successful `stackpilot run` startup, print each service's browser URL
  (from HTTP health checks / `port=`) under a **Services ready** block.
- Django `manage.py runserver` on Windows is taken over by StackPilot reload
  (`--noreload` + file watcher), so file changes restart the full process and
  URLs stay available after failed Django auto-reloads.
- `examples/` — minimal FastAPI, Flask, Django, Celery, Express, NestJS, and
  external-deps (Postgres + Redis) projects with generated `Stackfile.py` files.
- Checked-in dependency QA fixture under `tests/fixtures/stackpilot-test/`.
- `SECURITY.md` with supported versions, private reporting guidance, and threat model.
- **Issue Tracker** under `.stackpilot/issues/` — one compact
  `<service>.issue` table per service (TIME / STATUS / ERROR / FILE:LINE).
  Tracebacks are reduced to message + location. ACTIVE → FIXED → 1h row
  removal; empty files are deleted. An empty `issues/` dir means healthy.
- CLI command `stackpilot issues` (optional service filter, `--fixed`).
- CLI command `stackpilot ps` (active processes).
- On-disk runtime snapshot (`.stackpilot/runtime.json`) for cross-terminal
  `status` / `ps`.
- Optional `port=` on `stack.service(...)` for DX display when health checks
  do not expose a port.
- Automatic port detection for `status` / `ps`: live listen sockets,
  health-check host/URL, explicit `port=`, or common CLI patterns (`--port`,
  `-p`, `host:port`, `PORT=`).
- Runtime status module (`status.py`) tracking PID, uptime, start time, port, and state.
- ManagedService DX fields: `pid`, `uptime`, `started_at`, `status`, `port`.
- Structured crash reports pointing at the Issue Tracker directory.
- Shutdown summary with per-service stops and total shutdown time.
- Console log lines include timestamp, service name, and detectable log level (with color).
- `stackpilot doctor` developer diagnostics package (`stackpilot.diagnostics`) with
  Stackfile, Python, service path/command, port, dependency-graph, and health-check
  validation; colored ✓ / ✗ / ! report grouped into Environment / Dependencies /
  Ports / Health Checks / Configuration sections plus Checks Passed / Warnings /
  Errors summary.
- PEP 561 typing marker (`py.typed`) shipped with the package.
- External dependency validation retries with timeout before failing startup.
- Friendly CLI diagnosis for spawn failures (missing executable, permission denied,
  missing directory, invalid command).
- Runtime rejection of non-`http`/`https` health URL schemes and watch paths outside
  the project root.

### Changed

- Configuration file is `Stackfile.py` only (never `stackpilot.py`) to avoid
  import shadowing; discovery walks parents like Git.
- Command splitting is platform-aware: Win32 `CommandLineToArgvW` on Windows,
  POSIX `shlex` elsewhere (no `shlex.split(..., posix=True)` on Windows).
- Process spawn uses `CREATE_NEW_PROCESS_GROUP` on Windows and
  `start_new_session=True` on Linux/macOS so Ctrl+C can shut down the tree cleanly.
- Improved `pyproject.toml` metadata for PyPI readiness.
- CLI help text, examples, and missing-Stackfile guidance polished for v0.1.
- `stackpilot sync` prints a concise "Next: stackpilot run" hint.
- PyPI metadata: richer description/keywords, Repository + Bug Tracker URLs,
  `build` optional for packaging validation.
- CLI UX: one command = one responsibility. `stackpilot run` only starts services
  and streams logs; runtime tables live under `status` / `ps`.
- Persistent log files (`.stackpilot/logs/`) replaced by the Issue Tracker
  (`.stackpilot/issues/`). Console streaming is unchanged; only persistence
  changed. `stackpilot logs` is replaced by `stackpilot issues`.
- Runtime artifacts (`.stackpilot/issues/`, `runtime.json`) are always rooted
  under the discovered project root, including when commands are run from a
  subdirectory.
- Shutdown sequence: disable reload → stop processes → stop watchers → unbind →
  logger shutdown (prevents Ctrl+C vs reload races).

### Fixed

- Orphan child/grandchild processes after shutdown.
- Windows command parsing for quoted arguments.
- JSON structured logs that include `"level": "INFO"` (common in Django) are no
  longer labeled ``ERROR`` just because they arrived on stderr.
- Issue Tracker no longer records Python `UserWarning` / warning stack snippets
  as ACTIVE rows, and reactivates a FIXED fingerprint instead of stacking
  FIXED + ACTIVE copies of the same error on every restart.
- On Windows, uvicorn ``--reload`` no longer shuts down the whole stack. StackPilot
  strips that flag and reloads only the changed service (uvicorn's reload sends
  ``CTRL_C_EVENT``, which interrupted the parent console).
- Issue Tracker files created under `Path.cwd()` while `runtime.json` used the
  Stackfile project root when running from a subdirectory.
- Watcher `_forget_tree` no longer assumes Windows path separators (POSIX
  directory-delete signature cleanup).
- `stackpilot sync` discovers the nearest `Stackfile.py` like other commands
  (falls back to cwd only when none exists).
- Single canonical package version via `stackpilot.__version__` + setuptools
  dynamic version; `click` declared as a direct dependency.
- Child-process stop path always force-kills the process tree after Job Object
  terminate (Windows) / process-group signal (POSIX).

### Removed

- `.stackpilot/logs/` service log file persistence and `stackpilot logs`.
- Startup dashboard helpers formerly unused after the run UX split
  (`format_startup_dashboard` and related helpers).
- Public CLI commands `stackpilot stop` and `stackpilot restart`. v0.1.0 is a
  foreground tool: start with `stackpilot run`, stop with Ctrl+C; hot reload
  remains automatic via WatchManager. The out-of-band `runtime_control` module
  was removed with those commands (also eliminated the duplicate
  `UnknownServiceError` definition).
