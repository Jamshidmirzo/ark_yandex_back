"""Additional unit tests for the overlay order service
(``car_orders.services.overlay``) — gap-fill for ``test_overlay_service.py``.

Covers the (re)start-from-terminal branch, the «don't rewind an in-progress trip
on a double-tap» invariant, the requeue live-location teardown, and the
service-level extend conflict + reassign claim-clearing.
"""

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from car_orders.models import OrderLiveLocation, OrderMeta
from car_orders.services import overlay

TS = OrderMeta.TripState


def _meta(order_id, **kwargs):
    return OrderMeta.objects.create(order_id=order_id, **kwargs)


# ---- claim: terminal restart + no-rewind ----------------------------------

@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_claim_restarts_from_a_terminal_state():
    _meta(900, overlay_claimed=False, driver_id=None, trip_state=TS.COMPLETED, returning=True)
    meta = overlay.claim(900, driver_id=5)
    assert meta.trip_state == TS.ASSIGNED
    assert meta.returning is False
    assert meta.driver_id == 5


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_claim_does_not_rewind_an_in_progress_trip():
    # A double-tap by the SAME driver must be idempotent — never rewind to ASSIGNED.
    _meta(900, overlay_claimed=True, driver_id=5, trip_state=TS.IN_TRIP)
    meta = overlay.claim(900, driver_id=5)
    assert meta.trip_state == TS.IN_TRIP


# ---- release: requeue still clears the live marker ------------------------

@pytest.mark.django_db
def test_release_with_requeue_clears_live_location():
    _meta(900, overlay_claimed=True, driver_id=5, trip_state=TS.IN_TRIP)
    OrderLiveLocation.objects.create(order_id=900, lat=41.3, lng=69.2, last_seen=timezone.now())
    meta = overlay.release(900, requeue=True)
    assert meta.trip_state == TS.ASSIGNED
    assert meta.dispatchable is True
    assert meta.driver_id is None
    assert not OrderLiveLocation.objects.filter(order_id=900).exists()


# ---- reassign: clears every claim field -----------------------------------

@pytest.mark.django_db
def test_reassign_clears_all_claim_fields():
    _meta(
        900, overlay_claimed=True, driver_id=5, car_id=3, car_label="Damas (01A)",
        trip_state=TS.IN_TRIP, returning=True,
    )
    meta = overlay.reassign(900)
    assert meta.overlay_claimed is False
    assert meta.driver_id is None
    assert meta.car_id is None
    assert meta.car_label == ""
    assert meta.returning is False
    assert meta.dispatchable is True


# ---- extend: service-level conflict warning -------------------------------

@pytest.mark.django_db
def test_extend_flags_conflict_with_next_window():
    base = timezone.now() + timedelta(days=1)
    # Order A is being extended; order B sits in the driver's next window.
    _meta(900, driver_id=5, trip_state=TS.ASSIGNED, estimated_duration=120,
          planned_datetime=base, service_time=0)
    _meta(901, driver_id=5, trip_state=TS.ASSIGNED, estimated_duration=120,
          planned_datetime=base + timedelta(hours=3), service_time=0)
    # Extend A by 3h → [base, base+5h] now collides with B at base+3h.
    meta, conflict = overlay.extend(900, 180)
    assert meta.estimated_duration == 300
    assert conflict is not None
    assert conflict["order_id"] == 901
