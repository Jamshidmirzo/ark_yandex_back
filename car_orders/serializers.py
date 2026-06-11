from datetime import timedelta

from django.contrib.auth import get_user_model
from rest_framework import serializers

from auth_core.serializers import UserBasicSerializer
from car_orders.models import (
    Car,
    CarOrder,
    CarOrderActivity,
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

    class Meta:
        model = OrderMeta
        fields = [
            "order_id",
            "driver_id",
            "author_id",
            "is_urgent",
            "parent_order_id",
            "car_id",
            "car_label",
            "overlay_claimed",
            "origin_lat",
            "origin_lng",
            "address_lat",
            "address_lng",
            "estimated_duration",
            "service_time",
            "planned_datetime",
            "latest_start",
            "trip_state",
            "planned_end",
            "at_risk",
            "is_late",
        ]
        read_only_fields = ["order_id", "planned_end", "at_risk", "is_late"]

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
