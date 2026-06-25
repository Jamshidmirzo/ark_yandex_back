"""Permission matrix for the LOCAL overlay endpoints under enforcement
(``REQUIRE_OVERLAY_AUTH=True``).

Every overlay route from ``config/urls.py`` is exercised against five roles
(authenticated, demo-bridged) so a regression — or a silent loosening — of any
gate is caught. The contract has three tiers:

  • ANY        — any authenticated user (reads / self-scoped / shared tools)
  • DISPATCHER — only ``car_order:approve`` (dispatcher dashboards + writes)
  • (public)   — ``estimate`` / ``live-location GET`` stay open even when enforced

``# FINDING`` marks a row whose current gate is *looser* than its docstring/role
implies (an authenticated non-driver can mutate overlay trip state). Those are
PINNED to today's behaviour here and written up in PERMISSION_FINDINGS.md — they
are a product-policy call (an existing AUDIT test deliberately lets a permissionless
user overlay-claim), not silently changed.

Roles + the ``auth_client`` factory come from ``conftest.py``.
"""

import pytest
from django.test import override_settings
from django.utils import timezone

from car_orders.models import OrderMeta
from car_orders.tests.conftest import ADMIN, CUSTOMER, DISPATCHER, DRIVER, GARAGE

ROLES = {
    "customer": CUSTOMER,    # authenticated, zero car-order perms
    "driver": DRIVER,        # driver:accept_order (+ trip_control)
    "garage": GARAGE,        # garage:* — a "wrong perm" back-office role
    "dispatcher": DISPATCHER,  # car_order:approve
    "admin": ADMIN,          # administrator (wildcard)
}

ANY = set(ROLES)                       # every authenticated role allowed
DISP = {"dispatcher", "admin"}         # only car_order:approve (or admin)
DOD = {"driver", "dispatcher", "admin"}  # driver:accept_order ∨ car_order:approve


# (id, method, path, body, allowed_roles)
CASES = [
    # ---- ANY authenticated -------------------------------------------------
    ("templates_get", "GET", "/api/v1/car-orders/templates/", None, ANY),
    ("templates_post", "POST", "/api/v1/car-orders/templates/", {"name": "T"}, ANY),
    ("templates_patch", "PATCH", "/api/v1/car-orders/templates/1/", {"name": "T2"}, ANY),
    ("templates_delete", "DELETE", "/api/v1/car-orders/templates/1/", None, ANY),
    ("meta_get", "GET", "/api/v1/car-orders/500/meta/", None, ANY),
    ("meta_post", "POST", "/api/v1/car-orders/500/meta/", {"trip_state": "assigned"}, ANY),
    ("meta_batch", "POST", "/api/v1/car-orders/meta-batch/", {"order_ids": [500]}, ANY),
    ("claim_check", "POST", "/api/v1/car-orders/500/claim-check/", {"driver_id": 42}, ANY),
    ("claim_check_batch", "POST", "/api/v1/car-orders/claim-check-batch/",
     {"driver_id": 42, "order_ids": [500]}, ANY),
    ("my_overlay_orders", "GET", "/api/v1/car-orders/drivers/me/overlay-orders/", None, ANY),
    ("my_active_order", "GET", "/api/v1/car-orders/me/active-order/", None, ANY),
    ("auto_dispatch_get", "GET", "/api/v1/car-orders/auto-dispatch/", None, ANY),
    ("shift_get", "GET", "/api/v1/car-orders/drivers/me/shift/", None, ANY),
    # ---- DRIVER or DISPATCHER (§A fix) -------------------------------------
    # These MUTATE overlay claim / trip / shift state, so they now require an actual
    # driver (driver:accept_order) or a dispatcher (car_order:approve) — a customer-tier
    # token is denied, matching the native claim/release/my_location/my_shift gates.
    # (trip-state is gated separately by its service-layer actor check — see
    # test_views_errors.test_trip_state_permission_denied_is_403.)
    ("overlay_claim", "POST", "/api/v1/car-orders/500/overlay-claim/", {"car_id": 7}, DOD),
    ("overlay_release", "POST", "/api/v1/car-orders/500/overlay-release/", {}, DOD),
    ("no_show", "POST", "/api/v1/car-orders/500/no-show/", {}, DOD),
    ("extend", "POST", "/api/v1/car-orders/500/extend/", {"minutes": 15}, DOD),
    ("driver_location", "POST", "/api/v1/car-orders/drivers/me/location/",
     {"lat": 41.3, "lng": 69.2}, DOD),
    ("shift_patch", "PATCH", "/api/v1/car-orders/drivers/me/shift/",
     {"car_id": 7, "car_type_id": 3}, DOD),
    ("shift_delete", "DELETE", "/api/v1/car-orders/drivers/me/shift/", None, DOD),
    # ---- DISPATCHER only ---------------------------------------------------
    # Dashboards: now gated (was OverlayAuthenticated — fixed, see PERMISSION_FINDINGS.md §B).
    ("fleet_live", "GET", "/api/v1/car-orders/fleet/live/", None, DISP),
    ("driver_positions", "GET", "/api/v1/car-orders/drivers/positions/", None, DISP),
    ("driver_shifts", "GET", "/api/v1/car-orders/drivers/shifts/", None, DISP),
    # Privileged writes (already gated before this work):
    ("auto_dispatch_post", "POST", "/api/v1/car-orders/auto-dispatch/", {"enabled": True}, DISP),
    ("meta_delete", "DELETE", "/api/v1/car-orders/500/meta/", None, DISP),
    ("reassign", "POST", "/api/v1/car-orders/500/reassign/", None, DISP),
]


