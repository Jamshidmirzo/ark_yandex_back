"""Server-side geocoding proxy (search + reverse) for the order form's address
lookup.

Clients must NOT hit ``nominatim.openstreetmap.org`` directly: OSM's usage
policy forbids heavy browser-origin traffic and blocks the IP with ``HTTP 429``
after a burst of as-you-type lookups — which silently empties the order form's
«Откуда/Куда» suggestions and stops the map click from filling an address. We
proxy it here instead: one server IP with a proper ``User-Agent``, a 1 req/s
throttle and a day-long response cache, so the public OSM server stays happy and
address lookup no longer depends on a client-side geocoder key.

Results are region-biased (``bounded=1`` + an ``inRegion`` post-filter around the
fleet centre) so an ambiguous query can't resolve to the wrong continent — the
same contract the web client used to enforce on its own before it lost the
direct OSM access.
"""

import hashlib
import logging
import threading
import time

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_NOMINATIM = getattr(
    settings, "CAR_ORDER_NOMINATIM_URL", "https://nominatim.openstreetmap.org"
).rstrip("/")
# OSM policy requires a request to identify the app + a contact. Override per
# deployment via CAR_ORDER_GEOCODER_USER_AGENT.
_USER_AGENT = getattr(
    settings,
    "CAR_ORDER_GEOCODER_USER_AGENT",
    "ark-car-orders/1.0 (+https://ark.glob.uz)",
)
_TIMEOUT_S = 8
_CACHE_TTL_S = getattr(settings, "CAR_ORDER_GEOCODE_CACHE_TTL", 24 * 60 * 60)
_MIN_INTERVAL_S = 1.0  # OSM: at most one request per second

# Fleet region — mirrors the web client's DEFAULT_CENTER / REGION_RADIUS_DEG so a
# match outside the operating region is dropped instead of becoming a route point.
_CENTER = getattr(settings, "CAR_ORDER_FLEET_CENTER", (41.311081, 69.240562))
_RADIUS_DEG = float(getattr(settings, "CAR_ORDER_FLEET_RADIUS_DEG", 5))

# Serialise outbound calls so we never exceed OSM's 1 req/s. Held across the short
# sleep on purpose — the cache absorbs repeats, so contention stays low.
_throttle_lock = threading.Lock()
_last_call = [0.0]


def _throttle() -> None:
    with _throttle_lock:
        wait = _MIN_INTERVAL_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _in_region(lat: float, lng: float) -> bool:
    return abs(lat - _CENTER[0]) <= _RADIUS_DEG and abs(lng - _CENTER[1]) <= _RADIUS_DEG


def _viewbox() -> str:
    # Nominatim viewbox: lngLeft,latTop,lngRight,latBottom
    lat, lng = _CENTER
    r = _RADIUS_DEG
    return f"{lng - r},{lat + r},{lng + r},{lat - r}"


def search(query: str) -> list[dict]:
    """Free-text address/landmark search → ``[{lat, lng, label}]`` (region-filtered)."""
    q = (query or "").strip()
    if len(q) < 3:
        return []
    # Hash the query into the key — raw text (spaces / Cyrillic) is an invalid
    # memcached key and triggers a CacheKeyWarning.
    digest = hashlib.md5(q.lower().encode("utf-8")).hexdigest()
    key = f"geocode:search:{digest}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        _throttle()
        resp = requests.get(
            f"{_NOMINATIM}/search",
            params={
                "format": "jsonv2",
                "limit": 6,
                "accept-language": "ru",
                "bounded": 1,
                "viewbox": _viewbox(),
                "q": q,
            },
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        # Transient (429 / network) — don't cache the miss, so it self-heals.
        logger.warning("geocode search failed for %r: %s", q, exc)
        return []
    out: list[dict] = []
    for d in data:
        try:
            lat, lng = float(d["lat"]), float(d["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if _in_region(lat, lng):
            out.append({"lat": lat, "lng": lng, "label": d.get("display_name", "")})
    cache.set(key, out, _CACHE_TTL_S)
    return out


def reverse(lat: float, lng: float) -> str:
    """Coordinate → human-readable address (empty string if unavailable)."""
    key = f"geocode:reverse:{round(lat, 5)}:{round(lng, 5)}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        _throttle()
        resp = requests.get(
            f"{_NOMINATIM}/reverse",
            params={
                "format": "jsonv2",
                "accept-language": "ru",
                "lat": lat,
                "lon": lng,
            },
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        label = resp.json().get("display_name", "") or ""
    except (requests.RequestException, ValueError) as exc:
        logger.warning("geocode reverse failed for %s,%s: %s", lat, lng, exc)
        return ""
    cache.set(key, label, _CACHE_TTL_S)
    return label
