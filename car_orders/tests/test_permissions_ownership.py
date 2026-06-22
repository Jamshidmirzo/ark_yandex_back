"""Object-level (ownership / IDOR) permission tests for the overlay mutations under
enforcement (``REQUIRE_OVERLAY_AUTH=True``).

The role matrix in test_permissions_overlay.py proves *who may call* an endpoint
(customer vs driver vs dispatcher). This file proves the orthogonal axis the role
matrix can't: that a user who passes the role gate still can't act on **another
driver's** order, and that overlay state that owns a state machine can't be mutated
through a side door. These pin the fixes in PERMISSION_FINDINGS.md §E (ownership):

  • overlay-release / overlay-extend now check the actor owns the order (a driver
    can't release/extend a peer's order; a dispatcher still can).
  • trip-state can't be advanced on a DRIVERLESS order by a plain driver.
  • trip_state can't be written through the plain /meta/ upsert (state-machine bypass).
"""

import pytest
from django.test import override_settings

from car_orders.models import OrderMeta
from car_orders.tests.conftest import CUSTOMER, DISPATCHER, DRIVER

TS = OrderMeta.TripState

OWNER = 42
OTHER = 99


# ---- overlay-release ownership --------------------------------------------

@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_overlay_release_denied_for_non_owner_driver(auth_client):
    OrderMeta.objects.create(order_id=850, overlay_claimed=True, driver_id=OWNER, trip_state=TS.IN_TRIP)
    r = auth_client(perms=DRIVER, user_id=OTHER).post("/api/v1/car-orders/850/overlay-release/")
    assert r.status_code == 403
    assert r.data["error"]["code"] == "PERMISSION_DENIED"
    assert OrderMeta.objects.get(order_id=850).driver_id == OWNER  # claim intact


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_overlay_release_allowed_for_owner_and_dispatcher(auth_client):
    OrderMeta.objects.create(order_id=851, overlay_claimed=True, driver_id=OWNER, trip_state=TS.IN_TRIP)
    assert auth_client(perms=DRIVER, user_id=OWNER).post(
        "/api/v1/car-orders/851/overlay-release/"
    ).status_code == 200
    OrderMeta.objects.create(order_id=852, overlay_claimed=True, driver_id=OWNER, trip_state=TS.IN_TRIP)
    assert auth_client(perms=DISPATCHER, user_id=1).post(
        "/api/v1/car-orders/852/overlay-release/"
    ).status_code == 200


# ---- overlay-extend ownership ---------------------------------------------

@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_overlay_extend_denied_for_non_owner_driver(auth_client):
    OrderMeta.objects.create(order_id=860, driver_id=OWNER, trip_state=TS.IN_TRIP, estimated_duration=60)
    r = auth_client(perms=DRIVER, user_id=OTHER).post(
        "/api/v1/car-orders/860/extend/", {"minutes": 30}, format="json"
    )
    assert r.status_code == 403
    assert r.data["error"]["code"] == "PERMISSION_DENIED"
    assert OrderMeta.objects.get(order_id=860).estimated_duration == 60  # unchanged


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_overlay_extend_allowed_for_owner_and_dispatcher(auth_client):
    OrderMeta.objects.create(order_id=861, driver_id=OWNER, trip_state=TS.IN_TRIP, estimated_duration=60)
    assert auth_client(perms=DRIVER, user_id=OWNER).post(
        "/api/v1/car-orders/861/extend/", {"minutes": 30}, format="json"
    ).status_code == 200
    assert auth_client(perms=DISPATCHER, user_id=1).post(
        "/api/v1/car-orders/861/extend/", {"minutes": 30}, format="json"
    ).status_code == 200


# ---- trip-state: driverless order can't be driven by a plain driver -------

@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_trip_state_on_driverless_order_denied_for_driver(auth_client):
    OrderMeta.objects.create(order_id=870, driver_id=None, trip_state=TS.ASSIGNED)
    r = auth_client(perms=DRIVER, user_id=OTHER).post(
        "/api/v1/car-orders/870/trip-state/", {"trip_state": "to_client"}, format="json"
    )
    assert r.status_code == 403
    assert r.data["error"]["code"] == "PERMISSION_DENIED"


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_trip_state_on_driverless_order_allowed_for_dispatcher(auth_client):
    OrderMeta.objects.create(order_id=871, driver_id=None, trip_state=TS.ASSIGNED)
    r = auth_client(perms=DISPATCHER, user_id=1).post(
        "/api/v1/car-orders/871/trip-state/", {"trip_state": "to_client"}, format="json"
    )
    assert r.status_code != 403


# ---- meta POST can't move trip_state (state-machine bypass closed) ---------

@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
@pytest.mark.parametrize("perms", [CUSTOMER, DISPATCHER], ids=["customer", "dispatcher"])
def test_meta_post_cannot_set_trip_state(auth_client, perms):
    # Even a dispatcher must go through TripStateView — the plain feature-overlay upsert
    # may never jump trip_state (it would skip transitions / geofence / side-effects).
    OrderMeta.objects.create(order_id=880, driver_id=OWNER, trip_state=TS.ASSIGNED)
    r = auth_client(perms=perms, user_id=1).post(
        "/api/v1/car-orders/880/meta/",
        {"trip_state": "completed", "origin_lat": 41.3},
        format="json",
    )
    assert r.status_code == 200, r.content
    meta = OrderMeta.objects.get(order_id=880)
    assert meta.trip_state == TS.ASSIGNED  # trip_state stripped (read-only)
    assert meta.origin_lat == 41.3  # benign field still saved
