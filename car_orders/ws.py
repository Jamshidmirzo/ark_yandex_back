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


websocket_urlpatterns = [
    re_path(r"^ws/car-orders/(?P<order_id>\d+)/location/$", LiveLocationConsumer.as_asgi()),
]
