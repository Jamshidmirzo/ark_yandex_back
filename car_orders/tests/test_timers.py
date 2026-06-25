"""Unit tests for the two order timers — «поиск водителя» (search) and «ожидание
клиента на подаче» (pickup wait) — and the «клиент не вышел» no-show cancel.

Covers: where the two start-timestamps get stamped / reset, the serializer's
computed elapsed + overdue fields, and the no-show service + endpoint (overlay
teardown + native CarOrder mirror + audit)."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from car_orders.models import CarOrder, CarOrderActivity, OrderMeta
from car_orders.serializers import OrderMetaSerializer
from car_orders.services import overlay, trip_state

S = CarOrder.Status
TS = OrderMeta.TripState
User = get_user_model()


def _meta(state, *, order_id=900, driver_id=5, **extra):
    return OrderMeta.objects.create(
        order_id=order_id, driver_id=driver_id, overlay_claimed=True, trip_state=state,
        origin_lat=41.31, origin_lng=69.24, address_lat=41.35, address_lng=69.29,
        **extra,
    )


def _native_order(status, driver):
    return CarOrder.objects.create(
        created_by=driver, driver=driver, status=status,
        planned_datetime=timezone.now(), estimated_duration=timedelta(hours=1),
    )


# ---- arrived_at: stamped once on arrival, cleared on teardown --------------

@override_settings(CAR_ORDER_OSRM_URL="", CAR_ORDER_ARRIVAL_GEOFENCE_M=0)
@pytest.mark.django_db
def test_arrived_at_stamped_on_first_at_client():
    _meta(TS.TO_CLIENT)
    meta = trip_state.advance(900, TS.AT_CLIENT)
    assert meta.arrived_at is not None


@override_settings(CAR_ORDER_OSRM_URL="", CAR_ORDER_ARRIVAL_GEOFENCE_M=0)
@pytest.mark.django_db
def test_arrived_at_not_overwritten_on_retap():
    _meta(TS.TO_CLIENT)
    first = trip_state.advance(900, TS.AT_CLIENT).arrived_at
    # Same-state re-tap (at_client → at_client is always allowed) must not move it.
    again = trip_state.advance(900, TS.AT_CLIENT).arrived_at
    assert again == first


@pytest.mark.django_db
def test_arrived_at_cleared_on_release_and_reassign():
    _meta(TS.AT_CLIENT, order_id=901, arrived_at=timezone.now())
    overlay.release(901, requeue=True)
    assert OrderMeta.objects.get(order_id=901).arrived_at is None

    _meta(TS.AT_CLIENT, order_id=902, arrived_at=timezone.now())
    overlay.reassign(902)
    assert OrderMeta.objects.get(order_id=902).arrived_at is None


# ---- search_started_at: stamped on queue-entry, reset on requeue -----------

@pytest.mark.django_db
def test_mark_searching_stamps_when_queued_and_is_idempotent():
    OrderMeta.objects.create(order_id=910, driver_id=None, dispatchable=True)
    overlay.mark_searching(910)
    first = OrderMeta.objects.get(order_id=910).search_started_at
    assert first is not None
    # A re-approve / repeated upsert must NOT restart the running search clock.
    overlay.mark_searching(910)
    assert OrderMeta.objects.get(order_id=910).search_started_at == first


@pytest.mark.django_db
def test_mark_searching_noop_when_not_searching():
    # Not dispatchable → no clock.
    OrderMeta.objects.create(order_id=911, driver_id=None, dispatchable=False)
    overlay.mark_searching(911)
    assert OrderMeta.objects.get(order_id=911).search_started_at is None
    # Already has a driver → search is over, don't stamp.
    OrderMeta.objects.create(order_id=912, driver_id=7, dispatchable=True)
    overlay.mark_searching(912)
    assert OrderMeta.objects.get(order_id=912).search_started_at is None


@pytest.mark.django_db
def test_search_clock_restarts_on_requeue():
    old = timezone.now() - timedelta(hours=1)
    _meta(TS.AT_CLIENT, order_id=913, search_started_at=old)
    overlay.release(913, requeue=True)
    restarted = OrderMeta.objects.get(order_id=913).search_started_at
    assert restarted is not None and restarted > old


# ---- serializer: computed elapsed + overdue --------------------------------

@pytest.mark.django_db
def test_serializer_search_elapsed_only_while_driverless():
    searching = OrderMeta.objects.create(
        order_id=920, driver_id=None, dispatchable=True,
        search_started_at=timezone.now() - timedelta(seconds=90),
    )
    data = OrderMetaSerializer(searching).data
    assert data["search_elapsed_s"] >= 90
    assert data["wait_elapsed_s"] is None

    # Once a driver is assigned the search timer disappears.
    assigned = OrderMeta.objects.create(
        order_id=921, driver_id=7, dispatchable=True,
        search_started_at=timezone.now() - timedelta(seconds=90),
    )
    assert OrderMetaSerializer(assigned).data["search_elapsed_s"] is None


@override_settings(CAR_ORDER_PICKUP_WAIT_LIMIT_S=1800)
@pytest.mark.django_db
def test_serializer_wait_elapsed_and_overdue_only_at_client():
    waiting = _meta(TS.AT_CLIENT, order_id=922, arrived_at=timezone.now() - timedelta(seconds=120))
    data = OrderMetaSerializer(waiting).data
    assert data["wait_elapsed_s"] >= 120
    assert data["wait_limit_s"] == 1800
    assert data["wait_overdue"] is False  # 2 min < 30 min

    # Not at_client → no wait timer even with an arrived_at lingering.
    enroute = _meta(TS.TO_CLIENT, order_id=923, arrived_at=timezone.now())
    assert OrderMetaSerializer(enroute).data["wait_elapsed_s"] is None


@override_settings(CAR_ORDER_PICKUP_WAIT_LIMIT_S=1800)
@pytest.mark.django_db
def test_serializer_wait_overdue_flips_at_the_limit():
    over = _meta(TS.AT_CLIENT, order_id=924, arrived_at=timezone.now() - timedelta(seconds=1800))
    assert OrderMetaSerializer(over).data["wait_overdue"] is True
    under = _meta(TS.AT_CLIENT, order_id=925, arrived_at=timezone.now() - timedelta(seconds=1799))
    assert OrderMetaSerializer(under).data["wait_overdue"] is False


# ---- no-show cancel service -----------------------------------------------

@pytest.mark.django_db
def test_no_show_requires_at_client():
    _meta(TS.TO_CLIENT, order_id=930, driver_id=5)
    with pytest.raises(overlay.OverlayError) as exc:
        overlay.cancel_no_show(930, actor_driver_id=5)
    assert exc.value.code == "INVALID_STATUS"


@pytest.mark.django_db
def test_no_show_not_found():
    with pytest.raises(overlay.OverlayError) as exc:
        overlay.cancel_no_show(404, actor_driver_id=5)
    assert exc.value.code == "NOT_FOUND"


@pytest.mark.django_db
def test_no_show_denied_for_non_assigned_driver():
    _meta(TS.AT_CLIENT, order_id=931, driver_id=5, arrived_at=timezone.now())
    with pytest.raises(overlay.OverlayError) as exc:
        overlay.cancel_no_show(931, actor_driver_id=9, is_dispatcher=False)
    assert exc.value.code == "PERMISSION_DENIED"


@pytest.mark.django_db
def test_no_show_tears_down_overlay_and_mirrors_native():
    driver = User.objects.create(username="no-show-drv")
    order = _native_order(S.AWAITING_DRIVER, driver)
    _meta(
        TS.AT_CLIENT, order_id=order.pk, driver_id=driver.id,
        arrived_at=timezone.now() - timedelta(seconds=2000),
    )

    meta, waited_s = overlay.cancel_no_show(order.pk, actor=driver, actor_driver_id=driver.id)

    # Overlay torn down to terminal CANCELLED, claim cleared.
    assert meta.trip_state == TS.CANCELLED
    assert meta.driver_id is None
    # Native CarOrder mirrored to CANCELLED.
    order.refresh_from_db()
    assert order.status == S.CANCELLED
    assert order.finished_at is not None
    # Audited with the no-show reason + how long the driver waited.
    act = CarOrderActivity.objects.filter(order=order, kind=CarOrderActivity.Kind.CANCELLED).get()
    assert act.payload.get("reason") == "client_no_show"
    assert act.payload.get("waited_s") == waited_s and waited_s >= 2000


# ---- no-show endpoint ------------------------------------------------------

@pytest.mark.django_db
def test_no_show_endpoint_dispatcher_cancels(auth_client):
    from car_orders.tests.conftest import DISPATCHER

    driver = User.objects.create(username="ep-drv")
    order = _native_order(S.AWAITING_DRIVER, driver)
    _meta(TS.AT_CLIENT, order_id=order.pk, driver_id=driver.id, arrived_at=timezone.now())

    client = auth_client(perms=DISPATCHER)
    resp = client.post(f"/api/v1/car-orders/{order.pk}/no-show/", {}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["meta"]["trip_state"] == TS.CANCELLED
    order.refresh_from_db()
    assert order.status == S.CANCELLED


@pytest.mark.django_db
def test_no_show_endpoint_400_when_not_at_client(auth_client):
    from car_orders.tests.conftest import DISPATCHER

    _meta(TS.TO_CLIENT, order_id=940, driver_id=5)
    client = auth_client(perms=DISPATCHER)
    resp = client.post("/api/v1/car-orders/940/no-show/", {}, format="json")
    assert resp.status_code == 400
