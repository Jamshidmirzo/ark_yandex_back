"""Unit tests for the overlay permission helpers (``car_orders.permissions``).

These pin the fail-closed / open-dev behaviour of OverlayDispatcher and the
identity-resolution of acting_driver_id / assignee_driver_id — the security
surface that decides whether a body-supplied driver_id is trusted. No HTTP.
"""

import pytest
from django.test import override_settings

from car_orders import permissions


class _User:
    def __init__(self, *, authed=True, superuser=False, perms=None, uid=1):
        self.is_authenticated = authed
        self.is_superuser = superuser
        self.permissions = set(perms or [])
        self.id = uid


class _Req:
    def __init__(self, user=None, data=None):
        self.user = user
        self.data = data or {}


# ---- OverlayAuthenticated --------------------------------------------------

def test_overlay_authenticated_open_when_not_enforced():
    # Default REQUIRE_OVERLAY_AUTH is off → anyone (even anon) passes.
    assert permissions.OverlayAuthenticated().has_permission(_Req(user=None), None) is True


@override_settings(REQUIRE_OVERLAY_AUTH=True)
def test_overlay_authenticated_requires_user_when_enforced():
    p = permissions.OverlayAuthenticated()
    assert p.has_permission(_Req(user=_User(authed=False)), None) is False
    assert p.has_permission(_Req(user=_User(authed=True)), None) is True


# ---- OverlayDispatcher -----------------------------------------------------

def test_dispatcher_open_when_not_enforced():
    # Off → open, even for an anonymous request (dev behaviour).
    assert permissions.OverlayDispatcher().has_permission(_Req(user=None), None) is True


@override_settings(REQUIRE_OVERLAY_AUTH=True)
def test_dispatcher_fails_closed_when_unauthenticated():
    assert permissions.OverlayDispatcher().has_permission(_Req(user=_User(authed=False)), None) is False


@override_settings(REQUIRE_OVERLAY_AUTH=True)
def test_dispatcher_superuser_passes():
    req = _Req(user=_User(superuser=True))
    assert permissions.OverlayDispatcher().has_permission(req, None) is True


@override_settings(REQUIRE_OVERLAY_AUTH=True)
def test_dispatcher_requires_approve_permission():
    p = permissions.OverlayDispatcher()
    assert p.has_permission(_Req(user=_User(perms=["car_order:approve"])), None) is True
    assert p.has_permission(_Req(user=_User(perms=["car_order:create"])), None) is False


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.parametrize("perm", ["administrator", "car_order:approve_all"])
def test_dispatcher_honours_permission_hierarchy(perm):
    req = _Req(user=_User(perms=[perm]))
    assert permissions.OverlayDispatcher().has_permission(req, None) is True


# ---- acting_driver_id ------------------------------------------------------

def test_acting_driver_id_prefers_authenticated_user():
    assert permissions.acting_driver_id(_Req(user=_User(uid=671)), fallback=999) == 671


def test_acting_driver_id_falls_back_when_anonymous():
    assert permissions.acting_driver_id(_Req(user=_User(authed=False)), fallback=999) == 999


# ---- assignee_driver_id ----------------------------------------------------

@override_settings(REQUIRE_OVERLAY_AUTH=True)
def test_assignee_uses_body_for_dispatcher():
    req = _Req(user=_User(perms=["car_order:approve"], uid=1), data={"driver_id": 999})
    assert permissions.assignee_driver_id(req, None) == 999


@override_settings(REQUIRE_OVERLAY_AUTH=True)
def test_assignee_returns_non_int_body_unchanged_for_dispatcher():
    req = _Req(user=_User(perms=["car_order:approve"], uid=1), data={"driver_id": "abc"})
    assert permissions.assignee_driver_id(req, None) == "abc"


@override_settings(REQUIRE_OVERLAY_AUTH=True)
def test_assignee_ignores_body_for_plain_driver():
    # Enforced + non-dispatcher → identity comes from the token, not the body.
    req = _Req(user=_User(perms=[], uid=42), data={"driver_id": 999})
    assert permissions.assignee_driver_id(req, None) == 42


def test_assignee_open_dev_trusts_body():
    # Not enforced → OverlayDispatcher is open, so the body id is honoured.
    req = _Req(user=_User(authed=False), data={"driver_id": 42})
    assert permissions.assignee_driver_id(req, None) == 42
