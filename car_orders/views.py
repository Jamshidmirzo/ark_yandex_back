"""API for the car-orders block.

Workflow (see ТЗ §3):
    draft → pending(submit) → awaiting_driver(admin-approve)
          → in_progress(claim, uses shift car) → completed
          → rejected (dispatcher reject / author cancel, before in_progress)

Permissions mirror ark-backend codenames (``car_order:*``, ``driver:*``,
``garage:*``, ``vehicle_report:*``). Р1 = shift car; Р3 = live location.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from auth_core.models import AccessGroup, UserAccessGroup
from auth_core.permissions import HasPermission, user_has_permission
from car_orders import dispatch, geometry, scheduling, services
from car_orders.models import (
    Car,
    CarOrder,
    CarType,
    DispatchSettings,
    DriverPosition,
    DriverShift,
    DriverShiftState,
    OrderLiveLocation,
    OrderMeta,
    VehicleReport,
)
from car_orders.permissions import (
    OverlayAuthenticated,
    OverlayDispatcher,
    acting_driver_id,
    assignee_driver_id,
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
from car_orders.ws import broadcast_location
from config.auth import DemoTokenAuthentication

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


def _service_error_response(exc):
    """Map a service-layer error (``OrderError`` / ``OverlayError``) onto the standard
    error response, honouring its code / HTTP-status / details (403 → the shared
    PERMISSION_DENIED shape)."""
    if exc.http_status == status.HTTP_403_FORBIDDEN:
        return _forbidden(exc.message)
    return Response(
        {"error": {"code": exc.code, "message": str(exc.message), "details": exc.details}},
        status=exc.http_status,
    )


def _src(request):
    """Where a request came from, for the tracking log: ``📱 <ip>`` (a real phone)
    vs ``🖥 локально`` (our own server / the simulator on 127.0.0.1)."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    ip = xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR", "?")
    return "🖥 локально" if ip in ("127.0.0.1", "::1", "localhost") else f"📱 {ip}"


def _log_tracking(message):
    """Console line for GPS heartbeats / trip-state changes — so you can watch in
    real time what the mobile app sends (and tell it apart from our own/simulator
    traffic). Toggle with settings.LOG_TRACKING."""
    from django.conf import settings

    if getattr(settings, "LOG_TRACKING", False):
        print(message, flush=True)


@csrf_exempt
def admin_approve_overlay(request, pk):
    """Server hook on demo admin-approve: forward the call to demo and, on success,
    flip OUR OrderMeta to ``dispatchable=True`` so the auto-dispatcher picks the
    now-approved order up — regardless of which client approved it. Mounted before
    the gateway catch-all. (The web form already sets the flag; this guarantees it
    for any other approve path.)"""
    from config.gateway import gateway

    resp = gateway(request, f"car-orders/{pk}/admin-approve/")
    if 200 <= resp.status_code < 300:
        OrderMeta.objects.update_or_create(order_id=int(pk), defaults={"dispatchable": True})
    return resp


def _notify_dropped_driver(driver_id, order_id):
    return services.events.notify_dropped_driver(driver_id, order_id)


def _reset_driver_shift(driver):
    return services.shift.reset_driver_shift(driver)


def _active_shift(user):
    return services.shift.active_shift(user)


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

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

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


