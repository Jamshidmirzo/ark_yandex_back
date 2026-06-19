"""Native car-order lifecycle — the business logic behind the ``CarOrderViewSet``
workflow actions (claim → start → complete, plus cancel / release / reassign /
extend).

Extracted from the view so the rules (status preconditions, ownership, shift /
scheduling checks, audit trail, shift-status side-effects) live in one testable
place. Each function raises :class:`OrderError` (carrying a machine ``code``, an
HTTP-status hint and optional ``details``) on any rule violation; the view maps
that onto the error response. Order lookups are by pk — these endpoints resolve
the order directly (not via the visibility queryset) so a non-assignee gets an
explicit error rather than a 404.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from auth_core.permissions import user_has_permission
from car_orders import scheduling
from car_orders.models import CarOrder, DriverShift
from car_orders.services import audit, shift

_TERMINAL = (CarOrder.Status.COMPLETED, CarOrder.Status.REJECTED, CarOrder.Status.CANCELLED)
# AUDIT M3: cap a single extension so a bogus value can't blow planned_end out.
# Generous (above the web UI's 99h max) so no legitimate extend is rejected.
_MAX_EXTEND_MINUTES = 7 * 24 * 60


class OrderError(Exception):
    """A car-order rule was violated. Carries a stable ``code`` (so the API maps it
    onto a response without parsing messages), an HTTP-status hint (403 for
    permission, 409 for a window conflict, 400 otherwise) and optional ``details``.
    Mirrors ark-backend's code-carrying service exceptions."""

    def __init__(self, code: str, message, *, http_status: int = 400, details: dict | None = None):
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}
        super().__init__(str(message))


def _get(order_id):
    order = CarOrder.objects.filter(pk=order_id).first()
    if order is None:
        raise OrderError("NOT_FOUND", _("Order not found."))
    return order


def conflict_payload(order) -> dict:
    """The ``details`` block describing an overlapping order (window + address)."""
    return {
        "order_id": order.id,
        "planned_start": order.planned_datetime,
        "planned_end": order.planned_end,
        "address": order.address,
    }


def claim(order_id, driver):
    """Driver reserves an awaiting order into their schedule (Р1: shift car). Moves
    it to ``scheduled``; the window must not overlap another committed window
    (+ travel buffer), else ``TIME_CONFLICT`` (409)."""
    with transaction.atomic():
        try:
            order = CarOrder.objects.select_for_update().get(pk=order_id)
        except CarOrder.DoesNotExist:
            raise OrderError("NOT_FOUND", _("Order not found."))
        if order.status != CarOrder.Status.AWAITING_DRIVER:
            raise OrderError("ALREADY_TAKEN", _("This order is no longer available."))
        active = shift.active_shift(driver)
        if active is None:
            raise OrderError("NO_SHIFT", _("Select a car for your shift before accepting orders."))
        if order.car_type_id and active.car.type_id != order.car_type_id:
            raise OrderError(
                "TYPE_MISMATCH", _("Your shift car does not match the requested type.")
            )
        window = scheduling.order_window(order)
        if window:
            conflict = scheduling.find_time_conflict(driver, window[0], window[1])
            if conflict:
                raise OrderError(
                    "TIME_CONFLICT",
                    _("This time window overlaps another of your orders."),
                    http_status=409,
                    details=conflict_payload(conflict),
                )
        order.status = CarOrder.Status.SCHEDULED
        order.driver = driver
        order.car = active.car
        order.save(update_fields=["status", "driver", "car", "updated_at"])
    audit.record_accepted(order, driver)
    return order


def start(order_id, driver):
    """Driver begins a scheduled trip → ``in_progress`` (only one at a time)."""
    order = _get(order_id)
    if order.driver_id != driver.id:
        raise OrderError(
            "PERMISSION_DENIED", _("Only the assigned driver can start this trip."), http_status=403
        )
    if order.status != CarOrder.Status.SCHEDULED:
        raise OrderError("INVALID_STATUS", _("Only a scheduled order can be started."))
    if scheduling.active_trip(driver, exclude_id=order.pk) is not None:
        raise OrderError("DRIVER_BUSY", _("Finish your current trip before starting another."))
    order.status = CarOrder.Status.IN_PROGRESS
    order.started_at = timezone.now()
    order.save(update_fields=["status", "started_at", "updated_at"])
    active = shift.active_shift(driver)
    if active:
        active.status = DriverShift.Status.EN_ROUTE
        active.save(update_fields=["status", "updated_at"])
    return order


