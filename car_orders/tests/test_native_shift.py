"""API tests for the NATIVE driver-shift endpoints (``DriverViewSet`` me/shift,
me/schedule, me/cars) — the ``DriverShift`` model path (Р1), distinct from the
``DriverShiftState`` overlay path covered by ``test_shift_swap.py``.

Run against the standalone wiring (router mounted locally) — see tests/urls.py.
"""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from auth_core.models import AccessGroup, UserAccessGroup
from car_orders.models import Car, CarOrder, CarType, DriverShift

User = get_user_model()

pytestmark = pytest.mark.urls("car_orders.tests.urls")


def _user(username, *groups):
    u = User.objects.create_user(username=username, password="pw")
    for name in groups:
        UserAccessGroup.objects.create(user=u, group=AccessGroup.objects.get(name=name))
    return u


def _client(u):
    c = APIClient()
    c.force_authenticate(user=u)
    return c


@pytest.fixture
def env(db):
    req = _user("req", "Car Requester")
    drv = _user("drv", "Driver")
    ct = CarType.objects.create(name="Легковая")
    car = Car.objects.create(model="Damas", plate_number="01A001AA", type=ct, status="active")
    car.drivers.add(drv)
    return {"req_user": req, "drv_user": drv, "drv": _client(drv), "req": _client(req),
            "ct": ct, "car": car}


SHIFT = "/api/v1/car-orders/drivers/me/shift/"


def _go_on_shift(env, car=None):
    car = car or env["car"]
    return env["drv"].patch(SHIFT, {"car_id": car.id}, format="json")


# ---- PATCH guards ----------------------------------------------------------

@pytest.mark.django_db
def test_shift_rejects_car_not_assigned_to_driver(env):
    other = Car.objects.create(model="Faw", plate_number="02A002AA", type=env["ct"], status="active")
    r = env["drv"].patch(SHIFT, {"car_id": other.id}, format="json")
    assert r.status_code == 403


@pytest.mark.django_db
def test_shift_rejects_inactive_car(env):
    repair = Car.objects.create(
        model="Faw", plate_number="02A002AA", type=env["ct"], status="in_repair"
    )
    repair.drivers.add(env["drv_user"])
    r = env["drv"].patch(SHIFT, {"car_id": repair.id}, format="json")
    assert r.status_code == 400
    assert r.data["error"]["code"] == "CAR_UNAVAILABLE"


@pytest.mark.django_db
def test_shift_rejects_car_on_another_drivers_shift(env):
    other_driver = _user("drv2", "Driver")
    DriverShift.objects.create(driver=other_driver, car=env["car"])  # car already taken
    r = _go_on_shift(env)
    assert r.status_code == 400
    assert r.data["error"]["code"] == "CAR_BUSY"


# ---- PATCH switch ----------------------------------------------------------

@pytest.mark.django_db
def test_switch_car_ends_old_and_starts_new(env):
    car2 = Car.objects.create(model="Cobalt", plate_number="03A003AA", type=env["ct"], status="active")
    car2.drivers.add(env["drv_user"])
    assert _go_on_shift(env).status_code == 200
    assert _go_on_shift(env, car2).status_code == 200
    shifts = DriverShift.objects.filter(driver=env["drv_user"]).order_by("created_at")
    assert shifts.count() == 2
    assert shifts[0].ended_at is not None  # old shift closed
    active = DriverShift.objects.get(driver=env["drv_user"], ended_at__isnull=True)
    assert active.car_id == car2.id and active.status == DriverShift.Status.ONLINE


@pytest.mark.django_db
def test_switch_blocked_mid_trip(env):
    car2 = Car.objects.create(model="Cobalt", plate_number="03A003AA", type=env["ct"], status="active")
    car2.drivers.add(env["drv_user"])
    _go_on_shift(env)
    CarOrder.objects.create(
        created_by=env["req_user"], driver=env["drv_user"], car=env["car"],
        status=CarOrder.Status.IN_PROGRESS,
    )
    r = _go_on_shift(env, car2)
    assert r.status_code == 400
    assert r.data["error"]["code"] == "DRIVER_BUSY"


# ---- DELETE / GET ----------------------------------------------------------

@pytest.mark.django_db
def test_end_shift_blocked_mid_trip(env):
    _go_on_shift(env)
    CarOrder.objects.create(
        created_by=env["req_user"], driver=env["drv_user"], car=env["car"],
        status=CarOrder.Status.IN_PROGRESS,
    )
    r = env["drv"].delete(SHIFT)
    assert r.status_code == 400
    assert r.data["error"]["code"] == "DRIVER_BUSY"


@pytest.mark.django_db
def test_end_shift_when_free_sets_offline(env):
    _go_on_shift(env)
    r = env["drv"].delete(SHIFT)
    assert r.status_code == 200
    shift = DriverShift.objects.get(driver=env["drv_user"])
    assert shift.ended_at is not None and shift.status == DriverShift.Status.OFFLINE


@pytest.mark.django_db
def test_get_shift_returns_current_or_null(env):
    assert env["drv"].get(SHIFT).data is None
    _go_on_shift(env)
    data = env["drv"].get(SHIFT).data
    assert data["car"]["plate_number"] == "01A001AA"
    assert data["status"] == "online"


# ---- me/schedule + me/cars -------------------------------------------------

@pytest.mark.django_db
def test_my_schedule_requires_permission(env):
    # The requester isn't a driver → no driver:accept_order.
    assert env["req"].get("/api/v1/car-orders/drivers/me/schedule/").status_code == 403


@pytest.mark.django_db
def test_my_schedule_lists_committed_orders_in_order(env):
    now = timezone.now()
    later = CarOrder.objects.create(
        created_by=env["req_user"], driver=env["drv_user"], status=CarOrder.Status.SCHEDULED,
        planned_datetime=now + timedelta(hours=2),
    )
    sooner = CarOrder.objects.create(
        created_by=env["req_user"], driver=env["drv_user"], status=CarOrder.Status.IN_PROGRESS,
        planned_datetime=now + timedelta(hours=1),
    )
    # A finished order must not appear.
    CarOrder.objects.create(
        created_by=env["req_user"], driver=env["drv_user"], status=CarOrder.Status.COMPLETED,
        planned_datetime=now,
    )
    sched = env["drv"].get("/api/v1/car-orders/drivers/me/schedule/").data
    assert [o["id"] for o in sched] == [sooner.id, later.id]


@pytest.mark.django_db
def test_my_cars_lists_driven_cars(env):
    cars = env["drv"].get("/api/v1/car-orders/drivers/me/cars/").data
    assert [c["plate_number"] for c in cars] == ["01A001AA"]


@pytest.mark.django_db
def test_shift_create_race_maps_to_car_busy(env, monkeypatch):
    # AUDIT H3: the .exists() pre-check is not atomic; if a concurrent shift wins the
    # car between check and create, the DB constraint fires. Simulate that IntegrityError
    # and assert a clean 400 CAR_BUSY instead of an unhandled 500.
    from django.db import IntegrityError

    def boom(*a, **k):
        raise IntegrityError("one_active_shift_per_car")

    monkeypatch.setattr(DriverShift.objects, "create", boom)
    r = _go_on_shift(env)
    assert r.status_code == 400
    assert r.data["error"]["code"] == "CAR_BUSY"
