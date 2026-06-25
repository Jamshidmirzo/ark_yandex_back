"""Unit tests for the single-source-of-truth effective-status resolver
(``car_orders.services.status``) and its CarOrderSerializer wiring."""

import pytest
from django.contrib.auth import get_user_model

from car_orders.models import CarOrder, OrderMeta
from car_orders.services.status import effective_status

S = CarOrder.Status
TS = OrderMeta.TripState
User = get_user_model()


class _Meta:
    """Lightweight OrderMeta stand-in — the resolver only reads these attrs."""

    def __init__(self, *, overlay_claimed=False, trip_state=TS.ASSIGNED, dispatchable=False):
        self.overlay_claimed = overlay_claimed
        self.trip_state = trip_state
        self.dispatchable = dispatchable


# ---- pure resolver ---------------------------------------------------------

def test_no_meta_is_passthrough():
    assert effective_status(S.AWAITING_DRIVER, None) == S.AWAITING_DRIVER
    assert effective_status(S.DRAFT, None) == S.DRAFT
    assert effective_status(None, None) is None


def test_overlay_claimed_active_trip_is_in_progress():
    assert effective_status(
        S.AWAITING_DRIVER, _Meta(overlay_claimed=True, trip_state=TS.ASSIGNED)
    ) == S.IN_PROGRESS
    assert effective_status(
        S.AWAITING_DRIVER, _Meta(overlay_claimed=True, trip_state=TS.IN_TRIP)
    ) == S.IN_PROGRESS


def test_overlay_claimed_completed():
    assert effective_status(
        S.AWAITING_DRIVER, _Meta(overlay_claimed=True, trip_state=TS.COMPLETED)
    ) == S.COMPLETED


def test_overlay_claimed_cancelled_falls_back_to_demo():
    assert effective_status(
        S.AWAITING_DRIVER, _Meta(overlay_claimed=True, trip_state=TS.CANCELLED)
    ) == S.AWAITING_DRIVER


def test_demo_terminal_wins_over_active_overlay():
    m = _Meta(overlay_claimed=True, trip_state=TS.IN_TRIP)
    assert effective_status(S.COMPLETED, m) == S.COMPLETED
    assert effective_status(S.REJECTED, m) == S.REJECTED


def test_terminal_overlay_resolves_even_when_demo_status_unknown():
    """A finished overlay ride whose demo body can't be read (driver history) still
    reports a status — previously it was None (no badge). Active orders keep returning
    None request-less (demo-backed contract)."""
    assert effective_status(None, _Meta(overlay_claimed=True, trip_state=TS.COMPLETED)) == S.COMPLETED
    # A cancelled order has dropped its claim (overlay_claimed=False) — still resolves.
    assert effective_status(None, _Meta(overlay_claimed=False, trip_state=TS.CANCELLED)) == S.CANCELLED
    # Active claim with unknown demo stays None (WS contract — HTTP refetch backfills).
    assert effective_status(None, _Meta(overlay_claimed=True, trip_state=TS.IN_TRIP)) is None


def test_direct_create_dispatchable_draft_pending_to_awaiting():
    m = _Meta(overlay_claimed=False, dispatchable=True)
    assert effective_status(S.DRAFT, m) == S.AWAITING_DRIVER
    assert effective_status(S.PENDING, m) == S.AWAITING_DRIVER
    # dispatchable only promotes draft/pending — not other statuses.
    assert effective_status(S.SCHEDULED, m) == S.SCHEDULED


def test_unclaimed_non_dispatchable_passthrough():
    m = _Meta(overlay_claimed=False, dispatchable=False)
    assert effective_status(S.SCHEDULED, m) == S.SCHEDULED


# ---- serializer wiring (single-object fallback path) -----------------------

@pytest.mark.django_db
def test_serializer_exposes_effective_status():
    from car_orders.serializers import CarOrderSerializer

    drv = User.objects.create(username="es-drv")
    order = CarOrder.objects.create(created_by=drv, status=S.AWAITING_DRIVER)
    OrderMeta.objects.create(
        order_id=order.pk, driver_id=drv.id, overlay_claimed=True, trip_state=TS.IN_TRIP
    )
    data = CarOrderSerializer(order).data
    # Raw status stays for native gating; effective reflects the live trip.
    assert data["status"] == S.AWAITING_DRIVER
    assert data["effective_status"] == S.IN_PROGRESS


@pytest.mark.django_db
def test_serializer_effective_status_without_meta():
    drv = User.objects.create(username="es-drv2")
    order = CarOrder.objects.create(created_by=drv, status=S.SCHEDULED)
    from car_orders.serializers import CarOrderSerializer

    data = CarOrderSerializer(order).data
    assert data["effective_status"] == S.SCHEDULED


