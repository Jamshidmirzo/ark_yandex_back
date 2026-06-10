"""Tests for the phase-aware auto-simulator leg selection."""

import pytest
from django.utils import timezone

from car_orders.management.commands.auto_simulate import (
    leg_endpoints,
    one_moving_order_per_driver,
)
from car_orders.models import OrderMeta

ORIGIN = [69.240, 41.311]  # [lng, lat] pickup
DEST = [69.290, 41.351]  # [lng, lat] destination


def test_to_client_drives_from_current_pos_to_pickup():
    """The inter-order / approach leg: drive from where the driver IS to the
    pickup — NOT along the loaded origin→destination route."""
    prev = [69.300, 41.360]  # e.g. previous order's destination
    start, end = leg_endpoints(OrderMeta.TripState.TO_CLIENT, prev, ORIGIN, DEST)
    assert start == prev
    assert end == ORIGIN


def test_to_client_without_known_pos_starts_at_pickup():
    start, end = leg_endpoints(OrderMeta.TripState.TO_CLIENT, None, ORIGIN, DEST)
    assert start == ORIGIN
    assert end == ORIGIN


def test_in_trip_drives_pickup_to_destination():
    """The loaded leg is always origin → destination, regardless of the driver's
    current position."""
    start, end = leg_endpoints(OrderMeta.TripState.IN_TRIP, [1.0, 2.0], ORIGIN, DEST)
    assert start == ORIGIN
    assert end == DEST


def test_endpoints_are_copies_not_aliases():
    """Returned lists must not alias the inputs (the caller mutates driver_pos)."""
    start, end = leg_endpoints(OrderMeta.TripState.IN_TRIP, None, ORIGIN, DEST)
    start[0] = 0.0
    assert ORIGIN[0] == 69.240


def _meta(order_id, driver_id, state):
    now = timezone.now()
    return OrderMeta.objects.create(
        order_id=order_id, driver_id=driver_id, trip_state=state,
        origin_lat=1.0, origin_lng=1.0, address_lat=2.0, address_lng=2.0,
        planned_datetime=now, estimated_duration=60,
    )


@pytest.mark.django_db
def test_one_moving_order_per_driver_prefers_in_trip():
    """A driver with two moving orders → animate exactly one (the loaded leg),
    so driver_pos (one car = one position) isn't clobbered."""
    _meta(700, 5, OrderMeta.TripState.TO_CLIENT)
    _meta(701, 5, OrderMeta.TripState.IN_TRIP)
    _meta(702, 6, OrderMeta.TripState.TO_CLIENT)  # different driver
    kept = one_moving_order_per_driver(OrderMeta.objects.filter(order_id__in=[700, 701, 702]))
    by_driver = {m.driver_id: m for m in kept}
    assert len(kept) == 2  # one per driver
    assert by_driver[5].order_id == 701  # in_trip wins over to_client
    assert by_driver[6].order_id == 702


@pytest.mark.django_db
def test_one_moving_order_per_driver_collapses_same_phase():
    _meta(710, 8, OrderMeta.TripState.TO_CLIENT)
    _meta(711, 8, OrderMeta.TripState.TO_CLIENT)
    kept = one_moving_order_per_driver(OrderMeta.objects.filter(order_id__in=[710, 711]))
    assert len(kept) == 1
    assert kept[0].driver_id == 8
