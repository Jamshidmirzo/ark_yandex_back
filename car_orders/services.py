"""Business-logic side-effects for car orders: the audit trail and the hooks
that, on integration, fan out notifications and live-location WS events.

Kept deliberately thin and dependency-free so this block runs standalone. The
two ``notify`` / ``publish_driver_location`` functions are no-ops here; wire
them to ark-backend's ``apps.notifications`` and the WS bus on merge — see
INTEGRATION.md.
"""

import math
from datetime import timedelta

import requests
from django.conf import settings

from car_orders.models import CarOrderActivity


def _log(order, actor, kind, **payload):
    return CarOrderActivity.objects.create(
        order=order,
        actor=actor,
        kind=kind,
        payload=payload,
    )


def record_created(order, actor):
    return _log(order, actor, CarOrderActivity.Kind.CREATED)


def record_sent(order, actor):
    return _log(order, actor, CarOrderActivity.Kind.SENT)


def record_approved(order, actor):
    return _log(order, actor, CarOrderActivity.Kind.APPROVED)


def record_accepted(order, actor):
    return _log(order, actor, CarOrderActivity.Kind.ACCEPTED_BY_DRIVER, car_id=order.car_id)


def record_completed(order, actor):
    return _log(order, actor, CarOrderActivity.Kind.COMPLETED)


def record_rejected(order, actor, reason=""):
    return _log(order, actor, CarOrderActivity.Kind.REJECTED, reason=reason)


def record_cancelled(order, actor, reason=""):
    return _log(order, actor, CarOrderActivity.Kind.CANCELLED, reason=reason)


def record_released(order, actor, reason=""):
    return _log(order, actor, CarOrderActivity.Kind.RELEASED, reason=reason)


def record_extended(order, actor, added_minutes):
    return _log(order, actor, CarOrderActivity.Kind.EXTENDED, added_minutes=added_minutes)


def record_reassigned(order, actor, from_driver_id=None):
    return _log(order, actor, CarOrderActivity.Kind.REASSIGNED, from_driver_id=from_driver_id)


def notify(user, title, body="", route_type="car_order", extra=None):
    """No-op notification hook.

    On integration, replace with ark-backend's
    ``apps.notifications.send_notification(user, title, body, route_type, extra)``.
    """
    return None


def publish_driver_location(shift):
    """No-op live-location hook.

    On integration, fan out a ``driver_status`` WS event to the order author's
    bus group so the tracking map updates in real time (see INTEGRATION.md).
    """
    return None


# --- Routing / duration auto-estimate ---------------------------------------

_AVG_SPEED_KMH = 30  # city average, used by the offline fallback


def _haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def estimate_route(origin_lat, origin_lng, dest_lat, dest_lng):
    """Estimate the driving route between two points.

    Tries the configured OSRM server (``CAR_ORDER_OSRM_URL``) for an accurate
    distance / duration / polyline; falls back to a straight-line haversine
    estimate at an average city speed so the feature still works offline.

    Returns ``{distance_m, duration_s, geometry, source}`` where ``geometry`` is
    a list of ``[lng, lat]`` pairs (GeoJSON order), possibly just the endpoints.
    """
    base = getattr(settings, "CAR_ORDER_OSRM_URL", "").rstrip("/")
    coords = f"{origin_lng},{origin_lat};{dest_lng},{dest_lat}"
    if base:
        try:
            resp = requests.get(
                f"{base}/route/v1/driving/{coords}",
                params={"overview": "full", "geometries": "geojson"},
                timeout=8,
            )
            data = resp.json()
            if resp.ok and data.get("routes"):
                route = data["routes"][0]
                return {
                    "distance_m": route["distance"],
                    "duration_s": route["duration"],
                    "geometry": route["geometry"]["coordinates"],
                    "source": "osrm",
                }
        except (requests.RequestException, ValueError, KeyError, IndexError):
            pass  # fall through to the offline estimate

    distance_km = _haversine_km(origin_lat, origin_lng, dest_lat, dest_lng)
    duration_s = distance_km / _AVG_SPEED_KMH * 3600
    return {
        "distance_m": distance_km * 1000,
        "duration_s": duration_s,
        "geometry": [[origin_lng, origin_lat], [dest_lng, dest_lat]],
        "source": "haversine",
    }


def estimate_duration(origin_lat, origin_lng, dest_lat, dest_lng, service_time=None):
    """Total order duration = drive time + on-site service time (rounded up to
    the minute), as a :class:`datetime.timedelta`."""
    route = estimate_route(origin_lat, origin_lng, dest_lat, dest_lng)
    if service_time is None:
        service_time = getattr(settings, "CAR_ORDER_DEFAULT_SERVICE", timedelta(minutes=30))
    drive = timedelta(seconds=math.ceil(route["duration_s"] / 60) * 60)
    route["duration"] = drive + service_time
    route["service_s"] = service_time.total_seconds()
    return route


def estimate_payload(origin_lat, origin_lng, dest_lat, dest_lng, service_minutes=None):
    """JSON-ready estimate for the /estimate/ endpoint (minutes + geometry)."""
    service_td = timedelta(minutes=service_minutes) if service_minutes is not None else None
    result = estimate_duration(origin_lat, origin_lng, dest_lat, dest_lng, service_time=service_td)
    total = result["duration"]
    drive_s = total.total_seconds() - result["service_s"]
    return {
        "distance_m": round(result["distance_m"]),
        "drive_minutes": round(drive_s / 60),
        "service_minutes": round(result["service_s"] / 60),
        "duration_minutes": round(total.total_seconds() / 60),
        "geometry": result["geometry"],
        "source": result["source"],
    }
