"""Unit tests for the trip-state machine (``car_orders.services.trip_state``).

Exercise the rules directly at the service layer — no HTTP, auth or gateway — so
the invariants the old ``TripStateView`` enforced are pinned independently of the
(gateway-incompatible) integration suite.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from car_orders.models import (
    CarOrder,
    CarOrderActivity,
    DriverPosition,
    OrderLiveLocation,
    OrderMeta,
)
from car_orders.services import trip_state

TS = OrderMeta.TripState
User = get_user_model()


def _meta(state, *, order_id=900, driver_id=5, has_return=False, returning=False):
    return OrderMeta.objects.create(
        order_id=order_id, driver_id=driver_id, overlay_claimed=True, trip_state=state,
        origin_lat=41.31, origin_lng=69.24, address_lat=41.35, address_lng=69.29,
        has_return=has_return, returning=returning, return_lat=41.30, return_lng=69.20,
    )


def _fresh_position(driver_id, lat, lng):
    return DriverPosition.objects.create(
        driver_id=driver_id, lat=lat, lng=lng, last_seen=timezone.now()
    )


# ---- can_transition: the pure flow rules ----------------------------------

def test_can_transition_forward_flow():
    assert trip_state.can_transition(TS.ASSIGNED, TS.TO_CLIENT)
    assert trip_state.can_transition(TS.TO_CLIENT, TS.AT_CLIENT)
    assert trip_state.can_transition(TS.AT_CLIENT, TS.IN_TRIP)
    assert trip_state.can_transition(TS.AT_DESTINATION, TS.IN_TRIP)  # round-trip return leg
    assert trip_state.can_transition(TS.WAITING, TS.COMPLETED)


def test_can_transition_rejects_skips():
    assert not trip_state.can_transition(TS.ASSIGNED, TS.IN_TRIP)
    assert not trip_state.can_transition(TS.TO_CLIENT, TS.COMPLETED)


def test_can_transition_always_allows_same_state_and_cancel():
    assert trip_state.can_transition(TS.IN_TRIP, TS.IN_TRIP)  # idempotent re-tap
    assert trip_state.can_transition(TS.TO_CLIENT, TS.CANCELLED)  # cancel any time


# ---- validate: rule precedence + computed defaults ------------------------

@pytest.mark.django_db
def test_validate_returns_trip_state_default():
    m = _meta(TS.ASSIGNED)
    assert trip_state.validate(m, TS.TO_CLIENT) == {"trip_state": TS.TO_CLIENT}


@pytest.mark.django_db
def test_validate_flags_returning_on_return_leg():
    # Leaving the destination back into a moving stage = the RETURN leg started.
    m = _meta(TS.AT_DESTINATION, has_return=True, returning=False)
    assert trip_state.validate(m, TS.IN_TRIP) == {"trip_state": TS.IN_TRIP, "returning": True}


@pytest.mark.django_db
def test_validate_rejects_illegal_transition():
    m = _meta(TS.ASSIGNED)
    with pytest.raises(trip_state.TripStateError) as exc:
        trip_state.validate(m, TS.IN_TRIP)
    assert exc.value.code == "INVALID_TRANSITION"


@pytest.mark.django_db
def test_validate_blocks_complete_before_return_leg():
    m = _meta(TS.AT_DESTINATION, has_return=True, returning=False)
    with pytest.raises(trip_state.TripStateError) as exc:
        trip_state.validate(m, TS.COMPLETED)
    assert exc.value.code == "INVALID_TRANSITION"


@pytest.mark.django_db
def test_validate_only_assigned_driver_or_dispatcher():
    m = _meta(TS.ASSIGNED, driver_id=5)
    # A different driver is forbidden (403)…
    with pytest.raises(trip_state.TripStateError) as exc:
        trip_state.validate(m, TS.TO_CLIENT, actor_driver_id=9, is_dispatcher=False)
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.http_status == 403
    # …but a dispatcher may override.
    assert trip_state.validate(m, TS.TO_CLIENT, actor_driver_id=9, is_dispatcher=True)


@pytest.mark.django_db
def test_validate_rejects_advancing_a_completed_order():
    m = _meta(TS.COMPLETED)
    with pytest.raises(trip_state.TripStateError) as exc:
        trip_state.validate(m, TS.IN_TRIP)
    assert exc.value.code == "INVALID_STATUS"


# ---- geofence: arrival stages need a fresh, near-enough GPS fix -----------

@pytest.mark.django_db
def test_geofence_requires_fresh_gps():
    m = _meta(TS.TO_CLIENT, driver_id=5)  # no DriverPosition at all
    with pytest.raises(trip_state.TripStateError) as exc:
        trip_state.validate(m, TS.AT_CLIENT, actor_driver_id=None)
    assert exc.value.code == "NO_FRESH_GPS"


@pytest.mark.django_db
def test_geofence_rejects_arrival_when_too_far():
    m = _meta(TS.TO_CLIENT, driver_id=5)
    _fresh_position(5, 41.50, 69.50)  # ~25 km from the pickup → too far
    with pytest.raises(trip_state.TripStateError) as exc:
        trip_state.validate(m, TS.AT_CLIENT, actor_driver_id=None)
    assert exc.value.code == "TOO_FAR"


@pytest.mark.django_db
def test_geofence_passes_when_on_the_point():
    m = _meta(TS.TO_CLIENT, driver_id=5)
    _fresh_position(5, 41.31, 69.24)  # exactly on the pickup
    result = trip_state.validate(m, TS.AT_CLIENT, actor_driver_id=None)
    assert result == {"trip_state": TS.AT_CLIENT}


# ---- one moving trip at a time --------------------------------------------

@pytest.mark.django_db
def test_validate_blocks_second_moving_trip():
    _meta(TS.IN_TRIP, order_id=901, driver_id=5)  # already driving order 901
    gap = _meta(TS.AT_CLIENT, order_id=902, driver_id=5)  # parked on a gap order
    with pytest.raises(trip_state.TripStateError) as exc:
        trip_state.validate(gap, TS.IN_TRIP, actor_driver_id=None)
    assert exc.value.code == "ACTIVE_TRIP_EXISTS"


# ---- advance: validate + persist + side-effects ---------------------------

@pytest.mark.django_db
def test_advance_unknown_state():
    with pytest.raises(trip_state.TripStateError) as exc:
        trip_state.advance(900, "not-a-state")
    assert exc.value.code == "VALIDATION"


@pytest.mark.django_db
def test_advance_missing_order():
    with pytest.raises(trip_state.TripStateError) as exc:
        trip_state.advance(404, TS.TO_CLIENT)
    assert exc.value.code == "NOT_FOUND"


@override_settings(CAR_ORDER_OSRM_URL="")  # offline route path, no network
@pytest.mark.django_db
def test_advance_persists_the_new_state():
    _meta(TS.ASSIGNED)
    meta = trip_state.advance(900, TS.TO_CLIENT)
    assert meta.trip_state == TS.TO_CLIENT
    assert OrderMeta.objects.get(order_id=900).trip_state == TS.TO_CLIENT


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_advance_clears_live_location_on_terminal():
    _meta(TS.AT_DESTINATION)  # has_return=False → completing is allowed
    OrderLiveLocation.objects.create(order_id=900, lat=41.3, lng=69.2, last_seen=timezone.now())
    trip_state.advance(900, TS.COMPLETED)
    assert not OrderLiveLocation.objects.filter(order_id=900).exists()


# ---- advance → completed mirrors onto the native CarOrder ------------------
#
# The mobile client closes a trip ONLY via trip_state=completed (it never calls
# the native /start/ or /complete/ endpoints). An overlay-claimed order keeps its
# demo status at `awaiting_driver`, so without this reconciliation the native
# order would never reach `completed` — which is exactly why finishing on-location
# failed before. These pin the mirror + its idempotency.

def _native_order(status, driver):
    """A backing demo CarOrder whose pk an OrderMeta can point at."""
    return CarOrder.objects.create(
        created_by=driver,
        driver=driver,
        status=status,
        planned_datetime=timezone.now(),
        estimated_duration=timezone.timedelta(hours=1),
    )


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_advance_completed_reconciles_native_order():
    driver = User.objects.create(username="drv-reconcile")
    # Overlay-claimed → demo status stays `awaiting_driver` the whole trip.
    order = _native_order(CarOrder.Status.AWAITING_DRIVER, driver)
    _meta(TS.AT_DESTINATION, order_id=order.pk, driver_id=driver.id)

    trip_state.advance(order.pk, TS.COMPLETED, actor_driver_id=driver.id)

    order.refresh_from_db()
    assert order.status == CarOrder.Status.COMPLETED
    assert order.finished_at is not None
    assert order.started_at is not None  # stamped even though /start/ never ran
    assert CarOrderActivity.objects.filter(
        order=order, kind=CarOrderActivity.Kind.COMPLETED
    ).count() == 1


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_advance_completed_is_idempotent_for_terminal_native_order():
    driver = User.objects.create(username="drv-idem")
    order = _native_order(CarOrder.Status.COMPLETED, driver)  # already terminal
    _meta(TS.AT_DESTINATION, order_id=order.pk, driver_id=driver.id)

    trip_state.advance(order.pk, TS.COMPLETED, actor_driver_id=driver.id)

    # A terminal native order is left untouched → no second COMPLETED audit row.
    assert CarOrderActivity.objects.filter(
        order=order, kind=CarOrderActivity.Kind.COMPLETED
    ).count() == 0
