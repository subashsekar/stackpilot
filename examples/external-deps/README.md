# External dependencies example

Demonstrates application services that depend on Postgres and Redis declared
as `external_dependency` entries (validated, never started by StackPilot).

## Layout

```text
external-deps/
  Stackfile.py
  auth/
    main.py
    requirements.txt
  gateway/
    main.py
    requirements.txt
```

## Prerequisites

Start Postgres and Redis locally (or via Docker) before `stackpilot run`:

```bash
# Example with Docker
docker run -d --name ms-pg -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16
docker run -d --name ms-redis -p 6379:6379 redis:7
```

## Setup

```bash
pip install -r auth/requirements.txt
pip install stackpilot
```

## Run

```bash
cd examples/external-deps
stackpilot run
```

If Postgres or Redis is unreachable, StackPilot retries (default 5 attempts),
then aborts before starting apps. The message lists host, port, elapsed time,
attempts, dependent services, and a suggested fix.

Health:

- Auth: `http://127.0.0.1:8000/health`
- Gateway: `http://127.0.0.1:8001/health`

## Graph

```bash
stackpilot graph
```
