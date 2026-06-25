from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import TimestampMixin

__all__ = ("VehicleReport",)


class VehicleReport(TimestampMixin):
    """Daily condition report a responsible driver files for a car."""

    vehicle = models.ForeignKey(
        "Car",
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
