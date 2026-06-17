"""Fleet-wide live snapshot for the dispatcher dashboard.

One call returns every active overlay order (driver assigned, not terminal)
joined with its latest live position + the computed risk flags — what the
«Диспетчерская» map/board renders, and what the fleet WebSocket sends on connect.
"""

from car_orders import scheduling
from car_orders.models import OrderLiveLocation, OrderMeta
from car_orders.serializers import OrderMetaSerializer

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
        geom = services.estimate_route(o_lat, o_lng, d_lat, d_lng).get("geometry")
    except Exception:
        geom = None
    return downsample(geom) if geom else None


def fleet_live_orders():
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
    out = []
    for m in metas:
        # coords, window, trip_state, at_risk, is_late, car_label, driver_id …
        data = OrderMetaSerializer(m, context=ctx).data
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
