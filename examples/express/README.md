# Express example

Minimal Express (Node) service discovered by StackPilot.

## Layout

```text
express/
  Stackfile.py
  app/
    package.json
    server.js
```

## Setup

```bash
cd app
npm install
```

## Run

```bash
cd examples/express
stackpilot run
```

Health: `http://127.0.0.1:8000/`

## Re-sync

```bash
stackpilot sync --force
```
