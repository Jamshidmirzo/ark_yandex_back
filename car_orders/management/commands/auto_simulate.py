"""Auto-drive the live location of every active order — driver-centric and
phase-aware, so the simulator mirrors what the mobile app will stream.

Start once and leave it running; it picks up new orders automatically. For each
driver it drives the order that is currently *moving*, choosing the right leg by
trip stage:

  - ``to_client``: from the driver's CURRENT position → the **pickup** (origin).
    This is also the "empty" leg **between two orders** — after finishing order 1
    at its destination, the driver heads to order 2's pickup, and you see it.
  - ``in_trip``: from the **pickup** (origin) → the **destination** (address).

Stopped stages (``assigned`` / ``at_client`` / ``waiting`` / ``at_destination``)
and dead ones (``completed`` / ``cancelled``) stay put. The driver's position is
remembered across orders (one car = one position), so the inter-order leg starts
where the previous order left them; on a fresh start it's seeded from the
driver's last stored live-location.
"""

import time

import requests
from django.core.management.base import BaseCommand

from car_orders import services
from car_orders.management.commands.simulate_driver import _resample
from car_orders.models import OrderLiveLocation, OrderMeta

# Stages where the car is actually moving (and we animate it).
MOVING = (OrderMeta.TripState.TO_CLIENT, OrderMeta.TripState.IN_TRIP)
# When a driver somehow has more than one moving order at once (a car is only in
# one place), animate just one — prefer the loaded leg over the approach.
_MOVING_PRIORITY = {OrderMeta.TripState.IN_TRIP: 0, OrderMeta.TripState.TO_CLIENT: 1}


def one_moving_order_per_driver(orders):
    """Collapse moving orders to at most one per driver: the loaded leg (``in_trip``)
    wins over the approach (``to_client``), then the most-recently-updated. Keeps
    ``driver_pos`` (one car = one position) from being clobbered by a second order."""
    best: dict[int, object] = {}

    def rank(m):
        return (_MOVING_PRIORITY.get(m.trip_state, 9), -m.updated_at.timestamp())

    for m in orders:
        cur = best.get(m.driver_id)
        if cur is None or rank(m) < rank(cur):
            best[m.driver_id] = m
    return list(best.values())


def leg_endpoints(state, driver_pos, origin, dest, returning=False, return_pt=None):
    """``(start, end)`` ``[lng, lat]`` for the current moving leg.

    - ``to_client`` → drive from the driver's current position to the pickup
      (``origin``); if the position is unknown, start at the pickup (no approach).
    - ``in_trip`` (outbound) → drive the loaded leg pickup → destination.
    - ``in_trip`` while ``returning`` (round trip) → drive the RETURN leg
      destination → return point (falls back to the pickup if none set).
    """
    if state == OrderMeta.TripState.TO_CLIENT:
        return (list(driver_pos) if driver_pos else list(origin)), list(origin)
    if returning:
        return list(dest), list(return_pt or origin)
    return list(origin), list(dest)


