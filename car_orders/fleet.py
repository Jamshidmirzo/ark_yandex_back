"""Fleet-wide live snapshot for the dispatcher dashboard.

One call returns every active overlay order (driver assigned, not terminal)
joined with its latest live position + the computed risk flags — what the
«Диспетчерская» map/board renders, and what the fleet WebSocket sends on connect.
"""

from car_orders import scheduling
from car_orders.models import OrderLiveLocation, OrderMeta
from car_orders.serializers import OrderMetaSerializer
from car_orders.services.status import effective_status, status_map_for

TERMINAL = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)


def _planned_geometry(meta):
    """The pickup → destination route for an order with no live position YET — so the
    dispatcher sees where it should go even before a driver is assigned or departs
    (a driverless awaiting order has no OrderLiveLocation, hence no live geometry).

    The server owns the route here too, mirroring ``dispatch.push_order_route``.
    Returns a downsampled GeoJSON ``[lng, lat]`` polyline, or None when the order
    has no pickup/destination coords or the leg is implausibly long."""
    from car_orders import services
    from car_orders.geometry import MAX_LEG_KM, downsample, haversine_km

    o_lat, o_lng = meta.origin_lat, meta.origin_lng
    d_lat, d_lng = meta.address_lat, meta.address_lng
    if None in (o_lat, o_lng, d_lat, d_lng):
        return None
    if haversine_km(o_lat, o_lng, d_lat, d_lng) > MAX_LEG_KM:
        return None
    try:
        result = services.estimate_route(o_lat, o_lng, d_lat, d_lng)
    except Exception:
        result = None
    # Only draw a real road route on the dispatcher map; the straight-line haversine
    # fallback (OSRM down) cuts A→B through buildings, so show pins only until OSRM
    # answers — mirrors dispatch.planned_route_geometry.
    if not result or result.get("source") != "osrm":
        return None
    geom = result.get("geometry")
    return downsample(geom) if geom else None


def fleet_live_orders(request=None):
    # Every non-terminal overlay order, INCLUDING ones still awaiting a driver
    # (driver_id is None) — the dispatcher needs to see those to assign them.
    metas = list(OrderMeta.objects.exclude(trip_state__in=TERMINAL))
    # Index started trips per driver ONCE, so each order's at_risk is computed in
    # memory instead of a query per order (no N+1 over the fleet).
    active_by_driver: dict = {}
    for m in metas:
        if m.trip_state in scheduling.STARTED_STATES:
            active_by_driver.setdefault(m.driver_id, []).append(m)
    ctx = {"active_by_driver": active_by_driver}
    locs = {
        loc.order_id: loc
        for loc in OrderLiveLocation.objects.filter(order_id__in=[m.order_id for m in metas])
    }
    # The reconciled status, batched once for the whole snapshot — so the dispatcher
    # reads the SAME effective_status the order list/detail/mobile already show
    # (otherwise the board re-derived status from trip_state alone and drifted).
    # Orders live upstream (no local CarOrder mirror), so backfill their demo status
    # from the upstream bodies when we have the caller's request — the request-less WS
    # refresh keeps the local-only behaviour (it streams position/trip_state, and the
    # 6 s HTTP refetch carries the reconciled status).
    order_ids = [m.order_id for m in metas]
    status_map = status_map_for(order_ids)
    if request is not None:
        from car_orders.views import _fill_demo_statuses

        _fill_demo_statuses(status_map, order_ids, request)
    out = []
    for m in metas:
        # coords, window, trip_state, at_risk, is_late, car_label, driver_id …
        data = OrderMetaSerializer(m, context=ctx).data
        raw = status_map.get(m.order_id)
        data["status"] = raw
        data["effective_status"] = effective_status(raw, m)
        loc = locs.get(m.order_id)
        data["lat"] = loc.lat if loc else None
        data["lng"] = loc.lng if loc else None
        data["last_seen"] = loc.last_seen.isoformat() if loc else None
        from car_orders.geometry import trim_geometry

        if loc and loc.geometry and loc.lat is not None:
            # Live, moving leg: trim to the part AHEAD of the car (pinned to it) so the
            # line starts at the vehicle, not the leg's origin — and stays under the WS
            # frame limit.
            data["geometry"] = trim_geometry(loc.geometry, loc.lat, loc.lng)
        else:
            # No live position yet (awaiting / not departed): show the planned
            # pickup → destination route so the dispatcher sees where it should go.
            data["geometry"] = _planned_geometry(m)
        out.append(data)
    return out
