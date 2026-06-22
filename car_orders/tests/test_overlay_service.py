"""Unit tests for the overlay order service (``car_orders.services.overlay``).

Exercise claim / release / reassign / extend on ``OrderMeta`` directly at the
service layer (no HTTP), the granular counterpart to the overlay API tests.
"""

import pytest
from django.test import override_settings
from django.utils import timezone

from car_orders.models import OrderLiveLocation, OrderMeta
from car_orders.services import overlay

TS = OrderMeta.TripState


def _meta(order_id, **kwargs):
    return OrderMeta.objects.create(order_id=order_id, **kwargs)


# ---- claim ----------------------------------------------------------------

@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_claim_assigns_a_free_order():
    _meta(900)  # no claim yet, no coords → no route push
    meta = overlay.claim(900, driver_id=5, car_id=3, car_label="Damas (01A)")
    assert meta.overlay_claimed is True
    assert meta.driver_id == 5
    assert meta.trip_state == TS.ASSIGNED


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_claim_snapshots_driver_name_and_phone():
    # The requester can't read HR /employees/, so the driver's name + phone are
    # snapshotted onto the meta at claim and served inline — «who took my order».
    _meta(900)
    meta = overlay.claim(
        900, driver_id=5, car_label="Damas (01A)",
        driver_name="Иван Водитель", driver_phone="+998901234567",
    )
    assert meta.driver_name == "Иван Водитель"
    assert meta.driver_phone == "+998901234567"


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_reclaim_without_snapshot_keeps_existing():
    # An idempotent re-claim that omits the snapshot must not wipe a good one.
    _meta(900)
    overlay.claim(900, driver_id=5, driver_name="Иван", driver_phone="+99890")
    meta = overlay.claim(900, driver_id=5)
    assert meta.driver_name == "Иван"
    assert meta.driver_phone == "+99890"


@pytest.mark.django_db
def test_claim_rejects_order_taken_by_another_driver():
    _meta(900, overlay_claimed=True, driver_id=9, trip_state=TS.ASSIGNED)
    with pytest.raises(overlay.OverlayError) as exc:
        overlay.claim(900, driver_id=5)
    assert exc.value.code == "ALREADY_CLAIMED"


@pytest.mark.django_db
def test_claim_rejects_driver_with_another_active_order():
    _meta(901, driver_id=5, trip_state=TS.IN_TRIP)  # driver already busy
    _meta(902)
    with pytest.raises(overlay.OverlayError) as exc:
        overlay.claim(902, driver_id=5)
    assert exc.value.code == "DRIVER_BUSY"


# ---- release --------------------------------------------------------------

@pytest.mark.django_db
def test_release_terminal_by_default():
    _meta(900, overlay_claimed=True, driver_id=5, trip_state=TS.IN_TRIP)
    OrderLiveLocation.objects.create(order_id=900, lat=41.3, lng=69.2, last_seen=timezone.now())
    meta = overlay.release(900)
    assert meta.trip_state == TS.CANCELLED
    assert meta.driver_id is None
    assert not OrderLiveLocation.objects.filter(order_id=900).exists()


@pytest.mark.django_db
def test_release_with_requeue_returns_to_queue():
    _meta(900, overlay_claimed=True, driver_id=5, trip_state=TS.IN_TRIP)
    meta = overlay.release(900, requeue=True)
    assert meta.trip_state == TS.ASSIGNED
    assert meta.dispatchable is True
    assert meta.driver_id is None


@pytest.mark.django_db
def test_release_is_idempotent_when_missing():
    assert overlay.release(404) is None


# ---- reassign -------------------------------------------------------------

@pytest.mark.django_db
def test_reassign_returns_order_to_queue():
    _meta(900, overlay_claimed=True, driver_id=5, trip_state=TS.IN_TRIP)
    meta = overlay.reassign(900)
    assert meta.trip_state == TS.ASSIGNED
    assert meta.dispatchable is True
    assert meta.driver_id is None


@pytest.mark.django_db
def test_reassign_records_excluded_driver():
    """The driver we took the order OFF is remembered so auto-dispatch can't give
    it straight back to them."""
    _meta(900, overlay_claimed=True, driver_id=5, trip_state=TS.IN_TRIP)
    meta = overlay.reassign(900)
    assert meta.excluded_driver_ids == [5]
    # A second reassign off a different driver appends; no duplicates.
    meta.driver_id = 7
    meta.save()
    meta = overlay.reassign(900)
    assert meta.excluded_driver_ids == [5, 7]
    meta.driver_id = 5
    meta.save()
    meta = overlay.reassign(900)
    assert meta.excluded_driver_ids == [5, 7]  # 5 not duplicated


@pytest.mark.django_db
def test_reassign_missing_order():
    with pytest.raises(overlay.OverlayError) as exc:
        overlay.reassign(404)
    assert exc.value.code == "NOT_FOUND"


# ---- extend ---------------------------------------------------------------

@pytest.mark.django_db
def test_extend_adds_minutes_and_reports_no_conflict():
    _meta(
        900, driver_id=5, trip_state=TS.ASSIGNED, estimated_duration=60,
        planned_datetime=timezone.now() + timezone.timedelta(days=1), service_time=0,
    )
    meta, conflict = overlay.extend(900, 30)
    assert meta.estimated_duration == 90
    assert conflict is None


@pytest.mark.django_db
def test_extend_rejects_nonpositive_minutes():
    _meta(900, estimated_duration=60)
    with pytest.raises(overlay.OverlayError) as exc:
        overlay.extend(900, 0)
    assert exc.value.code == "VALIDATION"


@pytest.mark.django_db
def test_extend_with_no_prior_duration_starts_from_zero():
    # A freshly-created order has no estimated_duration — «продлить» must still
    # work, establishing the window from 0 rather than 400-ing.
    _meta(900)
    meta, conflict = overlay.extend(900, 15)
    assert meta.estimated_duration == 15
    assert conflict is None


@pytest.mark.django_db
def test_extend_rejects_missing_order():
    with pytest.raises(overlay.OverlayError) as exc:
        overlay.extend(999, 15)
    assert exc.value.code == "VALIDATION"
