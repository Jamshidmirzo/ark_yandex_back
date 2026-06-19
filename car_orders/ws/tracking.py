"""Downlink (server → client) tracking consumers: the per-order map stream, the
dispatcher fleet feed, per-user notifications, and a catch-all for foreign paths."""

import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from car_orders.ws.groups import FLEET_GROUP, group_name, user_group

logger = logging.getLogger(__name__)


async def _join_group(consumer, group):
    """Subscribe to a channel group AFTER the socket is accepted, tolerating a
    transient channel-layer (Redis) outage. ``group_add`` does a real Redis
    round-trip; when it was called BEFORE ``accept()`` an unreachable Redis raised
    here and aborted the handshake — the socket dropped with 1011 and the client
    reconnect-looped for as long as Redis was down. Now we log and keep a live
    socket instead: the connect replay / snapshot still works, only live fan-out is
    missed until the client reconnects against a healthy layer."""
    try:
        await consumer.channel_layer.group_add(group, consumer.channel_name)
    except Exception:
        logger.exception(
            "car_orders WS: group_add failed for %s — live updates disabled until reconnect",
            group,
        )


async def _leave_group(consumer, group):
    """Mirror of :func:`_join_group` for disconnect — a channel-layer outage must
    never raise out of ``disconnect()`` either."""
    try:
        await consumer.channel_layer.group_discard(group, consumer.channel_name)
    except Exception:
        logger.exception("car_orders WS: group_discard failed for %s", group)


class LiveLocationConsumer(AsyncJsonWebsocketConsumer):
    """Per-order map stream. Connect ``ws/car-orders/<id>/location/``; on connect
    you get the last known position + route geometry, then every position / stage
    frame as it happens. READ-ONLY (the driver does NOT send GPS here — see the
    driver uplink in ws/driver.py)."""

    async def connect(self):
        self.order_id = self.scope["url_route"]["kwargs"]["order_id"]
        self.group = group_name(self.order_id)
        await self.accept()
        await _join_group(self, self.group)
        latest = await self._latest()
        if latest:
            await self.send_json(latest)

    async def disconnect(self, code):
        await _leave_group(self, self.group)

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
            # Awaiting a driver (none assigned) → no live leg, so the map would
            # only get the A/B pins. Fall back to the ordered trip A→B so the
            # route still draws. Scoped to driverless orders ON PURPOSE: once a
            # driver is assigned the route is their live leg (driver→pickup, then
            # pickup→destination), which the live-location geometry carries — we
            # must NOT override that approach leg with the full A→B line.
            if not out.get("geometry") and meta.driver_id is None:
                from car_orders.dispatch import planned_route_geometry

                planned = planned_route_geometry(meta)
                if planned:
                    out["geometry"] = planned
        return out or None


class FleetConsumer(AsyncJsonWebsocketConsumer):
    """Dispatcher dashboard feed: on connect sends a full snapshot of all active
    orders, then forwards every per-order position/stage update (tagged with
    order_id) as it happens."""

    async def connect(self):
        await self.accept()
        await _join_group(self, FLEET_GROUP)
        await self.send_json({"type": "snapshot", "orders": await self._snapshot()})

    async def disconnect(self, code):
        await _leave_group(self, FLEET_GROUP)

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
        await self.accept()
        await _join_group(self, user_group(self.uid))

    async def disconnect(self, code):
        await _leave_group(self, user_group(self.uid))

    async def notify_event(self, event):
        await self.send_json(event["data"])


class FallbackConsumer(AsyncJsonWebsocketConsumer):
    """Cleanly close any WS path we don't serve. The host app opens its own sockets
    (e.g. /ws/board/, /ws/bus/) that live on the demo backend and are NOT proxied
    here — without this they raise «No route found» with a noisy traceback on every
    (re)connect. We just close so the log stays clean."""

    async def connect(self):
        # Accept first so we can close with an application code: a pre-accept
        # ``close()`` rejects the handshake as a bare 1006, which a client can't tell
        # apart from a transport drop and will reconnect-loop on. 4404 = «this WS path
        # isn't served here» — a clear signal to stop retrying.
        await self.accept()
        await self.close(code=4404)