# ---- status_map_for (batched backing-status lookup) ------------------------

@pytest.mark.django_db
def test_status_map_for_maps_local_orders_and_omits_missing():
    from car_orders.services.status import status_map_for

    drv = User.objects.create(username="sm-drv")
    a = CarOrder.objects.create(created_by=drv, status=S.AWAITING_DRIVER)
    b = CarOrder.objects.create(created_by=drv, status=S.COMPLETED)

    # A non-existent order_id is simply absent — callers get None (status unknown),
    # never a KeyError or a wrong status.
    m = status_map_for([a.pk, b.pk, 999_999])
    assert m == {a.pk: S.AWAITING_DRIVER, b.pk: S.COMPLETED}
    assert status_map_for([]) == {}


# ---- fleet snapshot decoration --------------------------------------------

@pytest.mark.django_db
def test_fleet_live_orders_carry_effective_status():
    from car_orders.fleet import fleet_live_orders

    drv = User.objects.create(username="fl-drv")
    # Overlay-claimed, live trip → effective reflects the trip even though demo
    # status stays awaiting_driver.
    order = CarOrder.objects.create(created_by=drv, status=S.AWAITING_DRIVER)
    OrderMeta.objects.create(
        order_id=order.pk, driver_id=drv.id, overlay_claimed=True, trip_state=TS.IN_TRIP
    )
    # An overlay row with NO backing local CarOrder (gateway/demo order not mirrored)
    # must not crash — effective_status falls back to None.
    OrderMeta.objects.create(
        order_id=888_888, driver_id=drv.id, overlay_claimed=True, trip_state=TS.IN_TRIP
    )

    rows = {r["order_id"]: r for r in fleet_live_orders()}

    assert rows[order.pk]["status"] == S.AWAITING_DRIVER
    assert rows[order.pk]["effective_status"] == S.IN_PROGRESS
    assert rows[888_888]["status"] is None
    assert rows[888_888]["effective_status"] is None


# ---- overlay-orders board decoration --------------------------------------

@pytest.mark.django_db
def test_overlay_rows_carry_effective_status():
    from car_orders.views import _overlay_rows

    drv = User.objects.create(username="ov-drv")
    order = CarOrder.objects.create(created_by=drv, status=S.AWAITING_DRIVER)
    OrderMeta.objects.create(
        order_id=order.pk, driver_id=drv.id, overlay_claimed=True, trip_state=TS.IN_TRIP
    )

    rows = _overlay_rows(OrderMeta.objects.filter(order_id=order.pk))
    assert len(rows) == 1
    assert rows[0]["status"] == S.AWAITING_DRIVER
    assert rows[0]["effective_status"] == S.IN_PROGRESS


@pytest.mark.django_db
def test_overlay_rows_backfill_demo_status_when_unmirrored(monkeypatch):
    """An overlay order living only upstream (no local CarOrder mirror) still gets a
    non-null status: with a request, the demo status is backfilled from the upstream
    body, so the board no longer reads `effective_status=null` (the desync bug)."""
    from django.test import RequestFactory

    from car_orders import views

    drv = User.objects.create(username="ov-bf-drv")
    # No local CarOrder for 777_001 — only the overlay row (the real-world case).
    OrderMeta.objects.create(
        order_id=777_001, driver_id=drv.id, overlay_claimed=True, trip_state=TS.IN_TRIP
    )
    monkeypatch.setattr(
        views,
        "_all_demo_orders",
        lambda req: {777_001: {"id": 777_001, "status": "awaiting_driver"}},
    )

    req = RequestFactory().get("/api/v1/car-orders/drivers/me/overlay-orders/")
    rows = views._overlay_rows(OrderMeta.objects.filter(order_id=777_001), request=req)
    assert rows[0]["status"] == S.AWAITING_DRIVER
    assert rows[0]["effective_status"] == S.IN_PROGRESS

    # Request-less (WS refresh) keeps the local-only contract: status unknown → None.
    ws_rows = views._overlay_rows(OrderMeta.objects.filter(order_id=777_001))
    assert ws_rows[0]["effective_status"] is None


