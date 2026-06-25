"""Overlay order operations on ``OrderMeta`` — claim / release / reassign / extend.

«Our layer»: the demo backend owns login + base order data, but we own the
schedule, the trip state and the «1 driver = 1 active order, sequentially with the
same car» rule the demo forbids. Each function raises :class:`OverlayError`
(carrying a machine ``code`` + HTTP-status hint) on a rule violation; the view
maps it. Side-effects (notify / route push / WS teardown) run after the DB change.
"""

from django.db import transaction
from django.utils import timezone
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


def mark_searching(order_id, *, reset=False):
    """Stamp ``search_started_at`` when an order enters the dispatch queue (the
    «поиск водителя» clock). Idempotent: only stamps when currently unset and the
    order is dispatchable + driverless, so re-approving an already-queued order
    doesn't reset the clock. ``reset=True`` (requeue / reassign) restarts it.
    Returns the OrderMeta (or None when there's no overlay row yet)."""
    meta = OrderMeta.objects.filter(order_id=order_id).first()
    if meta is None or not meta.dispatchable or meta.driver_id is not None:
        return meta
    if reset or meta.search_started_at is None:
        meta.search_started_at = timezone.now()
        meta.save(update_fields=["search_started_at"])
    return meta


def claim(order_id, driver_id, car_id=None, car_label="", driver_name="", driver_phone=""):
    """Claim an order in our overlay so a driver can take a 2nd order sequentially
    with the same car (the demo forbids that). Locks the row, enforces «one active
    order per driver», and (re)starts the trip from ASSIGNED on a fresh/terminal
    state. Raises ``ALREADY_CLAIMED`` / ``DRIVER_BUSY``; returns the OrderMeta.

    ``driver_name`` / ``driver_phone`` are the driver snapshot (captured client-side
    from the claiming driver's own session) so the requester can see + call the
    driver without HR access to ``/employees/``."""
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
        defaults = {
            "driver_id": driver_id,
            "car_id": car_id,
            "car_label": car_label,
            "overlay_claimed": True,
        }
        # Don't wipe an existing driver snapshot if a re-claim arrives without one.
        if driver_name:
            defaults["driver_name"] = driver_name
        if driver_phone:
            defaults["driver_phone"] = driver_phone
        meta, created = OrderMeta.objects.update_or_create(
            order_id=order_id,
            defaults=defaults,
        )
        # Don't rewind an in-progress trip on a double-tap; only (re)start from a
        # fresh / terminal state.
        if created or meta.trip_state in _TERMINAL:
            meta.trip_state = OrderMeta.TripState.ASSIGNED
            meta.returning = False
            # A fresh claim must not carry a previous run's arrival instant — the
            # «ожидание клиента» clock restarts when this driver reaches the pickup.
            meta.arrived_at = None
            meta.save(update_fields=["trip_state", "returning", "arrived_at"])
    events.notify_order_status(meta, OrderMeta.TripState.ASSIGNED)  # «Водитель назначен» → author
    from car_orders import dispatch

    dispatch.push_order_route(meta)  # send the approach route on assignment
    return meta


def _ensure_actor_owns(meta, actor_driver_id, is_dispatcher) -> None:
    """A driver may only release / extend their OWN order; a dispatcher (or a trusted
    server-side call that passes ``actor_driver_id=None`` — e.g. the demo reject hook)
    may act on any. Mirrors the native ownership checks (services.orders.release /
    .extend) and ``trip_state.validate`` so the overlay layer isn't an IDOR shortcut
    around them. Raises ``PERMISSION_DENIED`` (403) for a non-owner non-dispatcher."""
    if (
        actor_driver_id is not None
        and meta.driver_id is not None
        and str(actor_driver_id) != str(meta.driver_id)
        and not is_dispatcher
    ):
        raise OverlayError(
            "PERMISSION_DENIED",
            _("You can only act on your own order."),
            http_status=403,
        )


def release(order_id, *, requeue=False, actor_driver_id=None, is_dispatcher=False):
    """Tear down the overlay claim (on demo reject / cancel / release / done). With
    ``requeue`` the order returns to the queue (non-terminal + dispatchable);
    otherwise it goes terminal CANCELLED. Idempotent — returns the OrderMeta, or
    None when there was nothing to release. ``actor_driver_id``/``is_dispatcher``
    enforce ownership when a user (not a server hook) drives it."""
    meta = OrderMeta.objects.filter(order_id=order_id).first()
    if meta is None:
        return None
    _ensure_actor_owns(meta, actor_driver_id, is_dispatcher)
    prev_driver = meta.driver_id
    _drop_claim(meta)
    if requeue:
        meta.trip_state = OrderMeta.TripState.ASSIGNED
        meta.dispatchable = True
        # Back in the queue → restart the «поиск водителя» clock.
        meta.search_started_at = timezone.now()
    else:
        meta.trip_state = OrderMeta.TripState.CANCELLED
    meta.save()
    OrderLiveLocation.objects.filter(order_id=order_id).delete()
    events.broadcast_location(order_id, {"trip_state": "cancelled"})
    if not requeue:
        events.notify_order_status(meta, OrderMeta.TripState.CANCELLED)  # requester
    events.notify_dropped_driver(prev_driver, order_id)
    return meta


