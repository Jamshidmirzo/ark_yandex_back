"""Local overlay «driver on shift» (Р1, demo has no set-shift endpoint), the active
shift roster the dispatcher reads, and the runtime auto-dispatch on/off switch."""

from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from car_orders import dispatch
from car_orders.models import DispatchSettings, DriverShiftState, OrderMeta
from car_orders.permissions import (
    OverlayAuthenticated,
    OverlayDispatcher,
    OverlayDriverOrDispatcher,
    acting_driver_id,
)
from config.auth import DemoTokenAuthentication

from .base import _bad_request

__all__ = ("DriverShiftView", "DriverShiftsView", "AutoDispatchView")


class DriverShiftView(APIView):
    """Local OVERLAY «driver on shift» (Р1) — demo has no set-shift endpoint, so we
    keep it locally by demo driver id. GET current shift / PATCH go on shift (pick a
    car) / DELETE end. Mounted at /drivers/me/shift/ BEFORE the gateway catch-all."""

    authentication_classes = [DemoTokenAuthentication]

    def get_permissions(self):
        # Reading your own shift is harmless (any authenticated user); going ON shift
        # or ending it mutates the overlay, so require an actual driver (or dispatcher),
        # matching the native ``my_shift`` gate (driver:accept_order).
        if self.request.method in ("PATCH", "DELETE"):
            return [OverlayDriverOrDispatcher()]
        return [OverlayAuthenticated()]

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
    # Dispatcher-only: the full on-shift roster (every driver + their car) feeds the
    # dispatcher candidate list. Only dispatcher screens read it, so gate it on
    # car_order:approve rather than exposing every driver's shift to any token.
    permission_classes = [OverlayDispatcher]

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
