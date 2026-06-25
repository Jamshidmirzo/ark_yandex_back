"""Live driver position: the per-order live-location endpoint, the driver GPS uplink
that fans out over WebSocket, the dispatcher fleet snapshot and the latest-position
roster. ``_apply_driver_location`` is shared with the WS consumer and tests, so it's
re-exported."""

from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from car_orders import dispatch, geometry
from car_orders.models import DriverPosition, OrderLiveLocation, OrderMeta
from car_orders.permissions import (
    OverlayDispatcher,
    OverlayDriverOrDispatcher,
    acting_driver_id,
)
from car_orders.serializers import LocationSerializer
from car_orders.ws import broadcast_location
from config.auth import DemoTokenAuthentication

from .base import _forbidden, _log_tracking, _src

__all__ = (
    "FleetLiveView",
    "LiveLocationView",
    "DriverLocationView",
    "DriverPositionsView",
    "_apply_driver_location",
)


class FleetLiveView(APIView):
    """Dispatcher dashboard snapshot — every active order with its live position +
    risk flags, for «Диспетчерская». Live updates come over the fleet WebSocket
    (/ws/car-orders/fleet/)."""

    authentication_classes = [DemoTokenAuthentication]
    # Dispatcher-only: this is the whole «Диспетчерская» board (every active order +
    # live position + risk flags). Only dispatcher screens (web FleetLivePage, mobile
    # features/dispatcher) call it, so gating it on car_order:approve closes the leak
    # of the full fleet to any authenticated customer/driver token.
    permission_classes = [OverlayDispatcher]

    def get(self, request):
        from car_orders.fleet import fleet_live_orders

        return Response({"orders": fleet_live_orders(request)})


