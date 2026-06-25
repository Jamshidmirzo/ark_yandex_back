from django.contrib.auth import get_user_model
from rest_framework import serializers

from car_orders.models import Car, CarOrder, DriverShift

from .cars import CarSerializer

User = get_user_model()

__all__ = (
    "DriverShiftSerializer",
    "ShiftStartSerializer",
    "LocationSerializer",
    "DriverSerializer",
)


# --- Driver shift (Р1) + live location (Р3) ---------------------------------


class DriverShiftSerializer(serializers.ModelSerializer):
    car = CarSerializer(read_only=True)

    class Meta:
        model = DriverShift
        fields = ("id", "car", "status", "lat", "lng", "last_seen", "created_at", "ended_at")
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
        fields = ("id", "name", "is_available", "current_shift", "cars")

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
