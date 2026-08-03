#!/usr/bin/env python3
"""Verify Adaptive Health demo discovery, generation, and live probing."""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stackpilot.adapters.detect.health_probe import (  # noqa: E402
    select_working_health_endpoint,
)
from stackpilot.adapters.detect.health_routes import (  # noqa: E402
    discover_health_path,
)
from stackpilot.adapters.fastapi import FastAPIAdapter  # noqa: E402
from stackpilot.generator import generate_stackfile  # noqa: E402
from stackpilot.http_checker import HttpProbeResult  # noqa: E402
from stackpilot.scanner import scan_project  # noqa: E402

DEMO = Path(__file__).resolve().parent

EXPECTED = {
    "api_v1_health": ("FastAPI", "/api/v1/health", "http"),
    "api_ready": ("FastAPI", "/ready", "http"),
    "api_root": ("FastAPI", "/", "http"),
    "api_none": ("FastAPI", None, "tcp"),
    "web_flask": ("Flask", "/api/health", "http"),
    "web_django": ("Django", "/health/", "http"),
    "app_express": ("Express", "/internal/health", "http"),
    "app_nestjs": ("NestJS", "/health", "http"),
}


class _Handler(BaseHTTPRequestHandler):
    responses: dict[str, int] = {}

    def do_GET(self) -> None:  # noqa: N802
        code = self.responses.get(self.path, 404)
        self.send_response(code)
        self.end_headers()
        if code < 400:
            self.wfile.write(b"ok")

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _serve(responses: dict[str, int], stop: threading.Event) -> str:
    handler = type("H", (_Handler,), {"responses": dict(responses)})
    server = HTTPServer(("127.0.0.1", 0), handler)
    host, port = server.server_address[:2]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def _shutdown() -> None:
        stop.wait()
        server.shutdown()

    threading.Thread(target=_shutdown, daemon=True).start()
    return f"http://{host}:{port}"


def _ok(label: str) -> None:
    print(f"  PASS  {label}")


def _fail(label: str, detail: str) -> None:
    print(f"  FAIL  {label}: {detail}")
    raise AssertionError(f"{label}: {detail}")


def verify_scan_and_discovery() -> None:
    print("\n== Discovery ==")
    services = scan_project(DEMO)
    by_name = {s.name: s for s in services}

    missing = sorted(set(EXPECTED) - set(by_name))
    if missing:
        _fail("scan", f"missing services: {missing}")
    _ok(f"scan found {len(services)} services")

    for name, (framework, path, kind) in EXPECTED.items():
        service = by_name[name]
        if service.framework != framework:
            _fail(name, f"framework {service.framework!r} != {framework!r}")
        discovered = discover_health_path(Path(service.path), framework)
        if path is None:
            if discovered is not None:
                _fail(name, f"expected no health path, got {discovered!r}")
        elif discovered != path:
            # Django may normalize trailing slash variants.
            if {discovered, path} <= {"/health", "/health/"}:
                pass
            else:
                _fail(name, f"path {discovered!r} != {path!r}")
        _ok(f"{name}: {framework} -> {path or 'TCP'}")


def verify_stackfile() -> str:
    print("\n== Stackfile generation ==")
    services = scan_project(DEMO)
    text = generate_stackfile(services, project_root=DEMO)

    required_snippets = [
        "/api/v1/health",
        "/ready",
        'HttpHealthCheck(url="http://127.0.0.1:',
        "TcpHealthCheck(",
        "/api/health",
        "/internal/health",
        "/health",
    ]
    for snippet in required_snippets:
        if snippet not in text:
            _fail("stackfile", f"missing {snippet!r}")
        _ok(f"stackfile contains {snippet}")

    out = DEMO / "Stackfile.py"
    out.write_text(text, encoding="utf-8")
    _ok(f"wrote {out.relative_to(ROOT)}")
    return text


def verify_adapter_tcp_fallback() -> None:
    print("\n== Adapter TCP fallback ==")
    spec = FastAPIAdapter().generate_service(DEMO / "api_none", port=8099)
    if spec.health != "tcp":
        _fail("api_none adapter", f"health={spec.health!r}")
    _ok("api_none -> health=tcp")


def verify_live_probes() -> None:
    print("\n== Live HTTP probes ==")
    stop = threading.Event()
    try:
        base = _serve({"/health": 404, "/ready": 200, "/": 200}, stop)
        selection, result = select_working_health_endpoint(
            base_url=base,
            discovered_routes=["/health", "/ready", "/"],
        )
        if selection.path != "/ready" or result is None or result.kind != "healthy":
            _fail("404 fallback", f"got {selection.path!r} / {result}")
        _ok("404 on /health -> /ready succeeds")

        selection, result = select_working_health_endpoint(
            base_url=base,
            discovered_routes=["/health", "/ready"],
            explicit_path="/health",
        )
        if selection.kind != "explicit" or selection.path != "/health":
            _fail("explicit override", f"got {selection}")
        if result is None or result.kind != "not_found":
            _fail("explicit override probe", f"got {result}")
        _ok("explicit /health not overridden despite 404")

        selection, _ = select_working_health_endpoint(
            base_url=base,
            discovered_routes=["/docs"],
            tcp_check=lambda: True,
            probe=lambda _u: HttpProbeResult(kind="not_found", status_code=404),
        )
        if selection.kind != "tcp":
            _fail("tcp fallback", f"got {selection.kind}")
        _ok("no HTTP health -> TCP fallback")
    finally:
        stop.set()


def main() -> int:
    print(f"Adaptive Health Demo verification\nRoot: {DEMO}")
    try:
        verify_scan_and_discovery()
        verify_adapter_tcp_fallback()
        verify_stackfile()
        verify_live_probes()
    except AssertionError as exc:
        print(f"\nFAILED: {exc}")
        return 1
    print("\nAll adaptive-health demo checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
