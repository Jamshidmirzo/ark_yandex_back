from rest_framework import serializers

from auth_core.serializers import UserBasicSerializer
from car_orders.models import Car, VehicleReport

__all__ = ("VehicleReportSerializer",)


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
        fields = (
            "id",
            "vehicle_id",
            "vehicle",
            "submitted_by",
            "date",
            "comment",
            "mileage",
            "created_at",
        )
        read_only_fields = ("id", "vehicle", "submitted_by", "created_at")
