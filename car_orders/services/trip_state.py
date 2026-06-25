"""Trip-state machine for overlay orders — the single source of truth for how an
order advances through its delivery stages and what side-effects each step fires.

Extracted from the old ``TripStateView`` so the rules (allowed transitions, the
round-trip return leg, the arrival geofence, «one moving trip at a time») live in
one testable place and the view stays a thin HTTP adapter.

The flow (forward; same-state re-tap and → cancelled are always allowed)::

    assigned → to_client → at_client → in_trip → at_destination / waiting → completed
                                          ↑__________________|  (round-trip return leg)

The functions raise :class:`TripStateError` (carrying a machine ``code`` plus an
HTTP-status hint) on any rule violation; the view maps that onto the response.
"""

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from car_orders import geometry, scheduling
from car_orders.models import DriverPosition, OrderLiveLocation, OrderMeta

_TS = OrderMeta.TripState
TERMINAL = (_TS.COMPLETED, _TS.CANCELLED)

# Allowed forward transitions. Same-state re-tap and → cancelled are handled
# separately (always allowed). ``at_destination → in_trip`` is the round-trip
# RETURN leg; ``completed`` only from at_destination / waiting.
ALLOWED_TRANSITIONS = {
    _TS.ASSIGNED: {_TS.TO_CLIENT},
    _TS.TO_CLIENT: {_TS.AT_CLIENT},
    _TS.AT_CLIENT: {_TS.IN_TRIP},
    _TS.IN_TRIP: {_TS.AT_DESTINATION, _TS.WAITING},
    _TS.AT_DESTINATION: {_TS.IN_TRIP, _TS.WAITING, _TS.COMPLETED},
    _TS.WAITING: {_TS.IN_TRIP, _TS.COMPLETED},
}


class TripStateError(Exception):
    """A trip-state rule was violated. Carries a stable ``code`` (so the API maps it
    onto a response without parsing message strings) and an HTTP-status hint — 403
    for permission errors, 400 otherwise. Mirrors ark-backend's code-carrying
    service exceptions."""

    def __init__(self, code: str, message, *, http_status: int = 400):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(str(message))


def is_known_state(state) -> bool:
    return state in {choice for choice, _label in OrderMeta.TripState.choices}


def can_transition(current, new) -> bool:
    """Pure rule: may an order go ``current`` → ``new``? Same-state re-tap and
    → cancelled are always allowed."""
    if new in (current, _TS.CANCELLED):
        return True
    return new in ALLOWED_TRANSITIONS.get(current, set())


def check_arrival_geofence(meta, state) -> None:
    """The driver may only mark arrival (at_client / at_destination) within
    ``CAR_ORDER_ARRIVAL_GEOFENCE_M`` of the point AND with a fresh GPS fix. Raises
    :class:`TripStateError` when too far / no fresh fix. 0-radius disables it."""
    radius_m = getattr(settings, "CAR_ORDER_ARRIVAL_GEOFENCE_M", 100)
    if not radius_m:
        return
    if state == _TS.AT_CLIENT:
        tgt = (meta.origin_lat, meta.origin_lng)
    elif meta.returning and meta.return_lat is not None and meta.return_lng is not None:
        tgt = (meta.return_lat, meta.return_lng)
    else:
        tgt = (meta.address_lat, meta.address_lng)
    if tgt[0] is None or tgt[1] is None:
        return  # no target coords → can't enforce
    pos = DriverPosition.objects.filter(driver_id=meta.driver_id).first()
    fresh_s = getattr(settings, "CAR_ORDER_GPS_FRESH_S", 120)
    if pos is None or (timezone.now() - pos.last_seen).total_seconds() > fresh_s:
        raise TripStateError("NO_FRESH_GPS", _("Need a fresh GPS fix to confirm arrival."))
    dist_m = geometry.haversine_km(pos.lat, pos.lng, tgt[0], tgt[1]) * 1000
    if dist_m > radius_m:
        raise TripStateError(
            "TOO_FAR",
            _("Too far from the point (%(m)d m) to mark arrival.") % {"m": int(dist_m)},
        )


