"""HTTP readiness probe using only the standard library."""

from __future__ import annotations

import http.client
import socket
from dataclasses import dataclass
from typing import Mapping, Optional, Union
from urllib.parse import urlparse

from .config import HttpHealthCheck

ALLOWED_HTTP_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class HttpProbeResult:
    """
    Structured outcome of a single HTTP health probe.

    Kinds:
      healthy   — HTTP 200–399
      not_found — HTTP 404, or 401/403 (not a usable anonymous health surface)
      failed    — HTTP 5xx (endpoint exists; health failed)
      other     — other HTTP status
      refused   — connection refused (app not started)
      timeout   — request timed out
      error     — other transport / URL errors
    """

    kind: str
    status_code: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == "healthy"


def check_http(url: str, *, request_timeout: float = 2.0) -> bool:
    """
    Return True when ``GET url`` responds with HTTP 200–399.

    Connection errors, timeouts, and other statuses are unhealthy.
    """

    return probe_http(url, request_timeout=request_timeout).ok


def probe_http(url: str, *, request_timeout: float = 2.0) -> HttpProbeResult:
    """
    Probe ``url`` once and classify the result.

    Uses :mod:`http.client` (not ``urllib``) so probes never follow
    ``HTTP(S)_PROXY`` / macOS system proxy settings. Those proxies have been
    observed on GitHub Actions macOS runners to black-hole ``127.0.0.1`` and
    ignore urllib timeouts, starving service health checks.
    """

    text = (url or "").strip()
    if not text:
        return HttpProbeResult(kind="error", detail="empty url")

    try:
        parsed = urlparse(text)
    except ValueError as exc:
        return HttpProbeResult(kind="error", detail=f"invalid url: {exc}")
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_HTTP_SCHEMES:
        return HttpProbeResult(
            kind="error",
            detail=f"url must use http or https scheme (got {scheme!r})",
        )
    host = parsed.hostname
    if not host:
        return HttpProbeResult(kind="error", detail="url must include a host")

    # Prefer numeric / explicit host; never re-resolve "localhost" via DNS
    # (macOS may prefer ::1 while the service bound 127.0.0.1 only).
    if host in {"localhost", "0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"

    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80

    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    timeout = max(0.05, float(request_timeout))
    conn: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
    try:
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, int(port), timeout=timeout)
        else:
            conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
        conn.request("GET", path, headers={"Connection": "close"})
        response = conn.getresponse()
        try:
            # Drain body so the connection can close cleanly.
            response.read()
        except Exception:
            pass
        return _from_status(int(response.status))
    except TimeoutError:
        return HttpProbeResult(kind="timeout", detail="Timeout")
    except socket.timeout:
        return HttpProbeResult(kind="timeout", detail="Timeout")
    except ConnectionRefusedError:
        return HttpProbeResult(kind="refused", detail="Connection refused")
    except OSError as exc:
        message = str(exc)
        lower = message.lower()
        if "refused" in lower:
            return HttpProbeResult(kind="refused", detail="Connection refused")
        if "timed out" in lower or "timeout" in lower:
            return HttpProbeResult(kind="timeout", detail="Timeout")
        return HttpProbeResult(kind="error", detail=message or "OS error")
    except http.client.HTTPException as exc:
        return HttpProbeResult(kind="error", detail=str(exc) or "HTTP error")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _from_status(code: int) -> HttpProbeResult:
    if 200 <= code <= 399:
        return HttpProbeResult(
            kind="healthy",
            status_code=code,
            detail=f"{code} {_status_phrase(code)}".rstrip(),
        )
    if code == 404:
        return HttpProbeResult(
            kind="not_found",
            status_code=404,
            detail="404 Not Found",
        )
    if code in {401, 403}:
        # Auth-gated business routes are not usable health endpoints.
        return HttpProbeResult(
            kind="not_found",
            status_code=code,
            detail=f"{code} {_status_phrase(code)}".rstrip(),
        )
    if 500 <= code <= 599:
        return HttpProbeResult(
            kind="failed",
            status_code=code,
            detail=f"{code} {_status_phrase(code)}".rstrip(),
        )
    return HttpProbeResult(
        kind="other",
        status_code=code,
        detail=f"{code} {_status_phrase(code)}".rstrip(),
    )


def _status_phrase(code: int) -> str:
    phrases = {
        200: "OK",
        201: "Created",
        204: "No Content",
        301: "Moved Permanently",
        302: "Found",
        307: "Temporary Redirect",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        500: "Internal Server Error",
        502: "Bad Gateway",
        503: "Service Unavailable",
    }
    return phrases.get(code, "")


def http_url_from_config(
    health_check: Union[HttpHealthCheck, Mapping],
) -> Optional[str]:
    """Extract a non-empty URL from a health-check model or mapping."""

    if isinstance(health_check, HttpHealthCheck):
        text = health_check.url.strip()
        return text or None
    url = health_check.get("url")
    if url is None:
        return None
    text = str(url).strip()
    return text or None
