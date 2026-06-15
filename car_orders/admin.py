from django.contrib import admin

from car_orders.models import (
    Car,
    CarOrder,
    CarOrderActivity,
    CarType,
    DispatchSettings,
    DriverShift,
    VehicleReport,
)


@admin.register(DispatchSettings)
class DispatchSettingsAdmin(admin.ModelAdmin):
    """Runtime on/off for the auto-dispatch worker (ops-side mirror of the
    dispatcher's in-app switch)."""

    list_display = ("id", "auto_enabled", "updated_at", "updated_by")


@admin.register(CarType)
class CarTypeAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)


@admin.register(Car)
class CarAdmin(admin.ModelAdmin):
    list_display = ("id", "model", "plate_number", "type", "status")
    list_filter = ("status", "type")
    search_fields = ("model", "plate_number")
    filter_horizontal = ("drivers",)


@admin.register(CarOrder)
class CarOrderAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "car_type", "driver", "car", "created_by", "created_at")
    list_filter = ("status",)
    search_fields = ("address", "note", "project_name")
    autocomplete_fields = ("car_type", "driver", "car", "created_by", "rejected_by")


@admin.register(CarOrderActivity)
class CarOrderActivityAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "kind", "actor", "created_at")
    list_filter = ("kind",)


@admin.register(DriverShift)
class DriverShiftAdmin(admin.ModelAdmin):
    list_display = ("id", "driver", "car", "status", "last_seen", "ended_at")
    list_filter = ("status",)


@admin.register(VehicleReport)
class VehicleReportAdmin(admin.ModelAdmin):
    list_display = ("id", "vehicle", "submitted_by", "date", "mileage")
    list_filter = ("date",)
