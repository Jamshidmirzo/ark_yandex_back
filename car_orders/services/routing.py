"""Routing / duration auto-estimate.

Tries the configured OSRM server for an accurate route; falls back to a
straight-line haversine estimate at an average city speed so the feature still
works offline.

OSRM hits are memoised for a short TTL (:func:`estimate_route`): a leg gets
re-pushed on assignment, on every trip-state change and on each route deviation
while a driver is moving, so without a cache the same fixed leg (pickup→dest,
dest→return) would hit OSRM many times over. Only successful OSRM results are
cached — the cheap offline fallback is never cached, so a transient OSRM outage
never pins a straight-line route once the server recovers.
"""

import logging
import math
import threading
import time
from datetime import timedelta

import requests
from django.conf import settings

from car_orders import geometry

logger = logging.getLogger(__name__)

_AVG_SPEED_KMH = 30  # city average, used by the offline fallback

# OSRM is retried a couple of times with a short backoff before giving up: the
# default server is the public demo, which rate-limits / drops connections, and a
# single hiccup would otherwise collapse the road route to the 2-point straight
# line that cuts across buildings. Most flaky modes (429, connection reset) fail
# fast, so retries add little latency; a genuine outage still falls back cleanly.
_OSRM_TIMEOUT_S = 8  # per attempt
_OSRM_ATTEMPTS = 2  # total tries against a flaky/rate-limited server
_OSRM_BACKOFF_S = 0.3  # base backoff, scaled by attempt

# In-process memo of OSRM results: key → (monotonic_ts, result). Process-local
# (each worker caches independently) — that's fine, it just trims OSRM load.
_ROUTE_CACHE: dict[tuple, tuple[float, dict]] = {}
_ROUTE_CACHE_MAX = 512  # bound memory; drop the oldest entry past this
# Guard writes + eviction: under a threaded server two threads racing the eviction
# could make ``min()`` iterate a dict another thread is mutating.
_CACHE_LOCK = threading.Lock()


def _route_cache_key(base, origin_lat, origin_lng, dest_lat, dest_lng) -> tuple:
    # Key on the OSRM base too: a different (or disabled) server yields a different
    # route, so a result cached under one URL must never be served under another.
    # ~1 m precision (5 dp): dedupe repeated pushes of the same leg without merging
    # genuinely different legs.
    return (
        base,
        round(origin_lat, 5),
        round(origin_lng, 5),
        round(dest_lat, 5),
        round(dest_lng, 5),
    )


def clear_route_cache() -> None:
    """Drop all memoised routes. Mainly for tests / a manual cache flush."""
    _ROUTE_CACHE.clear()


def estimate_route(origin_lat, origin_lng, dest_lat, dest_lng):
    """Estimate the driving route between two points (memoised — see module docs).

    Tries the configured OSRM server (``CAR_ORDER_OSRM_URL``); falls back to a
    straight-line haversine estimate. Returns ``{distance_m, duration_s, geometry,
    source}`` where ``geometry`` is a list of ``[lng, lat]`` pairs (GeoJSON order).
    A *copy* is always returned, so callers (e.g. :func:`estimate_duration`) can
    mutate the result without corrupting the cache.
    """
    ttl = getattr(settings, "CAR_ORDER_ROUTE_CACHE_TTL", 60)
    if ttl <= 0:  # caching disabled
        return _estimate_route_uncached(origin_lat, origin_lng, dest_lat, dest_lng)

    base = getattr(settings, "CAR_ORDER_OSRM_URL", "").rstrip("/")
    key = _route_cache_key(base, origin_lat, origin_lng, dest_lat, dest_lng)
    now = time.monotonic()
    hit = _ROUTE_CACHE.get(key)
    if hit is not None and now - hit[0] < ttl:
        return dict(hit[1])

    result = _estimate_route_uncached(origin_lat, origin_lng, dest_lat, dest_lng)
    # Cache only real OSRM hits — the offline fallback is cheap and must not pin a
    # straight line over a recovered OSRM server.
    if result.get("source") == "osrm":
        with _CACHE_LOCK:
            _ROUTE_CACHE[key] = (now, result)
            if len(_ROUTE_CACHE) > _ROUTE_CACHE_MAX:
                oldest = min(_ROUTE_CACHE, key=lambda k: _ROUTE_CACHE[k][0])
                _ROUTE_CACHE.pop(oldest, None)
    return dict(result)


def _estimate_route_uncached(origin_lat, origin_lng, dest_lat, dest_lng):
    """The actual OSRM call + haversine fallback, without the memo layer.

    Retries a flaky OSRM a couple of times (see ``_OSRM_ATTEMPTS``) before giving
    up so a single hiccup doesn't collapse the road route to a straight line. Only a
    genuine failure (or an unconfigured server) falls back; the result's ``source``
    lets callers keep a known-good route instead of overwriting it with the line."""
    base = getattr(settings, "CAR_ORDER_OSRM_URL", "").rstrip("/")
    coords = f"{origin_lng},{origin_lat};{dest_lng},{dest_lat}"
    if base:
        for attempt in range(_OSRM_ATTEMPTS):
            try:
                resp = requests.get(
                    f"{base}/route/v1/driving/{coords}",
                    params={"overview": "full", "geometries": "geojson"},
                    timeout=_OSRM_TIMEOUT_S,
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
                # Server answered but has no route for these points — retrying won't
                # help, so stop and fall back to the offline estimate.
                break
            except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
                if attempt + 1 < _OSRM_ATTEMPTS:
                    time.sleep(_OSRM_BACKOFF_S * (attempt + 1))
                    continue
                logger.warning(
                    "car_orders: OSRM route failed after %d attempt(s) via %s (%s) — "
                    "using straight-line fallback",
                    _OSRM_ATTEMPTS,
                    base,
                    exc,
                )
    else:
        # Empty base is a config regression, not an outage: EVERY route becomes a
        # straight line over buildings. Make it loud, and distinct from the transient
        # «haversine» tag below, so monitoring can tell a misconfig from a hiccup.
        logger.warning(
            "car_orders: CAR_ORDER_OSRM_URL is empty — every route is a straight line; "
            "set a working OSRM base"
        )

    distance_km = geometry.haversine_km(origin_lat, origin_lng, dest_lat, dest_lng)
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
