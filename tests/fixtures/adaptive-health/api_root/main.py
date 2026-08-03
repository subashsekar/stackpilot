"""
Adaptive Health Demo — FastAPI with only root route.

Expected health endpoint: /
"""

from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def root():
    return {"ok": True, "service": "api_root"}
