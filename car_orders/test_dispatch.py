"""Tests for the backend auto-dispatch brain (car_orders/dispatch.py) — the
server-side port of the frontend ranking + auto-loop. Pure overlay data, no demo."""

from datetime import timedelta

import pytest
from django.utils import timezone

from car_orders import dispatch
from car_orders.models import DriverPosition, DriverShiftState, OrderMeta


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


# ---- ranking ---------------------------------------------------------------

@pytest.mark.django_db
def test_rank_prefers_right_type_then_nearest():
    _shift(1, 11, type_id=5, lat=41.30, lng=69.20)  # right type, far
    _shift(2, 12, type_id=5, lat=41.31, lng=69.24)  # right type, near pickup
    _shift(3, 13, type_id=9, lat=41.31, lng=69.24)  # wrong type, near
    pickup = (41.311, 69.241)
    shifts = list(DriverShiftState.objects.all())
    positions = dispatch.fresh_positions(300)
    ranked = dispatch.rank_drivers(5, pickup, shifts, positions, load={})
    # Nearest right-type driver first; wrong-type tagged and last.
    assert ranked[0][0] == 2 and ranked[0][4] == ""
    assert ranked[-1][0] == 3 and ranked[-1][4] == "wrong-type"


@pytest.mark.django_db
def test_rank_tags_overloaded_driver():
    _shift(1, 11, type_id=5, lat=41.31, lng=69.24)
    shifts = list(DriverShiftState.objects.all())
    ranked = dispatch.rank_drivers(5, (41.31, 69.24), shifts, dispatch.fresh_positions(300), load={1: 1})
    assert ranked[0][4] == "overloaded"  # already has 1 active → not ideal


# ---- due rules -------------------------------------------------------------

@pytest.mark.django_db
def test_due_rules():
    now = timezone.now()
    urgent = _order(10, 5, 41.3, 69.2, is_urgent=True)
    assert dispatch.is_due(urgent, {}, now, lead_min=45, stale_sec=180) is True

    soon = _order(11, 5, 41.3, 69.2, planned_datetime=now + timedelta(minutes=30))
    later = _order(12, 5, 41.3, 69.2, planned_datetime=now + timedelta(minutes=90))
    assert dispatch.is_due(soon, {}, now, lead_min=45, stale_sec=180) is True
    assert dispatch.is_due(later, {}, now, lead_min=45, stale_sec=180) is False

    asap = _order(13, 5, 41.3, 69.2)  # no time
    assert dispatch.is_due(asap, {13: now}, now, lead_min=45, stale_sec=180) is False
    old = now + timedelta(seconds=200)
    assert dispatch.is_due(asap, {13: now}, old, lead_min=45, stale_sec=180) is True


# ---- end-to-end pass -------------------------------------------------------

@pytest.mark.django_db
def test_run_once_assigns_ideal_driver():
    _shift(1, 11, type_id=5, lat=41.31, lng=69.24)
    _order(20, 5, 41.31, 69.24, is_urgent=True)
    assigned = dispatch.run_once({}, lead_min=45, stale_sec=180, pos_max_age=300)
    assert assigned == [(20, 1)]
    assert OrderMeta.objects.get(order_id=20).driver_id == 1


@pytest.mark.django_db
def test_run_once_one_per_driver():
    """Only one on-shift driver → at most ONE of two urgent orders is assigned."""
    _shift(1, 11, type_id=5, lat=41.31, lng=69.24)
    _order(30, 5, 41.31, 69.24, is_urgent=True)
    _order(31, 5, 41.31, 69.24, is_urgent=True)
    assigned = dispatch.run_once({}, lead_min=45, stale_sec=180, pos_max_age=300)
    assert len(assigned) == 1  # the driver is busy after the first


@pytest.mark.django_db
def test_run_once_skips_wrong_type_and_not_due():
    _shift(1, 11, type_id=9, lat=41.31, lng=69.24)  # only a wrong-type driver
    _order(40, 5, 41.31, 69.24, is_urgent=True)  # urgent but no matching type
    _order(41, 5, 41.31, 69.24, planned_datetime=timezone.now() + timedelta(hours=3))  # not due
    assigned = dispatch.run_once({}, lead_min=45, stale_sec=180, pos_max_age=300)
    assert assigned == []


@pytest.mark.django_db
def test_run_once_skips_non_dispatchable():
    _shift(1, 11, type_id=5, lat=41.31, lng=69.24)
    _order(50, 5, 41.31, 69.24, is_urgent=True, dispatchable=False)  # not approved
    assigned = dispatch.run_once({}, lead_min=45, stale_sec=180, pos_max_age=300)
    assert assigned == []


# ---- runtime toggle --------------------------------------------------------

@pytest.mark.django_db
def test_auto_enabled_off_by_default_then_on():
    from car_orders.models import DispatchSettings

    assert dispatch.auto_enabled() is False  # singleton defaults off
    cfg = DispatchSettings.load()
    cfg.auto_enabled = True
    cfg.save()
    assert dispatch.auto_enabled() is True


@pytest.mark.django_db
def test_auto_enabled_respects_env_kill_switch(settings):
    from car_orders.models import DispatchSettings

    DispatchSettings.objects.update_or_create(pk=1, defaults={"auto_enabled": True})
    settings.AUTO_DISPATCH_ENABLED = False  # ops master switch off
    assert dispatch.auto_enabled() is False


@pytest.mark.django_db
def test_dispatchsettings_is_singleton():
    from car_orders.models import DispatchSettings

    DispatchSettings.load()
    DispatchSettings(auto_enabled=True).save()  # forced pk=1
    assert DispatchSettings.objects.count() == 1


@pytest.mark.django_db
def test_auto_dispatch_api_get_and_toggle():
    from rest_framework.test import APIClient

    client = APIClient()
    # Defaults: dispatcher toggle off.
    r = client.get("/api/v1/car-orders/auto-dispatch/")
    assert r.status_code == 200
    assert r.data["enabled"] is False
    # Flip it on.
    r = client.post("/api/v1/car-orders/auto-dispatch/", {"enabled": True}, format="json")
    assert r.status_code == 200
    assert r.data["enabled"] is True and r.data["effective"] is True
    assert dispatch.auto_enabled() is True
    # Bad body rejected.
    r = client.post("/api/v1/car-orders/auto-dispatch/", {}, format="json")
    assert r.status_code == 400