class FleetLiveView(APIView):
    """Dispatcher dashboard snapshot — every active order with its live position +
    risk flags, for «Диспетчерская». Live updates come over the fleet WebSocket
    (/ws/car-orders/fleet/)."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def get(self, request):
        from car_orders.fleet import fleet_live_orders

        return Response({"orders": fleet_live_orders()})


class LiveLocationView(APIView):
    """Live driver position for an order, served locally (gateway/hybrid setup).
    GET returns the latest position or null; POST upserts {lat, lng}. Stays
    AllowAny — the auto-simulator pushes here without a demo JWT, and the data is
    keyed by order id. Mounted at /api/v1/car-orders/<id>/live-location/ BEFORE
    the gateway catch-all."""

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
        loc, _created = OrderLiveLocation.objects.update_or_create(order_id=pk, defaults=defaults)

        # Push the new position to connected trackers + the fleet dashboard.
        data = {"lat": loc.lat, "lng": loc.lng, "last_seen": loc.last_seen.isoformat()}
        if geometry is not None:  # carry the route on the first push
            data["geometry"] = geometry
        broadcast_location(pk, data)
        _log_tracking(f"🛰 LIVE [{_src(request)}] #{pk} ({loc.lat:.5f},{loc.lng:.5f})")
        return Response({"lat": loc.lat, "lng": loc.lng, "last_seen": loc.last_seen})


class OrderMetaView(APIView):
    """Local feature overlay for an order (coords / window / trip state), keyed by
    the demo order id. GET returns it or null; POST upserts the provided fields.
    AllowAny for now (the frontend sends the driver id). Mounted before the
    gateway catch-all."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def get(self, request, pk):
        meta = OrderMeta.objects.filter(order_id=pk).first()
        if not meta:
            return Response(None)
        return Response(OrderMetaSerializer(meta).data)

    def post(self, request, pk):
        serializer = OrderMetaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        meta, _created = OrderMeta.objects.update_or_create(
            order_id=pk, defaults=serializer.validated_data
        )
        return Response(OrderMetaSerializer(meta).data)


class ClaimCheckView(APIView):
    """Scheduling pre-check before a driver claims an order (overlay/hybrid).

    Body: ``{driver_id}``. Reads the order's saved window from OrderMeta and
    checks it against the driver's other committed orders (+ travel buffer).
    Returns ``{ok, conflict}`` — so a driver CAN take a second order when it fits
    a free gap, instead of a blanket "you already have an order"."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def post(self, request, pk):
        # Check is FOR the candidate driver (dispatcher picks them in the body),
        # not the dispatcher running the check.
        driver_id = assignee_driver_id(request, self)
        meta = OrderMeta.objects.filter(order_id=pk).first()
        # No saved window → nothing to schedule against; allow.
        if not meta or not meta.planned_datetime or not meta.estimated_duration:
            return Response({"ok": True, "conflict": None})
        new_end = scheduling.driving_end(meta.planned_datetime, meta.planned_end, meta.service_time)
        conflict = scheduling.meta_conflict(
            driver_id, meta.planned_datetime, new_end, exclude_order_id=int(pk)
        )
        if conflict is None:
            return Response({"ok": True, "conflict": None})
        return Response(
            {
                "ok": False,
                "conflict": {
                    "order_id": conflict.order_id,
                    "planned_start": conflict.planned_datetime,
                    "planned_end": conflict.planned_end,
                    "address": f"Заказ #{conflict.order_id}",
                },
            }
        )


class MetaBatchView(APIView):
    """Batch read of OrderMeta for a set of order ids, so the list can compute the
    effective (overlay) status per row. Body: ``{order_ids: [...]}``."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def post(self, request):
        order_ids = request.data.get("order_ids") or []
        metas = OrderMeta.objects.filter(order_id__in=order_ids)
        return Response({"results": OrderMetaSerializer(metas, many=True).data})


