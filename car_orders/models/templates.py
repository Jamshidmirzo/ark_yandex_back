from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import TimestampMixin

__all__ = ("CarOrderTemplate",)


class CarOrderTemplate(TimestampMixin):
    """A reusable «заготовка» for a recurring car order (e.g. съёмки «Севимли →
    Сквер»), so a frequent route doesn't have to be re-entered every time.

    Purely a LOCAL form-prefill overlay — the order itself is still created in the
    demo backend through the gateway. A template stores everything the create form
    needs EXCEPT the date/time (that's picked fresh for each order). ``car_type_id``
    is the demo CarType id (kept as a plain int, like ``OrderMeta.car_type_id``,
    since car types are served from demo). Templates are shared across the team
    (the whole list is visible to everyone); ``created_by_id`` is the demo user id
    of whoever saved it, kept for display only."""

    name = models.CharField(max_length=120, verbose_name=_("Template name"))
    project_name = models.CharField(
        max_length=255, blank=True, verbose_name=_("Default order name")
    )
    # Route A → B: coordinates + the human-readable labels the form shows.
    origin_lat = models.FloatField(null=True, blank=True, verbose_name=_("Origin latitude"))
    origin_lng = models.FloatField(null=True, blank=True, verbose_name=_("Origin longitude"))
    origin_label = models.CharField(max_length=500, blank=True, verbose_name=_("Origin label"))
    address = models.CharField(max_length=500, blank=True, verbose_name=_("Destination label"))
    address_lat = models.FloatField(null=True, blank=True, verbose_name=_("Destination latitude"))
    address_lng = models.FloatField(null=True, blank=True, verbose_name=_("Destination longitude"))
    car_type_id = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("Car type id (demo)")
    )
    estimated_duration = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("Estimated duration (minutes)")
    )
    service_time = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("On-site service time (minutes)")
    )
    note = models.TextField(blank=True, verbose_name=_("Note"))
    created_by_id = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("Created by (demo user id)")
    )

    class Meta:
        ordering = ["name"]
        verbose_name = _("Car order template")
        verbose_name_plural = _("Car order templates")

    def __str__(self):
        return f"CarOrderTemplate {self.id} «{self.name}»"
