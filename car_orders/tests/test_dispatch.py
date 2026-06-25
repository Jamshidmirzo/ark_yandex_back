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


@pytest.mark.django_db
def test_rank_tags_reassigned_off_driver():
    """A driver the dispatcher took the order off is tagged (never ideal), so the
    nearest free of the right type isn't auto-picked again."""
    _shift(1, 11, type_id=5, lat=41.31, lng=69.24)  # ideal but reassigned-off
    _shift(2, 12, type_id=5, lat=41.30, lng=69.20)  # ideal, farther
    shifts = list(DriverShiftState.objects.all())
    ranked = dispatch.rank_drivers(
        5, (41.311, 69.241), shifts, dispatch.fresh_positions(300), load={}, excluded={1}
    )
    assert ranked[0][0] == 2 and ranked[0][4] == ""  # the OTHER driver is best now
    assert ranked[-1][0] == 1 and ranked[-1][4] == "reassigned-off"


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
    # …but a fresh ASAP order with an IDEAL driver already free skips the wait.
    assert dispatch.is_due(asap, {13: now}, now, lead_min=45, stale_sec=180, has_ideal=True) is True


@pytest.mark.django_db
def test_run_once_assigns_fresh_asap_when_ideal_driver_free():
    """The «подходит, но не назначается быстро» fix: a just-created ASAP order (not
    urgent, no planned time, first seen now) is assigned on THIS pass when a free
    suitable driver exists — instead of waiting out the 3-min stale window."""
    _shift(1, 11, type_id=5, lat=41.31, lng=69.24)
    _order(21, 5, 41.31, 69.24)  # plain ASAP, freshly queued
    now = timezone.now()
    assigned = dispatch.run_once({21: now}, now=now, lead_min=45, stale_sec=180, pos_max_age=300)
    assert assigned == [(21, 1)]


@pytest.mark.django_db
def test_run_once_asap_still_waits_when_no_free_driver():
    """The stale wait only applies when nothing is free: a fresh ASAP order with no
    IDEAL candidate (the only driver is overloaded) stays queued, unchanged."""
    _shift(1, 11, type_id=5, lat=41.31, lng=69.24)
    # The only driver is already on an active trip → load 1, so not an IDEAL candidate.
    _order(22, 5, 41.31, 69.24, driver_id=1, trip_state=OrderMeta.TripState.IN_TRIP)
    _order(23, 5, 41.31, 69.24)  # fresh ASAP, no free suitable driver
    now = timezone.now()
    assigned = dispatch.run_once({23: now}, now=now, lead_min=45, stale_sec=180, pos_max_age=300)
    assert assigned == []
    assert OrderMeta.objects.get(order_id=23).driver_id is None


@pytest.mark.django_db
def test_run_once_urgent_beats_fresh_asap_for_the_only_driver():
    """Priority: with one ideal driver and both a fresh ASAP and an urgent order
    waiting, the URGENT one claims the driver (the ASAP no longer jumps the queue just
    because it now assigns immediately when a driver is free)."""
    _shift(1, 11, type_id=5, lat=41.31, lng=69.24)
    now = timezone.now()
    _order(24, 5, 41.31, 69.24)  # fresh ASAP (lower id — would be scanned first by default)
    _order(25, 5, 41.31, 69.24, is_urgent=True)  # urgent (higher priority)
    assigned = dispatch.run_once({24: now, 25: now}, now=now, lead_min=45, stale_sec=180, pos_max_age=300)
    assert assigned == [(25, 1)]  # urgent took the only driver
    assert OrderMeta.objects.get(order_id=24).driver_id is None


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


@pytest.mark.django_db
def test_run_once_never_reassigns_to_excluded_driver():
    """The only on-shift driver was taken off this order → it stays driverless
    rather than bouncing straight back to them."""
    _shift(1, 11, type_id=5, lat=41.31, lng=69.24)
    _order(60, 5, 41.31, 69.24, is_urgent=True, excluded_driver_ids=[1])
    assigned = dispatch.run_once({}, lead_min=45, stale_sec=180, pos_max_age=300)
    assert assigned == []
    assert OrderMeta.objects.get(order_id=60).driver_id is None


@pytest.mark.django_db
def test_run_once_assigns_a_different_driver_when_one_is_excluded():
    """Excluding one driver doesn't strand the order — another eligible driver
    still gets it."""
    _shift(1, 11, type_id=5, lat=41.311, lng=69.241)  # nearest, but excluded
    _shift(2, 12, type_id=5, lat=41.30, lng=69.20)  # farther, eligible
    _order(61, 5, 41.311, 69.241, is_urgent=True, excluded_driver_ids=[1])
    assigned = dispatch.run_once({}, lead_min=45, stale_sec=180, pos_max_age=300)
    assert assigned == [(61, 2)]


@pytest.mark.django_db
def test_claim_rejects_excluded_driver():
    """Even called directly, claim refuses an excluded driver (invariant guard)."""
    _order(62, 5, 41.31, 69.24, excluded_driver_ids=[1])
    assert dispatch.claim(62, driver_id=1, car_id=11, car_label="Cobalt (01A)") is False
    assert OrderMeta.objects.get(order_id=62).driver_id is None


# ---- reaper: free drivers stuck on an abandoned order ----------------------


def _pin(order_id, driver_id, *, trip_state="assigned", updated_age_sec=1800):
    """A driver pinned by a non-terminal order whose state last changed long ago."""
    m = OrderMeta.objects.create(
        order_id=order_id, driver_id=driver_id, overlay_claimed=True, trip_state=trip_state,
        dispatchable=True, origin_lat=41.31, origin_lng=69.24, address_lat=41.35, address_lng=69.29,
    )
    old = timezone.now() - timedelta(seconds=updated_age_sec)
    OrderMeta.objects.filter(order_id=order_id).update(updated_at=old)  # bypass auto_now
    return m


