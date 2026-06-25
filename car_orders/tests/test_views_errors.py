"""API-level tests for the overlay error→HTTP contract and the dispatcher-only
gates under enforcement (REQUIRE_OVERLAY_AUTH=True).

Pins the *current* status-code mapping (so a regression is caught) and proves the
ReassignView / OrderMetaView-DELETE / AutoDispatchView-POST gates actually close
for a plain driver while opening for a dispatcher. The overlay views answer
locally (mounted before the gateway catch-all), so no URL override is needed.

The demo-bridged ``auth_client`` factory + role bundles live in ``conftest.py``.
"""

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from car_orders.models import OrderMeta
from car_orders.tests.conftest import DISPATCHER, DRIVER

TS = OrderMeta.TripState


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
def test_dispatcher_assign_snapshots_driver_from_hr(monkeypatch):
    # A dispatcher manual assign sends NO driver_name/phone (it doesn't hold the chosen
    # driver's HR record), so the view fills the snapshot server-side from upstream HR —
    # else the assigned order shows a blank driver on the live-track / detail card.
    import car_orders.views as views

    monkeypatch.setattr(
        views, "_driver_snapshot", lambda request, driver_id: ("Иван Водитель", "+998901234567")
    )
    OrderMeta.objects.create(order_id=905)
    r = APIClient().post(
        "/api/v1/car-orders/905/overlay-claim/", {"driver_id": 7, "car_id": 1}, format="json"
    )
    assert r.status_code == 200
    meta = OrderMeta.objects.get(order_id=905)
    assert meta.driver_name == "Иван Водитель"
    assert meta.driver_phone == "+998901234567"


@pytest.mark.django_db
def test_self_claim_snapshot_is_not_overwritten_by_hr_fetch(monkeypatch):
    # A driver self-claim already carries its own name/phone, so the view must NOT hit
    # the HR fetch — the client-supplied snapshot wins (and we save a round-trip).
    import car_orders.views as views

    called = {"hit": False}

    def _boom(request, driver_id):
        called["hit"] = True
        return ("Should", "NotWin")

    monkeypatch.setattr(views, "_driver_snapshot", _boom)
    OrderMeta.objects.create(order_id=906)
    r = APIClient().post(
        "/api/v1/car-orders/906/overlay-claim/",
        {"driver_id": 8, "car_id": 1, "driver_name": "Сам Себя", "driver_phone": "+99890"},
        format="json",
    )
    assert r.status_code == 200
    meta = OrderMeta.objects.get(order_id=906)
    assert meta.driver_name == "Сам Себя"
    assert meta.driver_phone == "+99890"
    assert called["hit"] is False


@pytest.mark.django_db
def test_overlay_extend_validation_is_400():
    OrderMeta.objects.create(order_id=903, estimated_duration=60)
    r = APIClient().post("/api/v1/car-orders/903/extend/", {"minutes": 0}, format="json")
    assert r.status_code == 400
    assert r.data["error"]["code"] == "VALIDATION"


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_trip_state_permission_denied_is_403(auth_client):
    OrderMeta.objects.create(
        order_id=700, driver_id=42, trip_state=TS.ASSIGNED,
        origin_lat=41.31, origin_lng=69.24, address_lat=41.35, address_lng=69.29,
    )
    # A real driver (passes the OverlayDriverOrDispatcher class gate) who is NOT this
    # order's assignee → the service-layer actor check denies with PERMISSION_DENIED.
    client = auth_client(perms=DRIVER, user_id=671)
    r = client.post("/api/v1/car-orders/700/trip-state/", {"trip_state": "to_client"}, format="json")
    assert r.status_code == 403
    assert r.data["error"]["code"] == "PERMISSION_DENIED"


# ---- ReassignView gate -----------------------------------------------------

@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_reassign_forbidden_for_plain_driver(auth_client):
    client = auth_client(user_id=5)  # no perms
    r = client.post("/api/v1/car-orders/999/reassign/")
    assert r.status_code == 403


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_reassign_allowed_for_dispatcher(auth_client):
    OrderMeta.objects.create(order_id=800, overlay_claimed=True, driver_id=5, trip_state=TS.IN_TRIP)
    client = auth_client(perms=DISPATCHER, user_id=1)
    r = client.post("/api/v1/car-orders/800/reassign/")
    assert r.status_code == 200
    assert r.data["ok"] is True
    assert OrderMeta.objects.get(order_id=800).driver_id is None


# ---- OrderMetaView DELETE gate --------------------------------------------

@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_meta_delete_forbidden_for_plain_driver(auth_client):
    OrderMeta.objects.create(order_id=810, driver_id=5, trip_state=TS.ASSIGNED)
    client = auth_client(user_id=5)
    r = client.delete("/api/v1/car-orders/810/meta/")
    assert r.status_code == 403
    assert OrderMeta.objects.filter(order_id=810).exists()  # not deleted


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_meta_delete_allowed_for_dispatcher(auth_client):
    OrderMeta.objects.create(order_id=811, driver_id=5, trip_state=TS.ASSIGNED)
    client = auth_client(perms=DISPATCHER, user_id=1)
    r = client.delete("/api/v1/car-orders/811/meta/")
    assert r.status_code == 200
    assert not OrderMeta.objects.filter(order_id=811).exists()


# ---- AutoDispatchView POST gate -------------------------------------------

@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_auto_dispatch_post_forbidden_for_plain_driver(auth_client):
    client = auth_client(user_id=5)
    r = client.post("/api/v1/car-orders/auto-dispatch/", {"enabled": True}, format="json")
    assert r.status_code == 403


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_auto_dispatch_get_allowed_for_any_authenticated(auth_client):
    client = auth_client(user_id=5)
    r = client.get("/api/v1/car-orders/auto-dispatch/")
    assert r.status_code == 200
    assert "effective" in r.data
