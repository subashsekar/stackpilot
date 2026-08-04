# FastAPI example

Minimal FastAPI service discovered by StackPilot.

## Layout

```text
fastapi/
  Stackfile.py
  api/
    main.py
    requirements.txt
```

## Setup

```bash
pip install -r api/requirements.txt
# or from the repo root after editable install:
pip install stackpilot fastapi uvicorn
```

## Run

```bash
cd examples/fastapi
stackpilot run
```

Health: `http://127.0.0.1:8001/health`

## Re-sync

```bash
stackpilot sync --force
```
