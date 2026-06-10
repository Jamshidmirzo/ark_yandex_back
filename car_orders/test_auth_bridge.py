"""Tests for the demo auth bridge on overlay endpoints (REQUIRE_OVERLAY_AUTH).

demo's /auth/me/ is mocked so we don't need a real demo token. With the flag OFF
the existing test_overlay.py already covers the open behaviour; here we verify the
ON behaviour: token required, identity derived from the token (not the body)."""

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

import config.auth as demo_auth
from car_orders.models import OrderMeta


class _Resp:
    def __init__(self, ok, data):
        self.ok = ok
        self._data = data

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def _clear_token_cache():
    demo_auth._CACHE.clear()
    yield
    demo_auth._CACHE.clear()


def _auth_as(monkeypatch, user_id, perms=None):
    payload = {"id": user_id, "username": f"u{user_id}"}
    if perms is not None:
        payload["permissions"] = perms
    monkeypatch.setattr(demo_auth.requests, "get", lambda *a, **k: _Resp(True, payload))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer faketoken")
    return client


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_enforced_requires_a_token():
    client = APIClient()
    r = client.post(
        "/api/v1/car-orders/700/overlay-claim/", {"driver_id": 5, "car_id": 1}, format="json"
    )
    assert r.status_code == 401


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_enforced_invalid_token_is_401(monkeypatch):
    monkeypatch.setattr(demo_auth.requests, "get", lambda *a, **k: _Resp(False, {}))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer bad")
    r = client.post(
        "/api/v1/car-orders/720/trip-state/", {"trip_state": "to_client"}, format="json"
    )
    assert r.status_code == 401


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_enforced_identity_comes_from_token_not_body(monkeypatch):
    client = _auth_as(monkeypatch, 671)
    r = client.post(
        "/api/v1/car-orders/700/overlay-claim/",
        {"driver_id": 999, "car_id": 1, "car_label": "X"},  # body claims 999
        format="json",
    )
    assert r.status_code == 200, r.content
    assert OrderMeta.objects.get(order_id=700).driver_id == 671  # token wins, not 999


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_enforced_my_orders_ignores_query_driver_id(monkeypatch):
    OrderMeta.objects.create(order_id=710, driver_id=42, trip_state=OrderMeta.TripState.ASSIGNED)
    OrderMeta.objects.create(order_id=711, driver_id=99, trip_state=OrderMeta.TripState.ASSIGNED)
    client = _auth_as(monkeypatch, 42)
    r = client.get("/api/v1/car-orders/drivers/me/overlay-orders/?driver_id=99")
    assert r.status_code == 200
    assert [o["order_id"] for o in r.json()] == [710]  # only the token user's, not 99


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_live_location_stays_open_for_the_simulator(monkeypatch):
    # No token at all — the simulator pushes here without one.
    client = APIClient()
    r = client.post(
        "/api/v1/car-orders/730/live-location/", {"lat": 41.3, "lng": 69.2}, format="json"
    )
    assert r.status_code == 200, r.content
