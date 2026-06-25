from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from core.models import TimestampMixin

__all__ = ("DriverShift", "DriverShiftState")


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
        "Car",
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


class DriverShiftState(models.Model):
    """Local OVERLAY for «driver on shift» (Р1), keyed by the demo driver id —
    because the demo backend doesn't expose a set-shift endpoint. A driver goes
    online by picking one of their (demo) cars; this records which car + its type,
    so the dispatcher's nearest-driver match knows who's on shift with what.
    Active shift = a row that exists (ended → row deleted)."""

    driver_id = models.PositiveIntegerField(unique=True, db_index=True, verbose_name=_("Driver id"))
    car_id = models.PositiveIntegerField(verbose_name=_("Car id"))
    car_model = models.CharField(max_length=255, blank=True)
    car_plate = models.CharField(max_length=64, blank=True)
    car_type_id = models.PositiveIntegerField(null=True, blank=True)
    car_type_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, default="online")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Driver shift (overlay)")
        verbose_name_plural = _("Driver shifts (overlay)")

    def as_shift(self):
        """Shape the frontend's ShiftControl + driver ranking expect."""
        return {
            "id": self.driver_id,
            "status": self.status,
            "ended_at": None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "car": {
                "id": self.car_id,
                "model": self.car_model,
                "plate_number": self.car_plate,
                "type": {"id": self.car_type_id, "name": self.car_type_name},
            },
        }
