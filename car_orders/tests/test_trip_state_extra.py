"""Additional unit tests for the trip-state machine
(``car_orders.services.trip_state``) — gap-fill for ``test_trip_state.py``.

Focus on the geofence edges the original suite left thin: arrival at the
DESTINATION (not just the client), the ~100 m boundary, a stale-but-present GPS
fix, a missing-target skip, plus the return-leg target and the TO_CLIENT moving
block. Pure service layer.
"""

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from car_orders.models import DriverPosition, OrderMeta
from car_orders.services import trip_state

TS = OrderMeta.TripState


def _meta(state, *, order_id=900, driver_id=5, has_return=False, returning=False, **extra):
    base = dict(
        order_id=order_id, driver_id=driver_id, overlay_claimed=True, trip_state=state,
        origin_lat=41.31, origin_lng=69.24, address_lat=41.35, address_lng=69.29,
        has_return=has_return, returning=returning, return_lat=41.30, return_lng=69.20,
    )
    base.update(extra)
    return OrderMeta.objects.create(**base)


def _position(driver_id, lat, lng, *, age_s=0):
    return DriverPosition.objects.create(
        driver_id=driver_id, lat=lat, lng=lng,
        last_seen=timezone.now() - timedelta(seconds=age_s),
    )


# ---- geofence at the DESTINATION ------------------------------------------

@pytest.mark.django_db
def test_geofence_at_destination_rejects_when_too_far():
    m = _meta(TS.IN_TRIP, driver_id=5)
    _position(5, 41.50, 69.50)  # ~25 km from the destination
    with pytest.raises(trip_state.TripStateError) as exc:
        trip_state.validate(m, TS.AT_DESTINATION, actor_driver_id=None)
    assert exc.value.code == "TOO_FAR"


@pytest.mark.django_db
def test_geofence_at_destination_passes_on_the_point():
    m = _meta(TS.IN_TRIP, driver_id=5)
    _position(5, 41.35, 69.29)  # exactly on the destination
    assert trip_state.validate(m, TS.AT_DESTINATION, actor_driver_id=None) == {
        "trip_state": TS.AT_DESTINATION
    }


@pytest.mark.django_db
def test_geofence_return_leg_uses_return_coords():
    # Returning back: the arrival target is the RETURN point, not the destination.
    m = _meta(TS.IN_TRIP, driver_id=5, has_return=True, returning=True)
    _position(5, 41.30, 69.20)  # on the return point
    assert trip_state.validate(m, TS.AT_DESTINATION, actor_driver_id=None) == {
        "trip_state": TS.AT_DESTINATION
    }


# ---- geofence boundary (~100 m default radius) ----------------------------

@override_settings(CAR_ORDER_ARRIVAL_GEOFENCE_M=100)
@pytest.mark.django_db
def test_geofence_passes_just_inside_the_radius():
    m = _meta(TS.TO_CLIENT, driver_id=5)
    _position(5, 41.31 + 0.0008, 69.24)  # ~89 m north of the pickup → inside 100 m
    assert trip_state.validate(m, TS.AT_CLIENT, actor_driver_id=None) == {
        "trip_state": TS.AT_CLIENT
    }


@override_settings(CAR_ORDER_ARRIVAL_GEOFENCE_M=100)
@pytest.mark.django_db
def test_geofence_rejects_just_outside_the_radius():
    m = _meta(TS.TO_CLIENT, driver_id=5)
    _position(5, 41.31 + 0.0012, 69.24)  # ~133 m north of the pickup → outside 100 m
    with pytest.raises(trip_state.TripStateError) as exc:
        trip_state.validate(m, TS.AT_CLIENT, actor_driver_id=None)
    assert exc.value.code == "TOO_FAR"


# ---- geofence GPS freshness + missing target ------------------------------

@override_settings(CAR_ORDER_GPS_FRESH_S=120)
@pytest.mark.django_db
def test_geofence_rejects_a_stale_but_present_fix():
    m = _meta(TS.TO_CLIENT, driver_id=5)
    _position(5, 41.31, 69.24, age_s=300)  # right on the point but 5 min old
    with pytest.raises(trip_state.TripStateError) as exc:
        trip_state.validate(m, TS.AT_CLIENT, actor_driver_id=None)
    assert exc.value.code == "NO_FRESH_GPS"


@pytest.mark.django_db
def test_geofence_skipped_when_target_coords_are_missing():
    # No origin coords → arrival can't be geofenced, so it's allowed even with no GPS.
    m = _meta(TS.TO_CLIENT, driver_id=5, origin_lat=None, origin_lng=None)
    assert trip_state.validate(m, TS.AT_CLIENT, actor_driver_id=None) == {
        "trip_state": TS.AT_CLIENT
    }


@override_settings(CAR_ORDER_ARRIVAL_GEOFENCE_M=0)
@pytest.mark.django_db
def test_geofence_disabled_when_radius_zero():
    m = _meta(TS.TO_CLIENT, driver_id=5)  # no DriverPosition at all
    assert trip_state.validate(m, TS.AT_CLIENT, actor_driver_id=None) == {
        "trip_state": TS.AT_CLIENT
    }


# ---- one moving trip: the TO_CLIENT entry point ---------------------------

@pytest.mark.django_db
def test_blocks_second_moving_trip_on_to_client():
    _meta(TS.IN_TRIP, order_id=901, driver_id=5)  # already driving order 901
    parked = _meta(TS.ASSIGNED, order_id=902, driver_id=5)  # a second order, not moving
    with pytest.raises(trip_state.TripStateError) as exc:
        trip_state.validate(parked, TS.TO_CLIENT, actor_driver_id=None)
    assert exc.value.code == "ACTIVE_TRIP_EXISTS"


@pytest.mark.django_db
def test_waiting_to_in_trip_flags_returning_on_round_trip():
    m = _meta(TS.WAITING, has_return=True, returning=False)
    assert trip_state.validate(m, TS.IN_TRIP, actor_driver_id=None) == {
        "trip_state": TS.IN_TRIP, "returning": True
    }


@pytest.mark.django_db
def test_non_return_order_never_flags_returning():
    m = _meta(TS.AT_DESTINATION, has_return=False, returning=False)
    # at_destination → waiting is a legal non-moving transition; no returning flag.
    assert trip_state.validate(m, TS.WAITING, actor_driver_id=None) == {
        "trip_state": TS.WAITING
    }
