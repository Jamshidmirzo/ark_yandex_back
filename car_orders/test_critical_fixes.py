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
