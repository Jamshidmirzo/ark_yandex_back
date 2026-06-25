"""Garage ViewSets: car types, cars, the driver roster (+ the driver's own shift /
location / schedule actions) and vehicle reports."""

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from auth_core.models import AccessGroup, UserAccessGroup
from auth_core.permissions import HasPermission, user_has_permission
from car_orders import services
from car_orders.models import Car, CarOrder, CarType, DriverShift, VehicleReport
from car_orders.serializers import (
    CarOrderSerializer,
    CarSerializer,
    CarTypeSerializer,
    CarTypeWriteSerializer,
    CarWriteSerializer,
    DriverSerializer,
    DriverShiftSerializer,
    LocationSerializer,
    ShiftStartSerializer,
    VehicleReportSerializer,
)

from .base import DRIVER_GROUP, User, _active_shift, _bad_request, _forbidden

__all__ = (
    "CarTypeViewSet",
    "CarViewSet",
    "DriverViewSet",
    "VehicleReportViewSet",
    "_garage_permissions",
    "_driver_has_active_trip",
)


def _driver_has_active_trip(user):
    return CarOrder.objects.filter(driver=user, status=CarOrder.Status.IN_PROGRESS).exists()


def _garage_permissions(action_name):
    mapping = {
        "create": "garage:create",
        "update": "garage:update",
        "partial_update": "garage:update",
        "destroy": "garage:delete",
    }
    codename = mapping.get(action_name, "garage:list")
    return [IsAuthenticated(), HasPermission(codename)()]


class CarTypeViewSet(viewsets.ModelViewSet):
    queryset = CarType.objects.all()
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    search_fields = ["name"]

    def get_serializer_class(self):
        if self.action in ("create", "partial_update"):
            return CarTypeWriteSerializer
        return CarTypeSerializer

    def get_permissions(self):
        return _garage_permissions(self.action)


class CarViewSet(viewsets.ModelViewSet):
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    search_fields = ["model", "plate_number"]
    filterset_fields = ["type", "status"]

    def get_queryset(self):
        return Car.objects.select_related("type").prefetch_related("drivers")

    def get_serializer_class(self):
        if self.action in ("create", "partial_update"):
            return CarWriteSerializer
        return CarSerializer

    def get_permissions(self):
        return _garage_permissions(self.action)


