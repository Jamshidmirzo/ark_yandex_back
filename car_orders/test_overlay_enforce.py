"""Focused tests for the overlay-claim overlap policy (issue: one driver held
10+ overlapping orders). Policy «block for the system, soft for the driver»:
  • driver self-claim (no enforce)  → SOFT (200 + warning, assigned) — gap-filling
  • dispatcher/auto (enforce)        → HARD 409 OVERLAP_CONFLICT on a real overlap
  • dispatcher force (enforce+force) → overrides the block (200, assigned)

These bypass the demo-login fixture in tests.py (which needs seeded auth) — they
hit the local overlay endpoint directly (REQUIRE_OVERLAY_AUTH off by default).
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from car_orders.models import OrderMeta


def _committed(order_id, driver_id, start, minutes):
    return OrderMeta.objects.create(
        order_id=order_id,
        driver_id=driver_id,
        overlay_claimed=True,
        trip_state=OrderMeta.TripState.ASSIGNED,
        planned_datetime=start,
        estimated_duration=minutes,
        service_time=0,
    )


def _awaiting(order_id, start, minutes):
    return OrderMeta.objects.create(
        order_id=order_id,
        driver_id=None,
        planned_datetime=start,
        estimated_duration=minutes,
        service_time=0,
    )


def _claim(order_id, driver_id, **extra):
    return APIClient().post(
        f"/api/v1/car-orders/{order_id}/overlay-claim/",
        {"driver_id": driver_id, "car_id": 1, "car_label": "Damas (01A001AA)", **extra},
        format="json",
    )


@pytest.mark.django_db
def test_enforce_blocks_real_overlap():
    t0 = timezone.now().replace(microsecond=0)
    _committed(5000, 99, t0, 120)  # 12:00–14:00 driving
    _awaiting(5001, t0 + timedelta(minutes=30), 60)  # 12:30–13:30 → overlaps

    r = _claim(5001, 99, enforce=True)
    assert r.status_code == 409, r.content
    assert r.data["error"]["code"] == "OVERLAP_CONFLICT"
    assert r.data["error"]["details"]["order_id"] == 5000
    # Not assigned — the system refused to double-book.
    assert OrderMeta.objects.get(order_id=5001).driver_id is None


@pytest.mark.django_db
def test_dispatcher_force_overrides_block():
    t0 = timezone.now().replace(microsecond=0)
    _committed(5100, 99, t0, 120)
    _awaiting(5101, t0 + timedelta(minutes=30), 60)

    r = _claim(5101, 99, enforce=True, force=True)
    assert r.status_code == 200, r.content
    assert r.data["conflict"] is not None  # surfaced for the audit trail
    assert OrderMeta.objects.get(order_id=5101).driver_id == 99


@pytest.mark.django_db
def test_driver_selfclaim_stays_soft():
    t0 = timezone.now().replace(microsecond=0)
    _committed(6000, 77, t0, 120)
    _awaiting(6001, t0 + timedelta(minutes=30), 60)

    # No enforce → gap-filling: assigned anyway, conflict is a warning.
    r = _claim(6001, 77)
    assert r.status_code == 200, r.content
    assert r.data["conflict"] is not None
    assert OrderMeta.objects.get(order_id=6001).driver_id == 77


@pytest.mark.django_db
def test_enforce_allows_non_overlapping():
    t0 = timezone.now().replace(microsecond=0)
    _committed(7000, 55, t0, 60)  # ends t0+1h
    _awaiting(7001, t0 + timedelta(hours=5), 60)  # far later → no conflict

    r = _claim(7001, 55, enforce=True)
    assert r.status_code == 200, r.content
    assert OrderMeta.objects.get(order_id=7001).driver_id == 55


@pytest.mark.django_db
def test_enforce_ignores_parked_gap_order():
    """A driver PARKED on a long shoot (at_destination) is idle → a gap order must
    still be assignable even with enforce (parked states are excluded from the
    conflict)."""
    t0 = timezone.now().replace(microsecond=0)
    parked = _committed(8000, 33, t0, 240)  # 4h shoot
    parked.trip_state = OrderMeta.TripState.AT_DESTINATION
    parked.save(update_fields=["trip_state"])
    _awaiting(8001, t0 + timedelta(minutes=30), 60)  # overlaps the shoot window

    r = _claim(8001, 33, enforce=True)
    assert r.status_code == 200, r.content  # gap-fill survives the block
    assert OrderMeta.objects.get(order_id=8001).driver_id == 33
