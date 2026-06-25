"""Single source of truth for a car order's EFFECTIVE status.

A car order has two state stores: the demo ``CarOrder.status`` and our overlay
``OrderMeta`` (trip_state / overlay_claimed / dispatchable). An overlay-claimed
order deliberately keeps its demo status at ``awaiting_driver``, so the real
status must be reconciled from the overlay. This used to be done independently on
every client (and the implementations drifted), causing recurring "wrong status"
bugs. Computing it here — once, server-side — makes the backend authoritative and
the bug class structurally impossible.

This mirrors the canonical web logic (``ark_yandex_front/src/utils/orderStatus.ts``
:: ``effectiveStatus``). Consistent with ``trip_state._reconcile_native_completion``
(a completed trip already mirrors the demo status to ``completed``).
"""

from car_orders.models import CarOrder, OrderMeta

_S = CarOrder.Status
_TS = OrderMeta.TripState
_TERMINAL_DEMO = {_S.COMPLETED, _S.REJECTED, _S.CANCELLED}


def effective_status(demo_status, meta):
    """Reconcile ``demo_status`` (a ``CarOrder.status`` value) with the overlay
    ``meta`` (an ``OrderMeta`` or ``None``). Returns a ``CarOrder.Status`` value.

    - overlay-claimed: trip completed → completed; trip cancelled → demo (back in
      queue), or cancelled when the demo status is unknown; any other (active) trip →
      in_progress unless the demo is already terminal (a demo-terminal order wins over a
      stale active claim).
    - direct-created (dispatchable, still draft/pending on demo) → awaiting_driver.
    - otherwise the demo status is authoritative.

    A TERMINAL overlay trip (completed / cancelled) is resolved BEFORE the
    ``demo_status is None`` guard: its trip_state is authoritative even when the demo
    status can't be read (an overlay-only finished ride whose upstream body the caller
    can't fetch — a driver's history), so the card always shows a status badge
    («у заказов в истории нет статуса»). ACTIVE orders keep the demo-backed contract
    (request-less feeds still read None until the HTTP refetch backfills the status).
    """
    trip = getattr(meta, "trip_state", None) if meta is not None else None
    claimed = meta is not None and getattr(meta, "overlay_claimed", False)
    if claimed and trip == _TS.COMPLETED:
        return _S.COMPLETED
    # A cancelled order DROPS its claim (overlay_claimed=False), so key off trip_state,
    # not `claimed`, or a cancelled ride in history would have no status badge.
    if trip == _TS.CANCELLED and demo_status is None:
        return _S.CANCELLED
    if demo_status is None:
        return demo_status
    if claimed:
        if trip == _TS.CANCELLED:
            return demo_status
        if demo_status not in _TERMINAL_DEMO:
            return _S.IN_PROGRESS
        return demo_status
    if (
        meta is not None
        and getattr(meta, "dispatchable", False)
        and demo_status in (_S.DRAFT, _S.PENDING)
    ):
        return _S.AWAITING_DRIVER
    return demo_status


def status_map_for(order_ids):
    """Map ``order_id -> CarOrder.status`` for the backing local orders, in ONE query.

    Lets the OrderMeta-only feeds (the fleet snapshot, the overlay-orders board)
    reconcile each row's effective status without an N+1. ``OrderMeta.order_id`` is a
    plain id (not a FK) keyed by the demo order, so a local ``CarOrder`` may be absent
    (gateway/demo order not mirrored — same case ``trip_state._reconcile_native_completion``
    already tolerates). Missing ids are simply absent from the map, so callers get
    ``None`` → ``effective_status(None, meta)`` returns ``None`` (status absent, not wrong).
    """
    if not order_ids:
        return {}
    return dict(
        CarOrder.objects.filter(pk__in=list(order_ids)).values_list("pk", "status")
    )
