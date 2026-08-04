# Security Policy

## Supported versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | Yes                |
| < 0.1   | No                 |

## Reporting a vulnerability

If you discover a security issue in StackPilot, please report it privately.

1. Prefer a [GitHub security advisory](https://github.com/stackpilot-dev/stackpilot/security/advisories/new) on the StackPilot repository.
2. Include OS, Python version, the relevant `Stackfile.py` snippet, and steps to reproduce.
3. Do **not** open a public issue for unfixed vulnerabilities.

We will acknowledge reports as quickly as practical and coordinate a fix and disclosure timeline for confirmed issues.

## Threat model

StackPilot is a **local development** process orchestrator. It is not a multi-tenant
sandbox and does not isolate untrusted code.

### Trusted Stackfile model

`Stackfile.py` is **trusted project configuration**, equivalent to a shell script
you chose to run:

- StackPilot loads it with Python's import machinery (`exec_module`).
- Declarations such as `command=` are started as subprocesses with `shell=False`
  (argv lists only — never a shell).
- Anyone who can edit the Stackfile (or a path it references) can run arbitrary
  commands as your user when you invoke `stackpilot run` / `python Stackfile.py`.

Treat Stackfiles the same way you treat `Makefile`, `package.json` scripts, or
Docker Compose files: review them before running, and do not execute Stackfiles
from untrusted sources.

### What StackPilot mitigates

| Control | Behaviour |
|---------|-----------|
| No shell interpolation | `subprocess` uses `shell=False` everywhere under `src/` |
| HTTP health schemes | Runtime probes accept only `http://` and `https://` |
| Path containment | Service `path=` / `reload_dirs` must resolve under the project root |
| Process tree cleanup | Shutdown kills the full child tree (POSIX groups / Windows Job Objects) |
| External deps never started | Postgres/Redis are TCP-validated only (with retries + timeout) |
| Friendly failure UX | Expected spawn/config mistakes print Problem / Reason / Suggested fix (no traceback) |
| Runtime cleanup | Shutdown verifies process trees; doctor reports orphan PIDs / corrupt runtime.json |

### Out of scope

- Protecting against a malicious Stackfile author
- Sandboxing child processes (seccomp, containers, reduced privileges)
- Remote orchestration / multi-user shared hosts
- Stopping a compromised child from attacking the host

### Operational recommendations

- Run StackPilot only on projects you trust.
- Prefer least-privilege OS accounts for long-lived terminals.
- Keep infrastructure (Postgres/Redis) bound to localhost unless you intentionally
  expose them.
- Report unexpected process launches or path-escape bugs via the private channel above.
