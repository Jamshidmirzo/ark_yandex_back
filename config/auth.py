"""Auth bridge to the demo backend.

Login is proxied to demo, so the JWT the client holds is signed by **demo's**
secret — our local SIMPLE_JWT can't verify it. This DRF authentication class
validates the bearer token by asking demo ``GET /auth/me/`` and maps the result
to a lightweight principal (id + permission codenames), with a short per-process
cache so we don't hit demo on every request.

Gated by ``settings.REQUIRE_OVERLAY_AUTH``: when off (default) it is a complete
no-op — zero extra demo calls, behaviour identical to before — so it is safe to
ship disabled and flip on once login is verified end-to-end.
"""

import time

import requests
from django.conf import settings
from rest_framework import authentication

# token -> (expires_at_monotonic, DemoUser). Per-process; use a shared cache
# (Redis) if you run multiple workers.
_CACHE: dict = {}
_TTL = 60.0  # seconds


class DemoUser:
    """Minimal authenticated principal mapped from demo's ``/auth/me/``."""

    is_authenticated = True
    is_anonymous = False

    def __init__(self, user_id, username="", permissions=None, is_superuser=False):
        self.id = user_id
        self.pk = user_id
        self.username = username
        self.permissions = set(permissions or [])
        self.is_superuser = is_superuser

    def __str__(self):
        return f"DemoUser({self.id})"


def _demo_me_url():
    return f"{settings.UPSTREAM_API_BASE.rstrip('/')}/auth/me/"


def _extract_perms(data):
    out = set()
    perms = data.get("permissions")
    if isinstance(perms, list):
        for p in perms:
            if isinstance(p, str):
                out.add(p)
            elif isinstance(p, dict) and "codename" in p:
                out.add(str(p["codename"]))
    return out


class DemoTokenAuthentication(authentication.BaseAuthentication):
    """Validate a demo bearer token via demo ``/auth/me/``. Lenient: returns
    ``None`` (anonymous) when disabled, when no token, or when validation fails —
    it never raises, so the permission layer alone decides access."""

    def authenticate(self, request):
        if not getattr(settings, "REQUIRE_OVERLAY_AUTH", False):
            return None  # disabled → no-op, no demo call
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return None
        token = header[7:].strip()
        if not token:
            return None

        cached = _CACHE.get(token)
        if cached and cached[0] > time.monotonic():
            return (cached[1], token)

        try:
            resp = requests.get(
                _demo_me_url(),
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=8,
            )
        except requests.RequestException:
            return None  # demo unreachable → anonymous (whole app is down anyway)
        if not resp.ok:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        uid = data.get("id")
        if uid is None:
            return None
        user = DemoUser(
            uid,
            username=data.get("username", ""),
            permissions=_extract_perms(data),
            is_superuser=bool(data.get("is_superuser")),
        )
        _CACHE[token] = (time.monotonic() + _TTL, user)
        return (user, token)

    def authenticate_header(self, request):
        return "Bearer"
