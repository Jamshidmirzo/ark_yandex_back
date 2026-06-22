"""Additional tests for the route/leg logic in ``car_orders.dispatch`` — gap-fill
for ``test_route.py``.

Pins the subtle «don't overwrite a good OSRM route with a straight-line fallback»
branch, the first-assignment fallback, the planned (driverless) A→B route, and the
AT_CLIENT leg that starts from the driver's live position. The OSRM URL is forced
empty so routing takes the deterministic offline (haversine) path.
"""

import pytest
from django.test import override_settings
from django.utils import timezone

from car_orders import dispatch
from car_orders.models import OrderLiveLocation, OrderMeta
from car_orders.services import routing

TS = OrderMeta.TripState


class _OsrmResp:
    """Stand-in OSRM HTTP response with one road route ([lng, lat] coords)."""

    ok = True

    def json(self):
        return {
            "routes": [
                {
                    "distance": 1234,
                    "duration": 600,
                    "geometry": {"coordinates": [[69.24, 41.31], [69.26, 41.33], [69.29, 41.35]]},
                }
            ]
        }


def _meta(order_id, state=TS.AT_CLIENT, save=True, **extra):
    kwargs = dict(
        order_id=order_id, driver_id=5, trip_state=state,
        origin_lat=41.31, origin_lng=69.24, address_lat=41.35, address_lng=69.29,
    )
    kwargs.update(extra)
    return OrderMeta.objects.create(**kwargs) if save else OrderMeta(**kwargs)


# ---- push_order_route: fallback handling ----------------------------------

@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_push_route_keeps_good_route_when_only_fallback_available():
    m = _meta(900)
    good = [[69.24, 41.31], [69.26, 41.33], [69.29, 41.35]]  # a road route already on the map
    OrderLiveLocation.objects.create(
        order_id=900, lat=41.31, lng=69.24, last_seen=timezone.now(), geometry=good
    )
    out = dispatch.push_order_route(m)
    # Offline → haversine source → must NOT clobber the existing canonical route.
    assert out == good
    assert OrderLiveLocation.objects.get(order_id=900).geometry == good


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_push_route_draws_nothing_on_first_assignment_when_osrm_down():
    # New contract: with no road route yet and OSRM down, draw NOTHING rather than the
    # straight-line haversine fallback that cuts through buildings — the next GPS fix
    # re-routes once OSRM answers.
    m = _meta(901)  # no OrderLiveLocation yet
    assert dispatch.push_order_route(m) is None
    assert not OrderLiveLocation.objects.filter(order_id=901).exists()


# ---- planned (driverless) A→B route ---------------------------------------

@override_settings(CAR_ORDER_OSRM_URL="http://osrm.test", CAR_ORDER_ROUTE_CACHE_TTL=0)
@pytest.mark.django_db
def test_planned_route_geometry_happy_and_missing(monkeypatch):
    monkeypatch.setattr(routing.requests, "get", lambda *a, **k: _OsrmResp())
    m = _meta(902, save=False)
    geom = dispatch.planned_route_geometry(m)
    assert geom is not None and len(geom) >= 2
    m2 = _meta(903, save=False, origin_lat=None, origin_lng=None)
    assert dispatch.planned_route_geometry(m2) is None


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_planned_route_geometry_none_when_osrm_down():
    # Pins-only beats an A→B straight line through buildings when OSRM is unreachable.
    m = _meta(904, save=False)
    assert dispatch.planned_route_geometry(m) is None


# ---- push_order_route: directional snap + stale handling -------------------

@override_settings(CAR_ORDER_OSRM_URL="http://osrm.test", CAR_ORDER_ROUTE_CACHE_TTL=0)
@pytest.mark.django_db
def test_push_passes_bearing_to_osrm_for_a_live_leg(monkeypatch):
    captured = {}

    def _fake_get(url, params=None, timeout=None):
        captured["params"] = params
        return _OsrmResp()

    monkeypatch.setattr(routing.requests, "get", _fake_get)
    m = _meta(905, state=TS.IN_TRIP)
    # The leg starts at the driver's live position → the heading is forwarded to OSRM.
    dispatch.push_order_route(m, driver_pos=(41.32, 69.25), bearing=42)
    assert captured["params"].get("bearings", "").startswith("42,")


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_push_reanchors_stale_route_to_driver_on_osrm_failure():
    # OSRM down + a good route already on the map + a live fix → keep the road route
    # but RE-ANCHOR its start to the driver (so it doesn't appear to run backwards).
    good = [[69.24, 41.31], [69.26, 41.33], [69.29, 41.35]]
    OrderLiveLocation.objects.create(
        order_id=906, lat=41.31, lng=69.24, last_seen=timezone.now(), geometry=good
    )
    m = _meta(906, state=TS.IN_TRIP)
    out = dispatch.push_order_route(m, driver_pos=(41.33, 69.26))
    assert out is not None
    assert abs(out[0][0] - 69.26) < 1e-6 and abs(out[0][1] - 41.33) < 1e-6  # pinned to driver
    assert out[-1] == [69.29, 41.35]  # the rest of the canonical route ahead


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_push_clears_stale_geometry_on_absurd_leg():
    # An old straight line must not linger when the next leg is implausible (bad GPS).
    OrderLiveLocation.objects.create(
        order_id=907, lat=41.31, lng=69.24, last_seen=timezone.now(),
        geometry=[[69.24, 41.31], [69.29, 41.35]],
    )
    m = _meta(907, state=TS.TO_CLIENT)
    assert dispatch.push_order_route(m, driver_pos=(37.78, -122.40)) is None  # San Francisco
    assert OrderLiveLocation.objects.get(order_id=907).geometry == []


# ---- order_leg AT_CLIENT ----------------------------------------------------

def test_order_leg_at_client_starts_from_driver_position():
    m = OrderMeta(
        order_id=904, trip_state=TS.AT_CLIENT,
        origin_lat=41.31, origin_lng=69.24, address_lat=41.35, address_lng=69.29,
    )
    leg = dispatch.order_leg(m, driver_pos=(41.33, 69.26))
    assert leg == ((41.33, 69.26), (41.35, 69.29))
