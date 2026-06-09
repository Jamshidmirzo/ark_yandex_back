"""Auto-drive the live location of EVERY active order. Start once and leave it
running — it picks up new orders automatically, so you never run a per-order
``simulate_location`` again.

An order is "active" when our overlay (OrderMeta) has a driver assigned, the
trip isn't completed, and it has A→B coordinates. Each active order is advanced
one step along its route every ``--interval`` seconds; new orders are discovered
every ``--poll`` seconds.
"""

import time

import requests
from django.core.management.base import BaseCommand

from car_orders import services
from car_orders.management.commands.simulate_driver import _resample
from car_orders.models import OrderMeta


class Command(BaseCommand):
    help = "Continuously auto-drive every active order's live location."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=1.5, help="Seconds between steps.")
        parser.add_argument("--steps", type=int, default=90, help="Route resolution (points).")
        parser.add_argument("--poll", type=float, default=3.0, help="Re-scan for orders every N s.")
        parser.add_argument("--base", default="http://127.0.0.1:8000", help="Running server URL.")

    def handle(self, *args, **opts):
        base = opts["base"].rstrip("/")
        interval = opts["interval"]
        steps = max(2, opts["steps"])
        poll = opts["poll"]

        # Only drive orders that are genuinely moving (driver en route / in trip).
        # Stopped stages (assigned / at_client / waiting / at_destination) and dead
        # ones (completed / cancelled) stay put.
        moving = (OrderMeta.TripState.TO_CLIENT, OrderMeta.TripState.IN_TRIP)

        routes: dict[int, list] = {}
        progress: dict[int, int] = {}
        route_coords: dict[int, tuple] = {}  # detect A→B coord changes → re-route
        active: list = []
        last_scan = -1e9

        self.stdout.write(self.style.SUCCESS("Auto-simulator running — Ctrl-C to stop."))

        def post(oid, body):
            try:
                requests.post(
                    f"{base}/api/v1/car-orders/{oid}/live-location/", json=body, timeout=8
                )
            except requests.RequestException:
                pass

        def forget(oid):
            routes.pop(oid, None)
            progress.pop(oid, None)
            route_coords.pop(oid, None)

        while True:
            now = time.monotonic()
            if now - last_scan >= poll:
                last_scan = now
                active = list(
                    OrderMeta.objects.filter(
                        driver_id__isnull=False,
                        origin_lat__isnull=False,
                        origin_lng__isnull=False,
                        address_lat__isnull=False,
                        address_lng__isnull=False,
                        trip_state__in=moving,
                    )
                )
                active_ids = {m.order_id for m in active}
                for oid in list(routes):
                    if oid not in active_ids:
                        forget(oid)
                for m in active:
                    oid = m.order_id
                    key = (m.origin_lat, m.origin_lng, m.address_lat, m.address_lng)
                    if oid in routes and route_coords.get(oid) == key:
                        continue
                    route = services.estimate_route(*key)
                    routes[oid] = _resample(route["geometry"], steps)
                    progress[oid] = 0
                    route_coords[oid] = key
                    first = route["geometry"][0]
                    post(oid, {"lat": first[1], "lng": first[0], "geometry": route["geometry"]})
                    self.stdout.write(f"  + order {oid}: driving {len(routes[oid])} points")

            for m in active:
                oid = m.order_id
                # Re-read live state so a manual stage change / teardown stops the
                # marker immediately, not after a full poll.
                state = (
                    OrderMeta.objects.filter(order_id=oid)
                    .values_list("trip_state", flat=True)
                    .first()
                )
                if state not in moving:
                    forget(oid)
                    continue
                path = routes.get(oid)
                idx = progress.get(oid, 0)
                if not path or idx >= len(path):
                    continue  # not ready, or already arrived (stays put)
                lng, lat = path[idx]
                post(oid, {"lat": lat, "lng": lng})
                progress[oid] = idx + 1

            time.sleep(interval)
