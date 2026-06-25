"""Garbage-collect stale overlay rows (AUDIT M4).

The overlay tables are keyed by bare demo ids with NO foreign key, so nothing
removes their rows when the underlying order/driver disappears. This reaper clears:

  • OrderLiveLocation whose OrderMeta is terminal or gone (a live marker for a
    finished/deleted order — would otherwise leak on the map);
  • terminal OrderMeta older than ``--days`` (default 30);
  • DriverPosition not refreshed in ``--days`` (a driver long gone).

Safe to run from cron. ``--dry-run`` reports counts without deleting.

    python manage.py reap_overlay_orphans --days 30
    python manage.py reap_overlay_orphans --dry-run
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from car_orders.models import DriverPosition, OrderLiveLocation, OrderMeta

_TERMINAL = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)


class Command(BaseCommand):
    help = "Delete stale overlay rows with no live owner (OrderLiveLocation / OrderMeta / DriverPosition)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="Age cutoff for terminal/stale rows.")
        parser.add_argument("--dry-run", action="store_true", help="Report counts, delete nothing.")

    def handle(self, *args, **opts):
        cutoff = timezone.now() - timedelta(days=opts["days"])
        dry = opts["dry_run"]

        # Live markers whose order is terminal or no longer has an OrderMeta.
        live_ids = set(OrderLiveLocation.objects.values_list("order_id", flat=True))
        alive = set(
            OrderMeta.objects.filter(order_id__in=live_ids)
            .not_terminal()
            .values_list("order_id", flat=True)
        )
        dead_live = OrderLiveLocation.objects.filter(order_id__in=(live_ids - alive))

        terminal_meta = OrderMeta.objects.filter(trip_state__in=_TERMINAL, updated_at__lt=cutoff)
        stale_pos = DriverPosition.objects.filter(last_seen__lt=cutoff)

        counts = {
            "OrderLiveLocation (terminal/orphan)": dead_live.count(),
            f"OrderMeta (terminal > {opts['days']}d)": terminal_meta.count(),
            f"DriverPosition (stale > {opts['days']}d)": stale_pos.count(),
        }
        for label, n in counts.items():
            self.stdout.write(f"{'WOULD delete' if dry else 'Deleting'} {n}: {label}")

        if not dry:
            dead_live.delete()
            terminal_meta.delete()
            stale_pos.delete()
            self.stdout.write(self.style.SUCCESS("Reap complete."))
