"""Move a driver along an order's route to test live tracking without a phone.

Interpolates the driving route (origin → destination of the order, via the same
OSRM/haversine engine the ``/estimate`` endpoint uses) into ``--steps`` points
and writes them to the driver's active shift one ``--interval`` at a time — so
the dispatcher map, which polls ``driver_location``, shows the car moving.

Examples
--------
    python manage.py simulate_driver --order 12
    python manage.py simulate_driver --order 12 --steps 60 --interval 1
    python manage.py simulate_driver --driver 4 --from 41.31,69.24 --to 41.35,69.29
"""

import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from car_orders import services
from car_orders.models import CarOrder, DriverShift


def _parse_point(text):
    try:
        lat, lng = (float(x) for x in text.split(","))
        return lat, lng
    except (ValueError, AttributeError):
        raise CommandError(f"Bad coordinate {text!r}; expected 'lat,lng'.") from None


def _resample(points, n):
    """Resample a polyline of ``[lng, lat]`` points into ``n`` points spaced
    evenly by cumulative (euclidean) length."""
    if len(points) == 1:
        return [points[0]] * n
    # cumulative length along the polyline
    cum = [0.0]
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        cum.append(cum[-1] + ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5)
    total = cum[-1] or 1.0
    out = []
    seg = 0
    for i in range(n):
        target = total * i / (n - 1)
        while seg < len(cum) - 2 and cum[seg + 1] < target:
            seg += 1
        span = cum[seg + 1] - cum[seg] or 1.0
        t = (target - cum[seg]) / span
        x = points[seg][0] + (points[seg + 1][0] - points[seg][0]) * t
        y = points[seg][1] + (points[seg + 1][1] - points[seg][1]) * t
        out.append([x, y])
    return out


class Command(BaseCommand):
    help = "Simulate a driver moving along an order's route (for live-tracking tests)."

    def add_arguments(self, parser):
        parser.add_argument("--order", type=int, help="Order id to drive (uses its A→B coords).")
        parser.add_argument("--driver", type=int, help="Driver user id (with --from/--to).")
        parser.add_argument("--from", dest="origin", help="Origin 'lat,lng' (overrides order).")
        parser.add_argument("--to", dest="dest", help="Destination 'lat,lng' (overrides order).")
        parser.add_argument("--steps", type=int, default=40, help="Number of position updates.")
        parser.add_argument("--interval", type=float, default=2.0, help="Seconds between updates.")

    def handle(self, *args, **opts):
        order = None
        driver_id = opts["driver"]
        origin = _parse_point(opts["origin"]) if opts["origin"] else None
        dest = _parse_point(opts["dest"]) if opts["dest"] else None

        if opts["order"]:
            order = CarOrder.objects.filter(pk=opts["order"]).select_related("driver").first()
            if order is None:
                raise CommandError(f"Order {opts['order']} not found.")
            driver_id = driver_id or order.driver_id
            if origin is None and order.origin_lat is not None and order.origin_lng is not None:
                origin = (order.origin_lat, order.origin_lng)
            if dest is None and order.address_lat is not None and order.address_lng is not None:
                dest = (order.address_lat, order.address_lng)

        if driver_id is None:
            raise CommandError("Provide --driver or an --order that has an assigned driver.")

        shift = DriverShift.objects.filter(driver_id=driver_id, ended_at__isnull=True).first()
        if shift is None:
            raise CommandError(
                f"Driver {driver_id} has no active shift — put them on shift first "
                "(PATCH /drivers/me/shift/)."
            )

        if origin is None:
            if shift.lat is None or shift.lng is None:
                raise CommandError(
                    "No origin: set order.origin_* / pass --from, or seed a shift location."
                )
            origin = (shift.lat, shift.lng)
        if dest is None:
            raise CommandError("No destination: set order.address_lat/lng or pass --to 'lat,lng'.")

        route = services.estimate_route(origin[0], origin[1], dest[0], dest[1])
        path = _resample(route["geometry"], max(2, opts["steps"]))
        self.stdout.write(
            self.style.SUCCESS(
                f"Driving driver {driver_id} along {len(path)} points "
                f"({route['source']}, {round(route['distance_m'])} m); "
                f"Ctrl-C to stop."
            )
        )

        for idx, (lng, lat) in enumerate(path, start=1):
            shift.lat = lat
            shift.lng = lng
            shift.last_seen = timezone.now()
            shift.save(update_fields=["lat", "lng", "last_seen", "updated_at"])
            services.publish_driver_location(shift)
            self.stdout.write(f"  {idx}/{len(path)}  {lat:.5f}, {lng:.5f}")
            if idx < len(path):
                time.sleep(opts["interval"])

        self.stdout.write(self.style.SUCCESS("Arrived at destination."))
