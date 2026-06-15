"""Downlink (server → client) tracking consumers: the per-order map stream, the
dispatcher fleet feed, per-user notifications, and a catch-all for foreign paths."""

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from car_orders.ws.groups import FLEET_GROUP, group_name, user_group


class LiveLocationConsumer(AsyncJsonWebsocketConsumer):
    """Per-order map stream. Connect ``ws/car-orders/<id>/location/``; on connect
    you get the last known position + route geometry, then every position / stage
    frame as it happens. READ-ONLY (the driver does NOT send GPS here — see the
    driver uplink in ws/driver.py)."""

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
        """Connect replay: position + route (if any) AND the current stage
        (trip_state/returning), so the banner is correct after every reconnect —
        and a terminal order replays its completed/cancelled frame so the client
        closes cleanly instead of hanging blank."""
        from car_orders.models import OrderLiveLocation, OrderMeta

        meta = OrderMeta.objects.filter(order_id=self.order_id).first()
        loc = OrderLiveLocation.objects.filter(order_id=self.order_id).first()
        if not meta and not loc:
            return None
        out = {}
        if loc:
            from car_orders.geometry import trim_geometry

            # Anchor the line to the car's current position (drop the part behind it),
            # so on connect — even while parked — the route starts AT the vehicle and
            # goes to the target, not from the leg's original origin. Also bounds the frame.
            geom = (
                trim_geometry(loc.geometry, loc.lat, loc.lng)
                if (loc.geometry and loc.lat is not None)
                else loc.geometry
            )
            out.update(
                {
                    "lat": loc.lat,
                    "lng": loc.lng,
                    "last_seen": loc.last_seen.isoformat(),
                    "geometry": geom,
                }
            )
        if meta:
            out["trip_state"] = meta.trip_state
            out["returning"] = meta.returning
        return out or None


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


class FallbackConsumer(AsyncJsonWebsocketConsumer):
    """Cleanly close any WS path we don't serve. The host app opens its own sockets
    (e.g. /ws/board/, /ws/bus/) that live on the demo backend and are NOT proxied
    here — without this they raise «No route found» with a noisy traceback on every
    (re)connect. We just close so the log stays clean."""

    async def connect(self):
        await self.close()
