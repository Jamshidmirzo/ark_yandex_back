from django.db import models
from django.utils.translation import gettext_lazy as _


class TimestampMixin(models.Model):
    """Adds ``created_at`` / ``updated_at`` to a model.

    Mirrors ``apps.core.models.TimestampMixin`` in ark-backend.
    """

    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("Date and time of creation"),
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name=_("Date and time of last update"),
    )

    class Meta:
        abstract = True
        ordering = ("-created_at", "-id")
