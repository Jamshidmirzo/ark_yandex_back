"""Shared primitives for the car-orders view package — error-response helpers, the
tracking-log helpers, and thin service wrappers used across more than one view
module. No imports from sibling view modules, so it never participates in a cycle."""

import logging

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.response import Response

from car_orders import services

User = get_user_model()

# Keep the original module logger name so any logging config keyed on it still works.
logger = logging.getLogger("car_orders.views")

DRIVER_GROUP = "Driver"

__all__ = (
    "User",
    "logger",
    "DRIVER_GROUP",
    "_forbidden",
    "_bad_request",
    "_service_error_response",
    "_src",
    "_log_tracking",
    "_active_shift",
    "_reset_driver_shift",
    "_notify_dropped_driver",
)


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
    vs ``🖥 локально`` (our own server / the simulator on 127.0.0.1).

    AUDIT L2: X-Forwarded-For is client-spoofable — this is for the LOG ONLY; never
    repurpose it for any authorization / trust decision."""
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


def _notify_dropped_driver(driver_id, order_id):
    return services.events.notify_dropped_driver(driver_id, order_id)


def _reset_driver_shift(driver):
    return services.shift.reset_driver_shift(driver)


def _active_shift(user):
    return services.shift.active_shift(user)