def validate(meta, new_state, *, actor_driver_id=None, is_dispatcher=False) -> dict:
    """Check every rule for advancing ``meta`` to ``new_state`` and return the field
    updates to apply (``{"trip_state": ...}`` plus ``"returning": True`` on the
    round-trip return leg). Side-effect free — raises :class:`TripStateError` on the
    first violated rule, in the precedence the API has always used. ``meta`` is the
    current (persisted) order."""
    cur = meta.trip_state

    # Only the ASSIGNED driver (or a dispatcher) may advance the trip. A DRIVERLESS
    # order has no assignee, so a plain driver can't advance it either — otherwise any
    # driver could drive an unassigned order's state machine. A trusted server-side
    # call (the simulator / auto-dispatch) passes actor_driver_id=None and is exempt.
    if (
        actor_driver_id is not None
        and not is_dispatcher
        and (meta.driver_id is None or str(actor_driver_id) != str(meta.driver_id))
    ):
        raise TripStateError(
            "PERMISSION_DENIED",
            _("Only the assigned driver can change this order's stage."),
            http_status=403,
        )

    if cur == _TS.COMPLETED and new_state != _TS.COMPLETED:
        raise TripStateError("INVALID_STATUS", _("This order is already completed."))

    # Transitions must follow the flow (same-state re-tap and → cancelled ok).
    if not can_transition(cur, new_state):
        raise TripStateError(
            "INVALID_TRANSITION",
            _("Cannot go from %(a)s to %(b)s.") % {"a": cur, "b": new_state},
        )

    # Round trip: can't complete before the return leg is done.
    if new_state == _TS.COMPLETED and meta.has_return and not meta.returning:
        raise TripStateError("INVALID_TRANSITION", _("Drive the return leg before completing."))

    # Geofence the arrival stages.
    if new_state in (_TS.AT_CLIENT, _TS.AT_DESTINATION):
        check_arrival_geofence(meta, new_state)

    # Don't let a driver start DRIVING a 2nd order while already driving one (one car
    # / one place). A parked driver — on hold during a long shoot (waiting /
    # at_destination) — is free to take a gap order, so we only block the transition
    # INTO a moving stage while another is moving.
    if (
        meta.driver_id is not None
        and new_state in scheduling.MOVING_STATES
        and cur not in scheduling.MOVING_STATES
    ):
        other = scheduling.meta_active_trip(
            meta.driver_id,
            exclude_order_id=int(meta.order_id),
            states=scheduling.MOVING_STATES,
        )
        if other is not None:
            raise TripStateError(
                "ACTIVE_TRIP_EXISTS", _("Finish the current trip before starting another.")
            )

    defaults = {"trip_state": new_state}
    # Round trip: leaving the destination (at_destination/waiting) back INTO a moving
    # stage means the driver started the RETURN leg → flip `returning` so the
    # simulator/map drive destination → return point and «Завершить» only shows once
    # that leg is done.
    if (
        meta.has_return
        and not meta.returning
        and new_state == _TS.IN_TRIP
        and cur in (_TS.AT_DESTINATION, _TS.WAITING)
    ):
        defaults["returning"] = True
    return defaults