def complete(order_id, driver):
    """Assigned driver finishes the in-progress trip → ``completed``."""
    order = _get(order_id)
    if order.driver_id != driver.id:
        raise OrderError(
            "PERMISSION_DENIED",
            _("Only the assigned driver can complete this trip."),
            http_status=403,
        )
    if order.status != CarOrder.Status.IN_PROGRESS:
        raise OrderError("INVALID_STATUS", _("Only an in-progress trip can be completed."))
    order.status = CarOrder.Status.COMPLETED
    order.finished_at = timezone.now()
    order.save(update_fields=["status", "finished_at", "updated_at"])
    active = shift.active_shift(driver)
    if active:
        active.status = DriverShift.Status.ONLINE
        active.save(update_fields=["status", "updated_at"])
    audit.record_completed(order, driver)
    return order


def cancel(order_id, actor, reason=""):
    """Dispatcher (or author) cancels an order; frees the driver's window."""
    order = _get(order_id)
    if order.status in _TERMINAL:
        raise OrderError("INVALID_STATUS", _("This order can no longer be cancelled."))
    is_author = order.created_by_id == actor.id
    if not (is_author or user_has_permission(actor, "car_order:reject")):
        raise OrderError("PERMISSION_DENIED", _("You cannot cancel this order."), http_status=403)
    driver = order.driver
    order.status = CarOrder.Status.CANCELLED
    order.save(update_fields=["status", "updated_at"])
    shift.reset_driver_shift(driver)
    audit.record_cancelled(order, actor, reason=reason)
    return order


def release(order_id, driver, reason=""):
    """Assigned driver hands an order back; it returns to ``awaiting_driver``."""
    order = _get(order_id)
    if order.driver_id != driver.id:
        raise OrderError(
            "PERMISSION_DENIED",
            _("Only the assigned driver can release this order."),
            http_status=403,
        )
    if order.status not in (CarOrder.Status.SCHEDULED, CarOrder.Status.IN_PROGRESS):
        raise OrderError("INVALID_STATUS", _("This order cannot be released."))
    prev_driver = order.driver
    order.status = CarOrder.Status.AWAITING_DRIVER
    order.driver = None
    order.car = None
    order.started_at = None
    order.save(update_fields=["status", "driver", "car", "started_at", "updated_at"])
    shift.reset_driver_shift(prev_driver)
    audit.record_released(order, driver, reason=reason)
    return order


def reassign(order_id, actor):
    """Dispatcher takes an order off its driver → ``awaiting_driver`` so a new car
    can pick it up (e.g. when the driver can't make the latest start)."""
    order = _get(order_id)
    if order.status not in (CarOrder.Status.SCHEDULED, CarOrder.Status.IN_PROGRESS):
        raise OrderError("INVALID_STATUS", _("This order cannot be reassigned."))
    from_driver = order.driver
    order.status = CarOrder.Status.AWAITING_DRIVER
    order.driver = None
    order.car = None
    order.started_at = None
    order.save(update_fields=["status", "driver", "car", "started_at", "updated_at"])
    shift.reset_driver_shift(from_driver)
    audit.record_reassigned(order, actor, from_driver_id=from_driver.id if from_driver else None)
    return order


def extend(order_id, actor, minutes):
    """Add ``minutes`` to an active/scheduled order's duration and re-check the
    driver's next window. Returns ``(order, conflict_details_or_None)`` — the
    extension always applies; the conflict is a warning."""
    order = _get(order_id)
    is_driver = order.driver_id == actor.id
    if not (is_driver or user_has_permission(actor, "car_order:approve")):
        raise OrderError("PERMISSION_DENIED", _("You cannot extend this order."), http_status=403)
    if order.status not in (CarOrder.Status.SCHEDULED, CarOrder.Status.IN_PROGRESS):
        raise OrderError("INVALID_STATUS", _("Only an active order can be extended."))
    if minutes <= 0 or minutes > _MAX_EXTEND_MINUTES:
        raise OrderError(
            "VALIDATION",
            _("`minutes` must be between 1 and %(max)s.") % {"max": _MAX_EXTEND_MINUTES},
        )
    order.estimated_duration = (order.estimated_duration or timedelta()) + timedelta(
        minutes=minutes
    )
    order.save(update_fields=["estimated_duration", "updated_at"])
    audit.record_extended(order, actor, minutes)
    conflict = None
    window = scheduling.order_window(order)
    if window and order.driver_id:
        conflict = scheduling.find_time_conflict(
            order.driver, window[0], window[1], exclude_id=order.pk
        )
    return order, (conflict_payload(conflict) if conflict else None)
