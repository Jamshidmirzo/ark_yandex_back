"""Audit trail for car orders — every lifecycle step is recorded as a
``CarOrderActivity`` row so the order's history is reconstructable."""

from car_orders.models import CarOrderActivity


def _log(order, actor, kind, **payload):
    return CarOrderActivity.objects.create(
        order=order,
        actor=actor,
        kind=kind,
        payload=payload,
    )


def record_created(order, actor):
    return _log(order, actor, CarOrderActivity.Kind.CREATED)


def record_sent(order, actor):
    return _log(order, actor, CarOrderActivity.Kind.SENT)


def record_approved(order, actor):
    return _log(order, actor, CarOrderActivity.Kind.APPROVED)


def record_accepted(order, actor):
    return _log(order, actor, CarOrderActivity.Kind.ACCEPTED_BY_DRIVER, car_id=order.car_id)


def record_completed(order, actor):
    return _log(order, actor, CarOrderActivity.Kind.COMPLETED)


def record_rejected(order, actor, reason=""):
    return _log(order, actor, CarOrderActivity.Kind.REJECTED, reason=reason)


def record_cancelled(order, actor, reason=""):
    return _log(order, actor, CarOrderActivity.Kind.CANCELLED, reason=reason)


def record_released(order, actor, reason=""):
    return _log(order, actor, CarOrderActivity.Kind.RELEASED, reason=reason)


def record_extended(order, actor, added_minutes):
    return _log(order, actor, CarOrderActivity.Kind.EXTENDED, added_minutes=added_minutes)


def record_reassigned(order, actor, from_driver_id=None):
    return _log(order, actor, CarOrderActivity.Kind.REASSIGNED, from_driver_id=from_driver_id)