class Command(BaseCommand):
    help = "Continuously auto-drive every active order's live location (phase-aware)."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=1.5, help="Seconds between steps.")
        parser.add_argument("--steps", type=int, default=90, help="Route resolution (points).")
        parser.add_argument("--poll", type=float, default=3.0, help="Re-scan for orders every N s.")
        parser.add_argument("--base", default="http://127.0.0.1:8000", help="Running server URL.")
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Demo: re-drive a leg from the start when it finishes (continuous motion).",
        )

    def handle(self, *args, **opts):
        base = opts["base"].rstrip("/")
        interval = opts["interval"]
        steps = max(2, opts["steps"])
        poll = opts["poll"]
        loop = opts["loop"]

        seg_route: dict[int, list] = {}    # oid -> resampled [lng,lat] path of the current leg
        seg_progress: dict[int, int] = {}  # oid -> index along that path
        seg_key: dict[int, tuple] = {}     # oid -> (state, end_lng, end_lat) leg identity
        driver_pos: dict[int, list] = {}   # driver_id -> current [lng, lat] (one car)
        active: list = []
        last_scan = -1e9

        self.stdout.write(
            self.style.SUCCESS("Auto-simulator (phase-aware) running — Ctrl-C to stop.")
        )

        def post(oid, body):
            try:
                requests.post(
                    f"{base}/api/v1/car-orders/{oid}/live-location/", json=body, timeout=8
                )
            except requests.RequestException:
                pass

        def forget(oid):
            seg_route.pop(oid, None)
            seg_progress.pop(oid, None)
            seg_key.pop(oid, None)

        def seed_pos(driver_id):
            """Driver's last known position so the inter-order leg survives a
            simulator restart: the most recent live-location among their orders."""
            oids = list(
                OrderMeta.objects.filter(driver_id=driver_id).values_list("order_id", flat=True)
            )
            loc = (
                OrderLiveLocation.objects.filter(order_id__in=oids).order_by("-last_seen").first()
            )
            return [loc.lng, loc.lat] if loc else None

        while True:
            now = time.monotonic()
            if now - last_scan >= poll:
                last_scan = now
                active = one_moving_order_per_driver(
                    OrderMeta.objects.filter(
                        driver_id__isnull=False,
                        origin_lat__isnull=False,
                        origin_lng__isnull=False,
                        address_lat__isnull=False,
                        address_lng__isnull=False,
                        trip_state__in=MOVING,
                    )
                )
                active_ids = {m.order_id for m in active}
                for oid in list(seg_route):
                    if oid not in active_ids:
                        forget(oid)

            for m in active:
                oid = m.order_id
                drv = m.driver_id
                # Re-read live state AND coords together, so a manual stage change /
                # teardown stops the marker immediately and a moved pickup/destination
                # re-routes — instead of driving the cached (stale) coordinates.
                row = (
                    OrderMeta.objects.filter(order_id=oid)
                    .values_list(
                        "trip_state",
                        "origin_lng",
                        "origin_lat",
                        "address_lng",
                        "address_lat",
                        "returning",
                        "return_lng",
                        "return_lat",
                    )
                    .first()
                )
                if row is None:
                    forget(oid)
                    continue
                state, o_lng, o_lat, a_lng, a_lat, returning, r_lng, r_lat = row
                if state not in MOVING or None in (o_lng, o_lat, a_lng, a_lat):
                    forget(oid)
                    continue

                origin = [o_lng, o_lat]
                dest = [a_lng, a_lat]
                return_pt = [r_lng, r_lat] if r_lng is not None and r_lat is not None else None
                start, end = leg_endpoints(
                    state, driver_pos.get(drv) or seed_pos(drv), origin, dest, returning, return_pt
                )

                # Leg identity is the stage + its endpoint (+ the return flag, since
                # outbound and return share the in_trip stage but end differently);
                # it does NOT include the live position, so the route is built once
                # and not re-routed as the marker advances.
                key = (state, returning, round(end[0], 6), round(end[1], 6))
                if seg_key.get(oid) != key:
                    route = services.estimate_route(start[1], start[0], end[1], end[0])
                    geom = route["geometry"]
                    seg_route[oid] = _resample(geom, steps)
                    seg_progress[oid] = 0
                    seg_key[oid] = key
                    first = seg_route[oid][0]
                    driver_pos[drv] = [first[0], first[1]]
                    post(oid, {"lat": first[1], "lng": first[0], "geometry": geom})
                    leg = (
                        "→ подача"
                        if state == OrderMeta.TripState.TO_CLIENT
                        else "← обратно"
                        if returning
                        else "с клиентом"
                    )
                    self.stdout.write(f"  + order {oid} ({leg}): {len(seg_route[oid])} points")
                    continue

                path = seg_route.get(oid)
                if not path:
                    continue
                idx = seg_progress.get(oid, 0)
                if idx >= len(path):
                    if loop:
                        idx = 0  # demo: re-drive the same leg for continuous motion
                    else:
                        # Arrived at the leg end — keep the position FRESH (heartbeat),
                        # like a real phone still sending GPS while stopped. Otherwise
                        # the fix goes stale (>30 s) and the driver's «Прибыли на место»
                        # button stays blocked even though they're right at the point.
                        last = path[-1]
                        post(oid, {"lat": last[1], "lng": last[0]})
                        continue
                lng, lat = path[idx]
                post(oid, {"lat": lat, "lng": lng})
                driver_pos[drv] = [lng, lat]
                seg_progress[oid] = idx + 1

            time.sleep(interval)
