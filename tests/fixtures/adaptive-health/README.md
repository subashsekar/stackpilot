# Adaptive Health Fixture

Multi-service fixture for StackPilot **adaptive health detection** tests.

Each service exposes a different health surface so sync/generation can prove
discovery, ranking, TCP fallback, and Stackfile output — without changing app
code for StackPilot's sake.

This lives under `tests/fixtures/` (not public `examples/`) because it exists
to exercise the detector, not as an end-user starter project.

## Services

| Service | Framework | Expected health |
|---------|-----------|-----------------|
| `api_v1_health` | FastAPI | `/api/v1/health` (nested router prefixes) |
| `api_ready` | FastAPI | `/ready` (beats `/ping` and `/`) |
| `api_root` | FastAPI | `/` |
| `api_none` | FastAPI | **TCP** (no health-like HTTP route) |
| `web_flask` | Flask | `/api/health` (Blueprint `url_prefix`) |
| `web_django` | Django | `/health/` |
| `app_express` | Express | `/internal/health` |
| `app_nestjs` | NestJS | `/health` |

## Quick verify

From the StackPilot repo root:

```bash
python tests/fixtures/adaptive-health/verify_demo.py
python -m pytest tests/test_adaptive_health_demo.py -q
```

Or sync a Stackfile into this folder:

```bash
cd tests/fixtures/adaptive-health
stackpilot sync --force
```

## Live probe check

`verify_demo.py` also spins short-lived HTTP servers that mirror discovered
paths and asserts:

- ranked candidates are probed in order
- 404 on `/health` falls through to `/ready`
- explicit Stackfile paths are never overridden
- TCP fallback when no HTTP health exists
