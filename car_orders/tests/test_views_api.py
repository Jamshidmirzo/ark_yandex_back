"""API tests for the read/upsert overlay endpoints that the original suite skipped:
claim-check (single + batch), meta-batch, OrderMeta GET/POST, live-location
GET/POST (incl. geometry), and the driver-positions stale filter. Open-dev wiring
(AllowAny / OverlayAuthenticated off by default), so a bare APIClient is enough.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from car_orders.models import DriverPosition, OrderLiveLocation, OrderMeta

TS = OrderMeta.TripState


@pytest.fixture
def client():
    return APIClient()


# ---- claim-check (single) --------------------------------------------------

@pytest.mark.django_db
def test_claim_check_ok_when_no_saved_window(client):
    OrderMeta.objects.create(order_id=902)  # no window → nothing to schedule against
    r = client.post("/api/v1/car-orders/902/claim-check/", {"driver_id": 5}, format="json")
    assert r.status_code == 200
    assert r.data == {"ok": True, "conflict": None}


@pytest.mark.django_db
def test_claim_check_flags_window_conflict(client):
    base = timezone.now() + timedelta(days=1)
    OrderMeta.objects.create(
        order_id=900, driver_id=5, trip_state=TS.ASSIGNED,
        planned_datetime=base, estimated_duration=120, service_time=0,
    )
    OrderMeta.objects.create(
        order_id=901, planned_datetime=base + timedelta(minutes=30),
        estimated_duration=60, service_time=0,
    )
    r = client.post("/api/v1/car-orders/901/claim-check/", {"driver_id": 5}, format="json")
    assert r.status_code == 200
    assert r.data["ok"] is False
    assert r.data["conflict"]["order_id"] == 900


# ---- claim-check-batch + meta-batch ---------------------------------------

@pytest.mark.django_db
def test_claim_check_batch_per_order_results(client):
    base = timezone.now() + timedelta(days=1)
    OrderMeta.objects.create(
        order_id=900, driver_id=5, trip_state=TS.ASSIGNED,
        planned_datetime=base, estimated_duration=120, service_time=0,
    )
    OrderMeta.objects.create(
        order_id=901, planned_datetime=base + timedelta(minutes=30),
        estimated_duration=60, service_time=0,
    )
    OrderMeta.objects.create(order_id=902)  # no window → ok
    r = client.post(
        "/api/v1/car-orders/claim-check-batch/",
        {"driver_id": 5, "order_ids": [901, 902]}, format="json",
    )
    res = {x["order_id"]: x["ok"] for x in r.data["results"]}
    assert res == {901: False, 902: True}


@pytest.mark.django_db
def test_meta_batch_returns_only_known_orders(client):
    OrderMeta.objects.create(order_id=1, driver_id=5, trip_state=TS.ASSIGNED)
    OrderMeta.objects.create(order_id=2, driver_id=6, trip_state=TS.IN_TRIP)
    r = client.post("/api/v1/car-orders/meta-batch/", {"order_ids": [1, 2, 3]}, format="json")
    assert sorted(m["order_id"] for m in r.data["results"]) == [1, 2]


# ---- OrderMeta GET / POST --------------------------------------------------

@pytest.mark.django_db
def test_meta_get_null_then_value(client):
    assert client.get("/api/v1/car-orders/700/meta/").data is None
    OrderMeta.objects.create(order_id=700, driver_id=5, trip_state=TS.ASSIGNED)
    assert client.get("/api/v1/car-orders/700/meta/").data["order_id"] == 700


@pytest.mark.django_db
def test_meta_post_upserts_fields(client):
    r = client.post(
        "/api/v1/car-orders/701/meta/",
        {"origin_lat": 41.31, "origin_lng": 69.24, "is_urgent": True}, format="json",
    )
    assert r.status_code == 200
    m = OrderMeta.objects.get(order_id=701)
    assert m.is_urgent is True and m.origin_lat == 41.31


# ---- LiveLocation GET / POST ----------------------------------------------

@pytest.mark.django_db
def test_live_location_get_null_then_value(client):
    assert client.get("/api/v1/car-orders/720/live-location/").data is None
    r = client.post("/api/v1/car-orders/720/live-location/", {"lat": 41.3, "lng": 69.2}, format="json")
    assert r.status_code == 200
    assert client.get("/api/v1/car-orders/720/live-location/").data["lat"] == 41.3


@pytest.mark.django_db
def test_live_location_post_carries_geometry(client):
    geom = [[69.2, 41.3], [69.25, 41.33]]
    r = client.post(
        "/api/v1/car-orders/721/live-location/",
        {"lat": 41.3, "lng": 69.2, "geometry": geom}, format="json",
    )
    assert r.status_code == 200
    assert OrderLiveLocation.objects.get(order_id=721).geometry == geom


# ---- DriverPositions stale filter -----------------------------------------

@pytest.mark.django_db
def test_driver_positions_drops_stale_with_max_age(client):
    now = timezone.now()
    DriverPosition.objects.create(driver_id=1, lat=41.3, lng=69.2, last_seen=now)
    DriverPosition.objects.create(
        driver_id=2, lat=41.3, lng=69.2, last_seen=now - timedelta(seconds=600)
    )
    r = client.get("/api/v1/car-orders/drivers/positions/?max_age=300")
    assert set(r.data.keys()) == {"1"}
