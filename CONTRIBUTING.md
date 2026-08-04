# Contributing to StackPilot

Thanks for helping improve StackPilot. This document covers local setup, tests,
and the expectations for release-quality changes.

## Development setup

```bash
python -m pip install -e ".[dev]"
```

If the optional `dev` extra is unavailable in your checkout:

```bash
python -m pip install -e .
python -m pip install pytest
```

## Running tests

```bash
pytest
```

A fresh clone needs no extra setup beyond the editable install above. Dependency
QA uses the checked-in fixture at `tests/fixtures/stackpilot-test/` — do not
create a separate local `stackpilot-test` project.

CI runs the same suite on Ubuntu, macOS, and Windows across Python
3.10–3.13 (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)). The
package job builds wheel + sdist, runs `twine check`, installs the wheel into
a clean environment, and verifies `stackpilot` / `python -m stackpilot`.

## Project conventions

- **Config file:** projects use `Stackfile.py` — never `stackpilot.py` (that name
  would shadow the installed package).
- **CLI (frozen until v0.2.0):** do not add, rename, or remove public commands
  (`init`, `sync`, `run`, `stop`, `graph`, `status`, `ps`, `issues`, `doctor`,
  `version`). Discovery should not require a `-c` / `--config` flag.
- **Architecture:** keep the CLI → discovery → Orchestrator → Runner →
  ProcessManager layering. Runtime artifacts always live under the discovered
  project root.
- **Behavior:** prefer backwards-compatible API changes; dict health checks and
  existing Stackfile shapes should keep working.
- **Errors:** expected user mistakes must print Problem / Reason / Suggested fix
  without a traceback.
- **Scope:** refactor only what the change needs; avoid drive-by rewrites.

## Examples

Minimal framework samples live under `examples/` (FastAPI, Flask, Django,
Celery, Express, NestJS, `minimal`, plus `external-deps` for Postgres/Redis).
Prefer updating those when changing adapter detection or generated Stackfile
shapes. Every documented example command should run without modification.

## FAQ / docs

End-user FAQ, troubleshooting, Ctrl+C behaviour, health checks, external
dependencies, hot reload, Issue Tracker, and known limitations live in
[README.md](README.md). Keep SECURITY.md / this file / CHANGELOG.md aligned
with those command names.

## Pull requests

1. Add or update tests for the behavior you change.
2. Keep the public API exported from `stackpilot.__init__` in sync when adding
   user-facing types.
3. Update `CHANGELOG.md` under an Unreleased or version section when relevant.
4. Ensure `pytest` passes locally before opening a PR.

## Reporting issues

Include OS, Python version, the relevant `Stackfile.py` snippet, and the command
you ran (`stackpilot run`, etc.).

For security vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of
opening a public issue.
