from django.db import models
from django.utils.translation import gettext_lazy as _

__all__ = ("OrderLiveLocation", "DriverPosition")


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


class DriverPosition(models.Model):
    """Latest GPS position **per driver** (not per order). The driver app posts a
    heartbeat even when the driver is FREE (no active order), so the dispatcher
    can find the nearest available driver for an awaiting order. Keyed by the
    driver (demo) user id."""

    driver_id = models.PositiveIntegerField(unique=True, db_index=True, verbose_name=_("Driver id"))
    lat = models.FloatField(verbose_name=_("Latitude"))
    lng = models.FloatField(verbose_name=_("Longitude"))
    # Device-reported travel heading (deg, 0-360) for the last fix, when the app sends
    # it. Optional: the live re-route prefers a heading derived from consecutive fixes
    # and uses this only as a fallback, so legacy clients that omit it still work.
    heading = models.FloatField(null=True, blank=True, verbose_name=_("Heading"))
    last_seen = models.DateTimeField(verbose_name=_("Last seen"))

    class Meta:
        verbose_name = _("Driver position")
        verbose_name_plural = _("Driver positions")

    def __str__(self):
        return f"DriverPosition driver={self.driver_id} ({self.lat}, {self.lng})"
