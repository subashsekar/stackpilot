"""HTTP readiness probe using only the standard library."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from .config import HttpHealthCheck

ALLOWED_HTTP_SCHEMES = frozenset({"http", "https"})

# Health probes must never inherit HTTP(S)_PROXY from the environment —
# macOS/Linux CI runners sometimes set a proxy that black-holes 127.0.0.1
# and burns the full probe_timeout on every attempt.
_NO_PROXY_OPENER = build_opener(ProxyHandler({}))


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
    """Probe ``url`` once and classify the result."""

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
    if not parsed.netloc:
        return HttpProbeResult(kind="error", detail="url must include a host")

    request = Request(text, method="GET")
    try:
        with _NO_PROXY_OPENER.open(request, timeout=request_timeout) as response:
            code = int(response.status)
            return _from_status(code)
    except HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        return _from_status(code)
    except TimeoutError:
        return HttpProbeResult(kind="timeout", detail="Timeout")
    except URLError as exc:
        reason = exc.reason
        message = str(reason) if reason is not None else str(exc)
        lower = message.lower()
        if "timed out" in lower or "timeout" in lower:
            return HttpProbeResult(kind="timeout", detail="Timeout")
        if "refused" in lower:
            return HttpProbeResult(kind="refused", detail="Connection refused")
        return HttpProbeResult(kind="error", detail=message or "URL error")
    except OSError as exc:
        message = str(exc)
        lower = message.lower()
        if "refused" in lower:
            return HttpProbeResult(kind="refused", detail="Connection refused")
        if "timed out" in lower or "timeout" in lower:
            return HttpProbeResult(kind="timeout", detail="Timeout")
        return HttpProbeResult(kind="error", detail=message)


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
