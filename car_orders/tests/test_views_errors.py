"""API-level tests for the overlay error→HTTP contract and the dispatcher-only
gates under enforcement (REQUIRE_OVERLAY_AUTH=True).

Pins the *current* status-code mapping (so a regression is caught) and proves the
ReassignView / OrderMetaView-DELETE / AutoDispatchView-POST gates actually close
for a plain driver while opening for a dispatcher. The overlay views answer
locally (mounted before the gateway catch-all), so no URL override is needed.
"""

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

import config.auth as demo_auth
from car_orders.models import OrderMeta

TS = OrderMeta.TripState


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


# ---- error→HTTP contract (open dev) ---------------------------------------

@pytest.mark.django_db
def test_overlay_claim_driver_busy_is_400():
    # Pins current behaviour: DRIVER_BUSY maps to 400 (NOT 409). A window/time
    # conflict on the NATIVE path is 409 — see test_workflow.test_overlapping_window.
    OrderMeta.objects.create(order_id=901, driver_id=5, trip_state=TS.IN_TRIP)
    OrderMeta.objects.create(order_id=902)
    r = APIClient().post(
        "/api/v1/car-orders/902/overlay-claim/", {"driver_id": 5}, format="json"
    )
    assert r.status_code == 400
    assert r.data["error"]["code"] == "DRIVER_BUSY"


@pytest.mark.django_db
def test_overlay_extend_validation_is_400():
    OrderMeta.objects.create(order_id=903, estimated_duration=60)
    r = APIClient().post("/api/v1/car-orders/903/extend/", {"minutes": 0}, format="json")
    assert r.status_code == 400
    assert r.data["error"]["code"] == "VALIDATION"


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_trip_state_permission_denied_is_403(monkeypatch):
    OrderMeta.objects.create(
        order_id=700, driver_id=42, trip_state=TS.ASSIGNED,
        origin_lat=41.31, origin_lng=69.24, address_lat=41.35, address_lng=69.29,
    )
    client = _auth_as(monkeypatch, 671)  # a different driver, not a dispatcher
    r = client.post("/api/v1/car-orders/700/trip-state/", {"trip_state": "to_client"}, format="json")
    assert r.status_code == 403
    assert r.data["error"]["code"] == "PERMISSION_DENIED"


# ---- ReassignView gate -----------------------------------------------------

@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_reassign_forbidden_for_plain_driver(monkeypatch):
    client = _auth_as(monkeypatch, 5)  # no perms
    r = client.post("/api/v1/car-orders/999/reassign/")
    assert r.status_code == 403


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_reassign_allowed_for_dispatcher(monkeypatch):
    OrderMeta.objects.create(order_id=800, overlay_claimed=True, driver_id=5, trip_state=TS.IN_TRIP)
    client = _auth_as(monkeypatch, 1, perms=["car_order:approve"])
    r = client.post("/api/v1/car-orders/800/reassign/")
    assert r.status_code == 200
    assert r.data["ok"] is True
    assert OrderMeta.objects.get(order_id=800).driver_id is None


# ---- OrderMetaView DELETE gate --------------------------------------------

@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_meta_delete_forbidden_for_plain_driver(monkeypatch):
    OrderMeta.objects.create(order_id=810, driver_id=5, trip_state=TS.ASSIGNED)
    client = _auth_as(monkeypatch, 5)
    r = client.delete("/api/v1/car-orders/810/meta/")
    assert r.status_code == 403
    assert OrderMeta.objects.filter(order_id=810).exists()  # not deleted


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_meta_delete_allowed_for_dispatcher(monkeypatch):
    OrderMeta.objects.create(order_id=811, driver_id=5, trip_state=TS.ASSIGNED)
    client = _auth_as(monkeypatch, 1, perms=["car_order:approve"])
    r = client.delete("/api/v1/car-orders/811/meta/")
    assert r.status_code == 200
    assert not OrderMeta.objects.filter(order_id=811).exists()


# ---- AutoDispatchView POST gate -------------------------------------------

@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_auto_dispatch_post_forbidden_for_plain_driver(monkeypatch):
    client = _auth_as(monkeypatch, 5)
    r = client.post("/api/v1/car-orders/auto-dispatch/", {"enabled": True}, format="json")
    assert r.status_code == 403


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_auto_dispatch_get_allowed_for_any_authenticated(monkeypatch):
    client = _auth_as(monkeypatch, 5)
    r = client.get("/api/v1/car-orders/auto-dispatch/")
    assert r.status_code == 200
    assert "effective" in r.data
