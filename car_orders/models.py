"""Data model for the «Заявки на машину» (car orders) block.

Mirrors ark-backend's ``apps.car_orders`` models (statuses already use
``rejected``) and adds two product decisions from the approved ТЗ:

* **Р1 — «машина на смене»**: a driver picks ONE car when going on shift
  (:class:`DriverShift`). The awaiting-driver feed is filtered to that car's
  type, and ``claim`` uses the shift car (no per-order car choice).
* **Р3 — live tracking**: the active shift carries the driver's last known
  ``lat``/``lng``/``last_seen``; the order author watches it on a map while
  the trip is ``in_progress``.

See INTEGRATION.md for how this maps back onto ark-backend.
"""

from django.conf import settings
from django.db import models
from django.db.models import BooleanField, Case, Exists, OuterRef, Q, Value, When
from django.utils.translation import gettext_lazy as _

from core.models import TimestampMixin


class CarType(models.Model):
    name = models.CharField(max_length=255, verbose_name=_("Name"))
    picture_url = models.URLField(blank=True, verbose_name=_("Picture URL"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Car type")
        verbose_name_plural = _("Car types")

    def __str__(self):
        return self.name


class CarQuerySet(models.QuerySet):
    def with_availability(self):
        """Annotate ``is_available``: status='active' AND no in-progress order."""
        return self.annotate(
            is_available=Case(
                When(
                    status=Car.Status.ACTIVE,
                    then=~Exists(
                        CarOrder.objects.filter(
                            car_id=OuterRef("pk"),
                            status=CarOrder.Status.IN_PROGRESS,
                        )
                    ),
                ),
                default=Value(False),
                output_field=BooleanField(),
            ),
        )


class CarManager(models.Manager.from_queryset(CarQuerySet)):
    def get_queryset(self):
        return super().get_queryset().with_availability()


class Car(TimestampMixin):
    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        IN_REPAIR = "in_repair", _("In repair")
        DECOMMISSIONED = "decommissioned", _("Decommissioned")

    model = models.CharField(max_length=255, verbose_name=_("Model"))
    plate_number = models.CharField(
        max_length=50,
        unique=True,
        verbose_name=_("Plate number"),
    )
    type = models.ForeignKey(
        CarType,
        on_delete=models.PROTECT,
        related_name="cars",
        verbose_name=_("Type"),
    )
    num_seats = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("Number of seats"),
    )
    picture_url = models.URLField(blank=True, verbose_name=_("Picture URL"))
    drivers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="driven_cars",
        verbose_name=_("Drivers"),
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
        verbose_name=_("Status"),
    )

    objects = CarManager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Car")
        verbose_name_plural = _("Cars")

    def __str__(self):
        return f"{self.model} ({self.plate_number})"


class CarOrder(TimestampMixin):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PENDING = "pending", _("Pending")
        AWAITING_DRIVER = "awaiting_driver", _("Awaiting driver")
        SCHEDULED = "scheduled", _("Scheduled")  # claimed, window reserved, not started
        IN_PROGRESS = "in_progress", _("In progress")
        COMPLETED = "completed", _("Completed")
        REJECTED = "rejected", _("Rejected")
        CANCELLED = "cancelled", _("Cancelled")

    planned_datetime = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Planned start date and time"),
    )
    # Total time the order is expected to occupy the driver (drive + on-site).
    # ``planned_end`` is derived from this; both drive the scheduling overlap
    # check. Null = legacy/unscheduled order, excluded from conflict checks.
    estimated_duration = models.DurationField(
        null=True,
        blank=True,
        verbose_name=_("Estimated duration"),
    )
    # On-site time component, used by the auto-estimate (travel + service).
    service_time = models.DurationField(
        null=True,
        blank=True,
        verbose_name=_("On-site service time"),
    )
    # Latest acceptable start. If a delay pushes the planned start past this, the
    # order "cannot wait" and must be reassigned. Null = order can always wait.
    latest_start = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Latest acceptable start"),
    )
    address = models.CharField(max_length=500, blank=True, verbose_name=_("Address"))
    # Route endpoints (A → B) for the map, polyline and auto-estimate.
    origin_lat = models.FloatField(null=True, blank=True, verbose_name=_("Origin latitude"))
    origin_lng = models.FloatField(null=True, blank=True, verbose_name=_("Origin longitude"))
    address_lat = models.FloatField(null=True, blank=True, verbose_name=_("Destination latitude"))
    address_lng = models.FloatField(
        null=True, blank=True, verbose_name=_("Destination longitude")
    )
    note = models.TextField(blank=True, verbose_name=_("Note"))
    comment = models.TextField(blank=True, verbose_name=_("Comment"))
    project_name = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("Project name"),
    )
    car_type = models.ForeignKey(
        CarType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
        verbose_name=_("Requested car type"),
    )
    driver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="driven_car_orders",
        verbose_name=_("Driver"),
    )
    car = models.ForeignKey(
        Car,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
        verbose_name=_("Car"),
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
        verbose_name=_("Status"),
    )
    started_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Started at"))
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Finished at"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_car_orders",
        verbose_name=_("Created by"),
    )
    rejected_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Rejected at"))
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rejected_car_orders",
        verbose_name=_("Rejected by"),
    )
    reject_reason = models.TextField(blank=True, verbose_name=_("Reject reason"))

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Car order")
        verbose_name_plural = _("Car orders")

    def __str__(self):
        return f"CarOrder {self.id} [{self.status}]"

    @property
    def planned_end(self):
        """Planned finish = planned start + estimated duration (or ``None``)."""
        if self.planned_datetime and self.estimated_duration:
            return self.planned_datetime + self.estimated_duration
        return None

    def is_delayed(self, now):
        """In-progress trip that has run past its planned finish."""
        end = self.planned_end
        return bool(
            self.status == self.Status.IN_PROGRESS and end is not None and now > end
        )


