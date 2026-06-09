"""Scheduling logic for car orders.

A driver's day is a set of **non-overlapping time windows**. An order reserves
the window ``[planned_start, planned_end]``; a new order may be claimed only if
its window — expanded by a *travel buffer* on each side, to leave room to drive
between two jobs — does not overlap any window the driver has already committed
to (``scheduled`` or ``in_progress``).

These helpers are pure (no side effects) so they're easy to unit-test and reuse
from views, the schedule endpoint and the serializers.
"""

from datetime import timedelta

from django.conf import settings

from car_orders.models import CarOrder

# Statuses that occupy a slot in the driver's calendar.
COMMITTED_STATUSES = (CarOrder.Status.SCHEDULED, CarOrder.Status.IN_PROGRESS)


def travel_buffer() -> timedelta:
    return getattr(settings, "CAR_ORDER_TRAVEL_BUFFER", timedelta(minutes=30))


def order_window(order):
    """``(start, end)`` planned window, or ``None`` when the order isn't scheduled."""
    end = order.planned_end
    if order.planned_datetime and end:
        return order.planned_datetime, end
    return None


def driver_committed_orders(driver, exclude_id=None):
    """Orders that currently occupy ``driver``'s calendar."""
    qs = CarOrder.objects.filter(driver=driver, status__in=COMMITTED_STATUSES)
    if exclude_id is not None:
        qs = qs.exclude(pk=exclude_id)
    return qs


def find_time_conflict(driver, start, end, exclude_id=None, buffer=None):
    """Return the first committed order whose window overlaps ``[start, end]``
    (each side expanded by ``buffer``), or ``None`` if the window is free."""
    if buffer is None:
        buffer = travel_buffer()
    lo = start - buffer
    hi = end + buffer
    for other in driver_committed_orders(driver, exclude_id=exclude_id):
        win = order_window(other)
        if win is None:
            continue
        o_start, o_end = win
        if lo < o_end and o_start < hi:  # half-open interval overlap
            return other
    return None


def active_trip(driver, exclude_id=None):
    """The driver's currently-driven order, if any (at most one)."""
    qs = CarOrder.objects.filter(driver=driver, status=CarOrder.Status.IN_PROGRESS)
    if exclude_id is not None:
        qs = qs.exclude(pk=exclude_id)
    return qs.first()


def projected_start(order, now):
    """Soonest a scheduled order can realistically start.

    Normally its planned start; but if the driver is on an *overrunning* trip,
    it can't begin until that one finishes (plus the travel buffer).
    """
    base = order.planned_datetime
    if base is None:
        return None
    current = active_trip(order.driver, exclude_id=order.pk)
    if current is None:
        return base
    current_end = current.planned_end
    if current_end is not None and now > current_end:
        return max(base, now + travel_buffer())
    return base


def needs_reassign(order, now):
    """A scheduled order is at risk when its projected start blows past the
    latest acceptable start — it "can't wait" and should be reassigned."""
    if order.latest_start is None:
        return False
    ps = projected_start(order, now)
    return ps is not None and ps > order.latest_start
