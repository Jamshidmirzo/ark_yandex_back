"""Backend auto-dispatch worker — runs the «nearest free driver» assignment on
the server, so it works even when no dispatcher tab is open (the logic used to
live only in the browser).

Start once and leave it running (like `auto_simulate` / `order_watchdog`):

    python manage.py auto_dispatch

Each pass (every `--poll` seconds) it assigns every DUE, dispatchable order to the
best IDEAL driver (on shift · right car type · free · ≤1 active). Tunables come
from settings (AUTO_DISPATCH_LEAD_MIN / _STALE_SEC / _POS_MAX_AGE / _ENABLED).
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from car_orders import dispatch


class Command(BaseCommand):
    help = "Continuously auto-assign awaiting orders to the nearest free driver."

    def add_arguments(self, parser):
        parser.add_argument("--poll", type=float, default=15.0, help="Seconds between passes.")
        parser.add_argument("--once", action="store_true", help="Run a single pass and exit.")

    def handle(self, *args, **opts):
        lead = getattr(settings, "AUTO_DISPATCH_LEAD_MIN", 45)
        stale = getattr(settings, "AUTO_DISPATCH_STALE_SEC", 180)
        pos_age = getattr(settings, "AUTO_DISPATCH_POS_MAX_AGE", 180)
        poll = opts["poll"]
        first_seen: dict[int, object] = {}

        self.stdout.write(
            self.style.SUCCESS(
                f"Auto-dispatch worker running (lead={lead}m, stale={stale}s, poll={poll}s). "
                "Ctrl-C to stop."
            )
        )

        while True:
            if dispatch.auto_enabled():
                try:
                    assigned = dispatch.run_once(
                        first_seen,
                        now=timezone.now(),
                        lead_min=lead,
                        stale_sec=stale,
                        pos_max_age=pos_age,
                    )
                    for oid, did in assigned:
                        self.stdout.write(f"  🤖 assigned order #{oid} → driver {did}")
                except Exception as exc:  # never let one bad pass kill the worker
                    self.stderr.write(self.style.WARNING(f"pass error: {exc}"))
            if opts["once"]:
                break
            time.sleep(poll)