def _call(client, method, path, body):
    fn = getattr(client, method.lower())
    if method in ("GET", "DELETE"):
        return fn(path)
    return fn(path, body or {}, format="json")


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_overlay_permission_matrix(case, auth_client):
    label, method, path, body, allowed = case
    for role, perms in ROLES.items():
        # Fresh overlay row per role so a mutating call (claim/extend/release/meta
        # DELETE) starts from the same state and the result reflects only the gate,
        # never a leftover from the previous role's request.
        OrderMeta.objects.filter(order_id=500).delete()
        OrderMeta.objects.create(order_id=500, estimated_duration=60)
        client = auth_client(perms=perms)
        resp = _call(client, method, path, body)
        if role in allowed:
            assert resp.status_code != 403, (
                f"{label}: {role} should be ALLOWED, got {resp.status_code} ({resp.content[:200]!r})"
            )
        else:
            assert resp.status_code == 403, (
                f"{label}: {role} should be DENIED (403), got {resp.status_code}"
            )


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_unauthenticated_is_rejected_on_a_gated_endpoint(auth_client):
    """No token at all → 401 on an enforced overlay endpoint (the auth layer, before
    any per-role gate)."""
    from rest_framework.test import APIClient

    r = APIClient().post("/api/v1/car-orders/500/reassign/")
    assert r.status_code == 401


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_estimate_stays_public_even_when_enforced(auth_client):
    """``estimate`` is a pure function of coordinates and the mobile create card hits
    it with the no-auth client — it must stay open even under enforcement."""
    from rest_framework.test import APIClient

    r = APIClient().post(
        "/api/v1/car-orders/estimate/",
        {"origin_lat": 41.31, "origin_lng": 69.24, "dest_lat": 41.35, "dest_lng": 69.29},
        format="json",
    )
    assert r.status_code == 200


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_live_location_get_stays_public_when_enforced():
    """The live-location GET is deliberately public even under enforcement so a
    customer/admin tracker can read the driver marker without a privileged token
    (only the POST is owner/dispatcher-gated — see test_auth_bridge). Pin it so a
    future tightening of LiveLocationView is caught."""
    from rest_framework.test import APIClient

    from car_orders.models import OrderLiveLocation

    client = APIClient()  # no token
    assert client.get("/api/v1/car-orders/950/live-location/").status_code == 200  # null, 200
    OrderLiveLocation.objects.create(order_id=950, lat=41.3, lng=69.2, last_seen=timezone.now())
    r = client.get("/api/v1/car-orders/950/live-location/")
    assert r.status_code == 200
    assert r.data["lat"] == 41.3


# --------------------------------------------------------------------------- #
# A4 — identity / anti-spoofing at the HTTP layer (assignee_driver_id).        #
#                                                                             #
# The claim assignee is the driver the order is FOR — a DISPATCHER picks them  #
# in the body, a DRIVER acts on their OWN (token) and a spoofed body id is     #
# ignored. This is the IDOR guard; pin it at the HTTP layer, not just the unit.#
# --------------------------------------------------------------------------- #

@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_overlay_claim_dispatcher_assigns_chosen_driver(auth_client):
    OrderMeta.objects.create(order_id=600, estimated_duration=60)
    client = auth_client(perms=DISPATCHER, user_id=1)
    r = client.post(
        "/api/v1/car-orders/600/overlay-claim/", {"driver_id": 77, "car_id": 7}, format="json"
    )
    assert r.status_code == 200, r.content
    # A dispatcher legitimately assigns the order to the driver named in the body.
    assert OrderMeta.objects.get(order_id=600).driver_id == 77


@override_settings(REQUIRE_OVERLAY_AUTH=True)
@pytest.mark.django_db
def test_overlay_claim_plain_driver_body_driver_is_ignored(auth_client):
    OrderMeta.objects.create(order_id=601, estimated_duration=60)
    client = auth_client(perms=DRIVER, user_id=33)
    r = client.post(
        "/api/v1/car-orders/601/overlay-claim/", {"driver_id": 999, "car_id": 7}, format="json"
    )
    assert r.status_code == 200, r.content
    # A plain driver can only claim for THEMSELVES — the spoofed body id can't make
    # the order land on driver 999 (IDOR guard).
    assert OrderMeta.objects.get(order_id=601).driver_id == 33
