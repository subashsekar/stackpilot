"""
Adaptive Health Demo — FastAPI with no health-like routes.

Expected: TCP health fallback (no HTTP health endpoint).
"""

from fastapi import FastAPI

app = FastAPI()


@app.get("/docs-info")
def docs_info():
    return {"docs": "/docs"}


@app.get("/openapi-info")
def openapi_info():
    return {"openapi": "/openapi.json"}
