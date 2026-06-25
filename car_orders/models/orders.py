from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import TimestampMixin

__all__ = ("CarOrder", "CarOrderActivity")


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
        "CarType",
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
        "Car",
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
