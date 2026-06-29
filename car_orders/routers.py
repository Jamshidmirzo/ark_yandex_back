"""Database router for the telemetry / geo split.

`DriverPosition` and `OrderLiveLocation` are high-write, **FK-free** telemetry models
(keyed by demo ids, no ForeignKey to anything). They live in a separate PostGIS
database (`geo`) so the spatial nearest-driver query can use a GiST index and so the
1 Hz heartbeat write load is isolated from the transactional `default` DB. Everything
else — including `OrderMeta` (whose claim path uses `select_for_update`) — stays in
`default`, which remains PLAIN Postgres identical to upstream `ark-backend`.

Because the two models are FK-free, no query ever joins them to a `default` table:
dispatch ranks drivers by joining `shifts` (default) with a Python dict materialised
from the geo query — in Python, by `driver_id`. So there is never a cross-DB JOIN.

This whole module is a contained, revertible divergence from ark-backend: drop
`DATABASE_ROUTERS` + the `geo` alias + the `location` field and the app falls back to
single-DB plain Postgres with the existing Python-haversine ranking.
"""

__all__ = ("GeoRouter",)

GEO_DB = "geo"

# (app_label, model_name) pairs routed to the geo DB. model_name is always lowercase.
# Hardcoded (only two) and matched WITHOUT importing the models, so the router is safe
# to import at settings-load time (no app-registry ordering issues).
_GEO_MODELS = {
    ("car_orders", "driverposition"),
    ("car_orders", "orderlivelocation"),
}


class GeoRouter:
    """Routes ONLY the two telemetry models to the `geo` PostGIS database."""

    def _is_geo(self, model):
        return (model._meta.app_label, model._meta.model_name) in _GEO_MODELS

    def db_for_read(self, model, **hints):
        # Return an alias to pin, or None = "no opinion" → falls back to `default`.
        return GEO_DB if self._is_geo(model) else None

    def db_for_write(self, model, **hints):
        return GEO_DB if self._is_geo(model) else None

    def allow_relation(self, obj1, obj2, **hints):
        # The geo models are FK-free, so we never need to assert a cross-db relation.
        # Return None (no opinion) rather than False so we don't block any unrelated
        # same-db relation Django consults this for.
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # MUST return a concrete bool (never None) so the partition is STRICT: a None
        # here would let Django's default ("allow") leak contenttypes/auth into `geo`
        # and the geo tables into `default`.
        is_geo_model = (app_label, model_name) in _GEO_MODELS
        if db == GEO_DB:
            if model_name is None:
                # App-level op with no model: CreateExtension("postgis") + the
                # location backfill RunPython. Allow ONLY car_orders such ops on geo.
                # Assumption: car_orders has no `default`-targeted no-model migration
                # (verified — its history is all CreateModel/AddField/AlterField; the
                # only no-model ops are the new geo extension + backfill). Revisit this
                # rule if a default-DB data migration is ever added to car_orders.
                return app_label == "car_orders"
            return is_geo_model
        # `default` (and any other non-geo alias): everything EXCEPT the geo models.
        if model_name is None:
            return app_label != "car_orders"
        return not is_geo_model