@pytest.mark.django_db
def test_fleet_live_orders_backfill_demo_status_when_unmirrored(monkeypatch):
    """The fleet snapshot backfills the demo status for upstream-only orders too, so the
    dispatcher board matches the mobile board / list (request-less WS path unchanged)."""
    from django.test import RequestFactory

    from car_orders import views
    from car_orders.fleet import fleet_live_orders

    drv = User.objects.create(username="fl-bf-drv")
    OrderMeta.objects.create(
        order_id=777_002, driver_id=drv.id, overlay_claimed=True, trip_state=TS.IN_TRIP
    )
    monkeypatch.setattr(
        views,
        "_all_demo_orders",
        lambda req: {777_002: {"id": 777_002, "status": "awaiting_driver"}},
    )

    req = RequestFactory().get("/api/v1/car-orders/fleet/live/")
    rows = {r["order_id"]: r for r in fleet_live_orders(req)}
    assert rows[777_002]["status"] == S.AWAITING_DRIVER
    assert rows[777_002]["effective_status"] == S.IN_PROGRESS

    # Request-less keeps None (the existing WS contract — see the test above).
    ws_rows = {r["order_id"]: r for r in fleet_live_orders()}
    assert ws_rows[777_002]["effective_status"] is None


# ---- proxied list/detail enrichment (the gateway single-source-of-truth) --

@pytest.mark.django_db
def test_inject_effective_status_decorates_all_shapes():
    from car_orders.views import _inject_effective_status

    drv = User.objects.create(username="inj-drv")
    o = CarOrder.objects.create(created_by=drv, status=S.AWAITING_DRIVER)
    OrderMeta.objects.create(
        order_id=o.pk, driver_id=drv.id, overlay_claimed=True, trip_state=TS.IN_TRIP
    )

    # paginated {results:[...]}, a bare list, and a single detail dict all get decorated.
    paged = {"count": 1, "results": [{"id": o.pk, "status": "awaiting_driver"}]}
    _inject_effective_status(paged)
    assert paged["results"][0]["effective_status"] == S.IN_PROGRESS

    lst = [{"id": o.pk, "status": "awaiting_driver"}]
    _inject_effective_status(lst)
    assert lst[0]["effective_status"] == S.IN_PROGRESS

    one = {"id": o.pk, "status": "awaiting_driver"}
    _inject_effective_status(one)
    assert one["effective_status"] == S.IN_PROGRESS

    # An order with no overlay meta → demo status passes through unchanged.
    bare = {"id": 999_999, "status": "pending"}
    _inject_effective_status(bare)
    assert bare["effective_status"] == "pending"


@pytest.mark.django_db
def test_our_orders_list_returns_only_overlay_orders(monkeypatch):
    """The car-order LIST is narrowed to orders that have our OrderMeta; plain demo-only
    orders are hidden, and each row carries effective_status."""
    import json

    from django.test import RequestFactory

    from car_orders import views

    drv = User.objects.create(username="ours-drv")
    o1 = CarOrder.objects.create(created_by=drv, status=S.AWAITING_DRIVER)
    OrderMeta.objects.create(
        order_id=o1.pk, driver_id=drv.id, overlay_claimed=True, trip_state=TS.IN_TRIP
    )
    o2 = CarOrder.objects.create(created_by=drv, status=S.PENDING)
    OrderMeta.objects.create(order_id=o2.pk, driver_id=None, trip_state=TS.ASSIGNED)

    # Demo returns all three bodies; only o1/o2 have a local OrderMeta.
    fake_bodies = {
        o1.pk: {"id": o1.pk, "status": "awaiting_driver", "address": "A"},
        o2.pk: {"id": o2.pk, "status": "pending", "address": "B"},
        999_999: {"id": 999_999, "status": "pending", "address": "demo-only"},
    }
    monkeypatch.setattr(views, "_all_demo_orders", lambda req: fake_bodies)

    resp = views.car_order_proxy(RequestFactory().get("/api/v1/car-orders/"))
    data = json.loads(resp.content)
    ids = {r["id"] for r in data["results"]}
    assert ids == {o1.pk, o2.pk}          # demo-only #999999 is hidden
    assert data["count"] == 2
    byid = {r["id"]: r for r in data["results"]}
    assert byid[o1.pk]["effective_status"] == S.IN_PROGRESS   # claimed + active trip
    assert byid[o2.pk]["effective_status"] == S.PENDING       # neither claimed nor dispatchable


@pytest.mark.django_db
def test_my_overlay_orders_view_exposes_effective_status(auth_client):
    """The dispatcher board (whole active set) returns the reconciled status."""
    from car_orders.tests.conftest import DISPATCHER

    drv = User.objects.create(username="ov-view-drv")
    order = CarOrder.objects.create(created_by=drv, status=S.AWAITING_DRIVER)
    OrderMeta.objects.create(
        order_id=order.pk, driver_id=drv.id, overlay_claimed=True, trip_state=TS.IN_TRIP
    )

    client = auth_client(perms=DISPATCHER)
    resp = client.get("/api/v1/car-orders/drivers/me/overlay-orders/")
    assert resp.status_code == 200
    row = next(r for r in resp.json() if r["order_id"] == order.pk)
    assert row["effective_status"] == S.IN_PROGRESS
