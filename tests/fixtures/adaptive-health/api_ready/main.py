"""
Adaptive Health Demo — FastAPI readiness probe.

Expected health endpoint: /ready
"""

from fastapi import FastAPI

app = FastAPI()


@app.get("/ready")
def ready():
    return {"ready": True}


@app.get("/ping")
def ping():
    return {"pong": True}


@app.get("/")
def root():
    return {"service": "api_ready"}
