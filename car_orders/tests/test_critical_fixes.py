"""Tests for the critical mobile-integration fixes: reassign/release re-queue,
shift hardening, and the authoritative TripStateView (transitions + geofence).
Bypass the demo-login fixture — hit the local overlay endpoints directly."""

import pytest
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from car_orders.models import DriverPosition, OrderLiveLocation, OrderMeta

TS = OrderMeta.TripState


def _c():
    return APIClient()


# ---- Fix 3: reassign / release put the order BACK in the queue ----------------

@pytest.mark.django_db
def test_reassign_requeues_not_cancelled():
    OrderMeta.objects.create(order_id=400, driver_id=9, overlay_claimed=True, trip_state=TS.IN_TRIP)
    r = _c().post("/api/v1/car-orders/400/reassign/", {}, format="json")
    assert r.status_code == 200, r.content
    m = OrderMeta.objects.get(order_id=400)
    assert m.driver_id is None and m.dispatchable is True
    assert m.trip_state == TS.ASSIGNED  # non-terminal → re-dispatchable


@pytest.mark.django_db
def test_release_requeue_flag_vs_cancel():
    OrderMeta.objects.create(order_id=401, driver_id=9, overlay_claimed=True, trip_state=TS.ASSIGNED)
    OrderMeta.objects.create(order_id=402, driver_id=9, overlay_claimed=True, trip_state=TS.ASSIGNED)
    # requeue → back to queue
    _c().post("/api/v1/car-orders/401/overlay-release/", {"requeue": True}, format="json")
    m1 = OrderMeta.objects.get(order_id=401)
    assert m1.driver_id is None and m1.dispatchable is True and m1.trip_state == TS.ASSIGNED
    # default → terminal cancel
    _c().post("/api/v1/car-orders/402/overlay-release/", {}, format="json")
    assert OrderMeta.objects.get(order_id=402).trip_state == TS.CANCELLED


# ---- Fix 2: shift requires car_type_id; can't end shift with active orders -----

@pytest.mark.django_db
def test_shift_requires_car_type():
    r = _c().patch(
        "/api/v1/car-orders/drivers/me/shift/",
        {"driver_id": 5, "car_id": 1, "car_model": "Damas"},
        format="json",
    )
    assert r.status_code == 400 and r.data["error"]["code"] == "VALIDATION"


@pytest.mark.django_db
def test_shift_end_blocked_with_active_order():
    _c().patch(
        "/api/v1/car-orders/drivers/me/shift/",
        {"driver_id": 5, "car_id": 1, "car_type_id": 4},
        format="json",
    )
    OrderMeta.objects.create(order_id=410, driver_id=5, trip_state=TS.IN_TRIP)
    r = _c().delete("/api/v1/car-orders/drivers/me/shift/?driver_id=5")
    assert r.status_code == 400 and r.data["error"]["code"] == "HAS_ACTIVE_ORDERS"


# ---- Fix 4: TripStateView is authoritative -----------------------------------

@pytest.mark.django_db
def test_transition_order_enforced():
    OrderMeta.objects.create(order_id=420, driver_id=7, trip_state=TS.ASSIGNED)
    # assigned → to_client is valid
    r = _c().post("/api/v1/car-orders/420/trip-state/", {"trip_state": "to_client"}, format="json")
    assert r.status_code == 200, r.content
    # to_client → in_trip SKIPS at_client → rejected
    r2 = _c().post("/api/v1/car-orders/420/trip-state/", {"trip_state": "in_trip"}, format="json")
    assert r2.status_code == 400 and r2.data["error"]["code"] == "INVALID_TRANSITION"


@pytest.mark.django_db
def test_completed_only_from_at_destination():
    OrderMeta.objects.create(order_id=421, driver_id=7, trip_state=TS.IN_TRIP)
    r = _c().post("/api/v1/car-orders/421/trip-state/", {"trip_state": "completed"}, format="json")
    assert r.status_code == 400 and r.data["error"]["code"] == "INVALID_TRANSITION"


@pytest.mark.django_db
def test_completed_blocked_with_pending_return():
    OrderMeta.objects.create(
        order_id=422, driver_id=7, trip_state=TS.AT_DESTINATION, has_return=True, returning=False,
    )
    r = _c().post("/api/v1/car-orders/422/trip-state/", {"trip_state": "completed"}, format="json")
    assert r.status_code == 400 and r.data["error"]["code"] == "INVALID_TRANSITION"


@override_settings(CAR_ORDER_ARRIVAL_GEOFENCE_M=100)
@pytest.mark.django_db
def test_arrival_geofence():
    OrderMeta.objects.create(
        order_id=423, driver_id=7, trip_state=TS.TO_CLIENT, origin_lat=41.3100, origin_lng=69.2400,
    )
    # driver FAR from the pickup → blocked
    DriverPosition.objects.create(driver_id=7, lat=41.40, lng=69.30, last_seen=timezone.now())
    r = _c().post("/api/v1/car-orders/423/trip-state/", {"trip_state": "at_client"}, format="json")
    assert r.status_code == 400 and r.data["error"]["code"] == "TOO_FAR"
    # driver AT the pickup (fresh) → allowed
    DriverPosition.objects.filter(driver_id=7).update(lat=41.3101, lng=69.2401, last_seen=timezone.now())
    r2 = _c().post("/api/v1/car-orders/423/trip-state/", {"trip_state": "at_client"}, format="json")
    assert r2.status_code == 200, r2.content