class LiveLocationView(APIView):
    """Live driver position for an order, served locally (gateway/hybrid setup).
    GET returns the latest position or null; POST upserts {lat, lng}, keyed by order
    id. Mounted at /api/v1/car-orders/<id>/live-location/ BEFORE the gateway
    catch-all.

    AUDIT C3: the POST stays open in dev (auth off) so the local auto-simulator can
    push without a demo JWT, but when ``REQUIRE_OVERLAY_AUTH`` is enforced it requires
    the order's own driver (or a dispatcher) — otherwise any anonymous caller could
    move/forge another order's marker (and defeat the arrival geofence)."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [AllowAny]

    def get(self, request, pk):
        loc = OrderLiveLocation.objects.filter(order_id=pk).first()
        if not loc:
            return Response(None)
        return Response(
            {
                "lat": loc.lat,
                "lng": loc.lng,
                "last_seen": loc.last_seen,
                "geometry": loc.geometry,
            }
        )

    def post(self, request, pk):
        from django.conf import settings

        # AUDIT C3: when enforced, only the assigned driver (or a dispatcher) may write
        # an order's live position. Open in dev so the simulator keeps working.
        if getattr(settings, "REQUIRE_OVERLAY_AUTH", False):
            meta = OrderMeta.objects.filter(order_id=pk).first()
            actor = acting_driver_id(request)
            is_owner = (
                actor is not None
                and meta is not None
                and meta.driver_id is not None
                and str(meta.driver_id) == str(actor)
            )
            if not (is_owner or OverlayDispatcher().has_permission(request, self)):
                return _forbidden(_("You can only update your own order's live location."))
        serializer = LocationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        defaults = {
            "lat": serializer.validated_data["lat"],
            "lng": serializer.validated_data["lng"],
            "last_seen": timezone.now(),
        }
        geometry_payload = request.data.get("geometry")
        if geometry_payload is not None:
            defaults["geometry"] = geometry_payload
        loc, _created = OrderLiveLocation.objects.update_or_create(order_id=pk, defaults=defaults)

        # Push the new position to connected trackers + the fleet dashboard.
        data = {"lat": loc.lat, "lng": loc.lng, "last_seen": loc.last_seen.isoformat()}
        if geometry_payload is not None:  # carry the route on the first push
            data["geometry"] = geometry_payload
        broadcast_location(pk, data)
        _log_tracking(f"🛰 LIVE [{_src(request)}] #{pk} ({loc.lat:.5f},{loc.lng:.5f})")
        return Response({"lat": loc.lat, "lng": loc.lng, "last_seen": loc.last_seen})


def _apply_driver_location(driver_id, lat, lng, src="", heading=None):
    """Store the driver's position and attach it to their ACTIVE (non-terminal)
    order — NOT only the moving stages. With «1 водитель = 1 активный заказ» that's
    exactly their current order, so live phone GPS drives the map in every stage
    (assigned / en route / parked). Shared by the single + batch location endpoints.
    Returns the order ids whose live position was updated.

    ``heading`` (deg) is the device-reported travel direction, when the app sends it."""
    now = timezone.now()
    # Never attach to driver_id=None — a None filter matches EVERY driverless order
    # and smears one phone's GPS across all of them. The caller must identify the
    # driver (token or body driver_id).
    if driver_id is None:
        _log_tracking(f"📍 GPS [{src}] БЕЗ driver_id — пропущено (телефон не опознан)")
        return []
    # Travel direction for the OSRM start-snap (stops the route flipping to the
    # oncoming carriageway). Prefer the bearing DERIVED from the previous fix → this
    # one — that's the driver's true motion and is unambiguous — and fall back to the
    # device heading only when we can't (no prior fix / move too small to be real).
    prev_pos = DriverPosition.objects.filter(driver_id=driver_id).first()
    travel_bearing = None
    if prev_pos is not None and prev_pos.lat is not None:
        if geometry.haversine_km(prev_pos.lat, prev_pos.lng, lat, lng) * 1000 >= geometry.MIN_MOVE_M:
            travel_bearing = geometry.bearing_deg(prev_pos.lat, prev_pos.lng, lat, lng)
    if travel_bearing is None and heading is not None:
        travel_bearing = heading
    DriverPosition.objects.update_or_create(
        driver_id=driver_id,
        defaults={"lat": lat, "lng": lng, "heading": heading, "last_seen": now},
    )
    metas = list(OrderMeta.objects.active_for_driver(driver_id))
    # Stages where the driver is DRIVING — we keep the route fresh on these.
    moving = (
        OrderMeta.TripState.ASSIGNED,
        OrderMeta.TripState.TO_CLIENT,
        OrderMeta.TripState.IN_TRIP,
    )

    updated = []
    for meta in metas:
        prev = OrderLiveLocation.objects.filter(order_id=meta.order_id).first()
        moved_m = (
            geometry.haversine_km(prev.lat, prev.lng, lat, lng) * 1000
            if (prev and prev.lat is not None)
            else float("inf")
        )
        updated.append(meta.order_id)
        # Parked / GPS jitter: hasn't really moved since the last SHOWN point → keep
        # the marker and line exactly where they are (don't redraw — that was the
        # in-place flicker), just keep the fix fresh. Compare vs the last shown point
        # (not the last frame) so a slow crawl still accumulates and eventually updates.
        if prev is not None and moved_m < geometry.MIN_MOVE_M:
            OrderLiveLocation.objects.filter(order_id=meta.order_id).update(last_seen=now)
            # Heartbeat: the marker/line stay put, but watchers (customer detail +
            # dispatcher fleet) gate «Связь потеряна» on last_seen. A parked driver
            # who keeps streaming the same fix would otherwise look offline after 30s,
            # so push a last_seen-only frame — the client merge keeps prev lat/lng.
            broadcast_location(meta.order_id, {"last_seen": now.isoformat()})
            continue
        loc, _ = OrderLiveLocation.objects.update_or_create(
            order_id=meta.order_id, defaults={"lat": lat, "lng": lng, "last_seen": now}
        )
        # Snap the DISPLAYED marker onto the route so the dot rides the line instead of
        # floating 80–100 m beside it on biased GPS — within a heading-gated corridor
        # (SNAP_CORRIDOR_M); a real detour falls back to raw and the deviation re-route
        # below takes over. Display-only: the stored `loc`/`DriverPosition` and the
        # `lat/lng` used for the deviation check stay RAW, so re-routing is unchanged.
        show_lat, show_lng = lat, lng
        if meta.trip_state in moving and loc.geometry:
            show_lat, show_lng = geometry.snap_to_route(lat, lng, loc.geometry, travel_bearing)
        broadcast_location(
            meta.order_id, {"lat": show_lat, "lng": show_lng, "last_seen": now.isoformat()}
        )
        # RE-ROUTE on deviation: recompute the polyline from the LIVE position when
        # there's no route yet OR the driver has strayed >30 m off the current one
        # (turned the «wrong» way) — so it redraws along the road they actually took.
        # 30 m (was 80 m): in dense blocks 80 m is a street over, so the line could run
        # along a parallel/oncoming street before a re-route kicked in.
        if meta.trip_state in moving:
            deviated = (
                True
                if not loc.geometry
                else geometry.min_dist_km_to_polyline(lat, lng, loc.geometry) > 0.03
            )
            if deviated:
                dispatch.push_order_route(meta, driver_pos=(lat, lng), bearing=travel_bearing)
            elif loc.geometry:
                # On-route & actually moved: trim the canonical line to what's ahead
                # and pin its start to the car. Smooth follow without OSRM per frame.
                broadcast_location(
                    meta.order_id,
                    {"geometry": geometry.trim_geometry(loc.geometry, lat, lng)},
                )
    _log_tracking(
        f"📍 GPS [{src}] driver={driver_id} ({lat:.5f},{lng:.5f}) → "
        + (
            ", ".join(f"#{m.order_id} [{m.trip_state}]" for m in metas)
            if metas
            else "нет активного заказа"
        )
    )
    return updated


class DriverLocationView(APIView):
    """The driver app posts its GPS ONCE here; the server attaches it to the
    driver's ACTIVE order and fans it out over WebSocket — so the mobile app
    doesn't need to know which order id to send to (it just streams its position).
    Body: ``{driver_id, lat, lng}`` → ``{updated_orders: [...]}``."""

    authentication_classes = [DemoTokenAuthentication]
    # Posting GPS attaches it to the driver's active order — a driver (or dispatcher)
    # action, mirroring the native ``my_location`` gate (driver:accept_order).
    permission_classes = [OverlayDriverOrDispatcher]

    def post(self, request):
        driver_id = acting_driver_id(request, request.data.get("driver_id"))
        serializer = LocationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = _apply_driver_location(
            driver_id,
            serializer.validated_data["lat"],
            serializer.validated_data["lng"],
            _src(request),
            heading=serializer.validated_data.get("heading"),
        )
        return Response({"updated_orders": updated})


class DriverPositionsView(APIView):
    """Latest position per driver → `{ "671": {lat, lng, last_seen}, ... }`. Powers
    the dispatcher's «nearest free driver» suggestion. Optional `?max_age=600`
    (seconds) drops stale fixes."""

    authentication_classes = [DemoTokenAuthentication]
    # Dispatcher-only: the latest position of EVERY driver powers the dispatcher's
    # «nearest free driver» suggestion (only dispatcher screens call it). Gating on
    # car_order:approve stops any authenticated token from enumerating all drivers'
    # whereabouts.
    permission_classes = [OverlayDispatcher]

    def get(self, request):
        qs = DriverPosition.objects.all()
        try:
            max_age = int(request.query_params.get("max_age", 0))
        except (TypeError, ValueError):
            max_age = 0
        if max_age > 0:
            cutoff = timezone.now() - timedelta(seconds=max_age)
            qs = qs.filter(last_seen__gte=cutoff)
        return Response(
            {
                str(p.driver_id): {
                    "lat": p.lat,
                    "lng": p.lng,
                    "last_seen": p.last_seen.isoformat(),
                }
                for p in qs
            }
        )