class ClaimCheckBatchView(APIView):
    """Batch window check: for a list of order ids, which ones fit the driver's
    schedule (so the list can show «можно взять» / «пересекается»).
    Body: ``{driver_id, order_ids: [...]}`` → ``{results: [{order_id, ok, conflict}]}``."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def post(self, request):
        # Check is FOR the candidate driver (body), not the dispatcher running it.
        driver_id = assignee_driver_id(request, self)
        order_ids = request.data.get("order_ids") or []
        metas = {m.order_id: m for m in OrderMeta.objects.filter(order_id__in=order_ids)}
        results = []
        for oid in order_ids:
            meta = metas.get(oid)
            if meta is None or not meta.planned_datetime or not meta.estimated_duration:
                results.append({"order_id": oid, "ok": True, "conflict": None})
                continue
            new_end = scheduling.driving_end(
                meta.planned_datetime, meta.planned_end, meta.service_time
            )
            conflict = scheduling.meta_conflict(
                driver_id, meta.planned_datetime, new_end, exclude_order_id=int(oid)
            )
            results.append(
                {
                    "order_id": oid,
                    "ok": conflict is None,
                    "conflict": None
                    if conflict is None
                    else {
                        "order_id": conflict.order_id,
                        "planned_start": conflict.planned_datetime,
                        "planned_end": conflict.planned_end,
                    },
                }
            )
        return Response({"results": results})


class OverlayClaimView(APIView):
    """Claim an order in OUR layer (not demo), so a driver can take a second
    order with the SAME car sequentially — which the demo backend forbids
    (one car / one driver per active order). Runs the window conflict check
    first; on success records driver + car on the OrderMeta. demo stays the
    source of login/base data."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def post(self, request, pk):
        # The assignee — a dispatcher assigns to the CHOSEN driver (body), a driver
        # self-claims their own (token). NOT the acting user, or a dispatcher's
        # assignment would be claimed for the dispatcher.
        driver_id = assignee_driver_id(request, self)
        try:
            meta = services.overlay.claim(
                pk, driver_id, request.data.get("car_id"), request.data.get("car_label", "")
            )
        except services.overlay.OverlayError as exc:
            return _service_error_response(exc)
        return Response({"ok": True, "conflict": None, "meta": OrderMetaSerializer(meta).data})


class TripStateView(APIView):
    """Advance the richer trip state (overlay): to_client / at_client / in_trip /
    at_destination / waiting / completed. Authoritative: only the assigned driver
    (or a dispatcher) may advance it, transitions must follow the flow, and arrival
    stages are geofenced. Thin HTTP adapter over ``services.trip_state``."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def post(self, request, pk):
        try:
            meta = services.trip_state.advance(
                int(pk),
                request.data.get("trip_state"),
                actor_driver_id=acting_driver_id(request),
                is_dispatcher=OverlayDispatcher().has_permission(request, self),
            )
        except services.trip_state.TripStateError as exc:
            if exc.http_status == status.HTTP_403_FORBIDDEN:
                return _forbidden(exc.message)
            return _bad_request(exc.code, exc.message)
        _log_tracking(
            f"🚦 STATUS [{_src(request)}] #{pk} driver={meta.driver_id} → {meta.trip_state}"
        )
        return Response(OrderMetaSerializer(meta).data)


class OverlayReleaseView(APIView):
    """Tear down the overlay for an order — call it on demo reject / cancel /
    release / reassign / done. Clears the claim so the order stops blocking the
    driver's schedule and the auto-simulator, and pushes a ``cancelled`` state
    over the WebSocket. Idempotent."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def post(self, request, pk):
        meta = services.overlay.release(pk, requeue=bool(request.data.get("requeue")))
        if meta is None:
            return Response({"ok": True})
        return Response({"ok": True, "meta": OrderMetaSerializer(meta).data})


class ExtendView(APIView):
    """Add minutes to an order's planned duration in OUR overlay (demo doesn't
    store the window). Pushes ``planned_end`` out and re-checks the driver's next
    window. Body: ``{minutes}`` → ``{ok, meta, conflict}``. The extension is always
    applied; ``conflict`` is a warning the new end overlaps the driver's next order.
    Allowed for the driver or a dispatcher (the frontend gates the button)."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def post(self, request, pk):
        try:
            minutes = int(request.data.get("minutes", 0))
        except (TypeError, ValueError):
            minutes = 0
        try:
            meta, conflict = services.overlay.extend(pk, minutes)
        except services.overlay.OverlayError as exc:
            return _service_error_response(exc)
        return Response({"ok": True, "meta": OrderMetaSerializer(meta).data, "conflict": conflict})


class ReassignView(APIView):
    """Dispatcher takes an order off its driver and returns it to the queue
    (overlay). Frees our claim — the order stops blocking the driver's schedule
    and the simulator — and pushes a ``cancelled`` trip-state over the WebSocket,
    so another driver can pick it up. A plain demo claim is owned by demo and
    can't be reassigned from here (only overlay-claimed orders). Idempotent."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayDispatcher]

    def post(self, request, pk):
        try:
            meta = services.overlay.reassign(pk)
        except services.overlay.OverlayError as exc:
            return _service_error_response(exc)
        return Response({"ok": True, "meta": OrderMetaSerializer(meta).data})


