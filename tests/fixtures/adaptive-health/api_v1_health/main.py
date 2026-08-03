"""
Adaptive Health Demo — FastAPI service with nested router prefixes.

Expected health endpoint: /api/v1/health
"""

from fastapi import APIRouter, FastAPI

app = FastAPI()

router = APIRouter(prefix="/health")


@router.get("/")
def health():
    return {"ok": True, "service": "api_v1_health"}


@app.get("/")
def root():
    return {"service": "api_v1_health"}


@app.get("/docs-info")
def docs_info():
    return {"docs": "/docs"}


app.include_router(router, prefix="/api/v1")
