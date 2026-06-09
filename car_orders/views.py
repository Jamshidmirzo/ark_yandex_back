"""API for the car-orders block.

Workflow (see ТЗ §3):
    draft → pending(submit) → awaiting_driver(admin-approve)
          → in_progress(claim, uses shift car) → completed
          → rejected (dispatcher reject / author cancel, before in_progress)

Permissions mirror ark-backend codenames (``car_order:*``, ``driver:*``,
``garage:*``, ``vehicle_report:*``). Р1 = shift car; Р3 = live location.
"""

from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from auth_core.models import AccessGroup, UserAccessGroup
from auth_core.permissions import HasPermission, user_has_permission
from car_orders import scheduling, services
from car_orders.models import (
    Car,
    CarOrder,
    CarType,
    DriverShift,
    OrderLiveLocation,
    OrderMeta,
    VehicleReport,
)
from car_orders.serializers import (
    CarOrderActivitySerializer,
    CarOrderSerializer,
    CarOrderWriteSerializer,
    CarSerializer,
    CarTypeSerializer,
    CarTypeWriteSerializer,
    CarWriteSerializer,
    DriverSerializer,
    DriverShiftSerializer,
    LocationSerializer,
    OrderMetaSerializer,
    RouteEstimateSerializer,
    ShiftStartSerializer,
    VehicleReportSerializer,
)

User = get_user_model()

DRIVER_GROUP = "Driver"


def _forbidden(message):
    return Response(
        {"error": {"code": "PERMISSION_DENIED", "message": str(message), "details": {}}},
        status=status.HTTP_403_FORBIDDEN,
    )