def _apply_driver_location(driver_id, lat, lng, src=""):
    """Store the driver's position and attach it to their ACTIVE (non-terminal)
    order — NOT only the moving stages. With «1 водитель = 1 активный заказ» that's
    exactly their current order, so live phone GPS drives the map in every stage
    (assigned / en route / parked). Shared by the single + batch location endpoints.
    Returns the order ids whose live position was updated."""
    now = timezone.now()
    # Never attach to driver_id=None — a None filter matches EVERY driverless order
    # and smears one phone's GPS across all of them. The caller must identify the
    # driver (token or body driver_id).
    if driver_id is None:
        _log_tracking(f"📍 GPS [{src}] БЕЗ driver_id — пропущено (телефон не опознан)")
        return []
    DriverPosition.objects.update_or_create(
        driver_id=driver_id, defaults={"lat": lat, "lng": lng, "last_seen": now}
    )
    terminal = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)
    metas = list(OrderMeta.objects.filter(driver_id=driver_id).exclude(trip_state__in=terminal))
    # Stages where the driver is DRIVING — we keep the route fresh on these.
    moving = (
        OrderMeta.TripState.ASSIGNED,
        OrderMeta.TripState.TO_CLIENT,
        OrderMeta.TripState.IN_TRIP,
    )
    from car_orders import dispatch

    updated = []
    for meta in metas:
        prev = OrderLiveLocation.objects.filter(order_id=meta.order_id).first()
        moved_m = (
            geometry.haversine_km(prev.lat, prev.lng, lat, lng) * 1000
            if (prev and prev.lat is not None)
            else float("inf")
        )
        updated.append(meta.order_id)
        # Parked / GPS jitter: hasn't really moved since the last SHOWN point → keep
        # the marker and line exactly where they are (don't redraw — that was the
        # in-place flicker), just keep the fix fresh. Compare vs the last shown point
        # (not the last frame) so a slow crawl still accumulates and eventually updates.
        if prev is not None and moved_m < geometry.MIN_MOVE_M:
            OrderLiveLocation.objects.filter(order_id=meta.order_id).update(last_seen=now)
            continue
        loc, _ = OrderLiveLocation.objects.update_or_create(
            order_id=meta.order_id, defaults={"lat": lat, "lng": lng, "last_seen": now}
        )
        broadcast_location(meta.order_id, {"lat": lat, "lng": lng, "last_seen": now.isoformat()})
        # RE-ROUTE on deviation: recompute the polyline from the LIVE position when
        # there's no route yet OR the driver has strayed >80 m off the current one
        # (turned the «wrong» way) — so it redraws along the road they actually took.
        if meta.trip_state in moving:
            deviated = (
                True
                if not loc.geometry
                else geometry.min_dist_km_to_polyline(lat, lng, loc.geometry) > 0.08
            )
            if deviated:
                dispatch.push_order_route(meta, driver_pos=(lat, lng))
            elif loc.geometry:
                # On-route & actually moved: trim the canonical line to what's ahead
                # and pin its start to the car. Smooth follow without OSRM per frame.
                broadcast_location(
                    meta.order_id,
                    {"geometry": geometry.trim_geometry(loc.geometry, lat, lng)},
                )
    _log_tracking(
        f"📍 GPS [{src}] driver={driver_id} ({lat:.5f},{lng:.5f}) → "
        + (
            ", ".join(f"#{m.order_id} [{m.trip_state}]" for m in metas)
            if metas
            else "нет активного заказа"
        )
    )
    return updated


