"""The server owns the route: on assignment and on every trip-state change it
computes the current leg and broadcasts geometry, so the map always shows where
the driver should go — at every stage, not only once the trip is started."""

import pytest
from django.test import override_settings

from car_orders import dispatch, geometry
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


def _order(state, returning=False, has_return=False):
    return OrderMeta.objects.create(
        order_id=900, driver_id=5, overlay_claimed=True, trip_state=state,
        origin_lat=41.31, origin_lng=69.24, address_lat=41.35, address_lng=69.29,
        has_return=has_return, returning=returning, return_lat=41.30, return_lng=69.20,
    )


# ---- which leg per stage --------------------------------------------------

@pytest.mark.django_db
def test_leg_approach_uses_driver_position_when_to_client():
    m = _order(TS.TO_CLIENT)
    leg = dispatch.order_leg(m, driver_pos=(41.10, 69.10))
    assert leg == ((41.10, 69.10), (41.31, 69.24))  # driver → pickup


@pytest.mark.django_db
def test_leg_approach_skipped_without_position():
    # No fresh fix → no degenerate origin→origin line; the heartbeat re-pushes later.
    m = _order(TS.ASSIGNED)
    assert dispatch.order_leg(m, driver_pos=None) is None
    # On the pickup point exactly → also no line.
    assert dispatch.order_leg(m, driver_pos=(41.31, 69.24)) is None


@pytest.mark.django_db
def test_leg_in_trip_is_pickup_to_destination():
    m = _order(TS.IN_TRIP)
    assert dispatch.order_leg(m) == ((41.31, 69.24), (41.35, 69.29))  # no fix → pickup→dest


@pytest.mark.django_db
def test_leg_in_trip_reroutes_from_driver_position():
    # With a live fix the in-trip leg starts at the driver's CURRENT spot (so a
    # re-route follows the road taken) → destination, not pickup→dest.
    m = _order(TS.IN_TRIP)
    assert dispatch.order_leg(m, driver_pos=(41.33, 69.26)) == ((41.33, 69.26), (41.35, 69.29))


def test_min_dist_to_polyline():
    geom = [[69.20, 41.30], [69.21, 41.305], [69.22, 41.31]]  # [lng,lat]
    on_route = geometry.min_dist_km_to_polyline(41.305, 69.21, geom)
    off_route = geometry.min_dist_km_to_polyline(41.40, 69.40, geom)
    assert on_route < 0.05  # on the line
    assert off_route > 1.0  # strayed far


@pytest.mark.django_db
def test_leg_return_is_destination_to_return_point():
    m = _order(TS.IN_TRIP, returning=True, has_return=True)
    assert dispatch.order_leg(m) == ((41.35, 69.29), (41.30, 69.20))


@pytest.mark.django_db
def test_leg_at_destination_with_return_previews_the_way_back():
    m = _order(TS.AT_DESTINATION, has_return=True)
    assert dispatch.order_leg(m) == ((41.35, 69.29), (41.30, 69.20))


@pytest.mark.django_db
def test_leg_none_when_parked_or_terminal():
    assert dispatch.order_leg(_order(TS.WAITING)) is None
    OrderMeta.objects.all().delete()
    assert dispatch.order_leg(_order(TS.AT_DESTINATION)) is None  # final, no return
    OrderMeta.objects.all().delete()
    assert dispatch.order_leg(_order(TS.COMPLETED)) is None


# ---- push stores + broadcasts geometry ------------------------------------

@override_settings(CAR_ORDER_OSRM_URL="http://osrm.test", CAR_ORDER_ROUTE_CACHE_TTL=0)
@pytest.mark.django_db
def test_push_order_route_stores_geometry(monkeypatch):
    monkeypatch.setattr(routing.requests, "get", lambda *a, **k: _OsrmResp())
    m = _order(TS.IN_TRIP)
    geom = dispatch.push_order_route(m)
    assert geom and isinstance(geom, list)
    loc = OrderLiveLocation.objects.get(order_id=900)
    assert loc.geometry == geom  # persisted for late WS subscribers