def _bad_request(code, message):
    return Response(
        {"error": {"code": code, "message": str(message), "details": {}}},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _conflict_payload(order):
    return {
        "order_id": order.id,
        "planned_start": order.planned_datetime,
        "planned_end": order.planned_end,
        "address": order.address,
    }


def _time_conflict(order):
    return Response(
        {
            "error": {
                "code": "TIME_CONFLICT",
                "message": _("This time window overlaps another of your orders."),
                "details": _conflict_payload(order),
            }
        },
        status=status.HTTP_409_CONFLICT,
    )


def _reset_driver_shift(driver):
    """Put a driver's active shift back to ONLINE (e.g. after their trip is
    cancelled / reassigned out from under them)."""
    if driver is None:
        return
    shift = DriverShift.objects.filter(driver=driver, ended_at__isnull=True).first()
    if shift and shift.status != DriverShift.Status.ONLINE:
        shift.status = DriverShift.Status.ONLINE
        shift.save(update_fields=["status", "updated_at"])


def _active_shift(user):
    return (
        DriverShift.objects.filter(driver=user, ended_at__isnull=True)
        .select_related("car", "car__type")
        .first()
    )


def _driver_has_active_trip(user):
    return CarOrder.objects.filter(driver=user, status=CarOrder.Status.IN_PROGRESS).exists()


def _can_manage_any_car_order(user):
    return (
        user.is_superuser
        or user_has_permission(user, "car_order:list")
        or user_has_permission(user, "car_order:approve")
    )


def _garage_permissions(action_name):
    mapping = {
        "create": "garage:create",
        "update": "garage:update",
        "partial_update": "garage:update",
        "destroy": "garage:delete",
    }
    codename = mapping.get(action_name, "garage:list")
    return [IsAuthenticated(), HasPermission(codename)()]


class EstimateView(APIView):
    """Standalone route/duration estimate, served locally in the gateway setup
    (no upstream auth needed — it's a pure function of two coordinates). Mounted
    at /api/v1/car-orders/estimate/ BEFORE the gateway catch-all."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RouteEstimateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        return Response(
            services.estimate_payload(
                data["origin_lat"],
                data["origin_lng"],
                data["dest_lat"],
                data["dest_lng"],
                service_minutes=data.get("service_minutes"),
            )
        )


class LiveLocationView(APIView):
    """Live driver position for an order, served locally (gateway/hybrid setup).
    GET returns the latest position or null; POST upserts {lat, lng}. AllowAny so
    the simulator / driver app can push without the demo JWT for now. Mounted at
    /api/v1/car-orders/<id>/live-location/ BEFORE the gateway catch-all."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request, pk):
        loc = OrderLiveLocation.objects.filter(order_id=pk).first()
        if not loc:
            return Response(None)
        return Response(
            {
                "lat": loc.lat,
                "lng": loc.lng,
                "last_seen": loc.last_seen,
                "geometry": loc.geometry,
            }
        )

    def post(self, request, pk):
        serializer = LocationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        defaults = {
            "lat": serializer.validated_data["lat"],
            "lng": serializer.validated_data["lng"],
            "last_seen": timezone.now(),
        }
        geometry = request.data.get("geometry")
        if geometry is not None:
            defaults["geometry"] = geometry
        loc, _ = OrderLiveLocation.objects.update_or_create(order_id=pk, defaults=defaults)

        # Push the new position to any connected trackers (real-time, no polling).
        layer = get_channel_layer()
        if layer is not None:
            from car_orders.ws import group_name

            data = {"lat": loc.lat, "lng": loc.lng, "last_seen": loc.last_seen.isoformat()}
            if geometry is not None:  # carry the route on the first push
                data["geometry"] = geometry
            async_to_sync(layer.group_send)(
                group_name(pk),
                {"type": "location.update", "data": data},
            )
        return Response({"lat": loc.lat, "lng": loc.lng, "last_seen": loc.last_seen})


class OrderMetaView(APIView):
    """Local feature overlay for an order (coords / window / trip state), keyed by
    the demo order id. GET returns it or null; POST upserts the provided fields.
    AllowAny for now (the frontend sends the driver id). Mounted before the
    gateway catch-all."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request, pk):
        meta = OrderMeta.objects.filter(order_id=pk).first()
        if not meta:
            return Response(None)
        return Response(OrderMetaSerializer(meta).data)

    def post(self, request, pk):
        serializer = OrderMetaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        meta, _ = OrderMeta.objects.update_or_create(
            order_id=pk, defaults=serializer.validated_data
        )
        return Response(OrderMetaSerializer(meta).data)


class CarOrderViewSet(viewsets.ModelViewSet):
    """CRUD + workflow actions for car orders."""

    search_fields = ["address", "note", "project_name"]
    ordering_fields = ["created_at", "planned_datetime", "status"]
    filterset_fields = ["status"]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        qs = CarOrder.objects.select_related(
            "car_type", "car", "car__type", "driver", "created_by", "rejected_by"
        ).prefetch_related("car__drivers")
        user = self.request.user
        if _can_manage_any_car_order(user):
            return qs
        visibility = models.Q(created_by=user) | models.Q(driver=user)
        if user_has_permission(user, "driver:accept_order"):
            shift = _active_shift(user)
            if shift:
                visibility |= models.Q(
                    status=CarOrder.Status.AWAITING_DRIVER, car_type=shift.car.type_id
                )
        return qs.filter(visibility).distinct()

    def get_serializer_class(self):
        if self.action in ("create", "partial_update"):
            return CarOrderWriteSerializer
        if self.action == "activity":
            return CarOrderActivitySerializer
        return CarOrderSerializer

    # Per-action permissions. Centralised here because overriding
    # get_permissions bypasses any permission_classes set on @action.
    _action_permissions = {
        "create": ["car_order:create"],
        "estimate": ["car_order:create"],
        "admin_approve": ["car_order:approve"],
        "reassign": ["car_order:approve"],
        "claim": ["driver:accept_order"],
        "release": ["driver:accept_order"],
        "start": ["driver:trip_control"],
        "complete": ["driver:trip_control"],
    }

    def get_permissions(self):
        perms = [IsAuthenticated()]
        for codename in self._action_permissions.get(self.action, []):
            perms.append(HasPermission(codename)())
        return perms

    def _read(self, order):
        return CarOrderSerializer(order, context=self.get_serializer_context()).data

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = serializer.save(created_by=request.user, status=CarOrder.Status.DRAFT)
        services.record_created(order, request.user)
        return Response(self._read(order), status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        order = self.get_object()
        if order.status != CarOrder.Status.DRAFT:
            return _bad_request("INVALID_STATUS", _("Only draft orders can be edited."))
        if order.created_by_id != request.user.id:
            return _forbidden(_("Only the creator can edit a draft car order."))
        serializer = self.get_serializer(order, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        order = serializer.save()
        return Response(self._read(order))

    def destroy(self, request, *args, **kwargs):
        order = self.get_object()
        if order.status != CarOrder.Status.DRAFT:
            return _bad_request("INVALID_STATUS", _("Only draft orders can be deleted."))
        is_admin = request.user.is_superuser or user_has_permission(request.user, "administrator")
        if order.created_by_id != request.user.id and not is_admin:
            return _forbidden(_("Only the creator or an administrator can delete this draft."))
        order.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], url_path="submit")
    def submit(self, request, pk=None):
        order = self.get_object()
        if order.created_by_id != request.user.id:
            return _forbidden(_("Only the creator can submit this order."))
        if order.status != CarOrder.Status.DRAFT:
            return _bad_request("INVALID_STATUS", _("Only a draft can be submitted."))
        order.status = CarOrder.Status.PENDING
        order.save(update_fields=["status", "updated_at"])
        services.record_sent(order, request.user)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="admin-approve")
    def admin_approve(self, request, pk=None):
        order = self.get_object()
        if order.status != CarOrder.Status.PENDING:
            return _bad_request("INVALID_STATUS", _("Only a pending order can be approved."))
        order.status = CarOrder.Status.AWAITING_DRIVER
        order.save(update_fields=["status", "updated_at"])
        services.record_approved(order, request.user)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        order = self.get_object()
        if order.status not in (CarOrder.Status.PENDING, CarOrder.Status.AWAITING_DRIVER):
            return _bad_request(
                "INVALID_STATUS", _("This order can no longer be rejected or cancelled.")
            )
        is_author = order.created_by_id == request.user.id
        can_reject = user_has_permission(request.user, "car_order:reject")
        if not (is_author or can_reject):
            return _forbidden(_("You cannot reject this order."))
        order.status = CarOrder.Status.REJECTED
        order.rejected_at = timezone.now()
        order.rejected_by = request.user
        order.reject_reason = request.data.get("reason", "")
        order.save(
            update_fields=["status", "rejected_at", "rejected_by", "reject_reason", "updated_at"]
        )
        services.record_rejected(order, request.user, reason=order.reject_reason)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="claim")
    def claim(self, request, pk=None):
        """Driver reserves an awaiting order into their schedule (Р1: shift car).

        The order moves to ``scheduled`` (the time window is reserved); the
        driver starts it later with ``/start/``. A scheduled/in-progress order
        with a planned window must not overlap another of the driver's windows
        (plus the travel buffer) — otherwise we return ``TIME_CONFLICT``.
        """
        with transaction.atomic():
            try:
                order = CarOrder.objects.select_for_update().get(pk=pk)
            except CarOrder.DoesNotExist:
                return _bad_request("NOT_FOUND", _("Order not found."))
            if order.status != CarOrder.Status.AWAITING_DRIVER:
                return _bad_request("ALREADY_TAKEN", _("This order is no longer available."))
            shift = _active_shift(request.user)
            if shift is None:
                return _bad_request(
                    "NO_SHIFT", _("Select a car for your shift before accepting orders.")
                )
            if order.car_type_id and shift.car.type_id != order.car_type_id:
                return _bad_request(
                    "TYPE_MISMATCH", _("Your shift car does not match the requested type.")
                )
            window = scheduling.order_window(order)
            if window:
                conflict = scheduling.find_time_conflict(request.user, window[0], window[1])
                if conflict:
                    return _time_conflict(conflict)
            order.status = CarOrder.Status.SCHEDULED
            order.driver = request.user
            order.car = shift.car
            order.save(update_fields=["status", "driver", "car", "updated_at"])
        services.record_accepted(order, request.user)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="start")
    def start(self, request, pk=None):
        """Driver begins a scheduled trip → ``in_progress`` (only one at a time)."""
        order = CarOrder.objects.filter(pk=pk).first()
        if order is None:
            return _bad_request("NOT_FOUND", _("Order not found."))
        if order.driver_id != request.user.id:
            return _forbidden(_("Only the assigned driver can start this trip."))
        if order.status != CarOrder.Status.SCHEDULED:
            return _bad_request("INVALID_STATUS", _("Only a scheduled order can be started."))
        active = scheduling.active_trip(request.user, exclude_id=order.pk)
        if active is not None:
            return _bad_request(
                "DRIVER_BUSY", _("Finish your current trip before starting another.")
            )
        order.status = CarOrder.Status.IN_PROGRESS
        order.started_at = timezone.now()
        order.save(update_fields=["status", "started_at", "updated_at"])
        shift = _active_shift(request.user)
        if shift:
            shift.status = DriverShift.Status.EN_ROUTE
            shift.save(update_fields=["status", "updated_at"])
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="complete")
    def complete(self, request, pk=None):
        # Resolve directly (not via the visibility queryset) so a driver who is
        # not the assignee gets an explicit 403, per ТЗ §5.4 "only_assigned_driver".
        order = CarOrder.objects.filter(pk=pk).first()
        if order is None:
            return _bad_request("NOT_FOUND", _("Order not found."))
        if order.driver_id != request.user.id:
            return _forbidden(_("Only the assigned driver can complete this trip."))
        if order.status != CarOrder.Status.IN_PROGRESS:
            return _bad_request("INVALID_STATUS", _("Only an in-progress trip can be completed."))
        order.status = CarOrder.Status.COMPLETED
        order.finished_at = timezone.now()
        order.save(update_fields=["status", "finished_at", "updated_at"])
        shift = _active_shift(request.user)
        if shift:
            shift.status = DriverShift.Status.ONLINE
            shift.save(update_fields=["status", "updated_at"])
        services.record_completed(order, request.user)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        """Dispatcher (or author) cancels an order; frees the driver's window."""
        order = CarOrder.objects.filter(pk=pk).first()
        if order is None:
            return _bad_request("NOT_FOUND", _("Order not found."))
        terminal = (
            CarOrder.Status.COMPLETED,
            CarOrder.Status.REJECTED,
            CarOrder.Status.CANCELLED,
        )
        if order.status in terminal:
            return _bad_request("INVALID_STATUS", _("This order can no longer be cancelled."))
        is_author = order.created_by_id == request.user.id
        can_cancel = user_has_permission(request.user, "car_order:reject")
        if not (is_author or can_cancel):
            return _forbidden(_("You cannot cancel this order."))
        driver = order.driver
        order.status = CarOrder.Status.CANCELLED
        order.save(update_fields=["status", "updated_at"])
        _reset_driver_shift(driver)
        services.record_cancelled(order, request.user, reason=request.data.get("reason", ""))
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="release")
    def release(self, request, pk=None):
        """Assigned driver hands an order back; it returns to ``awaiting_driver``."""
        order = CarOrder.objects.filter(pk=pk).first()
        if order is None:
            return _bad_request("NOT_FOUND", _("Order not found."))
        if order.driver_id != request.user.id:
            return _forbidden(_("Only the assigned driver can release this order."))
        if order.status not in (CarOrder.Status.SCHEDULED, CarOrder.Status.IN_PROGRESS):
            return _bad_request("INVALID_STATUS", _("This order cannot be released."))
        driver = order.driver
        order.status = CarOrder.Status.AWAITING_DRIVER
        order.driver = None
        order.car = None
        order.started_at = None
        order.save(update_fields=["status", "driver", "car", "started_at", "updated_at"])
        _reset_driver_shift(driver)
        services.record_released(order, request.user, reason=request.data.get("reason", ""))
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="reassign")
    def reassign(self, request, pk=None):
        """Dispatcher takes an order off its driver → ``awaiting_driver`` so a new
        car can pick it up (e.g. when the driver can't make the latest start)."""
        order = CarOrder.objects.filter(pk=pk).first()
        if order is None:
            return _bad_request("NOT_FOUND", _("Order not found."))
        if order.status not in (CarOrder.Status.SCHEDULED, CarOrder.Status.IN_PROGRESS):
            return _bad_request("INVALID_STATUS", _("This order cannot be reassigned."))
        from_driver = order.driver
        order.status = CarOrder.Status.AWAITING_DRIVER
        order.driver = None
        order.car = None
        order.started_at = None
        order.save(update_fields=["status", "driver", "car", "started_at", "updated_at"])
        _reset_driver_shift(from_driver)
        services.record_reassigned(
            order, request.user, from_driver_id=from_driver.id if from_driver else None
        )
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="extend")
    def extend(self, request, pk=None):
        """Add minutes to the estimated duration of an active/scheduled order and
        re-check the driver's next window. Allowed for the driver or a dispatcher."""
        order = CarOrder.objects.filter(pk=pk).first()
        if order is None:
            return _bad_request("NOT_FOUND", _("Order not found."))
        is_driver = order.driver_id == request.user.id
        can_manage = user_has_permission(request.user, "car_order:approve")
        if not (is_driver or can_manage):
            return _forbidden(_("You cannot extend this order."))
        if order.status not in (CarOrder.Status.SCHEDULED, CarOrder.Status.IN_PROGRESS):
            return _bad_request("INVALID_STATUS", _("Only an active order can be extended."))
        try:
            minutes = int(request.data.get("minutes", 0))
        except (TypeError, ValueError):
            minutes = 0
        if minutes <= 0:
            return _bad_request("VALIDATION", _("`minutes` must be a positive integer."))
        order.estimated_duration = (order.estimated_duration or timedelta()) + timedelta(
            minutes=minutes
        )
        order.save(update_fields=["estimated_duration", "updated_at"])
        services.record_extended(order, request.user, minutes)
        conflict = None
        window = scheduling.order_window(order)
        if window and order.driver_id:
            conflict = scheduling.find_time_conflict(
                order.driver, window[0], window[1], exclude_id=order.pk
            )
        data = self._read(order)
        data["schedule_conflict"] = _conflict_payload(conflict) if conflict else None
        return Response(data)

    @action(detail=False, methods=["post"], url_path="estimate")
    def estimate(self, request):
        """Auto-estimate route + duration for the create-order card.

        Body: ``{origin_lat, origin_lng, dest_lat, dest_lng, service_minutes?}``.
        """
        serializer = RouteEstimateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        return Response(
            services.estimate_payload(
                data["origin_lat"],
                data["origin_lng"],
                data["dest_lat"],
                data["dest_lng"],
                service_minutes=data.get("service_minutes"),
            )
        )

    @action(detail=True, methods=["get"], url_path="activity")
    def activity(self, request, pk=None):
        order = self.get_object()
        qs = order.activities.select_related("actor").all()
        return Response(CarOrderActivitySerializer(qs, many=True).data)

    @action(detail=False, methods=["get"], url_path="me/active-order")
    def my_active_order(self, request):
        order = (
            self.get_queryset()
            .filter(driver=request.user, status=CarOrder.Status.IN_PROGRESS)
            .first()
        )
        return Response(self._read(order) if order else None)


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