class DriverLocationView(APIView):
    """The driver app posts its GPS ONCE here; the server attaches it to the
    driver's ACTIVE order and fans it out over WebSocket — so the mobile app
    doesn't need to know which order id to send to (it just streams its position).
    Body: ``{driver_id, lat, lng}`` → ``{updated_orders: [...]}``."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def post(self, request):
        driver_id = acting_driver_id(request, request.data.get("driver_id"))
        serializer = LocationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = _apply_driver_location(
            driver_id, serializer.validated_data["lat"], serializer.validated_data["lng"], _src(request)
        )
        return Response({"updated_orders": updated})


class DriverShiftView(APIView):
    """Local OVERLAY «driver on shift» (Р1) — demo has no set-shift endpoint, so we
    keep it locally by demo driver id. GET current shift / PATCH go on shift (pick a
    car) / DELETE end. Mounted at /drivers/me/shift/ BEFORE the gateway catch-all."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def get(self, request):
        driver_id = acting_driver_id(request, request.query_params.get("driver_id"))
        s = DriverShiftState.objects.filter(driver_id=driver_id).first() if driver_id else None
        return Response(s.as_shift() if s else None)

    def patch(self, request):
        """Go on shift OR swap the shift car. Swapping (an existing shift, a
        different car) is the «drove to the garage, changed the car, back on the
        line» flow — but it is BLOCKED while the driver still has ANY active
        (non-terminal) order: let them finish (or hand off) their work first, then
        change cars. This avoids splitting a half-done schedule across two
        vehicles. Re-selecting the SAME car isn't a change, so it's never blocked."""
        driver_id = acting_driver_id(request, request.data.get("driver_id"))
        car_id = request.data.get("car_id")
        if driver_id is None or car_id is None:
            return _bad_request("VALIDATION", _("driver and car_id are required."))

        def _int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        # Car type is REQUIRED — the dispatcher/auto-dispatcher matches orders by
        # car type, so an on-shift driver without a type is silently un-dispatchable.
        new_car_type = _int(request.data.get("car_type_id"))
        if new_car_type is None:
            return _bad_request(
                "VALIDATION",
                _("car_type_id is required to go on shift (orders are matched by car type)."),
            )
        new_car_id = _int(car_id)
        existing = DriverShiftState.objects.filter(driver_id=driver_id).first()
        changing = existing is not None and existing.car_id != new_car_id

        if changing:
            terminal = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)
            active = (
                OrderMeta.objects.filter(driver_id=driver_id)
                .exclude(trip_state__in=terminal)
                .count()
            )
            if active:
                return _bad_request(
                    "HAS_ACTIVE_ORDERS",
                    _("Finish your %(n)s active order(s) before changing cars.")
                    % {"n": active},
                )

        s, _created = DriverShiftState.objects.update_or_create(
            driver_id=driver_id,
            defaults={
                "car_id": new_car_id,
                "car_model": request.data.get("car_model", ""),
                "car_plate": request.data.get("car_plate", ""),
                "car_type_id": new_car_type,
                "car_type_name": request.data.get("car_type_name", ""),
                "status": "online",
            },
        )
        return Response(s.as_shift())

    def delete(self, request):
        driver_id = acting_driver_id(
            request, request.data.get("driver_id") or request.query_params.get("driver_id")
        )
        if driver_id is None:
            return Response(None)
        # Don't strand an in-flight order: refuse to end the shift while the driver
        # still has an active (non-terminal) order — finish or hand it off first.
        terminal = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)
        active = (
            OrderMeta.objects.filter(driver_id=driver_id).exclude(trip_state__in=terminal).count()
        )
        if active:
            return _bad_request(
                "HAS_ACTIVE_ORDERS",
                _("Finish your %(n)s active order(s) before ending the shift.") % {"n": active},
            )
        DriverShiftState.objects.filter(driver_id=driver_id).delete()
        return Response(None)