class DriverViewSet(viewsets.GenericViewSet):
    """Reader over users in the ``Driver`` group + the driver's own shift/location."""

    serializer_class = DriverSerializer
    search_fields = ["name", "username"]

    def get_queryset(self):
        return (
            User.objects.filter(access_group_memberships__group__name=DRIVER_GROUP)
            .distinct()
            .prefetch_related("driven_cars")
        )

    def list(self, request, *args, **kwargs):
        if not (request.user.is_superuser or user_has_permission(request.user, "driver:list")):
            return _forbidden(_("Requires permission: driver:list"))
        qs = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(DriverSerializer(page, many=True).data)
        return Response(DriverSerializer(qs, many=True).data)

    @action(detail=False, methods=["get"], url_path="me/cars")
    def my_cars(self, request):
        cars = request.user.driven_cars.select_related("type").all()
        return Response(CarSerializer(cars, many=True).data)

    @action(detail=False, methods=["get"], url_path="me/schedule")
    def my_schedule(self, request):
        """The driver's committed timeline: scheduled + in-progress orders,
        ordered by planned start, each annotated with delay / reassign flags."""
        if not user_has_permission(request.user, "driver:accept_order"):
            return _forbidden(_("Requires permission: driver:accept_order"))
        orders = (
            CarOrder.objects.filter(
                driver=request.user,
                status__in=[CarOrder.Status.SCHEDULED, CarOrder.Status.IN_PROGRESS],
            )
            .select_related("car_type", "car", "car__type", "driver", "created_by")
            .order_by("planned_datetime", "created_at")
        )
        return Response(
            CarOrderSerializer(orders, many=True, context=self.get_serializer_context()).data
        )

    @action(detail=False, methods=["get", "patch", "delete"], url_path="me/shift")
    def my_shift(self, request):
        if not user_has_permission(request.user, "driver:accept_order"):
            return _forbidden(_("Requires permission: driver:accept_order"))
        shift = _active_shift(request.user)

        if request.method == "GET":
            return Response(DriverShiftSerializer(shift).data if shift else None)

        if request.method == "DELETE":
            if not shift:
                return Response(None)
            if _driver_has_active_trip(request.user):
                return _bad_request(
                    "DRIVER_BUSY", _("Finish your active trip before ending the shift.")
                )
            shift.ended_at = timezone.now()
            shift.status = DriverShift.Status.OFFLINE
            shift.save(update_fields=["ended_at", "status", "updated_at"])
            return Response(DriverShiftSerializer(shift).data)

        # PATCH -> start / switch the shift car (Р1)
        serializer = ShiftStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        car = serializer.validated_data["car"]
        if not car.drivers.filter(pk=request.user.pk).exists():
            return _forbidden(_("This car is not assigned to you."))
        if car.status != Car.Status.ACTIVE:
            return _bad_request("CAR_UNAVAILABLE", _("This car is not active."))
        if (
            DriverShift.objects.filter(car=car, ended_at__isnull=True)
            .exclude(driver=request.user)
            .exists()
        ):
            return _bad_request("CAR_BUSY", _("This car is already on another driver's shift."))
        try:
            with transaction.atomic():
                if shift:
                    if _driver_has_active_trip(request.user):
                        return _bad_request(
                            "DRIVER_BUSY", _("Finish your active trip before switching cars.")
                        )
                    shift.ended_at = timezone.now()
                    shift.status = DriverShift.Status.OFFLINE
                    shift.save(update_fields=["ended_at", "status", "updated_at"])
                shift = DriverShift.objects.create(
                    driver=request.user, car=car, status=DriverShift.Status.ONLINE
                )
        except IntegrityError:
            # AUDIT H3: the .exists() pre-check above is not atomic — a concurrent
            # shift can grab the car (one_active_shift_per_car) or the driver
            # (one_active_shift_per_driver) between check and create. The DB
            # constraint then fires; map it to a clean 400 instead of a 500.
            return _bad_request("CAR_BUSY", _("This car is already on another driver's shift."))
        return Response(DriverShiftSerializer(shift).data)

    @action(detail=False, methods=["post"], url_path="me/location")
    def my_location(self, request):
        if not user_has_permission(request.user, "driver:accept_order"):
            return _forbidden(_("Requires permission: driver:accept_order"))
        shift = _active_shift(request.user)
        if not shift:
            return _bad_request("NO_SHIFT", _("No active shift."))
        serializer = LocationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        shift.lat = serializer.validated_data["lat"]
        shift.lng = serializer.validated_data["lng"]
        shift.last_seen = timezone.now()
        shift.save(update_fields=["lat", "lng", "last_seen", "updated_at"])
        services.publish_driver_location(shift)
        return Response({"lat": shift.lat, "lng": shift.lng, "last_seen": shift.last_seen})

    @action(
        detail=False,
        methods=["post"],
        url_path="make-driver",
        permission_classes=[IsAuthenticated, HasPermission("driver:assign_to_user")],
    )
    def make_driver(self, request):
        target = User.objects.filter(pk=request.data.get("user_id")).first()
        if not target:
            return _bad_request("NOT_FOUND", _("User not found."))
        group, _created = AccessGroup.objects.get_or_create(name=DRIVER_GROUP)
        UserAccessGroup.objects.get_or_create(
            user=target, group=group, defaults={"assigned_by": request.user}
        )
        return Response({"status": "ok", "user_id": target.id})

    @action(
        detail=False,
        methods=["post"],
        url_path="remove-driver",
        permission_classes=[IsAuthenticated, HasPermission("driver:assign_to_user")],
    )
    def remove_driver(self, request):
        user_id = request.data.get("user_id")
        group = AccessGroup.objects.filter(name=DRIVER_GROUP).first()
        if group:
            UserAccessGroup.objects.filter(user_id=user_id, group=group).delete()
        return Response({"status": "ok", "user_id": user_id})


class VehicleReportViewSet(viewsets.ModelViewSet):
    serializer_class = VehicleReportSerializer
    http_method_names = ["get", "post", "head", "options"]
    filterset_fields = ["vehicle", "date"]

    def get_queryset(self):
        user = self.request.user
        qs = VehicleReport.objects.select_related("submitted_by", "vehicle").all()
        if user.is_superuser or user_has_permission(user, "vehicle_report:list"):
            return qs
        return qs.filter(submitted_by=user)

    def get_permissions(self):
        if self.action == "create":
            return [IsAuthenticated(), HasPermission("vehicle_report:create")()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        serializer.save(submitted_by=self.request.user)
