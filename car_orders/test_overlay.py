"""Tests for the overlay feature endpoints (hybrid/gateway setup).

These hit the LOCAL views mounted before the gateway catch-all and work purely
on :class:`OrderMeta` — no demo backend / login needed (the views are AllowAny).
Kept separate from ``tests.py`` whose ``env`` fixture logs in through the gateway
(unreachable here)."""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from car_orders.models import OrderMeta


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
