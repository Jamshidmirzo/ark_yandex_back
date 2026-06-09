"""Simulate a driver's live position for an order (gateway/hybrid setup).

POSTs positions to ``/{id}/live-location/`` (so the WebSocket push happens in the
running server process) along a routed A→B path. Works for orders that live in
the demo backend; the tracking map polls/streams ``/{id}/live-location/``.

Examples
--------
    python manage.py simulate_location --order 42
    python manage.py simulate_location --order 42 --from 41.31,69.24 --to 41.35,69.29
    python manage.py simulate_location --order 42 --steps 60 --interval 2
"""

import time

import requests
from django.core.management.base import BaseCommand

from car_orders import services
from car_orders.management.commands.simulate_driver import _parse_point, _resample

DEFAULT_FROM = (41.311, 69.240)
DEFAULT_TO = (41.351, 69.290)


class Command(BaseCommand):
    help = "Simulate live driver coordinates for an order id (POSTs to live-location)."

    def add_arguments(self, parser):
        parser.add_argument("--order", type=int, required=True, help="Order id to drive.")
        parser.add_argument("--from", dest="origin", help="Origin 'lat,lng' (default: Tashkent).")
        parser.add_argument("--to", dest="dest", help="Destination 'lat,lng' (default: Tashkent).")
        parser.add_argument("--steps", type=int, default=40, help="Number of position updates.")
        parser.add_argument("--interval", type=float, default=2.0, help="Seconds between updates.")
        parser.add_argument(
            "--base", default="http://127.0.0.1:8000", help="Running server base URL."
        )

    def handle(self, *args, **opts):
        order_id = opts["order"]
        origin = _parse_point(opts["origin"]) if opts["origin"] else DEFAULT_FROM
        dest = _parse_point(opts["dest"]) if opts["dest"] else DEFAULT_TO
        url = f"{opts['base'].rstrip('/')}/api/v1/car-orders/{order_id}/live-location/"

        route = services.estimate_route(origin[0], origin[1], dest[0], dest[1])
        geometry = route["geometry"]
        path = _resample(geometry, max(2, opts["steps"]))
        self.stdout.write(
            self.style.SUCCESS(
                f"Order {order_id}: driving {len(path)} points "
                f"({route['source']}, {round(route['distance_m'])} m) → {url}; Ctrl-C to stop."
            )
        )

        for idx, (lng, lat) in enumerate(path, start=1):
            body = {"lat": lat, "lng": lng}
            if idx == 1:
                body["geometry"] = geometry  # store the route once
            try:
                requests.post(url, json=body, timeout=10)
            except requests.RequestException as exc:
                self.stderr.write(self.style.ERROR(f"POST failed: {exc}"))
            self.stdout.write(f"  {idx}/{len(path)}  {lat:.5f}, {lng:.5f}")
            if idx < len(path):
                time.sleep(opts["interval"])

        self.stdout.write(self.style.SUCCESS("Arrived at destination."))
