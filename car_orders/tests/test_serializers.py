"""Unit tests for the serializers (``car_orders.serializers``).

Pins the MinutesDurationField round-trip, the lat/lng + positive-duration
validation, and the computed read-only fields (driver_location, needs_reassign,
is_late, at_risk) — most of which were only seen on their happy path via the API.
"""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers as drf_serializers

from car_orders import serializers
from car_orders.models import Car, CarOrder, CarType, DriverShift, OrderMeta

User = get_user_model()
S = CarOrder.Status
TS = OrderMeta.TripState


@pytest.fixture
def env(db):
    ct = CarType.objects.create(name="Легковая")
    return {
        "ct": ct,
        "car": Car.objects.create(model="Damas", plate_number="01A001AA", type=ct, status="active"),
        "driver": User.objects.create_user(username="drv", password="pw"),
        "requester": User.objects.create_user(username="req", password="pw"),
    }


# ---- MinutesDurationField --------------------------------------------------

def test_minutes_field_representation_floors_to_minutes():
    f = serializers.MinutesDurationField()
    assert f.to_representation(timedelta(minutes=90)) == 90
    assert f.to_representation(timedelta(seconds=125)) == 2  # floor, not round
    assert f.to_representation(None) is None


def test_minutes_field_internal_value_and_errors():
    f = serializers.MinutesDurationField()
    assert f.to_internal_value(30) == timedelta(minutes=30)
    with pytest.raises(drf_serializers.ValidationError):
        f.to_internal_value(-5)
    with pytest.raises(drf_serializers.ValidationError):
        f.to_internal_value("abc")


# ---- write validation ------------------------------------------------------

def test_write_serializer_rejects_zero_duration():
    s = serializers.CarOrderWriteSerializer(data={"address": "X", "estimated_duration": 0})
    assert not s.is_valid()
    assert "estimated_duration" in s.errors


def test_write_serializer_rejects_out_of_range_lat():
    s = serializers.CarOrderWriteSerializer(
        data={"address": "X", "origin_lat": 91, "origin_lng": 69}
    )
    assert not s.is_valid()
    assert "origin_lat" in s.errors


def test_route_estimate_rejects_bad_coords():
    s = serializers.RouteEstimateSerializer(
        data={"origin_lat": 91, "origin_lng": 69, "dest_lat": 41, "dest_lng": 69}
    )
    assert not s.is_valid()
    assert "origin_lat" in s.errors


def test_location_serializer_bounds():
    assert not serializers.LocationSerializer(data={"lat": 200, "lng": 69}).is_valid()
    assert serializers.LocationSerializer(data={"lat": 41.3, "lng": 69.2}).is_valid()


# ---- CarOrderSerializer.driver_location -----------------------------------

@pytest.mark.django_db
def test_driver_location_null_when_not_in_progress(env):
    order = CarOrder.objects.create(
        created_by=env["requester"], driver=env["driver"], status=S.SCHEDULED
    )
    assert serializers.CarOrderSerializer(order).data["driver_location"] is None


@pytest.mark.django_db
def test_driver_location_null_when_shift_has_no_coords(env):
    DriverShift.objects.create(driver=env["driver"], car=env["car"])  # no lat/lng yet
    order = CarOrder.objects.create(
        created_by=env["requester"], driver=env["driver"], status=S.IN_PROGRESS
    )
    assert serializers.CarOrderSerializer(order).data["driver_location"] is None


@pytest.mark.django_db
def test_driver_location_present_while_tracking(env):
    DriverShift.objects.create(
        driver=env["driver"], car=env["car"], lat=41.31, lng=69.27,
        last_seen=timezone.now(), status=DriverShift.Status.EN_ROUTE,
    )
    order = CarOrder.objects.create(
        created_by=env["requester"], driver=env["driver"], status=S.IN_PROGRESS
    )
    loc = serializers.CarOrderSerializer(order).data["driver_location"]
    assert loc and loc["lat"] == 41.31 and loc["status"] == "en_route"


@pytest.mark.django_db
def test_needs_reassign_false_when_not_scheduled(env):
    now = timezone.now()
    order = CarOrder.objects.create(
        created_by=env["requester"], driver=env["driver"], status=S.IN_PROGRESS,
        planned_datetime=now - timedelta(hours=1), estimated_duration=timedelta(hours=1),
        latest_start=now - timedelta(hours=2),
    )
    assert serializers.CarOrderSerializer(order).data["needs_reassign"] is False


# ---- OrderMetaSerializer is_late / at_risk --------------------------------

@pytest.mark.django_db
def test_meta_is_late_true_when_assigned_and_overdue(db):
    m = OrderMeta.objects.create(
        order_id=1, driver_id=5, trip_state=TS.ASSIGNED,
        planned_datetime=timezone.now() - timedelta(hours=1),
    )
    assert serializers.OrderMetaSerializer(m).data["is_late"] is True


@pytest.mark.django_db
def test_meta_is_late_false_when_unclaimed(db):
    # Not yet claimed (driver_id None) — defaults to ASSIGNED but must not read late.
    m = OrderMeta.objects.create(
        order_id=2, driver_id=None, trip_state=TS.ASSIGNED,
        planned_datetime=timezone.now() - timedelta(hours=1),
    )
    assert serializers.OrderMetaSerializer(m).data["is_late"] is False


@pytest.mark.django_db
def test_meta_at_risk_uses_in_memory_active_index(db):
    now = timezone.now()
    # Driver 5 is on an overrunning trip (finished an hour ago, still moving).
    overrun = OrderMeta(
        order_id=1, driver_id=5, trip_state=TS.IN_TRIP,
        planned_datetime=now - timedelta(hours=3), estimated_duration=120,
    )
    target = OrderMeta.objects.create(
        order_id=2, driver_id=5, trip_state=TS.ASSIGNED,
        planned_datetime=now - timedelta(minutes=10), estimated_duration=60,
        latest_start=now - timedelta(minutes=5),
    )
    data = serializers.OrderMetaSerializer(
        target, context={"active_by_driver": {5: [overrun]}}
    ).data
    assert data["at_risk"] is True
