"""The driver app posts per-driver GPS to /drivers/me/location/; the server must
attach it to the driver's ACTIVE order (any non-terminal stage) and store the
per-driver position — so real phone GPS drives the map without the simulator."""

import pytest
from rest_framework.test import APIClient

from car_orders.models import DriverPosition, OrderLiveLocation, OrderMeta


def _post(driver_id, lat, lng):
    return APIClient().post(
        "/api/v1/car-orders/drivers/me/location/",
        {"driver_id": driver_id, "lat": lat, "lng": lng},
        format="json",
    )


@pytest.mark.django_db
def test_heartbeat_attaches_to_assigned_order():
    # An order that is ASSIGNED (not yet «moving») must still get the phone's GPS.
    OrderMeta.objects.create(
        order_id=119, driver_id=5, overlay_claimed=True,
        trip_state=OrderMeta.TripState.ASSIGNED,
    )
    r = _post(5, 41.31, 69.24)
    assert r.status_code == 200, r.content
    assert r.data["updated_orders"] == [119]
    assert DriverPosition.objects.get(driver_id=5).lat == 41.31
    loc = OrderLiveLocation.objects.get(order_id=119)
    assert (loc.lat, loc.lng) == (41.31, 69.24)


@pytest.mark.django_db
def test_heartbeat_skips_terminal_order_but_stores_position():
    OrderMeta.objects.create(
        order_id=120, driver_id=6, trip_state=OrderMeta.TripState.COMPLETED,
    )
    r = _post(6, 41.0, 69.0)
    assert r.status_code == 200, r.content
    assert r.data["updated_orders"] == []  # nothing active to attach to
    assert DriverPosition.objects.get(driver_id=6).lat == 41.0  # but position is stored
    assert not OrderLiveLocation.objects.filter(order_id=120).exists()


@pytest.mark.django_db
def test_free_driver_heartbeat_just_stores_position():
    r = _post(7, 41.5, 69.5)
    assert r.status_code == 200, r.content
    assert r.data["updated_orders"] == []
    assert DriverPosition.objects.get(driver_id=7).lat == 41.5
