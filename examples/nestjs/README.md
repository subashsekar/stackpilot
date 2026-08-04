# NestJS example

Minimal NestJS-shaped Node service discovered by StackPilot via
`@nestjs/core` in `package.json`.

## Layout

```text
nestjs/
  Stackfile.py
  app/
    package.json
    main.js
    app.controller.ts
```

The `start:dev` script runs a tiny HTTP server so the example works without a
full Nest CLI toolchain. Detection still matches NestJS through package
dependencies. The controller declares `@Get("health")` so sync emits an HTTP
health check at `/health` (TCP is used when no health route is present).

## Setup

```bash
cd app
npm install
```

## Run

```bash
cd examples/nestjs
stackpilot run
```

Health: `http://127.0.0.1:8005/health`

## Re-sync

```bash
stackpilot sync --force
```
