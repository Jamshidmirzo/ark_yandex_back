"""Permissions for the overlay feature endpoints.

Gated by ``settings.REQUIRE_OVERLAY_AUTH``: off (default) keeps the current open
dev behaviour; on requires a demo-bridged authenticated user (see
``config.auth.DemoTokenAuthentication``) and derives identity from the token, not
the request body.
"""

from django.conf import settings
from rest_framework.permissions import BasePermission


def _auth_required():
    return getattr(settings, "REQUIRE_OVERLAY_AUTH", False)


def _authed(request):
    user = getattr(request, "user", None)
    return bool(user and getattr(user, "is_authenticated", False))


class OverlayAuthenticated(BasePermission):
    """Allow all when auth isn't enforced; otherwise require an authenticated
    (demo-bridged) user."""

    def has_permission(self, request, view):
        return True if not _auth_required() else _authed(request)


class OverlayDispatcher(BasePermission):
    """Dispatcher-only actions (e.g. reassign). When enforced, require the
    ``car_order:approve`` codename; if perms aren't available from ``/auth/me/``,
    fall back to any authenticated user (graceful)."""

    def has_permission(self, request, view):
        if not _auth_required():
            return True
        if not _authed(request):
            return False
        user = request.user
        if getattr(user, "is_superuser", False):
            return True
        perms = getattr(user, "permissions", set())
        return "car_order:approve" in perms if perms else True


def acting_driver_id(request, fallback=None):
    """Authoritative driver id: the authenticated demo user's id when available,
    else the client-supplied ``fallback`` (only trusted when auth isn't enforced)."""
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user.id
    return fallback
