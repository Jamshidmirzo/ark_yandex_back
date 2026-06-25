from rest_framework import serializers

from car_orders.models import OrderMeta

__all__ = ("OrderMetaSerializer",)


class OrderMetaSerializer(serializers.ModelSerializer):
    """Local feature overlay for a (demo) order — coords, window, trip state."""

    planned_end = serializers.DateTimeField(read_only=True)
    # Scheduling risk (computed): `at_risk` — projected start blows past the
    # latest acceptable start (driver won't make it); `is_late` — accepted but not
    # departed and the planned pickup time has already passed.
    at_risk = serializers.SerializerMethodField()
    is_late = serializers.SerializerMethodField()
    # Timers (computed). The raw `search_started_at` / `arrived_at` go out too so the
    # clients tick a skew-free clock locally; `*_elapsed_s` are server-now snapshots.
    search_elapsed_s = serializers.SerializerMethodField()
    wait_elapsed_s = serializers.SerializerMethodField()
    wait_limit_s = serializers.SerializerMethodField()
    wait_overdue = serializers.SerializerMethodField()

    class Meta:
        model = OrderMeta
        fields = [
            "order_id",
            "driver_id",
            "author_id",
            "is_urgent",
            "car_type_id",
            "dispatchable",
            "parent_order_id",
            "car_id",
            "car_label",
            "driver_name",
            "driver_phone",
            "overlay_claimed",
            "excluded_driver_ids",
            "origin_lat",
            "origin_lng",
            "address_lat",
            "address_lng",
            "origin_address",
            "dest_address",
            "project_name",
            "note",
            "car_type_name",
            "created_by_name",
            "has_return",
            "return_lat",
            "return_lng",
            "returning",
            "estimated_duration",
            "service_time",
            "planned_datetime",
            "latest_start",
            "trip_state",
            "planned_end",
            "at_risk",
            "is_late",
            "search_started_at",
            "arrived_at",
            "search_elapsed_s",
            "wait_elapsed_s",
            "wait_limit_s",
            "wait_overdue",
        ]
        # `returning` is driven by the trip-state transition (not the form), so it's
        # read-only here — only TripStateView flips it on the return leg.
        # `trip_state` is ALSO read-only on this plain feature-overlay upsert: it may
        # only advance through the TripStateView state machine (transitions, arrival
        # geofence, assigned-driver check, side-effects). Otherwise any authenticated
        # user could POST {"trip_state": "completed"} to /meta/ and force-complete ANY
        # order, bypassing all of it (the clients only ever set it via setTripState).
        read_only_fields = [
            "order_id", "returning", "trip_state", "planned_end", "at_risk", "is_late",
            "excluded_driver_ids",
            # Timer timestamps are server-owned — set by approve / trip-state / requeue,
            # never via a plain meta upsert (same reasoning as trip_state above).
            "search_started_at", "arrived_at", "search_elapsed_s", "wait_elapsed_s",
            "wait_limit_s", "wait_overdue",
            # Descriptive snapshot is server-owned (filled from the demo body), never
            # set via a client meta upsert — so a client can't spoof another order's
            # project/note/etc.
            "project_name", "note", "car_type_name", "created_by_name",
        ]
        # `driver_name` / `driver_phone` are the driver's own display snapshot, captured
        # at claim by the claiming driver's client (both claim paths: overlay-claim AND
        # the free-car meta upsert). They're plain display strings — NOT assignment
        # control like driver_id — so they stay writable and are intentionally NOT in
        # MetaView._PROTECTED_FIELDS.

    def get_at_risk(self, obj) -> bool:
        from django.utils import timezone

        from car_orders import scheduling

        # `active_by_driver` (if provided in context) lets the fleet snapshot
        # compute risk from one in-memory index instead of a query per order.
        return scheduling.meta_needs_reassign(
            obj, timezone.now(), active=self.context.get("active_by_driver")
        )

    def get_is_late(self, obj) -> bool:
        from django.utils import timezone

        # «Опаздывает» = a driver ACCEPTED it (driver_id set) but hasn't departed
        # past the planned pickup. A freshly-created, not-yet-claimed order also
        # defaults to trip_state=assigned — it must NOT read as late.
        if (
            obj.driver_id is not None
            and obj.trip_state == OrderMeta.TripState.ASSIGNED
            and obj.planned_datetime
        ):
            return timezone.now() > obj.planned_datetime
        return False

    @staticmethod
    def _wait_limit_s() -> int:
        from django.conf import settings

        return int(getattr(settings, "CAR_ORDER_PICKUP_WAIT_LIMIT_S", 30 * 60))

    def get_search_elapsed_s(self, obj) -> int | None:
        # «Поиск водителя» counts up only while still searching (no driver yet); once
        # a driver is assigned the timer disappears.
        if obj.search_started_at is None or obj.driver_id is not None:
            return None
        from django.utils import timezone

        return max(0, int((timezone.now() - obj.search_started_at).total_seconds()))

    def get_wait_elapsed_s(self, obj) -> int | None:
        # «Ожидание клиента» counts up only while the driver is at the pickup.
        if obj.arrived_at is None or obj.trip_state != OrderMeta.TripState.AT_CLIENT:
            return None
        from django.utils import timezone

        return max(0, int((timezone.now() - obj.arrived_at).total_seconds()))

    def get_wait_limit_s(self, obj) -> int:
        return self._wait_limit_s()

    def get_wait_overdue(self, obj) -> bool:
        elapsed = self.get_wait_elapsed_s(obj)
        return elapsed is not None and elapsed >= self._wait_limit_s()
