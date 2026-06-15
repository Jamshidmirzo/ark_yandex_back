"""Tests for the «one order at a time» rule: a driver may hold only a single
active (non-terminal) overlay order. OverlayClaimView blocks a 2nd active order
on EVERY path (dispatcher, auto, driver self-claim) with 409 DRIVER_BUSY.
Bypasses the demo-login fixture — hits the local overlay endpoint directly."""

import pytest
from rest_framework.test import APIClient

from car_orders.models import OrderMeta


def _order(driver_id, order_id, state):
    return OrderMeta.objects.create(
        order_id=order_id, driver_id=driver_id, overlay_claimed=True,
        trip_state=state, car_id=10, car_label="Damas (01A001AA)",
    )


def _claim(order_id, driver_id, **extra):
    return APIClient().post(
        f"/api/v1/car-orders/{order_id}/overlay-claim/",
        {"driver_id": driver_id, "car_id": 1, "car_label": "Damas (01A001AA)", **extra},
        format="json",
    )


@pytest.mark.django_db
def test_free_driver_can_claim():
    r = _claim(700, 99)
    assert r.status_code == 200, r.content
    assert OrderMeta.objects.get(order_id=700).driver_id == 99


@pytest.mark.django_db
def test_second_active_order_blocked():
    _order(99, 701, OrderMeta.TripState.ASSIGNED)  # driver already has one
    r = _claim(702, 99)
    assert r.status_code == 400, r.content
    assert r.data["error"]["code"] == "DRIVER_BUSY"
    assert "701" in r.data["error"]["message"]
    assert OrderMeta.objects.filter(order_id=702).first() is None  # not created/assigned


@pytest.mark.django_db
def test_second_active_blocked_even_in_progress():
    _order(99, 703, OrderMeta.TripState.IN_TRIP)
    r = _claim(704, 99)
    assert r.status_code == 400, r.content
    assert r.data["error"]["code"] == "DRIVER_BUSY"


@pytest.mark.django_db
def test_reclaim_same_order_is_ok():
    _order(99, 705, OrderMeta.TripState.ASSIGNED)
    r = _claim(705, 99)  # same order → idempotent, not «busy»
    assert r.status_code == 200, r.content


@pytest.mark.django_db
def test_can_take_next_after_completion():
    _order(99, 706, OrderMeta.TripState.COMPLETED)  # finished → frees the driver
    r = _claim(707, 99)
    assert r.status_code == 200, r.content
    assert OrderMeta.objects.get(order_id=707).driver_id == 99


@pytest.mark.django_db
def test_other_driver_busy_does_not_block_me():
    _order(98, 708, OrderMeta.TripState.ASSIGNED)  # a DIFFERENT driver is busy
    r = _claim(709, 99)
    assert r.status_code == 200, r.content
