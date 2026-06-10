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
