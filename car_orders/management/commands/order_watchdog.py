"""Watchdog over the overlay schedule.

Reports orders that need attention so a dispatcher (or cron) can act:
  - **at risk** — the driver's projected start blows past ``latest_start`` (can't
    make it, see ``scheduling.meta_needs_reassign``);
  - **late** — accepted but not departed and the planned pickup time has passed;
  - **GPS lost** — an actively-driven order with no fresh live-location.

With ``--release`` it returns the late / at-risk orders that **haven't started**
to the queue (frees the driver's slot for someone who can make it). Started trips
are never yanked mid-way. Run from cron or a loop:

    python manage.py order_watchdog
    python manage.py order_watchdog --release --stale-sec 90
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from car_orders import scheduling
from car_orders.models import OrderLiveLocation, OrderMeta

TERMINAL = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)


class Command(BaseCommand):
    help = "Report (and optionally release) at-risk/late overlay orders + GPS-lost trips."

    def add_arguments(self, parser):
        parser.add_argument(
            "--stale-sec", type=int, default=60, help="Active trip with no GPS this long = lost."
        )
        parser.add_argument(
            "--release",
            action="store_true",
            help="Return late/at-risk NOT-yet-started orders to the queue.",
        )

    def handle(self, *args, **opts):
        now = timezone.now()
        stale_sec = opts["stale_sec"]

        metas = OrderMeta.objects.filter(driver_id__isnull=False).exclude(trip_state__in=TERMINAL)
        at_risk, late, gps_lost = [], [], []
        for m in metas:
            if scheduling.meta_needs_reassign(m, now):
                at_risk.append(m)
            elif (
                m.trip_state == OrderMeta.TripState.ASSIGNED
                and m.planned_datetime
                and now > m.planned_datetime
            ):
                late.append(m)
            if m.trip_state in scheduling.STARTED_STATES:
                loc = OrderLiveLocation.objects.filter(order_id=m.order_id).first()
                age = (now - loc.last_seen).total_seconds() if loc else None
                if loc is None or age > stale_sec:
                    gps_lost.append((m, age))

        self.stdout.write(self.style.WARNING(f"At risk (can't make latest start): {len(at_risk)}"))
        for m in at_risk:
            self.stdout.write(f"  order {m.order_id} drv {m.driver_id} latest={m.latest_start}")
        self.stdout.write(self.style.WARNING(f"Late (pickup passed, not departed): {len(late)}"))
        for m in late:
            self.stdout.write(f"  order {m.order_id} drv {m.driver_id} plan={m.planned_datetime}")
        self.stdout.write(self.style.WARNING(f"GPS lost (active, no fresh fix): {len(gps_lost)}"))
        for m, age in gps_lost:
            ago = f"{round(age)}s ago" if age is not None else "no fix yet"
            self.stdout.write(f"  order {m.order_id} drv {m.driver_id} last fix {ago}")

        if opts["release"]:
            released = 0
            for m in at_risk + late:
                if m.trip_state != OrderMeta.TripState.ASSIGNED:
                    continue  # never yank a driver mid-trip
                m.overlay_claimed = False
                m.driver_id = None
                m.car_id = None
                m.car_label = ""
                m.trip_state = OrderMeta.TripState.CANCELLED
                m.save()
                OrderLiveLocation.objects.filter(order_id=m.order_id).delete()
                released += 1
            self.stdout.write(
                self.style.SUCCESS(f"Released {released} late/at-risk unstarted orders.")
            )
