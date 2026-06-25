from rest_framework import serializers

from auth_core.serializers import UserBasicSerializer
from car_orders.models import CarOrder, CarOrderActivity, CarType, DriverShift

from .cars import CarSerializer, CarTypeSerializer
from .fields import MinutesDurationField

__all__ = (
    "CarOrderSerializer",
    "CarOrderWriteSerializer",
    "RouteEstimateSerializer",
    "CarOrderActivitySerializer",
)


class CarOrderBaseSerializer(serializers.ModelSerializer):
    """Scalar input fields shared by the read (detail) and write Car-order
    serializers. Declared once here so neither side re-lists the block — the read
    serializer adds nested objects + computed fields, the write serializer adds
    ``car_type_id`` and the coordinate/duration validation."""

    class Meta:
        model = CarOrder
        fields = (
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
        )


# --- Car orders -------------------------------------------------------------


class CarOrderSerializer(CarOrderBaseSerializer):
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

    class Meta(CarOrderBaseSerializer.Meta):
        fields = (
            "id",
            *CarOrderBaseSerializer.Meta.fields,
            "planned_end",
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
        )
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


class CarOrderWriteSerializer(CarOrderBaseSerializer):
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

    class Meta(CarOrderBaseSerializer.Meta):
        fields = (*CarOrderBaseSerializer.Meta.fields, "car_type_id")

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


class CarOrderActivitySerializer(serializers.ModelSerializer):
    actor = UserBasicSerializer(read_only=True)

    class Meta:
        model = CarOrderActivity
        fields = ("id", "kind", "actor", "payload", "created_at")
        read_only_fields = fields