def advance(order_id, new_state, *, actor_driver_id=None, is_dispatcher=False):
    """Advance an order to ``new_state``: validate every rule, persist the change,
    then fire the side-effects. Returns the updated ``OrderMeta``.

    Raises :class:`TripStateError` (unknown state / missing order / rule violation).
    """
    if not is_known_state(new_state):
        raise TripStateError("VALIDATION", _("Unknown trip_state."))

    # AUDIT H1: read-validate-write the state machine (incl. the «one moving trip»
    # check) inside ONE transaction, serialised per-driver so a double-tap / two
    # devices can't both advance. The advisory lock is taken BEFORE the row lock —
    # the same order as services.overlay.claim — so the two can't deadlock. We need
    # the driver id for the lock key, so peek it first (unlocked), then take the
    # lock and re-read the row under select_for_update.
    from django.db import transaction

    from car_orders import concurrency

    with transaction.atomic():
        peek = OrderMeta.objects.filter(order_id=order_id).only("driver_id").first()
        if peek is None:
            raise TripStateError("NOT_FOUND", _("No order to advance."))
        concurrency.lock_driver(peek.driver_id)
        meta = OrderMeta.objects.select_for_update().filter(order_id=order_id).first()
        if meta is None:
            raise TripStateError("NOT_FOUND", _("No order to advance."))
        defaults = validate(
            meta, new_state, actor_driver_id=actor_driver_id, is_dispatcher=is_dispatcher
        )
        # «Ожидание клиента» timer starts here: stamp the FIRST arrival at the pickup
        # so every surface counts the wait from the same instant. A same-state re-tap
        # won't overwrite it (meta.arrived_at is already set). Done in advance (not
        # validate) so validate stays a pure rule-check returning only the trip_state.
        if new_state == _TS.AT_CLIENT and meta.arrived_at is None:
            defaults["arrived_at"] = timezone.now()
        meta, _created = OrderMeta.objects.update_or_create(order_id=order_id, defaults=defaults)
        # The mobile client drives the whole trip through trip_state and never calls
        # the native /start/ or /complete/ endpoints, so the demo CarOrder.status
        # would otherwise never reach `completed` (an overlay-claimed order stays
        # `awaiting_driver`, a scheduled one stays `scheduled`). Mirror the terminal
        # onto the native order here — atomically with the meta update — so status,
        # shift and audit stay in sync.
        if meta.trip_state == _TS.COMPLETED:
            _reconcile_native_completion(meta)
    _fire_side_effects(meta, new_state)
    return meta


def _reconcile_native_completion(meta) -> None:
    """Mirror an overlay ``COMPLETED`` onto the demo ``CarOrder`` so the native
    status, the driver's shift and the audit trail stay in sync — even for an
    overlay-claimed or scheduled order the mobile client never explicitly
    /start/ed or /complete/d (its only completion signal is ``trip_state=completed``).

    Idempotent: a CarOrder already in a terminal state is left untouched, so a web
    client that closes a native order via ``/complete/`` (and may also post
    ``trip_state=completed``) is never double-recorded. No-op when there is no
    backing CarOrder (overlay unit tests, driverless metas)."""
    from car_orders.models import CarOrder
    from car_orders.services import audit, shift

    order = CarOrder.objects.select_for_update().filter(pk=meta.order_id).first()
    if order is None or order.status in (
        CarOrder.Status.COMPLETED,
        CarOrder.Status.REJECTED,
        CarOrder.Status.CANCELLED,
    ):
        return
    order.status = CarOrder.Status.COMPLETED
    fields = ["status", "updated_at"]
    if order.finished_at is None:
        order.finished_at = timezone.now()
        fields.append("finished_at")
    if order.started_at is None:
        # A scheduled order completed without a native /start/ has no started_at;
        # stamp it so finished_at ≥ started_at and the activity reads sanely.
        order.started_at = order.finished_at or timezone.now()
        fields.append("started_at")
    order.save(update_fields=fields)
    shift.reset_driver_shift(order.driver)
    if order.driver_id is not None:
        audit.record_completed(order, order.driver)


def _fire_side_effects(meta, new_state) -> None:
    """Side-effects of a committed stage change: drop the live marker on a terminal
    state, broadcast the stage, toast the driver + requester, and re-push the route
    for the NEW leg so the map always shows where to go. Imports the WS / routing
    helpers lazily (as the rest of the block does) to stay import-cycle-free."""
    from car_orders import dispatch
    from car_orders.services import events

    if new_state in TERMINAL:
        OrderLiveLocation.objects.filter(order_id=meta.order_id).delete()
    payload = {"trip_state": new_state, "returning": meta.returning}
    # Push the arrival instant so live watchers (customer / dispatcher) start the
    # «ожидание клиента» clock immediately, without waiting for the next poll.
    if new_state == _TS.AT_CLIENT and meta.arrived_at is not None:
        payload["arrived_at"] = meta.arrived_at.isoformat()
    events.broadcast_location(meta.order_id, payload)
    events.notify_order_status(meta, new_state)  # toast to driver + requester
    # Server owns the route: push the geometry for the NEW leg (approach to pickup /
    # pickup→destination / return), so the map always shows where to go.
    dispatch.push_order_route(meta)
