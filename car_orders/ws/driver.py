"""Uplink (driver phone → server): the driver app's own GPS socket.

One bidirectional socket: the phone STREAMS ``{lat,lng}``; on every frame the
server stores the position, attaches it to the driver's active order, re-routes
the «approach» leg, and REPLIES with the live marker position + the current leg
polyline (only when it changed). So the app stays dumb: send raw GPS, get back
where to put the marker and which polyline to draw.

Connect: ``ws://<host>/ws/drivers/me/location/?token=<demo jwt>`` (validated) or
``?driver_id=<id>`` (dev fallback); identity may also arrive in the first message.
"""

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer


class DriverLocationConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self._last_geom_pos = None  # where we last sent the line (movement dead-zone)
        self._last_trip_state = None
        self.driver_id = await self._from_payload(self._query())
        await self.accept()
        await self.send_json({"ok": True, "driver_id": self.driver_id})

    def _moved(self, lat, lng):
        """True if the car moved past the dead-zone since we last sent the line — so
        standing still (GPS jitter) doesn't keep resending/redrawing the same line."""
        from car_orders.geometry import MIN_MOVE_M, haversine_km

        if self._last_geom_pos is None:
            return True
        try:
            plat, plng = self._last_geom_pos
            return haversine_km(float(lat), float(lng), plat, plng) * 1000 >= MIN_MOVE_M
        except (TypeError, ValueError):
            return True

    async def receive_json(self, content, **kwargs):
        if self.driver_id is None:  # identity may arrive in the first message
            self.driver_id = await self._from_payload(content)
        lat, lng = content.get("lat"), content.get("lng")
        if self.driver_id is None or lat is None or lng is None:
            await self.send_json({"error": "need identity (?token / ?driver_id) and lat/lng"})
            return
        state = await self._apply(self.driver_id, lat, lng)
        # Always return the marker position + order/stage; include the polyline ONLY
        # when it changed (the approach re-route / a leg change), so we don't resend
        # the whole route every frame. The app moves the marker to lat/lng along the
        # last polyline, and redraws when a new `geometry` arrives.
        reply = {
            "order_id": state.get("order_id"),
            "trip_state": state.get("trip_state"),
            "lat": state.get("lat"),
            "lng": state.get("lng"),
        }
        # Send the (trimmed) line only when it should actually change: the car moved
        # past the dead-zone, OR the stage changed (new leg). Standing still → keep
        # the current line, don't resend — that was the in-place flicker/redraw on
        # GPS jitter. When moving, it's resent every frame → smooth follow.
        ts = state.get("trip_state")
        geom = state.get("geometry")
        if geom and (ts != self._last_trip_state or self._moved(lat, lng)):
            reply["geometry"] = geom
            self._last_geom_pos = (float(lat), float(lng))
        self._last_trip_state = ts
        await self.send_json(reply)

    def _query(self):
        from urllib.parse import parse_qs

        qs = parse_qs(self.scope.get("query_string", b"").decode())
        return {"driver_id": qs.get("driver_id", [None])[0], "token": qs.get("token", [None])[0]}

    @database_sync_to_async
    def _from_payload(self, content):
        from config.auth import validate_demo_token

        token = content.get("token")
        if token:
            user = validate_demo_token(token)
            if user:
                return user.id
        did = content.get("driver_id")
        try:
            return int(did) if did is not None else None
        except (TypeError, ValueError):
            return None

    @database_sync_to_async
    def _apply(self, driver_id, lat, lng):
        """Run the shared heartbeat logic, then read back the driver's active order
        + its current live position + route polyline for the reply."""
        from car_orders.models import OrderLiveLocation, OrderMeta
        from car_orders.views import _apply_driver_location

        try:
            _apply_driver_location(int(driver_id), float(lat), float(lng), "📡 ws")
        except (TypeError, ValueError):
            return {}
        terminal = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)
        meta = (
            OrderMeta.objects.filter(driver_id=driver_id)
            .exclude(trip_state__in=terminal)
            .order_by("order_id")
            .first()
        )
        if meta is None:
            return {"order_id": None, "lat": float(lat), "lng": float(lng)}
        loc = OrderLiveLocation.objects.filter(order_id=meta.order_id).first()
        # Trim the canonical route to the part ahead of the car (pinned to it), so the
        # driver app gets a smooth, shrinking line on every frame — not the full leg.
        from car_orders.geometry import trim_geometry

        geom = trim_geometry(loc.geometry, float(lat), float(lng)) if (loc and loc.geometry) else None
        return {
            "order_id": meta.order_id,
            "trip_state": meta.trip_state,
            "lat": loc.lat if loc else float(lat),
            "lng": loc.lng if loc else float(lng),
            "geometry": geom,
        }