def cancel_no_show(order_id, *, actor=None, actor_driver_id=None, is_dispatcher=False):
    """Driver / dispatcher cancels an order whose client never came out at the pickup.
    Only allowed while ``trip_state == at_client`` (the pickup-wait stage). Tears down
    the overlay (CANCELLED, notifies the requester), mirrors the terminal onto the
    native ``CarOrder`` (status + shift reset) and audits the no-show with how long
    the driver waited. Returns ``(meta, waited_s)``.

    Raises :class:`OverlayError` — ``NOT_FOUND`` / ``INVALID_STATUS`` / ``PERMISSION_DENIED``."""
    meta = OrderMeta.objects.filter(order_id=order_id).first()
    if meta is None:
        raise OverlayError("NOT_FOUND", _("No order to cancel."))
    _ensure_actor_owns(meta, actor_driver_id, is_dispatcher)
    if meta.trip_state != OrderMeta.TripState.AT_CLIENT:
        raise OverlayError(
            "INVALID_STATUS",
            _("«Клиент не вышел» is only available while waiting for the client at the pickup."),
        )
    waited_s = None
    if meta.arrived_at is not None:
        waited_s = int((timezone.now() - meta.arrived_at).total_seconds())
    meta = release(
        order_id, requeue=False, actor_driver_id=actor_driver_id, is_dispatcher=is_dispatcher
    )
    _reconcile_native_cancellation(order_id, actor=actor, reason="client_no_show", waited_s=waited_s)
    return meta, waited_s


def _reconcile_native_cancellation(order_id, *, actor=None, reason="", waited_s=None):
    """Mirror an overlay cancel onto the demo ``CarOrder`` so the native status, the
    driver's shift and the audit trail stay in sync — mirror of
    ``trip_state._reconcile_native_completion``. Idempotent: a CarOrder already in a
    terminal state keeps its status (we still record the no-show activity). No-op when
    there is no backing CarOrder (overlay unit tests, driverless metas)."""
    from car_orders.models import CarOrder
    from car_orders.services import audit, shift

    with transaction.atomic():
        order = CarOrder.objects.select_for_update().filter(pk=order_id).first()
        if order is None:
            return
        if order.status not in (
            CarOrder.Status.COMPLETED,
            CarOrder.Status.REJECTED,
            CarOrder.Status.CANCELLED,
        ):
            order.status = CarOrder.Status.CANCELLED
            fields = ["status", "updated_at"]
            if order.finished_at is None:
                order.finished_at = timezone.now()
                fields.append("finished_at")
            order.save(update_fields=fields)
            shift.reset_driver_shift(order.driver)
        audit.record_cancelled(order, actor, reason=reason, waited_s=waited_s)


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
    # Back in the queue → restart the «поиск водителя» clock.
    meta.search_started_at = timezone.now()
    # Remember who we just pulled it off, so the auto-dispatch worker doesn't hand
    # the order straight back to them (they're typically the nearest free driver).
    if prev_driver is not None:
        excluded = list(meta.excluded_driver_ids or [])
        if prev_driver not in excluded:
            excluded.append(prev_driver)
        meta.excluded_driver_ids = excluded
    meta.save()
    OrderLiveLocation.objects.filter(order_id=order_id).delete()
    # Tell the watchers the current tracking ended; the driver they were on is gone.
    events.broadcast_location(order_id, {"trip_state": "cancelled"})
    events.notify_dropped_driver(prev_driver, order_id)
    return meta


def extend(order_id, minutes, *, actor_driver_id=None, is_dispatcher=False):
    """Add ``minutes`` to the order's planned duration in our overlay and re-check the
    driver's next window. Returns ``(meta, conflict_dict_or_None)``; the extension
    always applies — the conflict is a warning. Raises ``VALIDATION``.
    ``actor_driver_id``/``is_dispatcher`` enforce ownership (driver's own order or a
    dispatcher), mirroring the native ``services.orders.extend`` gate."""
    if minutes <= 0 or minutes > _MAX_EXTEND_MINUTES:
        raise OverlayError(
            "VALIDATION",
            _("`minutes` must be between 1 and %(max)s.") % {"max": _MAX_EXTEND_MINUTES},
        )
    meta = OrderMeta.objects.filter(order_id=order_id).first()
    if meta is None:
        raise OverlayError("VALIDATION", _("No order to extend."))
    _ensure_actor_owns(meta, actor_driver_id, is_dispatcher)
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
    # Tearing down the claim ends any pickup wait — a re-driven order starts fresh.
    meta.arrived_at = None


def _conflict_payload(conflict):
    if conflict is None:
        return None
    return {
        "order_id": conflict.order_id,
        "planned_start": conflict.planned_datetime,
        "planned_end": conflict.planned_end,
        "address": f"Заказ #{conflict.order_id}",
    }
