from datetime import timedelta

from django.contrib.auth import get_user_model
from rest_framework import serializers

from auth_core.serializers import UserBasicSerializer
from car_orders.models import (
    Car,
    CarOrder,
    CarOrderActivity,
    CarOrderTemplate,
    CarType,
    DriverShift,
    OrderMeta,
    VehicleReport,
)

User = get_user_model()


class MinutesDurationField(serializers.Field):
    """Expose a model ``DurationField`` as an integer number of minutes, so the
    whole API speaks minutes (matching ``/estimate`` and ``/extend``)."""

    def to_representation(self, value):
        if value is None:
            return None
        return int(value.total_seconds() // 60)

    def to_internal_value(self, value):
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            raise serializers.ValidationError("Expected an integer number of minutes.") from None
        if minutes < 0:
            raise serializers.ValidationError("Must be zero or positive.")
        return timedelta(minutes=minutes)


# --- Car types --------------------------------------------------------------


class CarTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = CarType
        fields = ["id", "name", "picture_url"]


class CarTypeWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = CarType
        fields = ["name", "picture_url"]


# --- Cars (garage) ----------------------------------------------------------


class CarSerializer(serializers.ModelSerializer):
    type = CarTypeSerializer(read_only=True)
    drivers = UserBasicSerializer(many=True, read_only=True)
    is_available = serializers.SerializerMethodField()

    class Meta:
        model = Car
        fields = [
            "id",
            "model",
            "plate_number",
            "type",
            "num_seats",
            "picture_url",
            "drivers",
            "status",
            "is_available",
            "created_at",
        ]
        read_only_fields = fields

    def get_is_available(self, obj) -> bool:
        annotated = getattr(obj, "is_available", None)
        if annotated is not None:
            return annotated
        return (
            obj.status == Car.Status.ACTIVE
            and not CarOrder.objects.filter(car=obj, status=CarOrder.Status.IN_PROGRESS).exists()
        )


class CarWriteSerializer(serializers.ModelSerializer):
    type_id = serializers.PrimaryKeyRelatedField(
        queryset=CarType.objects.all(),
        source="type",
    )
    driver_ids = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        source="drivers",
        many=True,
        required=False,
    )

    class Meta:
        model = Car
        fields = [
            "model",
            "plate_number",
            "type_id",
            "num_seats",
            "picture_url",
            "driver_ids",
            "status",
        ]


# --- Driver shift (Р1) + live location (Р3) ---------------------------------


class DriverShiftSerializer(serializers.ModelSerializer):
    car = CarSerializer(read_only=True)

    class Meta:
        model = DriverShift
        fields = ["id", "car", "status", "lat", "lng", "last_seen", "created_at", "ended_at"]
        read_only_fields = fields


class ShiftStartSerializer(serializers.Serializer):
    """PATCH /drivers/me/shift/ — pick the car to go on shift with."""

    car_id = serializers.PrimaryKeyRelatedField(
        queryset=Car.objects.all(),
        source="car",
    )


class LocationSerializer(serializers.Serializer):
    """POST /drivers/me/location/ — GPS heartbeat from the driver app."""

    lat = serializers.FloatField(min_value=-90, max_value=90)
    lng = serializers.FloatField(min_value=-180, max_value=180)
    # Optional device travel heading (deg). Used as a fallback for the OSRM start-snap
    # when the server can't derive direction from consecutive fixes yet.
    heading = serializers.FloatField(min_value=0, max_value=360, required=False, allow_null=True)


# --- Drivers (reader view over Users in the Driver group) -------------------


class DriverSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    is_available = serializers.SerializerMethodField()
    current_shift = serializers.SerializerMethodField()
    cars = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "name", "is_available", "current_shift", "cars"]

    def get_name(self, obj) -> str:
        return getattr(obj, "name", "") or obj.get_full_name() or obj.get_username()

    def get_is_available(self, obj) -> bool:
        return not CarOrder.objects.filter(driver=obj, status=CarOrder.Status.IN_PROGRESS).exists()

    def get_current_shift(self, obj):
        shift = (
            DriverShift.objects.filter(driver=obj, ended_at__isnull=True)
            .select_related("car", "car__type")
            .first()
        )
        return DriverShiftSerializer(shift).data if shift else None

    def get_cars(self, obj) -> list:
        return [
            {"id": c.id, "model": c.model, "plate_number": c.plate_number}
            for c in obj.driven_cars.all()
        ]


# --- Car orders -------------------------------------------------------------