class CarOrderActivity(models.Model):
    """Audit log of car-order state transitions."""

    class Kind(models.TextChoices):
        CREATED = "created", _("Created")
        SENT = "sent", _("Sent to admin")
        APPROVED = "approved", _("Approved")
        ACCEPTED_BY_DRIVER = "accepted_by_driver", _("Accepted by driver")
        COMPLETED = "completed", _("Completed")
        REJECTED = "rejected", _("Rejected")
        CANCELLED = "cancelled", _("Cancelled")
        RELEASED = "released", _("Released by driver")
        EXTENDED = "extended", _("Extended")
        REASSIGNED = "reassigned", _("Reassigned")

    order = models.ForeignKey(
        CarOrder,
        on_delete=models.CASCADE,
        related_name="activities",
        verbose_name=_("Order"),
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="car_order_activities",
        verbose_name=_("Actor"),
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, verbose_name=_("Kind"))
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))

    class Meta:
        ordering = ["created_at"]
        verbose_name = _("Car order activity")
        verbose_name_plural = _("Car order activities")
        indexes = [models.Index(fields=["order", "created_at"])]

    def __str__(self):
        return f"CarOrderActivity {self.kind} on order {self.order_id}"


class DriverShift(TimestampMixin):
    """A driver's working shift on ONE chosen car (Р1) carrying live location (Р3).

    The *active* shift is the row with ``ended_at IS NULL``. A driver may have at
    most one active shift, and a car may back at most one active shift at a time
    (enforced by partial unique constraints below).
    """

    class Status(models.TextChoices):
        ONLINE = "online", _("Online")  # on shift, free to take orders
        BUSY = "busy", _("Busy")  # accepted an order, not moving yet
        EN_ROUTE = "en_route", _("En route")  # moving to / with the client
        OFFLINE = "offline", _("Offline")  # shift ended

    driver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="car_shifts",
        verbose_name=_("Driver"),
    )
    car = models.ForeignKey(
        Car,
        on_delete=models.PROTECT,
        related_name="shifts",
        verbose_name=_("Shift car"),
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ONLINE,
        db_index=True,
        verbose_name=_("Status"),
    )
    lat = models.FloatField(null=True, blank=True, verbose_name=_("Latitude"))
    lng = models.FloatField(null=True, blank=True, verbose_name=_("Longitude"))
    last_seen = models.DateTimeField(null=True, blank=True, verbose_name=_("Last seen"))
    ended_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Ended at"),
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Driver shift")
        verbose_name_plural = _("Driver shifts")
        constraints = [
            models.UniqueConstraint(
                fields=["driver"],
                condition=Q(ended_at__isnull=True),
                name="one_active_shift_per_driver",
            ),
            models.UniqueConstraint(
                fields=["car"],
                condition=Q(ended_at__isnull=True),
                name="one_active_shift_per_car",
            ),
        ]

    def __str__(self):
        state = "active" if self.ended_at is None else "ended"
        return f"Shift {self.driver_id} on {self.car_id} [{state}]"


class OrderLiveLocation(models.Model):
    """Live driver position for an order, keyed by the order id (which may live
    in the upstream/demo backend). Lets the tracking map work in the gateway /
    hybrid setup where the order itself isn't stored locally."""

    order_id = models.PositiveIntegerField(unique=True, db_index=True, verbose_name=_("Order id"))
    lat = models.FloatField(verbose_name=_("Latitude"))
    lng = models.FloatField(verbose_name=_("Longitude"))
    last_seen = models.DateTimeField(verbose_name=_("Last seen"))
    # The A→B route polyline ([[lng, lat], …]) so the tracking map can draw it.
    geometry = models.JSONField(null=True, blank=True, verbose_name=_("Route geometry"))

    class Meta:
        verbose_name = _("Order live location")
        verbose_name_plural = _("Order live locations")

    def __str__(self):
        return f"LiveLocation order={self.order_id} ({self.lat}, {self.lng})"


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

    order_id = models.PositiveIntegerField(unique=True, db_index=True, verbose_name=_("Order id"))
    driver_id = models.PositiveIntegerField(
        null=True, blank=True, db_index=True, verbose_name=_("Driver user id")
    )
    origin_lat = models.FloatField(null=True, blank=True)
    origin_lng = models.FloatField(null=True, blank=True)
    address_lat = models.FloatField(null=True, blank=True)
    address_lng = models.FloatField(null=True, blank=True)
    estimated_duration = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("Estimated duration (minutes)")
    )
    service_time = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("On-site time (minutes)")
    )
    planned_datetime = models.DateTimeField(null=True, blank=True)
    latest_start = models.DateTimeField(null=True, blank=True)
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


class VehicleReport(TimestampMixin):
    """Daily condition report a responsible driver files for a car."""

    vehicle = models.ForeignKey(
        Car,
        on_delete=models.CASCADE,
        related_name="reports",
        verbose_name=_("Vehicle"),
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="vehicle_reports",
        verbose_name=_("Submitted by"),
    )
    date = models.DateField(verbose_name=_("Date"))
    comment = models.TextField(blank=True, verbose_name=_("Comment"))
    mileage = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("Mileage (km)"),
    )

    class Meta:
        ordering = ["-date", "-created_at"]
        unique_together = [("vehicle", "date")]
        verbose_name = _("Vehicle report")
        verbose_name_plural = _("Vehicle reports")

    def __str__(self):
        return f"Report for {self.vehicle_id} on {self.date}"