@override_settings(CAR_ORDER_OSRM_URL="")  # OSRM unreachable → offline haversine only
@pytest.mark.django_db
def test_push_first_assignment_draws_nothing_when_osrm_down():
    # New contract: on the FIRST push with no road route yet, draw NOTHING rather
    # than the 2-point haversine straight line that cuts through buildings («по домам»).
    # The next GPS fix re-routes (empty geometry counts as deviated).
    m = _order(TS.IN_TRIP)
    assert dispatch.push_order_route(m) is None
    assert not OrderLiveLocation.objects.filter(order_id=900).exists()


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_push_returns_none_when_parked():
    m = _order(TS.WAITING)
    assert dispatch.push_order_route(m) is None
    assert not OrderLiveLocation.objects.filter(order_id=900).exists()


def test_downsample_caps_points():
    big = [[i, i] for i in range(60000)]
    out = geometry.downsample(big)
    assert len(out) <= geometry.MAX_GEOM_POINTS + 1
    assert out[0] == big[0] and out[-1] == big[-1]  # ends kept


@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_push_skips_absurd_leg():
    # Driver «stuck» in San Francisco, pickup in Tashkent → 11 000 km leg → skip,
    # no giant polyline stored (this overflowed the 1 MB WS frame).
    m = _order(TS.TO_CLIENT)  # pickup 41.31,69.24
    assert dispatch.push_order_route(m, driver_pos=(37.78, -122.40)) is None
    loc = OrderLiveLocation.objects.filter(order_id=900).first()
    assert loc is None or not loc.geometry


# ---- snap-to-route: the broadcast marker rides the line --------------------

# A north-south route + a fix east of it; one km-per-degree-longitude step at this
# latitude is 111.32 * cos(41.35°) ≈ 83.55 km/deg, so 60 m ≈ 0.000718°.
_SNAP_GEOM = [[69.30, 41.30], [69.30, 41.40]]


def _setup_moving_order(monkeypatch, order_id, driver_id):
    """An IN_TRIP order with a drawn north-south route, a prior driver fix to the
    south (so a northbound travel bearing is derived), and captured broadcasts with
    OSRM re-routing stubbed out."""
    from django.utils import timezone

    from car_orders import views
    from car_orders.models import DriverPosition

    OrderMeta.objects.create(
        order_id=order_id, driver_id=driver_id, overlay_claimed=True, trip_state=TS.IN_TRIP,
        origin_lat=41.30, origin_lng=69.30, address_lat=41.40, address_lng=69.30,
    )
    OrderLiveLocation.objects.create(
        order_id=order_id, lat=41.34, lng=69.30, last_seen=timezone.now(), geometry=_SNAP_GEOM,
    )
    DriverPosition.objects.create(driver_id=driver_id, lat=41.34, lng=69.30, last_seen=timezone.now())
    captured = []
    monkeypatch.setattr(views.tracking, "broadcast_location", lambda oid, data: captured.append(data))
    monkeypatch.setattr("car_orders.dispatch.push_order_route", lambda *a, **k: None)
    return views, captured


@pytest.mark.django_db
def test_apply_driver_location_broadcasts_snapped_marker(monkeypatch):
    views, captured = _setup_moving_order(monkeypatch, 901, 7)
    raw_lng = 69.30 + 0.060 / (111.32 * 0.75046)  # 60 m east → inside the 70 m corridor
    views._apply_driver_location(7, 41.35, raw_lng, "test")

    marker = next(d for d in captured if "lat" in d and "geometry" not in d)
    assert abs(marker["lng"] - 69.30) < 1e-4  # pulled onto the road
    assert abs(marker["lat"] - 41.35) < 1e-4
    # Raw fix is still authoritative for the deviation re-route (60 m > 30 m).
    from car_orders.models import DriverPosition

    assert abs(DriverPosition.objects.get(driver_id=7).lng - raw_lng) < 1e-9


@pytest.mark.django_db
def test_apply_driver_location_keeps_raw_marker_when_off_route(monkeypatch):
    views, captured = _setup_moving_order(monkeypatch, 902, 8)
    raw_lng = 69.30 + 0.200 / (111.32 * 0.75046)  # 200 m east → outside the corridor
    views._apply_driver_location(8, 41.35, raw_lng, "test")

    marker = next(d for d in captured if "lat" in d and "geometry" not in d)
    assert abs(marker["lng"] - raw_lng) < 1e-9  # shown as-is (real detour)
