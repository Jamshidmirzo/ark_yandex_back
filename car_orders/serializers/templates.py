from rest_framework import serializers

from car_orders.models import CarOrderTemplate

__all__ = ("CarOrderTemplateSerializer",)


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
        fields = (
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
        )
        read_only_fields = ("id", "created_by_id", "created_at", "updated_at")
