# Flask example

Minimal Flask service discovered by StackPilot.

## Layout

```text
flask/
  Stackfile.py
  web/
    app.py
    requirements.txt
```

## Setup

```bash
pip install -r web/requirements.txt
```

## Run

```bash
cd examples/flask
stackpilot run
```

Health: `http://127.0.0.1:8002/`

## Re-sync

```bash
stackpilot sync --force
```
