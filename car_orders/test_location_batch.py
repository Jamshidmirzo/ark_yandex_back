"""The mobile uploads buffered GPS in a BATCH to /api/v1/location/batch/. We take
the latest point and attach it to the driver's active order — format-tolerant."""

import pytest
from rest_framework.test import APIClient

from car_orders.models import DriverPosition, OrderLiveLocation, OrderMeta


def _batch(body):
    return APIClient().post("/api/v1/location/batch/", body, format="json")


def _active_order(order_id, driver_id):
    return OrderMeta.objects.create(
        order_id=order_id, driver_id=driver_id, overlay_claimed=True,
        trip_state=OrderMeta.TripState.TO_CLIENT,
    )


@pytest.mark.django_db
def test_batch_locations_key_attaches_latest():
    _active_order(119, 670)
    r = _batch({"locations": [
        {"lat": 41.30, "lng": 69.20, "driver_id": 670, "timestamp": "2026-06-13T09:00:00Z"},
        {"lat": 41.33, "lng": 69.24, "driver_id": 670, "timestamp": "2026-06-13T09:00:10Z"},
    ]})
    assert r.status_code == 200, r.content
    assert r.data["accepted"] == 2
    assert r.data["updated_orders"] == [119]
    loc = OrderLiveLocation.objects.get(order_id=119)
    assert (round(loc.lat, 2), round(loc.lng, 2)) == (41.33, 69.24)  # the LATEST point
    assert round(DriverPosition.objects.get(driver_id=670).lat, 2) == 41.33


@pytest.mark.django_db
def test_batch_bare_list_and_latitude_keys():
    _active_order(120, 671)
    r = _batch([
        {"latitude": 41.1, "longitude": 69.1, "driver_id": 671},
        {"latitude": 41.2, "longitude": 69.2, "driver_id": 671},
    ])
    assert r.status_code == 200, r.content
    assert r.data["updated_orders"] == [120]
    # No timestamps → last in the list wins.
    assert round(OrderLiveLocation.objects.get(order_id=120).lat, 1) == 41.2


@pytest.mark.django_db
def test_batch_top_level_driver_id():
    _active_order(121, 55)
    r = _batch({"driver_id": 55, "points": [{"lat": 41.5, "lng": 69.5}]})
    assert r.status_code == 200, r.content
    assert r.data["updated_orders"] == [121]


@pytest.mark.django_db
def test_batch_empty_is_accepted_noop():
    r = _batch({"locations": []})
    assert r.status_code == 200, r.content
    assert r.data["accepted"] == 0
    assert r.data["updated_orders"] == []


@pytest.mark.django_db
def test_batch_free_driver_stores_position_only():
    r = _batch({"driver_id": 77, "locations": [{"lat": 41.0, "lng": 69.0}]})
    assert r.status_code == 200, r.content
    assert r.data["updated_orders"] == []  # no active order
    assert round(DriverPosition.objects.get(driver_id=77).lat, 1) == 41.0
