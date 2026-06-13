"""Gateway request normalisation.

The web frontend talks to us with ``/api/v1/...`` (no language segment; the
gateway adds ``/ru/`` upstream via ``UPSTREAM_API_BASE``). The **mobile app**
uses the demo backend's native scheme — ``/<lang>/api/v1/...`` (language in the
path) and probes ``/<lang>/healthcheck/``. Stripping a leading language segment
lets BOTH schemes resolve to the same routes: our local feature views first,
then the gateway proxy. See [[dev-runtime-setup]] / INTEGRATION.md.
"""

import re

# A leading 2-letter language segment, only when it precedes our real paths
# (api/v1 or the health/healthcheck probes) — so we never strip a genuine path.
_LANG_PREFIX = re.compile(r"^/[a-z]{2}/(?=api/v1/|health)")


class MobileLanguagePrefixMiddleware:
    """Drop a leading ``/<lang>/`` so ``/ru/api/v1/...`` (mobile) routes exactly
    like ``/api/v1/...`` (web). No-op for paths without a language prefix, so the
    web frontend is unaffected."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        stripped = _LANG_PREFIX.sub("/", request.path_info, count=1)
        if stripped != request.path_info:
            request.path_info = stripped
            request.path = stripped
        return self.get_response(request)


def _source_tag(request):
    """``📱 <ip>`` (a real phone / remote client) vs ``🖥 локально`` (our server or
    the web frontend on 127.0.0.1) — so mobile traffic stands out in the log."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    ip = xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR", "?")
    return "🖥 локально" if ip in ("127.0.0.1", "::1", "localhost") else f"📱 {ip}"


class RequestLogMiddleware:
    """Log EVERY api/health request with its source, so the WHOLE mobile
    conversation is visible in one stream (not just GPS/status) — e.g. «get all
    orders», claim, detail. Grep «📱» to see only the phone, or «📱|🖥» for all.
    Gated by settings.LOG_TRACKING (default on in dev; LOG_TRACKING=0 to silence)."""

    def __init__(self, get_response):
        from django.conf import settings

        self.get_response = get_response
        self.enabled = getattr(settings, "LOG_TRACKING", False)

    @staticmethod
    def _loggable(path):
        return path.startswith("/api/v1/") or "/health" in path

    def __call__(self, request):
        import time

        if not self.enabled or not self._loggable(request.path):
            return self.get_response(request)
        t0 = time.monotonic()
        response = self.get_response(request)
        ms = (time.monotonic() - t0) * 1000
        qs = request.META.get("QUERY_STRING", "")
        path = request.path + ("?" + qs if qs else "")
        print(
            f"🌐 [{_source_tag(request)}] {request.method} {path} → {response.status_code} ({ms:.0f}ms)",
            flush=True,
        )
        return response
