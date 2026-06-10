"""Tests for the overlay feature endpoints (hybrid/gateway setup).

These hit the LOCAL views mounted before the gateway catch-all and work purely
on :class:`OrderMeta` — no demo backend / login needed (the views are AllowAny).
Kept separate from ``tests.py`` whose ``env`` fixture logs in through the gateway
(unreachable here)."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from car_orders import scheduling
from car_orders.models import OrderLiveLocation, OrderMeta


@pytest.mark.django_db
def test_extend_adds_minutes_and_flags_next_window_conflict():
    """Extend bumps estimated_duration and warns when the pushed-out end now
    overlaps the driver's next order (within the travel buffer)."""
    client = APIClient()
    start = timezone.now() + timedelta(hours=1)
    OrderMeta.objects.create(
        order_id=501,
        driver_id=9,
        planned_datetime=start,
        estimated_duration=60,
        overlay_claimed=True,
        trip_state=OrderMeta.TripState.IN_TRIP,
    )
    # Next order starts 95 min after A's start → 35 min after A's original end,
    # clear of the 30-min buffer. Extending A by 30 min eats that gap.
    OrderMeta.objects.create(
        order_id=502,
        driver_id=9,
        planned_datetime=start + timedelta(minutes=95),
        estimated_duration=60,
        overlay_claimed=True,
        trip_state=OrderMeta.TripState.ASSIGNED,
    )

    r = client.post("/api/v1/car-orders/501/extend/", {"minutes": 30}, format="json")

    assert r.status_code == 200, r.content
    assert r.data["ok"] is True
    assert OrderMeta.objects.get(order_id=501).estimated_duration == 90
    assert r.data["conflict"] is not None
    assert r.data["conflict"]["order_id"] == 502