@pytest.mark.django_db
def test_reap_frees_driver_gone_dark():
    """A driver with NO GPS, pinned by a stale `assigned` order they never started →
    requeued (driver freed, order back in the queue), WITHOUT a permanent exclusion."""
    _pin(70, driver_id=1)  # no DriverPosition at all
    freed = dispatch.reap_abandoned(abandon_sec=600)
    assert freed == [(70, 1)]
    m = OrderMeta.objects.get(order_id=70)
    assert m.driver_id is None and m.dispatchable is True  # back in the queue, unpinned
    assert not (m.excluded_driver_ids or [])  # NOT permanently barred — avoids starvation


@pytest.mark.django_db
def test_reap_skips_started_trip():
    """A STARTED order (driver reached/serving the customer, or parked during a shoot)
    is never auto-reaped on a GPS gap — only a human unwinds those."""
    for oid, ts in ((75, "to_client"), (76, "in_trip"), (77, "at_destination"), (78, "waiting")):
        _pin(oid, driver_id=oid, trip_state=ts)  # all dark (no DriverPosition), all stale
    assert dispatch.reap_abandoned(abandon_sec=600) == []
    assert OrderMeta.objects.filter(driver_id__isnull=False).count() == 4  # all still pinned


@pytest.mark.django_db
def test_reap_skips_future_scheduled():
    """A scheduled order assigned early for a FUTURE pickup isn't reaped before its
    time — the driver isn't expected to be moving (or streaming GPS) yet."""
    m = _pin(79, driver_id=1)  # assigned, dark, stale updated_at
    OrderMeta.objects.filter(order_id=79).update(
        planned_datetime=timezone.now() + timedelta(hours=2)
    )
    assert dispatch.reap_abandoned(abandon_sec=600) == []
    assert OrderMeta.objects.get(order_id=79).driver_id == 1


@pytest.mark.django_db
def test_reap_then_assign_routes_to_a_live_driver_not_the_dark_one():
    """End-to-end: a dark driver is pinned, a second driver is online. After reap+run_once
    the order goes to the LIVE driver — the dark one ranks last (no GPS), so no thrash."""
    _shift(1, 11, type_id=5, lat=41.31, lng=69.24)  # dark (stale GPS)
    DriverPosition.objects.filter(driver_id=1).update(
        last_seen=timezone.now() - timedelta(seconds=3600)
    )
    _shift(2, 12, type_id=5, lat=41.31, lng=69.24)  # online (fresh GPS from _shift)
    _pin(80, driver_id=1)
    assert dispatch.reap_abandoned(abandon_sec=600) == [(80, 1)]
    assigned = dispatch.run_once({}, now=timezone.now(), lead_min=45, stale_sec=180, pos_max_age=300)
    assert assigned == [(80, 2)]  # the live driver, never bounced back to the dark one


@pytest.mark.django_db
def test_reap_keeps_driver_with_fresh_gps():
    """A pinned driver who is still heartbeating (recent GPS) is NOT reaped."""
    _pin(71, driver_id=1)
    DriverPosition.objects.create(driver_id=1, lat=41.31, lng=69.24, last_seen=timezone.now())
    assert dispatch.reap_abandoned(abandon_sec=600) == []
    assert OrderMeta.objects.get(order_id=71).driver_id == 1


@pytest.mark.django_db
def test_reap_skips_fresh_assignment():
    """A just-assigned order (state changed now) isn't reaped even with no GPS yet —
    the driver hasn't had time to send a first fix."""
    _pin(72, driver_id=1, updated_age_sec=0)  # updated_at ≈ now
    assert dispatch.reap_abandoned(abandon_sec=600) == []
    assert OrderMeta.objects.get(order_id=72).driver_id == 1


@pytest.mark.django_db
def test_reap_disabled_with_zero():
    _pin(73, driver_id=1)
    assert dispatch.reap_abandoned(abandon_sec=0) == []
    assert OrderMeta.objects.get(order_id=73).driver_id == 1


# ---- address backfill ------------------------------------------------------


@pytest.mark.django_db
def test_fill_missing_addresses(monkeypatch):
    """Overlay order with coords but no address text → reverse-geocoded «откуда/куда»;
    idempotent once filled."""
    from car_orders.services import geocode

    monkeypatch.setattr(geocode, "reverse", lambda lat, lng: f"addr {round(lat, 2)},{round(lng, 2)}")
    OrderMeta.objects.create(
        order_id=300, driver_id=1, trip_state="assigned",
        origin_lat=41.31, origin_lng=69.24, address_lat=41.35, address_lng=69.29,
    )
    assert dispatch.fill_missing_addresses(limit=5) == 1
    m = OrderMeta.objects.get(order_id=300)
    assert m.origin_address == "addr 41.31,69.24" and m.dest_address == "addr 41.35,69.29"
    assert dispatch.fill_missing_addresses(limit=5) == 0  # nothing left to fill


@pytest.mark.django_db
def test_fill_missing_addresses_fills_terminal_skips_coordless(monkeypatch):
    """Terminal (history) orders WITH coords are filled too; a coordless order is skipped."""
    from car_orders.services import geocode

    monkeypatch.setattr(geocode, "reverse", lambda lat, lng: "X")
    OrderMeta.objects.create(order_id=301, trip_state="completed", origin_lat=41.0, origin_lng=69.0)
    OrderMeta.objects.create(order_id=302, trip_state="assigned")  # no coords → skipped
    assert dispatch.fill_missing_addresses(limit=5) == 1  # only the terminal-with-coords one
    assert OrderMeta.objects.get(order_id=301).origin_address == "X"


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
