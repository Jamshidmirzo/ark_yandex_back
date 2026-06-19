"""Regression tests for the High/Medium audit fixes (H4, M3, M4, M6).

H1/H2/H3 atomicity & race handling are exercised elsewhere (trip_state suites,
the gateway-hook tests, and test_native_shift's IntegrityError test); the
concurrency aspects are documented as Postgres-only in AUDIT.md.
"""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from car_orders import dispatch, geometry
from car_orders.models import DriverPosition, DriverShiftState, OrderLiveLocation, OrderMeta
from car_orders.services import orders, overlay

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


# ---- M3: extend upper bound ------------------------------------------------

@pytest.mark.django_db
def test_overlay_extend_allows_up_to_the_ui_max():
    # 99h (the web DurationField max) must be accepted — the cap only blocks absurd input.
    OrderMeta.objects.create(order_id=901, estimated_duration=60, driver_id=None)
    meta, _conflict = overlay.extend(901, 99 * 60)
    assert meta.estimated_duration == 60 + 99 * 60


@pytest.mark.django_db
def test_overlay_extend_rejects_above_cap():
    OrderMeta.objects.create(order_id=900, estimated_duration=60)
    with pytest.raises(overlay.OverlayError) as exc:
        overlay.extend(900, 7 * 24 * 60 + 1)
    assert exc.value.code == "VALIDATION"


@pytest.mark.django_db
def test_native_extend_rejects_above_cap(db):
    from django.contrib.auth import get_user_model

    from car_orders.models import CarOrder

    User = get_user_model()
    u = User.objects.create_user(username="d", password="pw")
    order = CarOrder.objects.create(
        created_by=u, driver=u, status=CarOrder.Status.SCHEDULED,
    )
    with pytest.raises(orders.OrderError) as exc:
        orders.extend(order.pk, u, 7 * 24 * 60 + 1)
    assert exc.value.code == "VALIDATION"


# ---- M6: first_seen seeded from updated_at ---------------------------------

@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_run_once_uses_updated_at_so_restart_does_not_reset_clock():
    _shift(1, 11, 5, 41.31, 69.24)
    _order(60, 5, 41.31, 69.24)  # ASAP order (no planned time, not urgent)
    # It entered the queue 10 min ago — a fresh worker (empty first_seen) must still
    # see it as «waited long enough», not restart its stale clock.
    OrderMeta.objects.filter(order_id=60).update(updated_at=timezone.now() - timedelta(minutes=10))
    assigned = dispatch.run_once({}, lead_min=45, stale_sec=300, pos_max_age=300)
    assert assigned == [(60, 1)]


# ---- M4: orphan reaper -----------------------------------------------------

@pytest.mark.django_db
def test_reaper_clears_orphans_keeps_active():
    now = timezone.now()
    # terminal + old → reaped (meta + its live marker)
    OrderMeta.objects.create(order_id=1, trip_state=TS.CANCELLED)
    OrderMeta.objects.filter(order_id=1).update(updated_at=now - timedelta(days=40))
    OrderLiveLocation.objects.create(order_id=1, lat=41, lng=69, last_seen=now)
    # live marker with NO meta at all → reaped
    OrderLiveLocation.objects.create(order_id=2, lat=41, lng=69, last_seen=now)
    # active order + marker → kept
    OrderMeta.objects.create(order_id=3, driver_id=5, trip_state=TS.IN_TRIP)
    OrderLiveLocation.objects.create(order_id=3, lat=41, lng=69, last_seen=now)
    # stale driver position → reaped
    DriverPosition.objects.create(driver_id=9, lat=41, lng=69, last_seen=now - timedelta(days=40))

    call_command("reap_overlay_orphans", days=30, stdout=StringIO())

    assert not OrderLiveLocation.objects.filter(order_id__in=[1, 2]).exists()
    assert OrderLiveLocation.objects.filter(order_id=3).exists()
    assert not OrderMeta.objects.filter(order_id=1).exists()
    assert OrderMeta.objects.filter(order_id=3).exists()
    assert not DriverPosition.objects.filter(driver_id=9).exists()


@pytest.mark.django_db
def test_reaper_dry_run_deletes_nothing():
    OrderLiveLocation.objects.create(order_id=2, lat=41, lng=69, last_seen=timezone.now())
    call_command("reap_overlay_orphans", dry_run=True, stdout=StringIO())
    assert OrderLiveLocation.objects.filter(order_id=2).exists()


# ---- H4: deviation distance measured to segments, not vertices -------------

def test_min_dist_to_segment_is_small_mid_leg():
    # A long N–S leg; a point beside its MIDDLE is on-route (~0.08 km), even though
    # both VERTICES are ~55 km away — the old vertex-only check would falsely re-route.
    geom = [[69.0, 41.0], [69.0, 42.0]]  # [lng, lat]
    d = geometry.min_dist_km_to_polyline(41.5, 69.001, geom)
    assert d < 0.2


def test_min_dist_single_point_falls_back_to_vertex():
    assert geometry.min_dist_km_to_polyline(41.0, 69.0, [[69.0, 41.0]]) < 1e-6