@pytest.mark.django_db
def test_extend_without_conflict_returns_null_conflict():
    client = APIClient()
    start = timezone.now() + timedelta(hours=1)
    OrderMeta.objects.create(
        order_id=510,
        driver_id=7,
        planned_datetime=start,
        estimated_duration=60,
        overlay_claimed=True,
        trip_state=OrderMeta.TripState.IN_TRIP,
    )
    r = client.post("/api/v1/car-orders/510/extend/", {"minutes": 15}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["conflict"] is None
    assert OrderMeta.objects.get(order_id=510).estimated_duration == 75


@pytest.mark.django_db
def test_extend_rejects_non_positive_minutes():
    client = APIClient()
    OrderMeta.objects.create(order_id=503, estimated_duration=60)
    r = client.post("/api/v1/car-orders/503/extend/", {"minutes": 0}, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_extend_requires_existing_window():
    client = APIClient()
    r = client.post("/api/v1/car-orders/999/extend/", {"minutes": 30}, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_reassign_frees_the_overlay_claim():
    client = APIClient()
    OrderMeta.objects.create(
        order_id=504,
        driver_id=9,
        car_id=3,
        car_label="Cobalt (01A777AA)",
        overlay_claimed=True,
        trip_state=OrderMeta.TripState.IN_TRIP,
    )
    r = client.post("/api/v1/car-orders/504/reassign/", {}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["ok"] is True
    meta = OrderMeta.objects.get(order_id=504)
    assert meta.overlay_claimed is False
    assert meta.driver_id is None
    assert meta.car_id is None
    assert meta.trip_state == OrderMeta.TripState.CANCELLED


@pytest.mark.django_db
def test_reassign_missing_meta_is_bad_request():
    client = APIClient()
    r = client.post("/api/v1/car-orders/888/reassign/", {}, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_meta_needs_reassign_when_current_trip_overruns():
    """The driver's current trip is overrunning → the next order's projected start
    blows past its latest_start, so it's at risk and should be reassigned."""
    now = timezone.now()
    OrderMeta.objects.create(  # current trip, due to finish 1h ago, still in progress
        order_id=601,
        driver_id=11,
        planned_datetime=now - timedelta(hours=2),
        estimated_duration=60,
        overlay_claimed=True,
        trip_state=OrderMeta.TripState.IN_TRIP,
    )
    nxt = OrderMeta.objects.create(
        order_id=602,
        driver_id=11,
        planned_datetime=now,
        estimated_duration=60,
        latest_start=now - timedelta(minutes=10),
        overlay_claimed=True,
        trip_state=OrderMeta.TripState.ASSIGNED,
    )
    assert scheduling.meta_needs_reassign(nxt, now) is True
    nxt.latest_start = now + timedelta(hours=5)
    nxt.save(update_fields=["latest_start"])
    assert scheduling.meta_needs_reassign(nxt, now) is False


@pytest.mark.django_db
def test_at_risk_and_is_late_exposed_in_meta():
    """The overlay serializer exposes at_risk / is_late so the UI can flag it."""
    client = APIClient()
    now = timezone.now()
    OrderMeta.objects.create(
        order_id=620,
        driver_id=13,
        planned_datetime=now - timedelta(minutes=20),  # pickup time already passed
        estimated_duration=60,
        overlay_claimed=True,
        trip_state=OrderMeta.TripState.ASSIGNED,  # accepted but not departed
    )
    data = client.get("/api/v1/car-orders/620/meta/").json()
    assert data["is_late"] is True
    assert "at_risk" in data


@pytest.mark.django_db
def test_live_location_cleared_on_terminal_trip_state():
    client = APIClient()
    OrderMeta.objects.create(order_id=610, driver_id=12, trip_state=OrderMeta.TripState.IN_TRIP)
    OrderLiveLocation.objects.create(order_id=610, lat=41.3, lng=69.2, last_seen=timezone.now())
    r = client.post(
        "/api/v1/car-orders/610/trip-state/", {"trip_state": "completed"}, format="json"
    )
    assert r.status_code == 200, r.content
    assert not OrderLiveLocation.objects.filter(order_id=610).exists()


@pytest.mark.django_db
def test_live_location_cleared_on_reassign():
    client = APIClient()
    OrderMeta.objects.create(
        order_id=611, driver_id=12, overlay_claimed=True, trip_state=OrderMeta.TripState.IN_TRIP
    )
    OrderLiveLocation.objects.create(order_id=611, lat=41.3, lng=69.2, last_seen=timezone.now())
    r = client.post("/api/v1/car-orders/611/reassign/", {}, format="json")
    assert r.status_code == 200, r.content
    assert not OrderLiveLocation.objects.filter(order_id=611).exists()


@pytest.mark.django_db
def test_cannot_start_second_trip_while_one_is_active():
    """A driver in one car/one place can't START a second order while another is
    already being driven."""
    client = APIClient()
    OrderMeta.objects.create(
        order_id=630, driver_id=15, overlay_claimed=True, trip_state=OrderMeta.TripState.IN_TRIP
    )
    OrderMeta.objects.create(
        order_id=631, driver_id=15, overlay_claimed=True, trip_state=OrderMeta.TripState.ASSIGNED
    )
    r = client.post(
        "/api/v1/car-orders/631/trip-state/", {"trip_state": "to_client"}, format="json"
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ACTIVE_TRIP_EXISTS"
    # advancing the already-started first order still works
    r2 = client.post(
        "/api/v1/car-orders/630/trip-state/", {"trip_state": "at_destination"}, format="json"
    )
    assert r2.status_code == 200, r2.content


@pytest.mark.django_db
def test_order_watchdog_release_frees_late_unstarted_but_not_active():
    now = timezone.now()
    OrderMeta.objects.create(  # late, accepted, not started → should be released
        order_id=640,
        driver_id=16,
        overlay_claimed=True,
        planned_datetime=now - timedelta(hours=1),
        estimated_duration=60,
        trip_state=OrderMeta.TripState.ASSIGNED,
    )
    OrderMeta.objects.create(  # mid-trip → must NOT be yanked
        order_id=641,
        driver_id=17,
        overlay_claimed=True,
        planned_datetime=now - timedelta(hours=1),
        estimated_duration=60,
        trip_state=OrderMeta.TripState.IN_TRIP,
    )
    call_command("order_watchdog", "--release", stdout=StringIO())
    m640 = OrderMeta.objects.get(order_id=640)
    m641 = OrderMeta.objects.get(order_id=641)
    assert m640.overlay_claimed is False and m640.driver_id is None
    assert m641.overlay_claimed is True and m641.driver_id == 17
