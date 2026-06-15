"""Realtime event fan-out — the server→client push side of the live map,
dispatcher dashboard and notification toasts.

Mirrors ark-backend's ``services/events.py``. Every broadcast is:

1. deferred to ``transaction.on_commit`` — a watcher never sees state the
   originating request hasn't committed yet, and a rolled-back request fans out
   nothing. Outside any open transaction ``on_commit`` runs synchronously, so the
   behaviour is unchanged for the (common) autocommit call sites.
2. routed through the safe :func:`_group_send` — a missing or unreachable channel
   layer (Redis down) is logged and swallowed instead of breaking the REST request
   that triggered it.

Channel groups:
- ``order_loc_<id>`` — per-order tracker stream (the order's map watchers).
- ``fleet_live``     — the dispatcher dashboard (every order's frames, tagged).
- ``user_<id>``      — a single user's notification toasts.
"""

import logging

from django.db import transaction

logger = logging.getLogger(__name__)

# Fleet-wide group — the dispatcher dashboard subscribes here to see EVERY order's
# movement / stage change at once (each frame carries its order_id).
FLEET_GROUP = "fleet_live"


def group_name(order_id) -> str:
    return f"order_loc_{order_id}"


def user_group(user_id) -> str:
    return f"user_{user_id}"


# Human-readable status messages pushed to the driver + requester.
_TRIP_MESSAGES = {
    "assigned": "Водитель назначен",
    "to_client": "Водитель выехал к месту подачи",
    "at_client": "Водитель на месте подачи",
    "in_trip": "Поездка началась",
    "at_destination": "Прибыли на место назначения",
    "waiting": "Поездка на паузе (ожидание)",
    "completed": "Заказ завершён",
    "cancelled": "Заказ отменён / возвращён в очередь",
}


def _group_send(group: str, message: dict) -> None:
    """Send ``message`` to a channel group, swallowing any failure. Realtime fan-out
    must never break the originating REST request: if channels isn't configured or
    Redis is down, log and carry on."""
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(group, message)
    except Exception:
        logger.exception("car_orders: failed to broadcast to group %s", group)


def _on_commit(fn) -> None:
    """Run ``fn`` after the current transaction commits. Outside any open
    transaction ``on_commit`` runs it synchronously — exactly what we want."""
    transaction.on_commit(fn)


def notify_user(user_id, payload) -> None:
    """Push an event to a single user's group (their app shows a toast)."""
    if user_id is None:
        return
    _on_commit(lambda: _group_send(user_group(user_id), {"type": "notify.event", "data": payload}))


def notify_order_status(meta, trip_state) -> None:
    """Notify BOTH the driver and the order's author of a status change."""
    payload = {
        "order_id": meta.order_id,
        "trip_state": trip_state,
        "message": _TRIP_MESSAGES.get(trip_state, trip_state),
    }
    notify_user(meta.driver_id, payload)
    notify_user(getattr(meta, "author_id", None), payload)


def notify_dropped_driver(driver_id, order_id) -> None:
    """Tell a driver an order was taken off them (reassign / release)."""
    if driver_id is None:
        return
    notify_user(
        driver_id,
        {
            "order_id": int(order_id),
            "trip_state": "cancelled",
            "message": "Заказ снят с вас / возвращён в очередь",
        },
    )


def broadcast_location(order_id, data) -> None:
    """Push a position / trip-state frame to the order's own group AND the fleet
    group (the latter tagged with order_id), so both the per-order tracker and the
    dispatcher dashboard update live — after the current transaction commits."""

    def _send():
        _group_send(group_name(order_id), {"type": "location.update", "data": data})
        _group_send(
            FLEET_GROUP,
            {"type": "location.update", "data": {**data, "order_id": int(order_id)}},
        )

    _on_commit(_send)
