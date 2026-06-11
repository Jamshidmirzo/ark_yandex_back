"""Remind a driver it's time to head to an upcoming order («пора выезжать»).

When the planned pickup of one of the driver's accepted-but-not-started orders
comes within ``--lead-min`` minutes, push them a nudge so they leave in time —
important when they took a 2nd «gap» order and the first one is now approaching.
Fires once per order. Run with ``--loop`` to keep checking.

    python manage.py remind_departures --loop
"""

import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from car_orders.models import OrderMeta
from car_orders.ws import notify_user


class Command(BaseCommand):
    help = "Remind drivers it's time to head to an upcoming order."

    def add_arguments(self, parser):
        parser.add_argument(
            "--lead-min", type=int, default=20, help="Remind this many minutes before the pickup."
        )
        parser.add_argument("--loop", action="store_true", help="Keep checking every 30s.")

    def handle(self, *args, **opts):
        lead = opts["lead_min"]
        while True:
            n = self._pass(lead)
            if n:
                self.stdout.write(f"reminded {n} driver(s) to depart")
            if not opts["loop"]:
                break
            time.sleep(30)

    def _pass(self, lead):
        now = timezone.now()
        soon = now + timedelta(minutes=lead)
        due = OrderMeta.objects.filter(
            driver_id__isnull=False,
            trip_state=OrderMeta.TripState.ASSIGNED,
            departure_reminded=False,
            planned_datetime__gt=now,
            planned_datetime__lte=soon,
        )
        count = 0
        for m in due:
            mins = max(1, int((m.planned_datetime - now).total_seconds() // 60))
            notify_user(
                m.driver_id,
                {
                    "order_id": m.order_id,
                    "trip_state": "assigned",
                    "message": (
                        f"Скоро подача заказа №{m.order_id} — через {mins} мин. "
                        "Пора выезжать, чтобы не опоздать."
                    ),
                    "kind": "departure_reminder",
                },
            )
            m.departure_reminded = True
            m.save(update_fields=["departure_reminded"])
            count += 1
        return count
