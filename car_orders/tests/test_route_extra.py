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

TS = OrderMeta.TripState


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
def test_push_route_draws_fallback_on_first_assignment():
    m = _meta(901)  # no OrderLiveLocation yet
    out = dispatch.push_order_route(m)
    assert out is not None and len(out) >= 2
    assert OrderLiveLocation.objects.filter(order_id=901).exists()


# ---- planned (driverless) A→B route ---------------------------------------

@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_planned_route_geometry_happy_and_missing():
    m = _meta(902, save=False)
    geom = dispatch.planned_route_geometry(m)
    assert geom is not None and len(geom) >= 2
    m2 = _meta(903, save=False, origin_lat=None, origin_lng=None)
    assert dispatch.planned_route_geometry(m2) is None


# ---- order_leg AT_CLIENT ----------------------------------------------------

def test_order_leg_at_client_starts_from_driver_position():
    m = OrderMeta(
        order_id=904, trip_state=TS.AT_CLIENT,
        origin_lat=41.31, origin_lng=69.24, address_lat=41.35, address_lng=69.29,
    )
    leg = dispatch.order_leg(m, driver_pos=(41.33, 69.26))
    assert leg == ((41.33, 69.26), (41.35, 69.29))
