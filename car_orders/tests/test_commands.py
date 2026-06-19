"""Tests for the management commands that wrap the overlay logic — the worker
entry points (``auto_dispatch --once``) and the schedule watchdog
(``order_watchdog`` reporting + ``--release``). These exercise the CLI wrappers,
not just the underlying ``dispatch`` / ``scheduling`` functions.
"""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from car_orders.models import (
    DispatchSettings,
    DriverPosition,
    DriverShiftState,
    OrderMeta,
)

TS = OrderMeta.TripState


def _shift(driver_id, car_id, type_id, lat, lng):
    DriverShiftState.objects.create(
        driver_id=driver_id, car_id=car_id, car_model="Cobalt", car_plate=f"0{driver_id}A",
        car_type_id=type_id, car_type_name="Легковая", status="online",
    )
    DriverPosition.objects.create(driver_id=driver_id, lat=lat, lng=lng, last_seen=timezone.now())


def _order(order_id, type_id, lat, lng, **extra):
    return OrderMeta.objects.create(
        order_id=order_id, dispatchable=True, car_type_id=type_id,
        origin_lat=lat, origin_lng=lng, address_lat=lat + 0.1, address_lng=lng + 0.1, **extra,
    )


# ---- auto_dispatch --once --------------------------------------------------

@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_auto_dispatch_once_does_nothing_when_toggle_off():
    _shift(1, 11, 5, 41.31, 69.24)
    _order(20, 5, 41.31, 69.24, is_urgent=True)
    call_command("auto_dispatch", "--once")
    assert OrderMeta.objects.get(order_id=20).driver_id is None  # singleton defaults off


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_auto_dispatch_once_assigns_when_enabled():
    DispatchSettings.objects.update_or_create(pk=1, defaults={"auto_enabled": True})
    _shift(1, 11, 5, 41.31, 69.24)
    _order(20, 5, 41.31, 69.24, is_urgent=True)
    call_command("auto_dispatch", "--once")
    assert OrderMeta.objects.get(order_id=20).driver_id == 1


# ---- order_watchdog --------------------------------------------------------

@pytest.mark.django_db
def test_order_watchdog_reports_gps_lost_trip():
    # An actively-driven order with no live location → GPS lost.
    OrderMeta.objects.create(order_id=1, driver_id=5, trip_state=TS.IN_TRIP)
    out = StringIO()
    call_command("order_watchdog", stdout=out)
    assert "GPS lost (active, no fresh fix): 1" in out.getvalue()


@pytest.mark.django_db
def test_order_watchdog_release_frees_late_but_not_started():
    now = timezone.now()
    late = OrderMeta.objects.create(
        order_id=1, driver_id=5, trip_state=TS.ASSIGNED, planned_datetime=now - timedelta(hours=1)
    )
    started = OrderMeta.objects.create(
        order_id=2, driver_id=6, trip_state=TS.TO_CLIENT, planned_datetime=now - timedelta(hours=1)
    )
    call_command("order_watchdog", "--release", stdout=StringIO())
    late.refresh_from_db()
    started.refresh_from_db()
    assert late.trip_state == TS.CANCELLED and late.driver_id is None  # unstarted → freed
    assert started.driver_id == 6  # started trip is never yanked
