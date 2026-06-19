"""Model-level tests: the data-integrity constraints and computed properties that
back the business rules — partial unique shift constraints (Р1), the one-report-
per-vehicle-per-day rule, the DispatchSettings singleton, and the planned_end /
is_delayed derivations.
"""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from car_orders.models import (
    Car,
    CarOrder,
    CarType,
    DispatchSettings,
    DriverShift,
    OrderMeta,
    VehicleReport,
)

User = get_user_model()


@pytest.fixture
def env(db):
    ct = CarType.objects.create(name="Легковая")
    return {
        "ct": ct,
        "car": Car.objects.create(model="Damas", plate_number="01A001AA", type=ct, status="active"),
        "driver": User.objects.create_user(username="drv", password="pw"),
    }


# ---- DriverShift partial unique constraints (Р1) --------------------------

@pytest.mark.django_db
def test_one_active_shift_per_driver(env):
    DriverShift.objects.create(driver=env["driver"], car=env["car"])
    car2 = Car.objects.create(model="Faw", plate_number="02A002AA", type=env["ct"], status="active")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            DriverShift.objects.create(driver=env["driver"], car=car2)


@pytest.mark.django_db
def test_one_active_shift_per_car(env):
    DriverShift.objects.create(driver=env["driver"], car=env["car"])
    driver2 = User.objects.create_user(username="drv2", password="pw")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            DriverShift.objects.create(driver=driver2, car=env["car"])


@pytest.mark.django_db
def test_ended_shift_frees_the_driver_and_car(env):
    s1 = DriverShift.objects.create(driver=env["driver"], car=env["car"])
    s1.ended_at = timezone.now()
    s1.save(update_fields=["ended_at"])
    # The constraint is partial (ended_at IS NULL) → a new active shift is allowed.
    s2 = DriverShift.objects.create(driver=env["driver"], car=env["car"])
    assert s2.pk and s2.ended_at is None


# ---- VehicleReport one-per-day --------------------------------------------

@pytest.mark.django_db
def test_vehicle_report_unique_per_vehicle_per_day(env):
    today = timezone.now().date()
    VehicleReport.objects.create(vehicle=env["car"], submitted_by=env["driver"], date=today)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            VehicleReport.objects.create(vehicle=env["car"], submitted_by=env["driver"], date=today)


# ---- DispatchSettings singleton -------------------------------------------

@pytest.mark.django_db
def test_dispatch_settings_save_forces_single_row(db):
    DispatchSettings.objects.all().delete()
    DispatchSettings(auto_enabled=True).save()
    DispatchSettings(auto_enabled=False).save()  # also forced to pk=1 → overwrites
    assert DispatchSettings.objects.count() == 1
    assert DispatchSettings.objects.get().auto_enabled is False


@pytest.mark.django_db
def test_dispatch_settings_load_creates_off(db):
    DispatchSettings.objects.all().delete()
    cfg = DispatchSettings.load()
    assert cfg.pk == DispatchSettings.SINGLETON_PK
    assert cfg.auto_enabled is False


# ---- computed properties ---------------------------------------------------

@pytest.mark.django_db
def test_car_order_planned_end_and_is_delayed(env):
    requester = User.objects.create_user(username="req", password="pw")
    base = timezone.now() - timedelta(hours=3)
    order = CarOrder.objects.create(
        created_by=requester, status=CarOrder.Status.IN_PROGRESS,
        planned_datetime=base, estimated_duration=timedelta(hours=1),
    )
    assert order.planned_end == base + timedelta(hours=1)
    assert order.is_delayed(timezone.now()) is True


@pytest.mark.django_db
def test_car_order_not_delayed_unless_in_progress(env):
    requester = User.objects.create_user(username="req", password="pw")
    base = timezone.now() - timedelta(hours=3)
    order = CarOrder.objects.create(
        created_by=requester, status=CarOrder.Status.SCHEDULED,
        planned_datetime=base, estimated_duration=timedelta(hours=1),
    )
    assert order.is_delayed(timezone.now()) is False


@pytest.mark.django_db
def test_car_order_planned_end_none_without_duration(env):
    requester = User.objects.create_user(username="req", password="pw")
    order = CarOrder.objects.create(
        created_by=requester, status=CarOrder.Status.PENDING, planned_datetime=timezone.now(),
    )
    assert order.planned_end is None


@pytest.mark.django_db
def test_order_meta_planned_end(db):
    base = timezone.now()
    m = OrderMeta.objects.create(order_id=1, planned_datetime=base, estimated_duration=90)
    assert m.planned_end == base + timedelta(minutes=90)


@pytest.mark.django_db
def test_order_meta_planned_end_none_when_incomplete(db):
    assert OrderMeta.objects.create(order_id=2, planned_datetime=timezone.now()).planned_end is None
    assert OrderMeta.objects.create(order_id=3, estimated_duration=60).planned_end is None
