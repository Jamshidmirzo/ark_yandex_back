from django.contrib.gis.db import models as gis_models
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
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


class DriverPositionQuerySet(models.QuerySet):
    """Reusable telemetry filters + the spatial nearest-driver query (style rule 5:
    reusable querysets live on a custom QuerySet, not inline in dispatch/views)."""

    def fresh(self, cutoff):
        """Positions with a GPS fix at/after ``cutoff`` (drop stale fixes)."""
        return self.filter(last_seen__gte=cutoff)

    def near(self, point, *, within_m=None):
        """Candidates ordered nearest-first to ``point`` (a geos Point, srid 4326),
        annotated with ``distance_m``. ``location`` is a geography column, so
        ``dwithin``/``Distance`` are in METRES and the GiST index drives an
        index-assisted KNN ordering (``<->``). ``within_m`` optionally bounds the
        search radius (also metres)."""
        qs = self.exclude(location__isnull=True)
        if within_m is not None:
            qs = qs.filter(location__dwithin=(point, within_m))
        return qs.annotate(distance_m=Distance("location", point)).order_by("distance_m")


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
    # Spatial mirror of (lat, lng), kept in sync by save(). The lat/lng FloatFields
    # stay the client-facing source of truth (all existing readers untouched);
    # `location` exists ONLY to back the index-assisted nearest query
    # (DriverPositionQuerySet.near). geography=True → Distance/dwithin in metres;
    # spatial_index=True → GiST. Lives in the `geo` PostGIS DB (car_orders.routers).
    location = gis_models.PointField(
        geography=True,
        srid=4326,
        null=True,
        blank=True,
        spatial_index=True,
        verbose_name=_("Location (synced from lat/lng)"),
    )

    objects = DriverPositionQuerySet.as_manager()

    class Meta:
        verbose_name = _("Driver position")
        verbose_name_plural = _("Driver positions")

    def __str__(self):
        return f"DriverPosition driver={self.driver_id} ({self.lat}, {self.lng})"

    def save(self, *args, **kwargs):
        # Single sync point: every write path (heartbeat update_or_create in
        # views/tracking.py, the seed_driver_positions command, admin, shell) goes
        # through save(), so this is the only place that keeps `location` in step
        # with lat/lng. NOTE: never use DriverPosition.objects.update(lat=…) — a
        # queryset UPDATE bypasses save() and would desync `location` (audit: no such
        # call exists today). Point takes (x=lng, y=lat) — axis order matters.
        if self.lat is not None and self.lng is not None:
            self.location = Point(self.lng, self.lat, srid=4326)
        else:
            self.location = None
        super().save(*args, **kwargs)
