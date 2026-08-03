"""
Adaptive Health Demo — Flask Blueprint health route.

Expected health endpoint: /api/health
"""

from flask import Blueprint, Flask

app = Flask(__name__)
bp = Blueprint("api", __name__, url_prefix="/api")


@bp.route("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return "flask ok"


@app.get("/status")
def status():
    return {"status": "up"}


app.register_blueprint(bp)
