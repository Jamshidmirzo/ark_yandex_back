"""The main car-order CRUD + workflow ViewSet (submit / approve / reject / claim /
start / complete / cancel / release / reassign / extend / activity / my-active)."""

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from auth_core.permissions import HasPermission, user_has_permission
from car_orders import services
from car_orders.models import CarOrder, OrderMeta
from car_orders.serializers import (
    CarOrderActivitySerializer,
    CarOrderSerializer,
    CarOrderWriteSerializer,
    RouteEstimateSerializer,
)

from .base import _active_shift, _bad_request, _forbidden, _service_error_response

__all__ = ("CarOrderViewSet", "_can_manage_any_car_order")


def _can_manage_any_car_order(user):
    return (
        user.is_superuser
        or user_has_permission(user, "car_order:list")
        or user_has_permission(user, "car_order:approve")
    )


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

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        # Batch the overlay metas for a LIST so CarOrderSerializer.effective_status
        # resolves without an N+1 (one query for the filtered set, not per row).
        if self.action == "list":
            ids = list(
                self.filter_queryset(self.get_queryset()).values_list("pk", flat=True)
            )
            ctx["metas_by_order_id"] = {
                m.order_id: m for m in OrderMeta.objects.filter(order_id__in=ids)
            }
        return ctx

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
        # Direct create: a requester with car_order:create_direct (or a dispatcher
        # with car_order:approve) skips the draft→pending→approve dance — their own
        # brand-new order lands straight in the driver queue. This only fast-tracks
        # the order being created here; it grants no authority over anyone else's
        # orders (no claim/approve/reject), so a customer never sees driver actions.
        if user_has_permission(request.user, "car_order:create_direct") or user_has_permission(
            request.user, "car_order:approve"
        ):
            order.status = CarOrder.Status.AWAITING_DRIVER
            order.save(update_fields=["status", "updated_at"])
            services.record_sent(order, request.user)
            services.record_approved(order, request.user)
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
        The order moves to ``scheduled``; its window must not overlap another of
        the driver's (plus the travel buffer), else ``TIME_CONFLICT``."""
        try:
            order = services.orders.claim(pk, request.user)
        except services.orders.OrderError as exc:
            return _service_error_response(exc)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="start")
    def start(self, request, pk=None):
        """Driver begins a scheduled trip → ``in_progress`` (only one at a time)."""
        try:
            order = services.orders.start(pk, request.user)
        except services.orders.OrderError as exc:
            return _service_error_response(exc)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="complete")
    def complete(self, request, pk=None):
        """Assigned driver finishes the in-progress trip → ``completed``."""
        try:
            order = services.orders.complete(pk, request.user)
        except services.orders.OrderError as exc:
            return _service_error_response(exc)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        """Dispatcher (or author) cancels an order; frees the driver's window."""
        try:
            order = services.orders.cancel(pk, request.user, reason=request.data.get("reason", ""))
        except services.orders.OrderError as exc:
            return _service_error_response(exc)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="release")
    def release(self, request, pk=None):
        """Assigned driver hands an order back; it returns to ``awaiting_driver``."""
        try:
            order = services.orders.release(pk, request.user, reason=request.data.get("reason", ""))
        except services.orders.OrderError as exc:
            return _service_error_response(exc)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="reassign")
    def reassign(self, request, pk=None):
        """Dispatcher takes an order off its driver → ``awaiting_driver`` so a new
        car can pick it up (e.g. when the driver can't make the latest start)."""
        try:
            order = services.orders.reassign(pk, request.user)
        except services.orders.OrderError as exc:
            return _service_error_response(exc)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="extend")
    def extend(self, request, pk=None):
        """Add minutes to an active/scheduled order's duration and re-check the
        driver's next window. Allowed for the driver or a dispatcher."""
        try:
            minutes = int(request.data.get("minutes", 0))
        except (TypeError, ValueError):
            minutes = 0
        try:
            order, conflict = services.orders.extend(pk, request.user, minutes)
        except services.orders.OrderError as exc:
            return _service_error_response(exc)
        data = self._read(order)
        data["schedule_conflict"] = conflict
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
