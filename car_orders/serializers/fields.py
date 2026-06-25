from datetime import timedelta

from rest_framework import serializers

__all__ = ("MinutesDurationField",)


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
