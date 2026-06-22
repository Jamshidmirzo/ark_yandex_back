"""Permission matrix for the NATIVE car_orders viewsets (the *standalone* router).

In production these paths proxy to the upstream demo backend (gateway), so they are
mounted only under the test URLconf ``car_orders.tests.urls`` — we point
``ROOT_URLCONF`` there with ``@pytest.mark.urls`` and force-authenticate a real
Django ``User`` (the native viewsets use the JWT default auth, which a fake bearer
token can't satisfy; permissions resolve through the DB-backed ``user_has_permission``).

Each action is checked against eight roles. The gate fires at ``check_permissions``
(before the object is fetched), so a role lacking the codename gets 403 even on a
missing order; an allowed role gets through the gate (then a 200/400/404 from the
body — anything but 403/401). This pins the ``_action_permissions`` map + the inline
``user_has_permission`` checks in views.py so a future edit that drops a gate fails here.
"""

import pytest
from rest_framework.test import APIClient

from car_orders.tests.conftest import (
    ADMIN,
    CREATOR,
    CUSTOMER,
    DISPATCHER,
    DRIVER,
    DRIVER_ADMIN,
    GARAGE,
    REPORTER,
)

NATIVE_ROLES = {
    "customer": CUSTOMER,        # authenticated, no car-order perms
    "creator": CREATOR,          # car_order:create
    "driver": DRIVER,            # driver:accept_order + driver:trip_control
    "dispatcher": DISPATCHER,    # car_order:approve
    "garage": GARAGE,            # garage:*
    "driver_admin": DRIVER_ADMIN,  # driver:list + driver:assign_to_user
    "reporter": REPORTER,        # vehicle_report:*
    "admin": ADMIN,              # administrator (wildcard)
}

# Allowed-role sets, named by the codename the action requires.
CREATE = {"creator", "admin"}            # car_order:create
APPROVE = {"dispatcher", "admin"}        # car_order:approve
DRIVE = {"driver", "admin"}              # driver:accept_order
TRIP = {"driver", "admin"}               # driver:trip_control
GARAGE_LIST = {"garage", "admin"}        # garage:list / garage:retrieve
GARAGE_WRITE = {"garage", "admin"}       # garage:create / garage:update / garage:delete
DRV_LIST = {"driver_admin", "admin"}     # driver:list
ASSIGN = {"driver_admin", "admin"}       # driver:assign_to_user
REPORT = {"reporter", "admin"}           # vehicle_report:create
ANY_AUTH = set(NATIVE_ROLES)             # IsAuthenticated only (no codename)

ESTIMATE_BODY = {"origin_lat": 41.31, "origin_lng": 69.24, "dest_lat": 41.35, "dest_lng": 69.29}

# (id, method, path, body, allowed_roles)
NATIVE_CASES = [
    # CarOrderViewSet — _action_permissions + inline checks
    ("create", "POST", "/api/v1/car-orders/", {"name": "T"}, CREATE),
    ("estimate", "POST", "/api/v1/car-orders/estimate/", ESTIMATE_BODY, CREATE),
    ("admin_approve", "POST", "/api/v1/car-orders/1/admin-approve/", None, APPROVE),
    ("reassign", "POST", "/api/v1/car-orders/1/reassign/", None, APPROVE),
    ("claim", "POST", "/api/v1/car-orders/1/claim/", None, DRIVE),
    ("release", "POST", "/api/v1/car-orders/1/release/", None, DRIVE),
    ("start", "POST", "/api/v1/car-orders/1/start/", None, TRIP),
    ("complete", "POST", "/api/v1/car-orders/1/complete/", None, TRIP),
    # Garage (CarType / Car) — _garage_permissions. NB: the router is mounted under
    # /api/v1/car-orders/, so every sibling viewset lives there too (matches the web
    # client's /car-orders/cars/, /car-orders/drivers/, … paths).
    ("cartype_list", "GET", "/api/v1/car-orders/car-types/", None, GARAGE_LIST),
    ("cartype_create", "POST", "/api/v1/car-orders/car-types/", {"name": "X"}, GARAGE_WRITE),
    ("cartype_update", "PATCH", "/api/v1/car-orders/car-types/1/", {"name": "Y"}, GARAGE_WRITE),
    ("cartype_delete", "DELETE", "/api/v1/car-orders/car-types/1/", None, GARAGE_WRITE),
    ("car_list", "GET", "/api/v1/car-orders/cars/", None, GARAGE_LIST),
    ("car_create", "POST", "/api/v1/car-orders/cars/", {"model": "X"}, GARAGE_WRITE),
    ("car_update", "PATCH", "/api/v1/car-orders/cars/1/", {"model": "Y"}, GARAGE_WRITE),
    ("car_delete", "DELETE", "/api/v1/car-orders/cars/1/", None, GARAGE_WRITE),
    # DriverViewSet — inline user_has_permission
    ("driver_list", "GET", "/api/v1/car-orders/drivers/", None, DRV_LIST),
    ("make_driver", "POST", "/api/v1/car-orders/drivers/make-driver/", {"user_id": 999999}, ASSIGN),
    ("remove_driver", "POST", "/api/v1/car-orders/drivers/remove-driver/", {"user_id": 999999}, ASSIGN),
    ("my_schedule", "GET", "/api/v1/car-orders/drivers/me/schedule/", None, DRIVE),
    ("my_shift_get", "GET", "/api/v1/car-orders/drivers/me/shift/", None, DRIVE),
    ("my_location", "POST", "/api/v1/car-orders/drivers/me/location/", {"lat": 41.3, "lng": 69.2}, DRIVE),
    # VehicleReportViewSet
    ("vehicle_report_create", "POST", "/api/v1/car-orders/vehicle-reports/", {}, REPORT),
    # IsAuthenticated-only (any authenticated role gets through the gate)
    ("my_cars", "GET", "/api/v1/car-orders/drivers/me/cars/", None, ANY_AUTH),
    ("car_order_list", "GET", "/api/v1/car-orders/", None, ANY_AUTH),
    ("vehicle_report_list", "GET", "/api/v1/car-orders/vehicle-reports/", None, ANY_AUTH),
]


