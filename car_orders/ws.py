"""WebSocket consumer for live driver tracking.

Browsers connect to ``ws://<host>/ws/car-orders/<order_id>/location/``; on
connect they get the last known position (with the route geometry), then every
heartbeat is pushed in real time. The HTTP ``POST /{id}/live-location/`` view
publishes movement to the ``order_loc_<id>`` group (see views.LiveLocationView).
"""

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.urls import re_path


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
    async_to_sync(layer.group_send)(
        group_name(order_id), {"type": "location.update", "data": data}
    )
    async_to_sync(layer.group_send)(
        FLEET_GROUP,
        {"type": "location.update", "data": {**data, "order_id": int(order_id)}},
    )


class LiveLocationConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.order_id = self.scope["url_route"]["kwargs"]["order_id"]
        self.group = group_name(self.order_id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        latest = await self._latest()
        if latest:
            await self.send_json(latest)

    async def disconnect(self, code):
        await self.channel_layer.group_discard(self.group, self.channel_name)

    # Fan-out handler: channel_layer.group_send(..., {"type": "location.update", ...})
    async def location_update(self, event):
        await self.send_json(event["data"])

    @database_sync_to_async
    def _latest(self):
        from car_orders.models import OrderLiveLocation

        loc = OrderLiveLocation.objects.filter(order_id=self.order_id).first()
        if not loc:
            return None
        return {
            "lat": loc.lat,
            "lng": loc.lng,
            "last_seen": loc.last_seen.isoformat(),
            "geometry": loc.geometry,
        }


class FleetConsumer(AsyncJsonWebsocketConsumer):
    """Dispatcher dashboard feed: on connect sends a full snapshot of all active
    orders, then forwards every per-order position/stage update (tagged with
    order_id) as it happens."""

    async def connect(self):
        await self.channel_layer.group_add(FLEET_GROUP, self.channel_name)
        await self.accept()
        await self.send_json({"type": "snapshot", "orders": await self._snapshot()})

    async def disconnect(self, code):
        await self.channel_layer.group_discard(FLEET_GROUP, self.channel_name)

    async def location_update(self, event):
        await self.send_json({"type": "update", **event["data"]})

    @database_sync_to_async
    def _snapshot(self):
        from car_orders.fleet import fleet_live_orders

        return fleet_live_orders()


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    """Per-user notification stream: the driver and the requester subscribe to
    their own group and get a toast on every status change of their orders."""

    async def connect(self):
        self.uid = self.scope["url_route"]["kwargs"]["user_id"]
        await self.channel_layer.group_add(user_group(self.uid), self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(user_group(self.uid), self.channel_name)

    async def notify_event(self, event):
        await self.send_json(event["data"])


websocket_urlpatterns = [
    re_path(r"^ws/car-orders/fleet/$", FleetConsumer.as_asgi()),
    re_path(r"^ws/notifications/(?P<user_id>\d+)/$", NotificationConsumer.as_asgi()),
    re_path(r"^ws/car-orders/(?P<order_id>\d+)/location/$", LiveLocationConsumer.as_asgi()),
]
