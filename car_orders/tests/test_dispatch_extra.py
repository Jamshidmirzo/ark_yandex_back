"""Additional unit tests for the auto-dispatch brain (``car_orders.dispatch``) —
gap-fill for ``test_dispatch.py``.

Focus on ``dispatch.claim`` directly (the «1 водитель = 1 активный заказ» guard
that the original suite only exercised transitively through ``run_once``), plus
the queue/ranking/freshness filters and the within-pass load accounting.
"""

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from car_orders import dispatch
from car_orders.models import DriverPosition, DriverShiftState, OrderMeta

TS = OrderMeta.TripState


def _shift(driver_id, car_id, type_id, lat=None, lng=None):
    DriverShiftState.objects.create(
        driver_id=driver_id, car_id=car_id, car_model="Cobalt", car_plate=f"0{driver_id}A",
        car_type_id=type_id, car_type_name="Легковая", status="online",
    )
    if lat is not None:
        DriverPosition.objects.create(driver_id=driver_id, lat=lat, lng=lng, last_seen=timezone.now())


def _order(order_id, type_id, lat, lng, dispatchable=True, **extra):
    return OrderMeta.objects.create(
        order_id=order_id, dispatchable=dispatchable, car_type_id=type_id,
        origin_lat=lat, origin_lng=lng, address_lat=lat + 0.1, address_lng=lng + 0.1, **extra,
    )


# ---- claim guards (the headline invariant) --------------------------------

@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_claim_assigns_a_free_order():
    _order(20, 5, 41.31, 69.24)
    assert dispatch.claim(20, driver_id=1, car_id=11, car_label="Cobalt") is True
    m = OrderMeta.objects.get(order_id=20)
    assert m.driver_id == 1 and m.trip_state == TS.ASSIGNED and m.overlay_claimed is True


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_claim_returns_false_when_order_already_held():
    _order(20, 5, 41.31, 69.24, driver_id=2, trip_state=TS.IN_TRIP)  # another driver, active
    assert dispatch.claim(20, driver_id=1, car_id=11, car_label="Cobalt") is False
    assert OrderMeta.objects.get(order_id=20).driver_id == 2  # untouched


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_claim_returns_false_when_driver_already_busy():
    _order(21, 5, 41.31, 69.24, driver_id=1, trip_state=TS.IN_TRIP)  # driver 1 busy elsewhere
    _order(22, 5, 41.31, 69.24)
    assert dispatch.claim(22, driver_id=1, car_id=11, car_label="Cobalt") is False
    assert OrderMeta.objects.get(order_id=22).driver_id is None


@pytest.mark.django_db
def test_claim_returns_false_for_missing_order():
    assert dispatch.claim(999, driver_id=1, car_id=11, car_label="Cobalt") is False


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_claim_can_take_a_terminal_order_again():
    _order(23, 5, 41.31, 69.24, driver_id=None, trip_state=TS.CANCELLED, returning=True)
    assert dispatch.claim(23, driver_id=1, car_id=11, car_label="Cobalt") is True
    m = OrderMeta.objects.get(order_id=23)
    assert m.trip_state == TS.ASSIGNED and m.returning is False


# ---- queue / freshness / ranking filters ----------------------------------

@pytest.mark.django_db
def test_queue_orders_excludes_assigned_terminal_and_coordless():
    _order(30, 5, 41.31, 69.24)  # eligible
    _order(31, 5, 41.31, 69.24, driver_id=7)  # already has a driver
    _order(32, 5, 41.31, 69.24, dispatchable=False)  # not approved
    _order(33, 5, 41.31, 69.24, trip_state=TS.COMPLETED)  # terminal
    OrderMeta.objects.create(order_id=34, dispatchable=True, car_type_id=5)  # no coords
    ids = {m.order_id for m in dispatch.queue_orders()}
    assert ids == {30}


@pytest.mark.django_db
def test_fresh_positions_drops_stale_fixes():
    now = timezone.now()
    DriverPosition.objects.create(driver_id=1, lat=41.3, lng=69.2, last_seen=now)
    DriverPosition.objects.create(
        driver_id=2, lat=41.3, lng=69.2, last_seen=now - timedelta(seconds=600)
    )
    fresh = dispatch.fresh_positions(300, now)
    assert set(fresh) == {1}


@pytest.mark.django_db
def test_rank_unknown_type_order_treats_everyone_as_right_type():
    _shift(1, 11, type_id=5, lat=41.31, lng=69.24)
    shifts = list(DriverShiftState.objects.all())
    ranked = dispatch.rank_drivers(None, (41.31, 69.24), shifts, dispatch.fresh_positions(300), load={})
    assert ranked[0][4] == ""  # no type filter → ideal


@pytest.mark.django_db
def test_rank_no_position_sorts_after_positioned():
    _shift(1, 11, type_id=5, lat=41.31, lng=69.24)  # has GPS
    _shift(2, 12, type_id=5)  # on shift, no GPS fix
    shifts = list(DriverShiftState.objects.all())
    ranked = dispatch.rank_drivers(5, (41.31, 69.24), shifts, dispatch.fresh_positions(300), load={})
    assert ranked[0][0] == 1  # positioned driver ranks ahead of the GPS-less one
    assert ranked[-1][0] == 2


# ---- within-pass load accounting -------------------------------------------

@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_run_once_two_drivers_two_orders_assigns_both():
    _shift(1, 11, type_id=5, lat=41.31, lng=69.24)
    _shift(2, 12, type_id=5, lat=41.31, lng=69.24)
    _order(40, 5, 41.31, 69.24, is_urgent=True)
    _order(41, 5, 41.31, 69.24, is_urgent=True)
    assigned = dispatch.run_once({}, lead_min=45, stale_sec=180, pos_max_age=300)
    assert sorted(d for _o, d in assigned) == [1, 2]


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_run_once_one_driver_leaves_second_order_unassigned():
    _shift(1, 11, type_id=5, lat=41.31, lng=69.24)
    _order(50, 5, 41.31, 69.24, is_urgent=True)
    _order(51, 5, 41.31, 69.24, is_urgent=True)
    assigned = dispatch.run_once({}, lead_min=45, stale_sec=180, pos_max_age=300)
    assert len(assigned) == 1
    unassigned = OrderMeta.objects.filter(driver_id__isnull=True).values_list("order_id", flat=True)
    assert len(unassigned) == 1  # exactly one order is left for the next pass
