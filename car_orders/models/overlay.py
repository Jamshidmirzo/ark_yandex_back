from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import TimestampMixin

__all__ = ("OrderMeta",)


class OrderMeta(TimestampMixin):
    """Local feature overlay for an order that lives in the demo backend.

    Stores the things demo doesn't have — route A→B coordinates, planned window
    (start + duration) for the scheduling conflict check, and the richer trip
    state — keyed by the demo order id.
    """

    class TripState(models.TextChoices):
        ASSIGNED = "assigned", _("Assigned")  # claimed into the schedule
        TO_CLIENT = "to_client", _("Driving to client")
        AT_CLIENT = "at_client", _("Arrived, waiting for client")
        IN_TRIP = "in_trip", _("In trip with client")
        AT_DESTINATION = "at_destination", _("Arrived at destination")
        WAITING = "waiting", _("On hold (driver stepped away)")
        COMPLETED = "completed", _("Completed")
        CANCELLED = "cancelled", _("Cancelled / released")

    order_id = models.PositiveIntegerField(unique=True, db_index=True, verbose_name=_("Order id"))
    driver_id = models.PositiveIntegerField(
        null=True, blank=True, db_index=True, verbose_name=_("Driver user id")
    )
    # Who created the order (demo user id) — so status changes can be pushed to
    # the requester, not just the driver. Saved by the frontend at create time.
    author_id = models.PositiveIntegerField(
        null=True, blank=True, db_index=True, verbose_name=_("Author user id")
    )
    # Urgent (e.g. for a live broadcast) — sorted first and flagged for dispatch.
    is_urgent = models.BooleanField(default=False, verbose_name=_("Urgent"))
    # The order's required car type (mirrored from demo at create time) so the
    # backend auto-dispatch worker can match drivers without calling demo.
    car_type_id = models.PositiveIntegerField(
        null=True, blank=True, db_index=True, verbose_name=_("Car type id")
    )
    # True once the order is approved and ready for a driver. The backend
    # auto-dispatch worker only considers dispatchable + driverless orders, so a
    # draft / unapproved order is never auto-assigned.
    dispatchable = models.BooleanField(default=False, db_index=True, verbose_name=_("Ready for dispatch"))
    # Set once we've reminded the driver it's time to head to this order, so the
    # «пора выезжать» nudge fires only once.
    departure_reminded = models.BooleanField(default=False, verbose_name=_("Departure reminded"))
    # For a return/pickup sub-order created together with the main one («отвёз и
    # забери обратно»): the main order's id. Null for a normal order.
    parent_order_id = models.PositiveIntegerField(
        null=True, blank=True, db_index=True, verbose_name=_("Parent order id")
    )
    # Overlay claim: the car the driver took the order with (so a second order
    # can reuse the same car sequentially, which the demo backend forbids).
    car_id = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Car id"))
    car_label = models.CharField(max_length=255, blank=True, verbose_name=_("Car label"))
    # Driver snapshot captured AT CLAIM (like `car_label`) — the claiming driver's
    # own session can read their name/phone, but the order's requester can't reach
    # the HR `/employees/` endpoint, so we snapshot it here and serve it inline with
    # the meta. Lets the customer see «who took my order» + call them, no HR access.
    driver_name = models.CharField(max_length=255, blank=True, verbose_name=_("Driver name"))
    driver_phone = models.CharField(max_length=32, blank=True, verbose_name=_("Driver phone"))
    # True ONLY when the order was claimed via OUR layer (overlay-claim), not the
    # demo claim. Distinguishes "managed by us" from a normal demo claim where we
    # still record driver_id just for the window check.
    overlay_claimed = models.BooleanField(default=False, verbose_name=_("Claimed in our layer"))
    # Demo driver ids the dispatcher has deliberately taken this order OFF of (via
    # «Переназначить»). The auto-dispatch worker must NEVER hand the order back to
    # them — otherwise it instantly re-assigns the nearest free driver, who is the
    # very one just removed, and the order bounces straight back. Append-only per
    # order; a manual assignment from the dispatcher is still allowed (deliberate
    # override). See dispatch.rank_drivers / dispatch.claim.
    excluded_driver_ids = models.JSONField(
        default=list, blank=True, verbose_name=_("Drivers excluded from auto-dispatch")
    )
    origin_lat = models.FloatField(null=True, blank=True)
    origin_lng = models.FloatField(null=True, blank=True)
    address_lat = models.FloatField(null=True, blank=True)
    address_lng = models.FloatField(null=True, blank=True)
    # Human-readable «откуда / куда» snapshotted onto the overlay so EVERY client (and
    # an overlay-only order with no local/demo CarOrder body — e.g. an app-created one)
    # can show the route as text, not just coords. Filled server-side (reverse-geocode)
    # by the dispatch worker; the destination can also carry the demo order's own label.
    origin_address = models.CharField(max_length=500, blank=True, verbose_name=_("Origin address"))
    dest_address = models.CharField(max_length=500, blank=True, verbose_name=_("Destination address"))
    # Demo-only DESCRIPTIVE fields snapshotted onto the overlay (like the driver
    # snapshot above) so an order whose demo body the requester can't read (the
    # detail proxy 404s — see CarOrderViewSet.get_queryset / car_order_proxy) still
    # shows full info via the client's overlay fallback. Filled server-side, lazily,
    # whenever a privileged client fetches the demo bodies (see _snapshot_descriptive).
    project_name = models.CharField(max_length=500, blank=True, verbose_name=_("Project name"))
    note = models.TextField(blank=True, verbose_name=_("Note / purpose"))
    car_type_name = models.CharField(max_length=255, blank=True, verbose_name=_("Car type name"))
    created_by_name = models.CharField(max_length=255, blank=True, verbose_name=_("Created by"))
    # «Туда-обратно» as ONE order: after dropping at the destination the driver
    # waits on site (shoot), then drives a RETURN leg to `return_*` (defaults to
    # the pickup if not set). No return time — the shoot end is unknown, so the
    # driver starts the return manually when it's over.
    has_return = models.BooleanField(default=False, verbose_name=_("Has return leg"))
    return_lat = models.FloatField(null=True, blank=True)
    return_lng = models.FloatField(null=True, blank=True)
    # True once the driver has STARTED the return leg (destination → return point),
    # so the simulator/map drive the second leg and «Завершить» appears only after
    # the return is done.
    returning = models.BooleanField(default=False, verbose_name=_("On the return leg"))
    estimated_duration = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("Estimated duration (minutes)")
    )
    service_time = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("On-site time (minutes)")
    )
    planned_datetime = models.DateTimeField(null=True, blank=True)
    latest_start = models.DateTimeField(null=True, blank=True)
    # «Поиск водителя» timer: stamped when the order enters the dispatch queue
    # (dispatchable + driverless) and reset when it re-enters on requeue/reassign.
    # The search clock runs only while driver_id is None (the serializer reports
    # elapsed only then), so a separate "search ended" field isn't needed.
    search_started_at = models.DateTimeField(null=True, blank=True)
    # «Ожидание клиента на подаче» timer: stamped on the FIRST transition to
    # at_client (driver arrived, waiting for the client) and cleared when the
    # claim is torn down / re-claimed so a re-driven order starts a fresh wait.
    arrived_at = models.DateTimeField(null=True, blank=True)
    trip_state = models.CharField(
        max_length=20, choices=TripState.choices, default=TripState.ASSIGNED
    )

    class Meta:
        verbose_name = _("Order meta")
        verbose_name_plural = _("Order meta")

    def __str__(self):
        return f"OrderMeta order={self.order_id} [{self.trip_state}]"

    @property
    def planned_end(self):
        if self.planned_datetime and self.estimated_duration:
            from datetime import timedelta

            return self.planned_datetime + timedelta(minutes=self.estimated_duration)
        return None
