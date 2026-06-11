"""Seed/refresh per-driver GPS positions — so the dispatcher's «nearest free
driver» suggestion + auto-dispatch can be tested WITHOUT a real phone.

The driver app would normally POST `drivers/me/location/` continuously (even when
free); this command fakes that by writing `DriverPosition` rows directly. With
`--loop` it drifts each driver a little every tick (idle roaming) and keeps
`last_seen` fresh, so they stay «online».

    # one-shot: scatter the drivers known from orders around Tashkent
    python manage.py seed_driver_positions

    # explicit drivers + continuous drift (recommended while testing)
    python manage.py seed_driver_positions --drivers 671,13,42 --loop

Note: a driver only shows up as a *candidate* if demo lists them **on shift with a
car of the order's type** (the «Водители» page shows who's on shift). This command
only gives them a position; put them on shift in the app/demo first.
"""

import math
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from car_orders.models import DriverPosition, OrderMeta

TASHKENT = (41.311081, 69.240562)


def _spread_point(center, idx, spread):
    """A deterministic point around `center`, fanned out by index (no RNG so the
    layout is stable across restarts)."""
    angle = (idx * 2.399963)  # golden angle → even fan
    r = spread * (0.35 + 0.65 * ((idx % 5) / 4.0))
    return (center[0] + r * math.cos(angle), center[1] + r * math.sin(angle))


class Command(BaseCommand):
    help = "Seed/refresh DriverPosition rows so the nearest-driver suggestion is testable."

    def add_arguments(self, parser):
        parser.add_argument(
            "--drivers",
            default="",
            help="Comma-separated driver ids. Default: the driver ids seen in OrderMeta.",
        )
        parser.add_argument("--center", default="", help="lat,lng (default Tashkent).")
        parser.add_argument(
            "--spread", type=float, default=0.06, help="Fan-out radius in degrees (~6 km)."
        )
        parser.add_argument("--loop", action="store_true", help="Keep drifting positions.")
        parser.add_argument("--interval", type=float, default=3.0, help="Seconds between drifts.")

    def handle(self, *args, **opts):
        if opts["center"]:
            lat, lng = (float(x) for x in opts["center"].split(","))
            center = (lat, lng)
        else:
            center = TASHKENT

        if opts["drivers"]:
            driver_ids = [int(x) for x in opts["drivers"].split(",") if x.strip()]
        else:
            driver_ids = sorted(
                {m.driver_id for m in OrderMeta.objects.exclude(driver_id=None)}
            )
        if not driver_ids:
            self.stderr.write(
                "No driver ids — pass --drivers 671,13,… (see them on the «Водители» page)."
            )
            return

        # initial scatter
        pos = {d: _spread_point(center, i, opts["spread"]) for i, d in enumerate(driver_ids)}
        self._write(pos)
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(pos)} driver positions around {center}."))

        if not opts["loop"]:
            return

        # drift: each driver wanders a little every tick (simulates idle movement)
        step = opts["spread"] * 0.02
        tick = 0
        try:
            while True:
                time.sleep(opts["interval"])
                tick += 1
                for i, d in enumerate(driver_ids):
                    la, ln = pos[d]
                    ang = (tick + i) * 0.7
                    pos[d] = (la + step * math.cos(ang), ln + step * math.sin(ang))
                self._write(pos)
                self.stdout.write(f"  drift #{tick}: {len(pos)} drivers")
        except KeyboardInterrupt:
            self.stdout.write("stopped.")

    def _write(self, pos):
        now = timezone.now()
        for d, (la, ln) in pos.items():
            DriverPosition.objects.update_or_create(
                driver_id=d, defaults={"lat": la, "lng": ln, "last_seen": now}
            )