class CarOrderSerializer(serializers.ModelSerializer):
    car_type = CarTypeSerializer(read_only=True)
    car = CarSerializer(read_only=True)
    driver = UserBasicSerializer(read_only=True)
    created_by = UserBasicSerializer(read_only=True)
    rejected_by = UserBasicSerializer(read_only=True)
    driver_location = serializers.SerializerMethodField()
    estimated_duration = MinutesDurationField(read_only=True)
    service_time = MinutesDurationField(read_only=True)
    planned_end = serializers.DateTimeField(read_only=True)
    is_delayed = serializers.SerializerMethodField()
    needs_reassign = serializers.SerializerMethodField()
    # The demo status reconciled with our overlay trip — the SINGLE source of
    # truth clients should display (so they stop re-deriving it). See
    # services/status.py. The raw `status` stays for native workflow gating.
    effective_status = serializers.SerializerMethodField()

    class Meta:
        model = CarOrder
        fields = [
            "id",
            "project_name",
            "planned_datetime",
            "estimated_duration",
            "service_time",
            "latest_start",
            "planned_end",
            "address",
            "origin_lat",
            "origin_lng",
            "address_lat",
            "address_lng",
            "note",
            "comment",
            "car_type",
            "driver",
            "car",
            "status",
            "effective_status",
            "started_at",
            "finished_at",
            "created_by",
            "rejected_by",
            "rejected_at",
            "reject_reason",
            "driver_location",
            "is_delayed",
            "needs_reassign",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_is_delayed(self, obj) -> bool:
        from django.utils import timezone

        return obj.is_delayed(timezone.now())

    def get_needs_reassign(self, obj) -> bool:
        from django.utils import timezone

        from car_orders import scheduling

        if obj.status != CarOrder.Status.SCHEDULED:
            return False
        return scheduling.needs_reassign(obj, timezone.now())

    def get_effective_status(self, obj) -> str:
        from car_orders.models import OrderMeta
        from car_orders.services.status import effective_status

        # `metas_by_order_id` is provided by the viewset for LISTS (one query for
        # the page — see CarOrderViewSet.get_serializer_context). For a single
        # object (retrieve / me-active / schedule) fall back to one lookup.
        metas = self.context.get("metas_by_order_id")
        meta = (
            metas.get(obj.pk)
            if metas is not None
            else OrderMeta.objects.filter(order_id=obj.pk).first()
        )
        return effective_status(obj.status, meta)

    def get_driver_location(self, obj):
        """Р3: live driver position, visible while the trip is in progress."""
        if obj.status != CarOrder.Status.IN_PROGRESS or not obj.driver_id:
            return None
        shift = (
            DriverShift.objects.filter(driver_id=obj.driver_id, ended_at__isnull=True)
            .only("lat", "lng", "last_seen", "status")
            .first()
        )
        if not shift or shift.lat is None or shift.lng is None:
            return None
        return {
            "lat": shift.lat,
            "lng": shift.lng,
            "last_seen": shift.last_seen,
            "status": shift.status,
        }


class CarOrderWriteSerializer(serializers.ModelSerializer):
    car_type_id = serializers.PrimaryKeyRelatedField(
        queryset=CarType.objects.all(),
        source="car_type",
        required=False,
        allow_null=True,
    )
    origin_lat = serializers.FloatField(
        min_value=-90, max_value=90, required=False, allow_null=True
    )
    origin_lng = serializers.FloatField(
        min_value=-180, max_value=180, required=False, allow_null=True
    )
    address_lat = serializers.FloatField(
        min_value=-90, max_value=90, required=False, allow_null=True
    )
    address_lng = serializers.FloatField(
        min_value=-180, max_value=180, required=False, allow_null=True
    )
    estimated_duration = MinutesDurationField(required=False, allow_null=True)
    service_time = MinutesDurationField(required=False, allow_null=True)

    class Meta:
        model = CarOrder
        fields = [
            "project_name",
            "planned_datetime",
            "estimated_duration",
            "service_time",
            "latest_start",
            "address",
            "origin_lat",
            "origin_lng",
            "address_lat",
            "address_lng",
            "note",
            "comment",
            "car_type_id",
        ]

    def validate_estimated_duration(self, value):
        if value is not None and value.total_seconds() <= 0:
            raise serializers.ValidationError("Duration must be positive.")
        return value


class RouteEstimateSerializer(serializers.Serializer):
    """POST /car-orders/estimate/ — auto-estimate route + duration A → B."""

    origin_lat = serializers.FloatField(min_value=-90, max_value=90)
    origin_lng = serializers.FloatField(min_value=-180, max_value=180)
    dest_lat = serializers.FloatField(min_value=-90, max_value=90)
    dest_lng = serializers.FloatField(min_value=-180, max_value=180)
    service_minutes = serializers.IntegerField(min_value=0, required=False)


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


class CarOrderTemplateSerializer(serializers.ModelSerializer):
    """Reusable order «заготовка» — route + car type + duration + note, minus the
    date/time. Coordinates are validated like the order write serializer."""

    origin_lat = serializers.FloatField(
        min_value=-90, max_value=90, required=False, allow_null=True
    )
    origin_lng = serializers.FloatField(
        min_value=-180, max_value=180, required=False, allow_null=True
    )
    address_lat = serializers.FloatField(
        min_value=-90, max_value=90, required=False, allow_null=True
    )
    address_lng = serializers.FloatField(
        min_value=-180, max_value=180, required=False, allow_null=True
    )

    class Meta:
        model = CarOrderTemplate
        fields = [
            "id",
            "name",
            "project_name",
            "origin_lat",
            "origin_lng",
            "origin_label",
            "address",
            "address_lat",
            "address_lng",
            "car_type_id",
            "estimated_duration",
            "service_time",
            "note",
            "created_by_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_by_id", "created_at", "updated_at"]


class CarOrderActivitySerializer(serializers.ModelSerializer):
    actor = UserBasicSerializer(read_only=True)

    class Meta:
        model = CarOrderActivity
        fields = ["id", "kind", "actor", "payload", "created_at"]
        read_only_fields = fields


# --- Vehicle reports --------------------------------------------------------


class VehicleReportSerializer(serializers.ModelSerializer):
    submitted_by = UserBasicSerializer(read_only=True)
    vehicle_id = serializers.PrimaryKeyRelatedField(
        queryset=Car.objects.all(),
        source="vehicle",
        write_only=True,
    )
    vehicle = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = VehicleReport
        fields = [
            "id",
            "vehicle_id",
            "vehicle",
            "submitted_by",
            "date",
            "comment",
            "mileage",
            "created_at",
        ]
        read_only_fields = ["id", "vehicle", "submitted_by", "created_at"]
