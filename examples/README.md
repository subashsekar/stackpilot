# StackPilot examples

Minimal projects that demonstrate framework detection and a generated
`Stackfile.py`.

HTTP framework examples use **unique preferred ports** (`8001`–`8007`) so they
do not collide when run side by side. Ports come from each service's source
(or `.env` / listen fallback) and are preserved by `stackpilot sync --force`.

| Example | Framework | Service directory | Port |
|---------|-----------|-------------------|------|
| [`minimal/`](minimal/) | Inline Python | `./app` | — |
| [`fastapi/`](fastapi/) | FastAPI | `./api` | `8001` |
| [`flask/`](flask/) | Flask | `./web` | `8002` |
| [`django/`](django/) | Django | `./web` | `8003` |
| [`celery/`](celery/) | Celery | `./worker` | — |
| [`express/`](express/) | Express | `./app` | `8004` |
| [`nestjs/`](nestjs/) | NestJS | `./app` | `8005` |
| [`external-deps/`](external-deps/) | FastAPI + Postgres/Redis | `./auth`, `./gateway` | `8006`, `8007` |

## Try one

```bash
pip install stackpilot
cd examples/minimal
stackpilot run
```

Framework samples (install their `requirements.txt` / `package.json` first):

```bash
cd examples/fastapi
pip install -r api/requirements.txt
stackpilot run
```

Or re-generate the Stackfile (framework examples only):

```bash
stackpilot sync --force
```

### Flask note

`stackpilot sync` emits `flask run --host 0.0.0.0 --port N` so the process
always listens on the assigned Stackfile port — even when `app.py` hardcodes a
different `app.run(port=…)`.

### NestJS note

When the app exposes `/health` (controller route or `@nestjs/terminus`), sync
writes an HTTP health check. Otherwise it falls back to TCP.

### Stopping a leftover session

```bash
stackpilot stop          # kill recorded processes from runtime.json
stackpilot run --force   # clear stale session, then start
```

The `external-deps/` example is hand-written: it shows `external_dependency`
declarations for Postgres and Redis. Start those yourself, then `stackpilot run`.
MongoDB (`27017`) and RabbitMQ (`5672`) are detected the same way when present
under nested compose / config directories.

Each framework example keeps the service in a **nested** directory. StackPilot
never treats the project root itself as a service.
