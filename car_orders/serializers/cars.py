from django.contrib.auth import get_user_model
from rest_framework import serializers

from auth_core.serializers import UserBasicSerializer
from car_orders.models import Car, CarOrder, CarType

User = get_user_model()

__all__ = (
    "CarTypeSerializer",
    "CarTypeWriteSerializer",
    "CarSerializer",
    "CarWriteSerializer",
)


# --- Car types --------------------------------------------------------------


class CarTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = CarType
        fields = ("id", "name", "picture_url")


class CarTypeWriteSerializer(CarTypeSerializer):
    class Meta(CarTypeSerializer.Meta):
        # Read fields minus the read-only ``id`` — no re-listing of name/picture_url.
        fields = tuple(f for f in CarTypeSerializer.Meta.fields if f != "id")


# --- Cars (garage) ----------------------------------------------------------


class CarBaseSerializer(serializers.ModelSerializer):
    """Shared scalar fields between the read and write Car serializers — the only
    block they have in common (read adds nested objects + ``is_available``; write
    swaps in ``type_id`` / ``driver_ids``). Kept as a base so the common columns
    are declared once."""

    class Meta:
        model = Car
        fields = ("model", "plate_number", "num_seats", "picture_url", "status")


class CarSerializer(CarBaseSerializer):
    type = CarTypeSerializer(read_only=True)
    drivers = UserBasicSerializer(many=True, read_only=True)
    is_available = serializers.SerializerMethodField()

    class Meta(CarBaseSerializer.Meta):
        fields = (
            "id",
            *CarBaseSerializer.Meta.fields,
            "type",
            "drivers",
            "is_available",
            "created_at",
        )
        read_only_fields = fields

    def get_is_available(self, obj) -> bool:
        annotated = getattr(obj, "is_available", None)
        if annotated is not None:
            return annotated
        return (
            obj.status == Car.Status.ACTIVE
            and not CarOrder.objects.filter(car=obj, status=CarOrder.Status.IN_PROGRESS).exists()
        )


class CarWriteSerializer(CarBaseSerializer):
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

    class Meta(CarBaseSerializer.Meta):
        fields = (*CarBaseSerializer.Meta.fields, "type_id", "driver_ids")
