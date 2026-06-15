"""Unit tests for the native order-lifecycle service (``car_orders.services.orders``).

Exercise the rules directly at the service layer (no HTTP) — the granular
counterpart to the end-to-end ``test_workflow`` suite.
"""

import pytest
from django.contrib.auth import get_user_model

from car_orders.models import Car, CarOrder, CarType, DriverShift
from car_orders.services import orders

User = get_user_model()
S = CarOrder.Status


@pytest.fixture
def env(db):
    ct = CarType.objects.create(name="Легковая")
    car = Car.objects.create(model="Damas", plate_number="01A001AA", type=ct, status="active")
    return {
        "ct": ct,
        "car": car,
        "driver": User.objects.create_user(username="drv", password="pw"),
        "requester": User.objects.create_user(username="req", password="pw"),
    }


def _order(env, status, **kwargs):
    kwargs.setdefault("created_by", env["requester"])
    return CarOrder.objects.create(status=status, **kwargs)


def _on_shift(env, status=DriverShift.Status.ONLINE):
    return DriverShift.objects.create(driver=env["driver"], car=env["car"], status=status)


# ---- claim ----------------------------------------------------------------

@pytest.mark.django_db
def test_claim_requires_active_shift(env):
    order = _order(env, S.AWAITING_DRIVER, car_type=env["ct"])
    with pytest.raises(orders.OrderError) as exc:
        orders.claim(order.pk, env["driver"])
    assert exc.value.code == "NO_SHIFT"


@pytest.mark.django_db
def test_claim_rejects_already_taken(env):
    _on_shift(env)
    order = _order(env, S.SCHEDULED, car_type=env["ct"])  # not awaiting
    with pytest.raises(orders.OrderError) as exc:
        orders.claim(order.pk, env["driver"])
    assert exc.value.code == "ALREADY_TAKEN"


@pytest.mark.django_db
def test_claim_happy_path_reserves_into_schedule(env):
    _on_shift(env)
    order = _order(env, S.AWAITING_DRIVER, car_type=env["ct"])
    result = orders.claim(order.pk, env["driver"])
    assert result.status == S.SCHEDULED
    assert result.driver_id == env["driver"].id
    assert result.car_id == env["car"].id


# ---- start ----------------------------------------------------------------

@pytest.mark.django_db
def test_start_rejects_non_assigned_driver(env):
    order = _order(env, S.SCHEDULED, driver=env["requester"], car=env["car"])
    with pytest.raises(orders.OrderError) as exc:
        orders.start(order.pk, env["driver"])
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.http_status == 403


@pytest.mark.django_db
def test_start_rejects_when_already_driving(env):
    _on_shift(env)
    _order(env, S.IN_PROGRESS, driver=env["driver"], car=env["car"])  # already on a trip
    order = _order(env, S.SCHEDULED, driver=env["driver"], car=env["car"])
    with pytest.raises(orders.OrderError) as exc:
        orders.start(order.pk, env["driver"])
    assert exc.value.code == "DRIVER_BUSY"


@pytest.mark.django_db
def test_start_sets_shift_en_route(env):
    shift = _on_shift(env)
    order = _order(env, S.SCHEDULED, driver=env["driver"], car=env["car"])
    orders.start(order.pk, env["driver"])
    shift.refresh_from_db()
    assert shift.status == DriverShift.Status.EN_ROUTE


# ---- complete / cancel / extend -------------------------------------------

@pytest.mark.django_db
def test_complete_sets_shift_online(env):
    shift = _on_shift(env, status=DriverShift.Status.EN_ROUTE)
    order = _order(env, S.IN_PROGRESS, driver=env["driver"], car=env["car"])
    result = orders.complete(order.pk, env["driver"])
    assert result.status == S.COMPLETED
    shift.refresh_from_db()
    assert shift.status == DriverShift.Status.ONLINE


@pytest.mark.django_db
def test_cancel_rejects_unauthorized_user(env):
    order = _order(env, S.SCHEDULED, driver=env["driver"], car=env["car"])
    stranger = User.objects.create_user(username="x", password="pw")  # not author, no perms
    with pytest.raises(orders.OrderError) as exc:
        orders.cancel(order.pk, stranger)
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.http_status == 403


@pytest.mark.django_db
def test_cancel_by_author_frees_the_order(env):
    order = _order(env, S.SCHEDULED, driver=env["driver"], car=env["car"])
    result = orders.cancel(order.pk, env["requester"], reason="client off")  # author
    assert result.status == S.CANCELLED


@pytest.mark.django_db
def test_extend_rejects_nonpositive_minutes(env):
    order = _order(env, S.SCHEDULED, driver=env["driver"], car=env["car"])
    with pytest.raises(orders.OrderError) as exc:
        orders.extend(order.pk, env["driver"], 0)
    assert exc.value.code == "VALIDATION"
