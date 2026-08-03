# Celery example

Minimal Celery worker discovered by StackPilot.

## Layout

```text
celery/
  Stackfile.py
  worker/
    celery.py
    requirements.txt
```

## Setup

```bash
pip install -r worker/requirements.txt
```

This example sets `task_always_eager = True` so the worker can start without a
broker for local demos. Point `broker_url` at Redis/RabbitMQ for a real worker.

## Run

```bash
cd examples/celery
stackpilot run
```

Health: process liveness (no HTTP port).

## Re-sync

```bash
stackpilot sync --force
```