@override_settings(CAR_ORDER_ARRIVAL_GEOFENCE_M=0)
@pytest.mark.django_db
def test_geofence_disabled_allows_arrival():
    OrderMeta.objects.create(
        order_id=424, driver_id=7, trip_state=TS.TO_CLIENT, origin_lat=41.31, origin_lng=69.24,
    )
    r = _c().post("/api/v1/car-orders/424/trip-state/", {"trip_state": "at_client"}, format="json")
    assert r.status_code == 200, r.content  # no position, but geofence off


# ---- Fix: dispatcher's overlay-claim assigns to the CHOSEN driver, not the
#      acting dispatcher (regression: order silently claimed for the admin) ------

@pytest.mark.django_db
def test_dispatcher_claim_assigns_to_chosen_driver_not_admin():
    """An authenticated dispatcher assigns an order to driver #555. The order must
    land on #555, not on the dispatcher — even though the dispatcher is the caller."""
    from django.contrib.auth import get_user_model

    admin = get_user_model().objects.create(username="disp-admin", is_superuser=True)
    OrderMeta.objects.create(
        order_id=701, dispatchable=True,
        origin_lat=41.31, origin_lng=69.24, address_lat=41.34, address_lng=69.30,
    )
    client = APIClient()
    client.force_authenticate(admin)  # the caller is the dispatcher, id == admin.id
    r = client.post(
        "/api/v1/car-orders/701/overlay-claim/",
        {"driver_id": 555, "car_id": 1, "car_label": "Cobalt"},
        format="json",
    )
    assert r.status_code == 200, r.content
    meta = OrderMeta.objects.get(order_id=701)
    assert meta.driver_id == 555  # the chosen driver…
    assert meta.driver_id != admin.id  # …NOT the acting dispatcher


@pytest.mark.django_db
def test_assignee_driver_id_resolution(settings):
    """Unit-level: dispatcher → body id; driver-with-perms → own token id (a spoofed
    body id is ignored)."""
    from car_orders.permissions import assignee_driver_id

    class _Req:
        def __init__(self, data, user):
            self.data, self.user = data, user

    class _User:
        def __init__(self, uid, is_superuser=False, permissions=None):
            self.id, self.is_authenticated, self.is_superuser = uid, True, is_superuser
            if permissions is not None:
                self.permissions = permissions

    # Auth not enforced, but an authenticated dispatcher is the caller → body wins
    # (this is the exact bug: acting_driver_id would return the dispatcher's id).
    settings.REQUIRE_OVERLAY_AUTH = False
    assert assignee_driver_id(_Req({"driver_id": 42}, _User(1)), None) == 42

    # Auth enforced: a non-dispatcher driver can't claim FOR another (body ignored).
    settings.REQUIRE_OVERLAY_AUTH = True
    driver = _User(7, permissions={"car_order:create"})  # has perms, lacks approve
    assert assignee_driver_id(_Req({"driver_id": 99}, driver), None) == 7

    # Auth enforced: a dispatcher (superuser) assigns to the chosen driver.
    assert assignee_driver_id(_Req({"driver_id": 42}, _User(1, is_superuser=True)), None) == 42


# ---- Fix: rejecting an order tears down OUR overlay so it leaves the queue ------

@pytest.mark.django_db
def test_reject_hook_releases_overlay(monkeypatch):
    """A rejected (approved) order must drop out of the auto-dispatch queue, not keep
    getting auto-assigned. The reject hook proxies to demo, then releases our overlay."""
    from django.http import HttpResponse

    import config.gateway as gw
    from car_orders.dispatch import queue_orders

    # Pretend the demo backend accepted the reject (the hook only cleans up on 2xx).
    monkeypatch.setattr(
        gw, "gateway", lambda request, path: HttpResponse(b"{}", status=200, content_type="application/json")
    )
    OrderMeta.objects.create(
        order_id=771, dispatchable=True, trip_state=TS.ASSIGNED,
        origin_lat=41.31, origin_lng=69.24, address_lat=41.34, address_lng=69.30,
    )
    assert 771 in [m.order_id for m in queue_orders()]  # in the queue before reject

    r = _c().post("/api/v1/car-orders/771/reject/", {"reason": "не нужен"}, format="json")
    assert r.status_code == 200, r.content

    m = OrderMeta.objects.get(order_id=771)
    assert m.trip_state == TS.CANCELLED  # overlay torn down
    assert 771 not in [x.order_id for x in queue_orders()]  # left the queue