def _call(client, method, path, body):
    fn = getattr(client, method.lower())
    if method in ("GET", "DELETE"):
        return fn(path)
    return fn(path, body or {}, format="json")


@pytest.mark.urls("car_orders.tests.urls")
@pytest.mark.django_db
@pytest.mark.parametrize("case", NATIVE_CASES, ids=[c[0] for c in NATIVE_CASES])
def test_native_permission_matrix(case, make_user):
    label, method, path, body, allowed = case
    for role, perms in NATIVE_ROLES.items():
        user = make_user(perms=perms)
        client = APIClient()
        client.force_authenticate(user=user)
        resp = _call(client, method, path, body)
        if role in allowed:
            assert resp.status_code != 403, (
                f"{label}: {role} should be ALLOWED, got 403 ({getattr(resp, 'data', b'')!r})"
            )
        else:
            assert resp.status_code == 403, (
                f"{label}: {role} should be DENIED (403), got {resp.status_code}"
            )


@pytest.mark.urls("car_orders.tests.urls")
@pytest.mark.django_db
def test_native_unauthenticated_is_401():
    """No credentials at all → 401 from IsAuthenticated, before any codename gate."""
    r = APIClient().post("/api/v1/car-orders/", {"name": "T"}, format="json")
    assert r.status_code in (401, 403)


# --------------------------------------------------------------------------- #
# cancel / reject — inline-gated on (author ∨ car_order:reject) AFTER get_object,#
# so the matrix (which probes a missing order) can't reach the gate: a missing   #
# order 404s first. Exercise them against a REAL order so the 403 surfaces.      #
# --------------------------------------------------------------------------- #

@pytest.mark.urls("car_orders.tests.urls")
@pytest.mark.django_db
def test_create_direct_skips_approval(make_user):
    """car_order:create_direct fast-tracks a brand-new order to the driver queue.

    A plain requester still lands a DRAFT (the moderation gate); a requester granted
    create_direct — and a dispatcher (approve) — land straight in AWAITING_DRIVER.
    Pins CarOrderViewSet.create so a future edit can't quietly drop the fast-track
    or, worse, fast-track a plain requester who should wait for approval.
    """
    from car_orders.models import CarOrder

    def create_status(perms):
        user = make_user(perms=perms)
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(
            "/api/v1/car-orders/",
            {"project_name": "T", "address": "B"},
            format="json",
        )
        assert resp.status_code == 201, (perms, resp.status_code, getattr(resp, "data", None))
        return resp.data["status"]

    # Plain requester → draft, awaits a dispatcher's approval.
    assert create_status(["car_order:create", "car_order:list_own"]) == CarOrder.Status.DRAFT
    # Requester WITH create_direct → straight to the driver queue, no driver perms.
    assert (
        create_status(["car_order:create", "car_order:create_direct"])
        == CarOrder.Status.AWAITING_DRIVER
    )
    # Dispatcher (create + approve) also fast-tracks their own create.
    assert (
        create_status(["car_order:create", "car_order:approve"])
        == CarOrder.Status.AWAITING_DRIVER
    )


@pytest.mark.urls("car_orders.tests.urls")
@pytest.mark.django_db
@pytest.mark.parametrize("action", ["reject", "cancel"])
def test_native_reject_cancel_require_author_or_reject_perm(make_user, action):
    from car_orders.models import CarOrder

    author = make_user()  # no perms — but the order's creator
    # A dispatcher (car_order:approve) can SEE the order, but reject/cancel uniquely
    # require car_order:reject (or authorship) — approve is NOT enough. This is the
    # subtle gap: every other dispatcher action keys on approve, these two don't.
    dispatcher = make_user(perms=["car_order:approve"])
    admin = make_user(perms=["administrator"])  # satisfies car_order:reject via hierarchy

    def fresh():
        return CarOrder.objects.create(created_by=author, status=CarOrder.Status.AWAITING_DRIVER)

    def call(user, order):
        c = APIClient()
        c.force_authenticate(user=user)
        return c.post(f"/api/v1/car-orders/{order.id}/{action}/")

    assert call(dispatcher, fresh()).status_code == 403  # approve ≠ reject
    assert call(author, fresh()).status_code != 403  # author may always
    assert call(admin, fresh()).status_code != 403  # administrator ⊇ car_order:reject


@pytest.mark.urls("car_orders.tests.urls")
@pytest.mark.django_db
def test_native_create_hierarchy_admin_passes(make_user):
    """``administrator`` satisfies ``car_order:create`` via the ARK hierarchy — the
    same expansion the web client uses — so an admin is never blocked by a codename
    gate (here: not 403 on create)."""
    client = APIClient()
    client.force_authenticate(user=make_user(perms=["administrator"]))
    r = client.post("/api/v1/car-orders/", {"name": "T"}, format="json")
    assert r.status_code != 403
