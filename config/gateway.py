"""Transparent reverse-proxy to the real backend (UPSTREAM_API_BASE).

The browser talks only to THIS service (so our CORS config applies); we forward
every /api/v1/* call server-to-server to the real DEV/demo backend at
https://demo.ark.glob.uz/ru/api/v1 — real accounts, drivers, car orders.
"""

import http.cookiejar
import logging
import time

import requests
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from requests.adapters import HTTPAdapter

logger = logging.getLogger("gateway")

_SKIP_REQUEST_HEADERS = {"host", "content-length", "connection", "accept-encoding"}
_SKIP_RESPONSE_HEADERS = {
    "content-encoding",
    "transfer-encoding",
    "connection",
    "content-length",
    "access-control-allow-origin",
    "access-control-allow-credentials",
    "access-control-allow-headers",
    "access-control-allow-methods",
}


class _BlockAllCookies(http.cookiejar.CookiePolicy):
    """Never store or send cookies. The session is shared across ALL callers, so
    persisting a Set-Cookie from one user's response would leak it into the next
    user's request. We forward Authorization (Bearer) explicitly, so cookies are
    not needed anyway."""

    return_ok = set_ok = domain_return_ok = path_return_ok = lambda self, *a, **k: False
    netscape = True
    rfc2965 = hide_cookie2 = False


# One pooled session for the whole process. keep-alive + connection reuse means
# we pay the DNS + TCP + TLS handshake to the upstream ONCE and reuse the socket
# afterwards — over a slow link to demo that handshake is the bulk of the
# per-request latency, so this is the real speed win (vs. just a bigger timeout).
_session = requests.Session()
_session.cookies.set_policy(_BlockAllCookies())
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=50)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)

# Log any upstream call slower than this (seconds) so the slow endpoint is easy
# to spot in the server console.
_SLOW_UPSTREAM_S = 3.0

# Retry transient upstream CONNECTION resets. The pooled session keeps sockets
# alive; the remote upstream drops idle ones, so the next request can grab a dead
# socket and fail with "Connection reset by peer" / "Connection aborted" — even
# though the very next attempt succeeds. This used to surface as an intermittent
# 502 on auth/refresh + auth/me, which made the mobile app re-bootstrap and tear
# down its WebSockets (connect/disconnect loop). Retry only the connection-failed
# case (the reset fires at send time, so the request never reached the upstream —
# safe to replay); a real response or a read timeout is NOT retried.
_UPSTREAM_RETRIES = 2
_UPSTREAM_BACKOFF_S = 0.15


def _bad_gateway(url, exc):
    return JsonResponse(
        {
            "error": {
                "code": "UPSTREAM_UNREACHABLE",
                "message": str(exc),
                "details": {"upstream": url},
            }
        },
        status=502,
    )


@csrf_exempt
def gateway(request, path):
    base = settings.UPSTREAM_API_BASE.rstrip("/")
    url = f"{base}/{path}"
    qs = request.META.get("QUERY_STRING")
    if qs:
        url = f"{url}?{qs}"

    # AUDIT L3: we forward all inbound client headers (minus _SKIP_REQUEST_HEADERS) to
    # upstream verbatim, including Authorization. That's fine here — upstream is the
    # auth authority and cookies are isolated separately — but DON'T start trusting any
    # forwarded header for a local decision without re-validating it.
    headers = {}
    for key, value in request.headers.items():
        if key.lower() not in _SKIP_REQUEST_HEADERS:
            headers[key] = value
    headers.setdefault("Accept", "application/json")

    started = time.monotonic()
    upstream = None
    for attempt in range(_UPSTREAM_RETRIES + 1):
        try:
            upstream = _session.request(
                request.method,
                url,
                data=request.body or None,
                headers=headers,
                timeout=settings.UPSTREAM_TIMEOUT,  # (connect, read) — see settings
                allow_redirects=False,
            )
            break
        except requests.ConnectionError as exc:
            # Stale pooled keep-alive socket (or upstream briefly unreachable). urllib3
            # has already evicted the bad socket, so a retry gets a fresh connection.
            if attempt < _UPSTREAM_RETRIES:
                time.sleep(_UPSTREAM_BACKOFF_S * (attempt + 1))
                continue
            logger.warning("upstream connection failed after %d attempts, %.1fs %s %s — %s",
                           attempt + 1, time.monotonic() - started, request.method, path, exc)
            return _bad_gateway(url, exc)
        except requests.RequestException as exc:
            # Other errors (e.g. a read timeout to a hung upstream): don't retry —
            # the request may already have been received and processed upstream.
            logger.warning("upstream error after %.1fs %s %s — %s",
                           time.monotonic() - started, request.method, path, exc)
            return _bad_gateway(url, exc)

    elapsed = time.monotonic() - started
    if elapsed >= _SLOW_UPSTREAM_S:
        logger.warning("slow upstream %.1fs %s %s (status %s)",
                       elapsed, request.method, path, upstream.status_code)

    response = HttpResponse(
        upstream.content,
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "application/json"),
    )
    for key, value in upstream.headers.items():
        if key.lower() not in _SKIP_RESPONSE_HEADERS and key.lower() != "content-type":
            response[key] = value
    return response
