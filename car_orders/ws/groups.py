"""WebSocket groups + server→client push helpers (no consumers here).

Two channel groups:
- ``order_loc_<id>`` — per-order tracker stream (the order's map watchers).
- ``fleet_live``     — the dispatcher dashboard (every order's frames, tagged).
- ``user_<id>``      — a single user's notification toasts.
"""


def group_name(order_id) -> str:
    return f"order_loc_{order_id}"


# Fleet-wide group — the dispatcher dashboard subscribes here to see EVERY order's
# movement / stage change at once (each frame carries its order_id).
FLEET_GROUP = "fleet_live"


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


def notify_user(user_id, payload):
    """Push an event to a single user's group (their app shows a toast)."""
    if user_id is None:
        return
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(user_group(user_id), {"type": "notify.event", "data": payload})


def notify_order_status(meta, trip_state):
    """Notify BOTH the driver and the order's author of a status change."""
    payload = {
        "order_id": meta.order_id,
        "trip_state": trip_state,
        "message": _TRIP_MESSAGES.get(trip_state, trip_state),
    }
    notify_user(meta.driver_id, payload)
    notify_user(getattr(meta, "author_id", None), payload)


def broadcast_location(order_id, data):
    """Push a position / trip-state frame to the order's own group AND the fleet
    group (the latter tagged with order_id), so both the per-order tracker and the
    dispatcher dashboard update live. No-op if channels isn't configured."""
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(group_name(order_id), {"type": "location.update", "data": data})
    async_to_sync(layer.group_send)(
        FLEET_GROUP,
        {"type": "location.update", "data": {**data, "order_id": int(order_id)}},
    )
