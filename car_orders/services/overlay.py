"""Overlay order operations on ``OrderMeta`` — claim / release / reassign / extend.

«Our layer»: the demo backend owns login + base order data, but we own the
schedule, the trip state and the «1 driver = 1 active order, sequentially with the
same car» rule the demo forbids. Each function raises :class:`OverlayError`
(carrying a machine ``code`` + HTTP-status hint) on a rule violation; the view
maps it. Side-effects (notify / route push / WS teardown) run after the DB change.
"""

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from car_orders import concurrency, scheduling
from car_orders.models import OrderLiveLocation, OrderMeta
from car_orders.services import events

_TERMINAL = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)
# AUDIT M3: upper bound on a single «продлить» so a bogus value can't push planned_end
# absurdly far. Generous on purpose — above the web UI's own max (DurationField allows
# up to 99h) so no legitimate input is rejected; this only blocks nonsensical overflow.
_MAX_EXTEND_MINUTES = 7 * 24 * 60


class OverlayError(Exception):
    """An overlay rule was violated — carries a stable ``code``, an HTTP-status hint
    and optional ``details``, so the view maps it without parsing messages."""

    def __init__(self, code: str, message, *, http_status: int = 400, details: dict | None = None):
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}
        super().__init__(str(message))


def claim(order_id, driver_id, car_id=None, car_label=""):
    """Claim an order in our overlay so a driver can take a 2nd order sequentially
    with the same car (the demo forbids that). Locks the row, enforces «one active
    order per driver», and (re)starts the trip from ASSIGNED on a fresh/terminal
    state. Raises ``ALREADY_CLAIMED`` / ``DRIVER_BUSY``; returns the OrderMeta."""
    with transaction.atomic():
        # Serialise concurrent claims for THIS driver so the «busy?» check below
        # (which the order-row lock doesn't cover) can't race two orders onto one
        # driver — AUDIT C1. Take it before any read so both contenders order here.
        concurrency.lock_driver(driver_id)
        meta = OrderMeta.objects.select_for_update().filter(order_id=order_id).first()
        # Already taken by a DIFFERENT driver and still active → reject.
        if (
            meta
            and meta.overlay_claimed
            and meta.trip_state not in _TERMINAL
            and meta.driver_id is not None
            and str(meta.driver_id) != str(driver_id)
        ):
            raise OverlayError(
                "ALREADY_CLAIMED", _("This order is already taken by another driver.")
            )
        # One active order per driver — re-claiming the SAME order is idempotent.
        busy = (
            OrderMeta.objects.filter(driver_id=driver_id)
            .exclude(trip_state__in=_TERMINAL)
            .exclude(order_id=int(order_id))
            .first()
        )
        if busy:
            raise OverlayError(
                "DRIVER_BUSY",
                _("This driver already has an active order (#%(id)s). Finish it first.")
                % {"id": busy.order_id},
            )
        meta, created = OrderMeta.objects.update_or_create(
            order_id=order_id,
            defaults={
                "driver_id": driver_id,
                "car_id": car_id,
                "car_label": car_label,
                "overlay_claimed": True,
            },
        )
        # Don't rewind an in-progress trip on a double-tap; only (re)start from a
        # fresh / terminal state.
        if created or meta.trip_state in _TERMINAL:
            meta.trip_state = OrderMeta.TripState.ASSIGNED
            meta.returning = False
            meta.save(update_fields=["trip_state", "returning"])
    events.notify_order_status(meta, OrderMeta.TripState.ASSIGNED)  # «Водитель назначен» → author
    from car_orders import dispatch

    dispatch.push_order_route(meta)  # send the approach route on assignment
    return meta


def release(order_id, *, requeue=False):
    """Tear down the overlay claim (on demo reject / cancel / release / done). With
    ``requeue`` the order returns to the queue (non-terminal + dispatchable);
    otherwise it goes terminal CANCELLED. Idempotent — returns the OrderMeta, or
    None when there was nothing to release."""
    meta = OrderMeta.objects.filter(order_id=order_id).first()
    if meta is None:
        return None
    prev_driver = meta.driver_id
    _drop_claim(meta)
    if requeue:
        meta.trip_state = OrderMeta.TripState.ASSIGNED
        meta.dispatchable = True
    else:
        meta.trip_state = OrderMeta.TripState.CANCELLED
    meta.save()
    OrderLiveLocation.objects.filter(order_id=order_id).delete()
    events.broadcast_location(order_id, {"trip_state": "cancelled"})
    if not requeue:
        events.notify_order_status(meta, OrderMeta.TripState.CANCELLED)  # requester
    events.notify_dropped_driver(prev_driver, order_id)
    return meta


def reassign(order_id):
    """Dispatcher takes an order off its driver → back to the QUEUE (non-terminal +
    dispatchable) so the auto-dispatcher re-assigns it. Raises ``NOT_FOUND``."""
    meta = OrderMeta.objects.filter(order_id=order_id).first()
    if meta is None:
        raise OverlayError("NOT_FOUND", _("Nothing to reassign for this order."))
    prev_driver = meta.driver_id
    _drop_claim(meta)
    meta.trip_state = OrderMeta.TripState.ASSIGNED  # non-terminal «awaiting» state
    meta.dispatchable = True
    meta.save()
    OrderLiveLocation.objects.filter(order_id=order_id).delete()
    # Tell the watchers the current tracking ended; the driver they were on is gone.
    events.broadcast_location(order_id, {"trip_state": "cancelled"})
    events.notify_dropped_driver(prev_driver, order_id)
    return meta


def extend(order_id, minutes):
    """Add ``minutes`` to the order's planned duration in our overlay and re-check the
    driver's next window. Returns ``(meta, conflict_dict_or_None)``; the extension
    always applies — the conflict is a warning. Raises ``VALIDATION``."""
    if minutes <= 0 or minutes > _MAX_EXTEND_MINUTES:
        raise OverlayError(
            "VALIDATION",
            _("`minutes` must be between 1 and %(max)s.") % {"max": _MAX_EXTEND_MINUTES},
        )
    meta = OrderMeta.objects.filter(order_id=order_id).first()
    if meta is None:
        raise OverlayError("VALIDATION", _("No order to extend."))
    # An order created without a route estimate has no duration yet — treat a
    # missing duration as 0 so «продлить» still works (it establishes / pushes the
    # window out) instead of 400-ing on every freshly-created order.
    meta.estimated_duration = (meta.estimated_duration or 0) + minutes
    meta.save()
    conflict = None
    if meta.driver_id and meta.planned_datetime:
        new_end = scheduling.driving_end(meta.planned_datetime, meta.planned_end, meta.service_time)
        conflict = scheduling.meta_conflict(
            meta.driver_id, meta.planned_datetime, new_end, exclude_order_id=int(order_id)
        )
    return meta, _conflict_payload(conflict)


def _drop_claim(meta) -> None:
    """Clear the overlay claim fields on ``meta`` (caller sets trip_state + saves)."""
    meta.overlay_claimed = False
    meta.driver_id = None
    meta.car_id = None
    meta.car_label = ""
    meta.returning = False


def _conflict_payload(conflict):
    if conflict is None:
        return None
    return {
        "order_id": conflict.order_id,
        "planned_start": conflict.planned_datetime,
        "planned_end": conflict.planned_end,
        "address": f"Заказ #{conflict.order_id}",
    }
