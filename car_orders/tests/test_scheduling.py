"""Unit tests for the scheduling helpers (``car_orders.scheduling``).

The window/buffer/gap-fill maths is the heart of «один водитель — несколько
заказов в свободных окнах». Most of it was only exercised indirectly via the API;
these pin the pure functions: driving_end (service-time gap), meta_conflict
(parked states free the driver, null windows ignored), find_time_conflict, the
overrun projected_start, and the in-memory meta_active_trip index.
"""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from car_orders import scheduling
from car_orders.models import CarOrder, OrderMeta

User = get_user_model()
TS = OrderMeta.TripState
S = CarOrder.Status


@pytest.fixture
def requester(db):
    return User.objects.create_user(username="req", password="pw")


@pytest.fixture
def driver(db):
    return User.objects.create_user(username="drv", password="pw")


def _meta(order_id, driver_id, state, start, dur_min, service=0):
    return OrderMeta.objects.create(
        order_id=order_id, driver_id=driver_id, trip_state=state,
        planned_datetime=start, estimated_duration=dur_min, service_time=service,
    )


# ---- driving_end: the service-time gap ------------------------------------

def test_driving_end_subtracts_service_time():
    start = timezone.now()
    end = start + timedelta(hours=3)
    assert scheduling.driving_end(start, end, 60) == end - timedelta(minutes=60)


def test_driving_end_without_service_is_full_end():
    start = timezone.now()
    end = start + timedelta(hours=3)
    assert scheduling.driving_end(start, end, 0) == end
    assert scheduling.driving_end(start, end, None) == end


def test_driving_end_never_before_start():
    start = timezone.now()
    end = start + timedelta(minutes=30)
    # Service longer than the whole window → fall back to the full end, not before start.
    assert scheduling.driving_end(start, end, 120) == end


def test_driving_end_none_window():
    assert scheduling.driving_end(None, None, 30) is None
    assert scheduling.driving_end(timezone.now(), None, 30) is None


# ---- meta_conflict: parked frees, moving blocks, nulls ignored ------------

@pytest.mark.django_db
def test_meta_conflict_moving_order_blocks():
    base = timezone.now()
    _meta(1, 5, TS.IN_TRIP, base, 120)  # busy 0–2h
    c = scheduling.meta_conflict(5, base + timedelta(minutes=30), base + timedelta(minutes=90))
    assert c is not None and c.order_id == 1


@pytest.mark.django_db
def test_meta_conflict_parked_order_does_not_block():
    base = timezone.now()
    _meta(1, 5, TS.AT_DESTINATION, base, 120)
    _meta(2, 5, TS.WAITING, base, 120)
    assert scheduling.meta_conflict(5, base + timedelta(minutes=30), base + timedelta(minutes=90)) is None


@pytest.mark.django_db
def test_meta_conflict_ignores_null_window():
    base = timezone.now()
    _meta(1, 5, TS.ASSIGNED, None, None)  # no planned window
    assert scheduling.meta_conflict(5, base, base + timedelta(hours=1)) is None


@pytest.mark.django_db
def test_meta_conflict_excludes_self():
    base = timezone.now()
    _meta(1, 5, TS.ASSIGNED, base, 120)
    assert scheduling.meta_conflict(5, base, base + timedelta(minutes=120), exclude_order_id=1) is None


@pytest.mark.django_db
def test_meta_conflict_service_time_opens_a_gap():
    base = timezone.now()
    # 5h window but a 4h on-site tail → the driver only DRIVES the first hour, so a
    # gap order two hours in must NOT conflict.
    _meta(1, 5, TS.ASSIGNED, base, 300, service=240)
    assert scheduling.meta_conflict(
        5, base + timedelta(hours=2), base + timedelta(hours=3)
    ) is None


@pytest.mark.django_db
def test_meta_conflict_none_for_null_driver():
    assert scheduling.meta_conflict(None, timezone.now(), timezone.now()) is None


# ---- find_time_conflict (native CarOrder) ---------------------------------

@pytest.mark.django_db
def test_find_time_conflict_blocks_and_respects_exclude(requester, driver):
    base = timezone.now()
    occupied = CarOrder.objects.create(
        created_by=requester, driver=driver, status=S.SCHEDULED,
        planned_datetime=base, estimated_duration=timedelta(hours=2),
    )
    hit = scheduling.find_time_conflict(driver, base + timedelta(minutes=30), base + timedelta(minutes=90))
    assert hit is not None and hit.pk == occupied.pk
    # Excluding the only committed order leaves the window free.
    assert scheduling.find_time_conflict(
        driver, base, base + timedelta(hours=2), exclude_id=occupied.pk
    ) is None


@pytest.mark.django_db
def test_order_window_none_when_unscheduled(requester):
    order = CarOrder.objects.create(created_by=requester, status=S.PENDING)
    assert scheduling.order_window(order) is None


# ---- active_trip + projected_start (overrun) ------------------------------

@pytest.mark.django_db
def test_active_trip_excludes_self(requester, driver):
    o = CarOrder.objects.create(
        created_by=requester, driver=driver, status=S.IN_PROGRESS,
        planned_datetime=timezone.now(), estimated_duration=timedelta(hours=1),
    )
    assert scheduling.active_trip(driver, exclude_id=o.pk) is None
    assert scheduling.active_trip(driver).pk == o.pk


@pytest.mark.django_db
def test_projected_start_pushes_past_an_overrunning_trip(requester, driver):
    now = timezone.now()
    # Current trip was due to finish an hour ago but is still in progress.
    CarOrder.objects.create(
        created_by=requester, driver=driver, status=S.IN_PROGRESS,
        planned_datetime=now - timedelta(hours=3), estimated_duration=timedelta(hours=2),
    )
    nxt = CarOrder.objects.create(
        created_by=requester, driver=driver, status=S.SCHEDULED,
        planned_datetime=now - timedelta(minutes=10), estimated_duration=timedelta(hours=1),
    )
    ps = scheduling.projected_start(nxt, now)
    assert ps >= now + scheduling.travel_buffer()  # can't start until the overrun clears


@pytest.mark.django_db
def test_projected_start_is_base_when_no_active_trip(requester, driver):
    now = timezone.now()
    nxt = CarOrder.objects.create(
        created_by=requester, driver=driver, status=S.SCHEDULED,
        planned_datetime=now + timedelta(hours=1), estimated_duration=timedelta(hours=1),
    )
    assert scheduling.projected_start(nxt, now) == now + timedelta(hours=1)


@pytest.mark.django_db
def test_needs_reassign_false_without_latest_start(requester, driver):
    now = timezone.now()
    nxt = CarOrder.objects.create(
        created_by=requester, driver=driver, status=S.SCHEDULED,
        planned_datetime=now, estimated_duration=timedelta(hours=1), latest_start=None,
    )
    assert scheduling.needs_reassign(nxt, now) is False


# ---- meta_active_trip in-memory index -------------------------------------

@pytest.mark.django_db
def test_meta_active_trip_uses_inmemory_index_without_querying(django_assert_num_queries):
    m = _meta(1, 5, TS.IN_TRIP, timezone.now(), 120)
    index = {5: [m]}
    with django_assert_num_queries(0):
        found = scheduling.meta_active_trip(5, active=index, states=scheduling.MOVING_STATES)
    assert found.order_id == 1
    with django_assert_num_queries(0):
        assert scheduling.meta_active_trip(
            5, exclude_order_id=1, active=index, states=scheduling.MOVING_STATES
        ) is None
