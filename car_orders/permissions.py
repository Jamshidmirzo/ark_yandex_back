"""Permissions for the overlay feature endpoints.

Gated by ``settings.REQUIRE_OVERLAY_AUTH``: off (default) keeps the current open
dev behaviour; on requires a demo-bridged authenticated user (see
``config.auth.DemoTokenAuthentication``) and derives identity from the token, not
the request body.
"""

from django.conf import settings
from rest_framework.permissions import BasePermission

from auth_core.permissions import expand_permission_codename


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
    ``car_order:approve`` codename (or superuser). Fails CLOSED when perms aren't
    available: in enforced mode an unidentified user is NOT a dispatcher, so a
    spoofed body ``driver_id`` can never override the token identity
    (see test_auth_bridge::test_enforced_identity_comes_from_token_not_body)."""

    def has_permission(self, request, view):
        if not _auth_required():
            return True
        if not _authed(request):
            return False
        user = request.user
        if getattr(user, "is_superuser", False):
            return True
        perms = getattr(user, "permissions", set())
        # Apply the ARK permission hierarchy the rest of the app and the web client
        # use (administrator ⊇ everything, X_all ⊇ X), so an `administrator` /
        # `car_order:approve_all` holder counts as a dispatcher too — matching
        # useMyPermissions on the frontend. DemoUser carries an in-memory perms set,
        # so expand against it rather than the DB-backed user_has_permission.
        return bool(expand_permission_codename("car_order:approve") & set(perms))


class OverlayDriverOrDispatcher(BasePermission):
    """Overlay actions that mutate a trip / claim / shift (overlay-claim, release,
    extend, GPS uplink, go on shift). The actor must actually be a DRIVER
    (``driver:accept_order`` — the same codename the native ``claim``/``release``
    require) or a DISPATCHER assigning on a driver's behalf (``car_order:approve``).

    Fails CLOSED when enforced: an authenticated user holding neither codename (a
    customer-tier token) cannot drive overlay trip state, so the overlay contract
    matches its native twin instead of being silently looser. Identity is still
    derived from the token (see ``assignee_driver_id``), so a dispatcher may name the
    driver in the body while a plain driver only ever acts on themselves. Open in dev
    (auth off) like the other overlay gates."""

    def has_permission(self, request, view):
        if not _auth_required():
            return True
        if not _authed(request):
            return False
        user = request.user
        if getattr(user, "is_superuser", False):
            return True
        perms = set(getattr(user, "permissions", set()))
        allowed = expand_permission_codename("driver:accept_order") | expand_permission_codename(
            "car_order:approve"
        )
        return bool(allowed & perms)


def acting_driver_id(request, fallback=None):
    """Authoritative driver id: the authenticated demo user's id when available,
    else the client-supplied ``fallback`` (only trusted when auth isn't enforced)."""
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user.id
    return fallback


def assignee_driver_id(request, view):
    """The driver an overlay claim / schedule-check is FOR (the assignee) — which is
    NOT necessarily the caller.

    A DISPATCHER assigning an order to a chosen driver supplies that driver in the
    body ``driver_id`` → use it. A DRIVER acting on their OWN order derives identity
    from the token (a spoofed body id is ignored). When overlay auth isn't enforced
    the body id is trusted (open dev behaviour).

    Without this, ``acting_driver_id`` returns the authenticated DISPATCHER's own id,
    so a dispatcher-assigned order is silently claimed for the dispatcher instead of
    the driver.
    """
    body = request.data.get("driver_id")
    if body is not None and OverlayDispatcher().has_permission(request, view):
        try:
            return int(body)
        except (TypeError, ValueError):
            return body
    return acting_driver_id(request, body)
