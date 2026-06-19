"""Additional unit tests for the native order-lifecycle service
(``car_orders.services.orders``) — the gap-fill counterpart to
``test_orders_service.py``.

Covers the error branches the original suite left untested: NOT_FOUND on every
verb, TYPE_MISMATCH, service-level TIME_CONFLICT (409), the permission branches of
cancel/extend, and the wrong-status guards on start/complete/release/reassign/
extend. Pure service layer — no HTTP, no gateway.
"""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from auth_core.models import AccessGroup, UserAccessGroup
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


def _admin(username="adm"):
    """A user with the Car Admin access group (carries car_order:approve/reject) —
    the same wiring the API workflow tests use."""
    u = User.objects.create_user(username=username, password="pw")
    UserAccessGroup.objects.create(user=u, group=AccessGroup.objects.get(name="Car Admin"))
    return u


# ---- NOT_FOUND on every verb ----------------------------------------------

@pytest.mark.django_db
def test_every_verb_raises_not_found_for_missing_order(env):
    bogus = 999_999
    calls = [
        lambda: orders.claim(bogus, env["driver"]),
        lambda: orders.start(bogus, env["driver"]),
        lambda: orders.complete(bogus, env["driver"]),
        lambda: orders.release(bogus, env["driver"]),
        lambda: orders.cancel(bogus, env["requester"]),
        lambda: orders.reassign(bogus, env["requester"]),
        lambda: orders.extend(bogus, env["requester"], 10),
    ]
    for call in calls:
        with pytest.raises(orders.OrderError) as exc:
            call()
        assert exc.value.code == "NOT_FOUND"


# ---- claim: type + time guards --------------------------------------------

@pytest.mark.django_db
def test_claim_rejects_type_mismatch(env):
    _on_shift(env)  # shift car is of type env["ct"]
    other = CarType.objects.create(name="Грузовая")
    order = _order(env, S.AWAITING_DRIVER, car_type=other)
    with pytest.raises(orders.OrderError) as exc:
        orders.claim(order.pk, env["driver"])
    assert exc.value.code == "TYPE_MISMATCH"


@pytest.mark.django_db
def test_claim_time_conflict_is_409_with_details(env):
    _on_shift(env)
    base = timezone.now() + timedelta(days=1)
    # An existing committed order occupies [base, base+5h] for this driver.
    occupied = CarOrder.objects.create(
        created_by=env["requester"], driver=env["driver"], car=env["car"],
        status=S.SCHEDULED, car_type=env["ct"],
        planned_datetime=base, estimated_duration=timedelta(hours=5),
    )
    # A new awaiting order overlapping that window.
    new = _order(
        env, S.AWAITING_DRIVER, car_type=env["ct"],
        planned_datetime=base + timedelta(hours=2), estimated_duration=timedelta(hours=2),
    )
    with pytest.raises(orders.OrderError) as exc:
        orders.claim(new.pk, env["driver"])
    assert exc.value.code == "TIME_CONFLICT"
    assert exc.value.http_status == 409
    assert exc.value.details["order_id"] == occupied.id


# ---- wrong-status guards ---------------------------------------------------

@pytest.mark.django_db
def test_start_rejects_non_scheduled_status(env):
    _on_shift(env)
    order = _order(env, S.AWAITING_DRIVER, driver=env["driver"], car=env["car"])
    with pytest.raises(orders.OrderError) as exc:
        orders.start(order.pk, env["driver"])
    assert exc.value.code == "INVALID_STATUS"


@pytest.mark.django_db
def test_complete_rejects_non_in_progress_status(env):
    order = _order(env, S.SCHEDULED, driver=env["driver"], car=env["car"])
    with pytest.raises(orders.OrderError) as exc:
        orders.complete(order.pk, env["driver"])
    assert exc.value.code == "INVALID_STATUS"


@pytest.mark.django_db
def test_release_rejects_wrong_status(env):
    order = _order(env, S.AWAITING_DRIVER, driver=env["driver"], car=env["car"])
    with pytest.raises(orders.OrderError) as exc:
        orders.release(order.pk, env["driver"])
    assert exc.value.code == "INVALID_STATUS"


@pytest.mark.django_db
def test_reassign_rejects_wrong_status(env):
    order = _order(env, S.AWAITING_DRIVER)
    with pytest.raises(orders.OrderError) as exc:
        orders.reassign(order.pk, env["requester"])
    assert exc.value.code == "INVALID_STATUS"


# ---- cancel: terminal + permission branch ---------------------------------

@pytest.mark.django_db
def test_cancel_rejects_terminal_order(env):
    order = _order(env, S.COMPLETED, driver=env["driver"], car=env["car"])
    with pytest.raises(orders.OrderError) as exc:
        orders.cancel(order.pk, env["requester"])
    assert exc.value.code == "INVALID_STATUS"


@pytest.mark.django_db
def test_cancel_allowed_for_reject_permission_holder(env):
    order = _order(env, S.SCHEDULED, driver=env["driver"], car=env["car"])
    result = orders.cancel(order.pk, _admin(), reason="ops")  # not author, has the perm
    assert result.status == S.CANCELLED


# ---- reassign / release side effects --------------------------------------

@pytest.mark.django_db
def test_reassign_from_in_progress_clears_driver_and_resets_shift(env):
    shift = _on_shift(env, status=DriverShift.Status.EN_ROUTE)
    order = _order(
        env, S.IN_PROGRESS, driver=env["driver"], car=env["car"], started_at=timezone.now()
    )
    result = orders.reassign(order.pk, env["requester"])
    assert result.status == S.AWAITING_DRIVER
    assert result.driver is None
    assert result.started_at is None
    shift.refresh_from_db()
    assert shift.status == DriverShift.Status.ONLINE


@pytest.mark.django_db
def test_release_resets_shift_to_online(env):
    shift = _on_shift(env, status=DriverShift.Status.EN_ROUTE)
    order = _order(env, S.SCHEDULED, driver=env["driver"], car=env["car"])
    orders.release(order.pk, env["driver"])
    shift.refresh_from_db()
    assert shift.status == DriverShift.Status.ONLINE


# ---- extend: permission + status branches ---------------------------------

@pytest.mark.django_db
def test_extend_allowed_for_approve_permission_holder(env):
    order = _order(
        env, S.SCHEDULED, driver=env["driver"], car=env["car"],
        estimated_duration=timedelta(hours=1),
    )
    result, conflict = orders.extend(order.pk, _admin(), 30)  # dispatcher, not the driver
    assert result.estimated_duration == timedelta(hours=1, minutes=30)
    assert conflict is None


@pytest.mark.django_db
def test_extend_denied_for_unrelated_user(env):
    stranger = User.objects.create_user(username="z", password="pw")
    order = _order(env, S.SCHEDULED, driver=env["driver"], car=env["car"])
    with pytest.raises(orders.OrderError) as exc:
        orders.extend(order.pk, stranger, 30)
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.http_status == 403


@pytest.mark.django_db
def test_extend_rejects_non_active_status(env):
    order = _order(env, S.AWAITING_DRIVER, driver=env["driver"], car=env["car"])
    with pytest.raises(orders.OrderError) as exc:
        orders.extend(order.pk, env["driver"], 30)
    assert exc.value.code == "INVALID_STATUS"
