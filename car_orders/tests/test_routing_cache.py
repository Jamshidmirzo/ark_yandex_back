"""Unit tests for the OSRM route memo (``car_orders.services.routing``).

Pin the fix for «OSRM is hit on every re-push of the same leg»: successful OSRM
results are memoised for a TTL, the offline fallback is never cached, and callers
get a copy so mutating the result can't corrupt the cache.
"""

import pytest
from django.test import override_settings

from car_orders.services import routing


class _OsrmResp:
    """A stand-in OSRM HTTP response with one route."""

    ok = True

    def json(self):
        return {
            "routes": [
                {
                    "distance": 1234,
                    "duration": 600,
                    "geometry": {"coordinates": [[69.20, 41.30], [69.30, 41.40]]},
                }
            ]
        }


@pytest.fixture(autouse=True)
def _clear_cache():
    routing.clear_route_cache()
    yield
    routing.clear_route_cache()


@override_settings(CAR_ORDER_OSRM_URL="http://osrm.test", CAR_ORDER_ROUTE_CACHE_TTL=60)
def test_caches_osrm_hits(monkeypatch):
    calls = {"n": 0}

    def _fake_get(*args, **kwargs):
        calls["n"] += 1
        return _OsrmResp()

    monkeypatch.setattr(routing.requests, "get", _fake_get)

    first = routing.estimate_route(41.30, 69.20, 41.40, 69.30)
    second = routing.estimate_route(41.30, 69.20, 41.40, 69.30)

    assert first["source"] == "osrm" and second["source"] == "osrm"
    assert first == second
    assert calls["n"] == 1  # second call served from the memo — no 2nd OSRM request


@override_settings(CAR_ORDER_OSRM_URL="", CAR_ORDER_ROUTE_CACHE_TTL=60)
def test_offline_fallback_is_not_cached():
    routing.estimate_route(41.30, 69.20, 41.40, 69.30)
    assert routing._ROUTE_CACHE == {}  # haversine fallback must never be pinned


@override_settings(CAR_ORDER_OSRM_URL="http://osrm.test", CAR_ORDER_ROUTE_CACHE_TTL=0)
def test_ttl_zero_disables_cache(monkeypatch):
    calls = {"n": 0}

    def _fake_get(*args, **kwargs):
        calls["n"] += 1
        return _OsrmResp()

    monkeypatch.setattr(routing.requests, "get", _fake_get)

    routing.estimate_route(41.30, 69.20, 41.40, 69.30)
    routing.estimate_route(41.30, 69.20, 41.40, 69.30)
    assert calls["n"] == 2  # caching off → OSRM hit every time
    assert routing._ROUTE_CACHE == {}


@override_settings(CAR_ORDER_OSRM_URL="http://osrm.test", CAR_ORDER_ROUTE_CACHE_TTL=60)
def test_callers_cannot_corrupt_the_cache(monkeypatch):
    monkeypatch.setattr(routing.requests, "get", lambda *a, **k: _OsrmResp())

    # estimate_duration mutates the dict it gets back (adds duration / service_s).
    routing.estimate_duration(41.30, 69.20, 41.40, 69.30)
    # A fresh read must NOT see those mutations leaked into the cached entry.
    fresh = routing.estimate_route(41.30, 69.20, 41.40, 69.30)
    assert "duration" not in fresh
    assert "service_s" not in fresh
