from django.db import models
from django.utils.translation import gettext_lazy as _

__all__ = ("DispatchSettings",)


class DispatchSettings(models.Model):
    """Singleton runtime config for the auto-dispatch worker, so the dispatcher can
    flip auto-assignment on/off LIVE from the «Диспетчерская» page (no redeploy).

    The env var ``AUTO_DISPATCH_ENABLED`` stays a hard ops kill-switch; the worker
    runs only when env AND this row are both on (see ``dispatch.auto_enabled``).
    Always one row, ``pk=1``."""

    SINGLETON_PK = 1

    auto_enabled = models.BooleanField(
        default=False, verbose_name=_("Auto-dispatch enabled")
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("Updated by (user id)")
    )

    class Meta:
        verbose_name = _("Dispatch settings")
        verbose_name_plural = _("Dispatch settings")

    def __str__(self):
        return f"Auto-dispatch: {'on' if self.auto_enabled else 'off'}"

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_PK  # never create a second row
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        """Return the singleton row, creating it (off) on first access."""
        obj, _created = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj
