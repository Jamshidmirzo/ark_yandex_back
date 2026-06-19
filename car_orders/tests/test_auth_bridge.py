"""Tests for the demo auth bridge on overlay endpoints (REQUIRE_OVERLAY_AUTH).

demo's /auth/me/ is mocked so we don't need a real demo token. With the flag OFF
the existing test_overlay.py already covers the open behaviour; here we verify the
ON behaviour: token required, identity derived from the token (not the body)."""

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

import config.auth as demo_auth
from car_orders.models import OrderLiveLocation, OrderMeta


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
def test_admin_overlay_orders_sees_the_whole_board(monkeypatch):
    """A dispatcher (car_order:approve) gets EVERY active order, not just their own
    — while a plain driver only ever sees their own (covered above)."""
    OrderMeta.objects.create(order_id=810, driver_id=42, trip_state=OrderMeta.TripState.ASSIGNED)
    OrderMeta.objects.create(order_id=811, driver_id=99, trip_state=OrderMeta.TripState.IN_TRIP)
    # Terminal orders never show on the active board.
    OrderMeta.objects.create(order_id=812, driver_id=99, trip_state=OrderMeta.TripState.COMPLETED)
    client = _auth_as(monkeypatch, 1, perms=["car_order:approve"])
    r = client.get("/api/v1/car-orders/drivers/me/overlay-orders/")
    assert r.status_code == 200
    assert sorted(o["order_id"] for o in r.json()) == [810, 811]


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
@pytest.mark.parametrize("perm", ["administrator", "car_order:approve_all"])
def test_admin_overlay_orders_honours_permission_hierarchy(monkeypatch, perm):
    """A user who satisfies car_order:approve via the ARK hierarchy (administrator
    or car_order:approve_all) is a dispatcher too — same expansion the web client
    uses — so they get the whole board, not just their own (empty) list."""
    OrderMeta.objects.create(order_id=830, driver_id=42, trip_state=OrderMeta.TripState.ASSIGNED)
    OrderMeta.objects.create(order_id=831, driver_id=99, trip_state=OrderMeta.TripState.IN_TRIP)
    client = _auth_as(monkeypatch, 1, perms=[perm])
    r = client.get("/api/v1/car-orders/drivers/me/overlay-orders/")
    assert r.status_code == 200
    assert sorted(o["order_id"] for o in r.json()) == [830, 831]


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_admin_overlay_orders_can_filter_to_one_driver(monkeypatch):
    """An admin may narrow the board to a single driver via ?driver_id= (a power a
    plain driver doesn't get — for them the param is ignored)."""
    OrderMeta.objects.create(order_id=820, driver_id=42, trip_state=OrderMeta.TripState.ASSIGNED)
    OrderMeta.objects.create(order_id=821, driver_id=99, trip_state=OrderMeta.TripState.IN_TRIP)
    client = _auth_as(monkeypatch, 1, perms=["car_order:approve"])
    r = client.get("/api/v1/car-orders/drivers/me/overlay-orders/?driver_id=99")
    assert r.status_code == 200
    assert [o["order_id"] for o in r.json()] == [821]


@pytest.mark.django_db
def test_live_location_open_for_simulator_in_dev():
    # Default dev posture (REQUIRE_OVERLAY_AUTH off): the simulator pushes without a token.
    client = APIClient()
    r = client.post(
        "/api/v1/car-orders/730/live-location/", {"lat": 41.3, "lng": 69.2}, format="json"
    )
    assert r.status_code == 200, r.content


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_live_location_write_rejects_anonymous_when_enforced():
    # AUDIT C3 fix: an unauthenticated caller can no longer write any order's position.
    client = APIClient()
    r = client.post(
        "/api/v1/car-orders/730/live-location/", {"lat": 41.3, "lng": 69.2}, format="json"
    )
    assert r.status_code in (401, 403)
    assert not OrderLiveLocation.objects.filter(order_id=730).exists()  # nothing written


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_live_location_write_allowed_for_owning_driver_when_enforced(monkeypatch):
    OrderMeta.objects.create(
        order_id=732, driver_id=671, trip_state=OrderMeta.TripState.ASSIGNED
    )
    client = _auth_as(monkeypatch, 671)  # the order's assigned driver
    r = client.post(
        "/api/v1/car-orders/732/live-location/", {"lat": 41.3, "lng": 69.2}, format="json"
    )
    assert r.status_code == 200, r.content


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_meta_post_strips_assignment_fields_for_non_dispatcher(monkeypatch):
    # AUDIT C3/M2 fix: a non-dispatcher can't self-assign a driver / flip dispatchable
    # via the feature-overlay upsert. The coords still save; the privileged fields drop.
    client = _auth_as(monkeypatch, 5)  # plain authenticated user, no perms
    r = client.post(
        "/api/v1/car-orders/740/meta/",
        {"origin_lat": 41.3, "origin_lng": 69.2, "driver_id": 999, "dispatchable": True},
        format="json",
    )
    assert r.status_code == 200, r.content
    meta = OrderMeta.objects.get(order_id=740)
    assert meta.origin_lat == 41.3  # benign field saved
    assert meta.driver_id is None and meta.dispatchable is False  # privileged fields ignored


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_meta_post_allows_dispatcher_to_set_assignment(monkeypatch):
    client = _auth_as(monkeypatch, 1, perms=["car_order:approve"])  # dispatcher
    r = client.post(
        "/api/v1/car-orders/741/meta/", {"dispatchable": True}, format="json"
    )
    assert r.status_code == 200, r.content
    assert OrderMeta.objects.get(order_id=741).dispatchable is True
