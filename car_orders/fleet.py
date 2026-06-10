"""Fleet-wide live snapshot for the dispatcher dashboard.

One call returns every active overlay order (driver assigned, not terminal)
joined with its latest live position + the computed risk flags — what the
«Диспетчерская» map/board renders, and what the fleet WebSocket sends on connect.
"""

from car_orders import scheduling
from car_orders.models import OrderLiveLocation, OrderMeta
from car_orders.serializers import OrderMetaSerializer

TERMINAL = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)


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
        data["geometry"] = loc.geometry if loc else None  # route polyline for the map
        out.append(data)
    return out
