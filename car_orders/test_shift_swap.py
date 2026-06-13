"""Tests for the mid-shift car swap (driver drives to the garage, changes the
car, goes back on the line). Rule: a car swap is blocked while the driver still
has ANY active (non-terminal) order — finish them first. Bypasses the demo-login
fixture — hits the local shift overlay endpoint directly."""

import pytest
from rest_framework.test import APIClient

from car_orders.models import DriverShiftState, OrderMeta


def _go_online(driver_id, car_id, **car):
    body = {"driver_id": driver_id, "car_id": car_id, "car_type_id": 1, **car}
    return APIClient().patch("/api/v1/car-orders/drivers/me/shift/", body, format="json")


def _order(driver_id, order_id, state, car_id=10):
    return OrderMeta.objects.create(
        order_id=order_id, driver_id=driver_id, overlay_claimed=True,
        trip_state=state, car_id=car_id,
    )


@pytest.mark.django_db
def test_go_on_shift_then_swap_when_free():
    drv = 671
    r = _go_online(drv, 10, car_model="Damas", car_plate="01A001AA", car_type_id=1)
    assert r.status_code == 200, r.content
    assert r.data["car"]["id"] == 10

    # No active orders → swap allowed.
    r = _go_online(drv, 20, car_model="Cobalt", car_plate="01B002BB", car_type_id=1)
    assert r.status_code == 200, r.content
    assert DriverShiftState.objects.get(driver_id=drv).car_id == 20


@pytest.mark.django_db
def test_swap_blocked_with_queued_order():
    drv = 671
    _go_online(drv, 10, car_model="Damas", car_plate="01A001AA")
    _order(drv, 900, OrderMeta.TripState.ASSIGNED)  # queued, not started — still blocks
    r = _go_online(drv, 20, car_model="Cobalt", car_plate="01B002BB")
    assert r.status_code == 400, r.content
    assert r.data["error"]["code"] == "HAS_ACTIVE_ORDERS"
    assert DriverShiftState.objects.get(driver_id=drv).car_id == 10  # unchanged


@pytest.mark.django_db
def test_swap_blocked_during_active_trip():
    drv = 671
    _go_online(drv, 10, car_model="Damas", car_plate="01A001AA")
    _order(drv, 901, OrderMeta.TripState.IN_TRIP)
    r = _go_online(drv, 20, car_model="Cobalt", car_plate="01B002BB")
    assert r.status_code == 400, r.content
    assert r.data["error"]["code"] == "HAS_ACTIVE_ORDERS"


@pytest.mark.django_db
def test_swap_allowed_after_orders_finished():
    """Only terminal (completed/cancelled) orders left → swap allowed."""
    drv = 671
    _go_online(drv, 10, car_model="Damas", car_plate="01A001AA")
    _order(drv, 902, OrderMeta.TripState.COMPLETED)
    _order(drv, 903, OrderMeta.TripState.CANCELLED)
    r = _go_online(drv, 20, car_model="Cobalt", car_plate="01B002BB")
    assert r.status_code == 200, r.content
    assert DriverShiftState.objects.get(driver_id=drv).car_id == 20


@pytest.mark.django_db
def test_reselecting_same_car_is_noop_not_blocked():
    drv = 671
    _go_online(drv, 10, car_model="Damas", car_plate="01A001AA")
    _order(drv, 904, OrderMeta.TripState.IN_TRIP)  # active, but same car isn't a change
    r = _go_online(drv, 10, car_model="Damas", car_plate="01A001AA")
    assert r.status_code == 200, r.content
