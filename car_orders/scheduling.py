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

from car_orders.models import CarOrder, OrderMeta

# Statuses that occupy a slot in the driver's calendar.
COMMITTED_STATUSES = (CarOrder.Status.SCHEDULED, CarOrder.Status.IN_PROGRESS)


def meta_conflict(driver_id, start, end, exclude_order_id=None, buffer=None):
    """Overlay scheduling check (hybrid/gateway setup): does ``[start, end]``
    (expanded by ``buffer``) overlap any of the driver's other committed
    :class:`OrderMeta` windows? Returns the conflicting meta or ``None``.

    Used when orders live in the demo backend but their windows + driver
    assignment are tracked locally in OrderMeta.
    """
    if driver_id is None or start is None or end is None:
        return None
    if buffer is None:
        buffer = travel_buffer()
    lo, hi = start - buffer, end + buffer
    qs = OrderMeta.objects.filter(driver_id=driver_id).exclude(
        trip_state__in=(
            OrderMeta.TripState.COMPLETED,
            OrderMeta.TripState.CANCELLED,
            # Parked on-site during a (long) shoot — the driver is physically idle
            # and free to fill the gap with another order, so a parked order must
            # NOT block a new claim. This is the core gap-filling feature: keep the
            # driver busy during the wait. A late return is surfaced via `at_risk`,
            # not by forbidding the gap order.
            OrderMeta.TripState.AT_DESTINATION,
            OrderMeta.TripState.WAITING,
        )
    )
    if exclude_order_id is not None:
        qs = qs.exclude(order_id=exclude_order_id)
    for m in qs:
        o_start, o_end = m.planned_datetime, m.planned_end
        if o_start is None or o_end is None:
            continue
        if lo < o_end and o_start < hi:  # half-open interval overlap
            return m
    return None


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


# --- Overlay (OrderMeta) variants — hybrid/gateway setup --------------------
# The functions above act on the demo CarOrder; in the hybrid setup the windows
# and driver assignment live in OrderMeta, so we mirror the overrun/at-risk logic
# on the overlay.

# Trip stages where the driver is actively executing an order (between accepting
# and finishing) — these occupy the driver "now".
STARTED_STATES = (
    OrderMeta.TripState.TO_CLIENT,
    OrderMeta.TripState.AT_CLIENT,
    OrderMeta.TripState.IN_TRIP,
    OrderMeta.TripState.AT_DESTINATION,
    OrderMeta.TripState.WAITING,
)
# The driver is physically DRIVING (no spare capacity). A parked/waiting driver
# (e.g. on hold during a long shoot) is NOT here — they can take a gap order.
MOVING_STATES = (OrderMeta.TripState.TO_CLIENT, OrderMeta.TripState.IN_TRIP)


def meta_active_trip(driver_id, exclude_order_id=None, states=STARTED_STATES, active=None):
    """The driver's order in one of ``states`` (default: any started/non-terminal
    stage). Pass ``MOVING_STATES`` to ask only «is the driver actively driving».

    ``active`` (optional) is a precomputed ``{driver_id: [OrderMeta]}`` index of
    started trips — pass it to avoid a per-order DB query when scanning a fleet."""
    if driver_id is None:
        return None
    if active is not None:
        for m in active.get(driver_id, ()):  # in-memory, no query
            if m.trip_state in states and m.order_id != exclude_order_id:
                return m
        return None
    qs = OrderMeta.objects.filter(driver_id=driver_id, trip_state__in=states)
    if exclude_order_id is not None:
        qs = qs.exclude(order_id=exclude_order_id)
    return qs.first()


def meta_projected_start(meta, now, active=None):
    """Soonest this overlay order can realistically start: its planned time, or —
    if the driver is on an *overrunning* trip — when that one finishes + buffer."""
    base = meta.planned_datetime
    if base is None:
        return None
    current = meta_active_trip(meta.driver_id, exclude_order_id=meta.order_id, active=active)
    if current is None:
        return base
    current_end = current.planned_end
    if current_end is not None and now > current_end:
        return max(base, now + travel_buffer())
    return base


def meta_needs_reassign(meta, now, active=None):
    """Overlay order is *at risk* when its projected start (given the driver's
    current overrunning trip) blows past the latest acceptable start."""
    if meta.latest_start is None:
        return False
    ps = meta_projected_start(meta, now, active=active)
    return ps is not None and ps > meta.latest_start