class DriverShiftsView(APIView):
    """All active overlay shifts → `{ "671": {car_id, car_type_id, car_model, …} }`.
    The dispatcher merges this into the driver roster so an on-shift driver becomes a
    candidate with the right car type."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def get(self, request):
        return Response(
            {
                str(s.driver_id): {
                    "car_id": s.car_id,
                    "car_model": s.car_model,
                    "car_plate": s.car_plate,
                    "car_type_id": s.car_type_id,
                    "car_type_name": s.car_type_name,
                    "status": s.status,
                }
                for s in DriverShiftState.objects.all()
            }
        )


class AutoDispatchView(APIView):
    """Runtime on/off switch for the server-side auto-dispatch worker, so the
    dispatcher can flip auto-assignment from the «Диспетчерская» page.

      GET  → current state (anyone authenticated may read)
      POST → {"enabled": bool}  set the switch (dispatcher-only)

    `enabled` is the dispatcher toggle; `effective` also factors in the env-var
    master kill-switch and is what the worker actually obeys."""

    authentication_classes = [DemoTokenAuthentication]

    def get_permissions(self):
        # Reading the state is fine for any dispatcher tab; flipping it is gated.
        if self.request.method == "POST":
            return [OverlayDispatcher()]
        return [OverlayAuthenticated()]

    def _state(self):
        from django.conf import settings

        cfg = DispatchSettings.load()
        return Response(
            {
                "enabled": cfg.auto_enabled,
                "env_enabled": bool(getattr(settings, "AUTO_DISPATCH_ENABLED", True)),
                "effective": dispatch.auto_enabled(),
                "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
                "updated_by": cfg.updated_by,
            }
        )

    def get(self, request):
        return self._state()

    def post(self, request):
        enabled = request.data.get("enabled")
        if not isinstance(enabled, bool):
            return Response(
                {"detail": "`enabled` (bool) is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        cfg = DispatchSettings.load()
        cfg.auto_enabled = enabled
        cfg.updated_by = acting_driver_id(request)
        cfg.save()
        return self._state()


class DriverPositionsView(APIView):
    """Latest position per driver → `{ "671": {lat, lng, last_seen}, ... }`. Powers
    the dispatcher's «nearest free driver» suggestion. Optional `?max_age=600`
    (seconds) drops stale fixes."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def get(self, request):
        qs = DriverPosition.objects.all()
        try:
            max_age = int(request.query_params.get("max_age", 0))
        except (TypeError, ValueError):
            max_age = 0
        if max_age > 0:
            cutoff = timezone.now() - timedelta(seconds=max_age)
            qs = qs.filter(last_seen__gte=cutoff)
        return Response(
            {
                str(p.driver_id): {
                    "lat": p.lat,
                    "lng": p.lng,
                    "last_seen": p.last_seen.isoformat(),
                }
                for p in qs
            }
        )


class MyOverlayOrdersView(APIView):
    """A driver's active orders from our overlay (both demo-claimed and
    overlay-claimed have driver_id on OrderMeta). Powers the «Мои заказы» page.
    ``?driver_id=X`` (the frontend passes the logged-in user id)."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def get(self, request):
        # When auth is enforced, the driver is the token's user — so ?driver_id=
        # can't enumerate another driver's orders (IDOR).
        driver_id = acting_driver_id(request, request.query_params.get("driver_id"))
        if not driver_id:
            return Response([])
        qs = (
            OrderMeta.objects.filter(driver_id=driver_id)
            .exclude(
                trip_state__in=(OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)
            )
            .order_by("planned_datetime", "order_id")
        )
        return Response(OrderMetaSerializer(qs, many=True).data)


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
