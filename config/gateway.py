"""Transparent reverse-proxy to the real backend (UPSTREAM_API_BASE).

The browser talks only to THIS service (so our CORS config applies); we forward
every /api/v1/* call server-to-server to the real DEV/demo backend at
https://demo.ark.glob.uz/ru/api/v1 — real accounts, drivers, car orders.
"""

import requests
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

# Headers we must NOT forward upstream (let requests/host set them).
_SKIP_REQUEST_HEADERS = {"host", "content-length", "connection", "accept-encoding"}
# Upstream response headers we must NOT copy back (CORS is added by our
# middleware; hop-by-hop/encoding headers would corrupt the response).
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


@csrf_exempt
def gateway(request, path):
    base = settings.UPSTREAM_API_BASE.rstrip("/")
    url = f"{base}/{path}"
    qs = request.META.get("QUERY_STRING")
    if qs:
        url = f"{url}?{qs}"

    headers = {}
    for key, value in request.headers.items():
        if key.lower() not in _SKIP_REQUEST_HEADERS:
            headers[key] = value
    headers.setdefault("Accept", "application/json")

    try:
        upstream = requests.request(
            request.method,
            url,
            data=request.body or None,
            headers=headers,
            timeout=30,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
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

    response = HttpResponse(
        upstream.content,
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "application/json"),
    )
    for key, value in upstream.headers.items():
        if key.lower() not in _SKIP_RESPONSE_HEADERS and key.lower() != "content-type":
            response[key] = value
    return response
