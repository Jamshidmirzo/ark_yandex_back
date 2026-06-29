"""Shared pytest fixtures for the car-orders test suite."""

import pytest
from rest_framework.test import APIClient

import config.auth as demo_auth
from car_orders.services import routing


def pytest_collection_modifyitems(items):
    """Grant every DB-touching test access to BOTH databases (default + geo).

    The telemetry models (DriverPosition, OrderLiveLocation) live in the `geo`
    PostGIS database (car_orders.routers.GeoRouter); without this a test that queries
    them fails with «Database queries to 'geo' are not allowed». Tests declare a bare
    ``@pytest.mark.django_db`` per function, so rather than editing every decorator we
    merge ``databases`` into the closest marker (preserving transaction/
    reset_sequences). An explicit ``databases=`` on a test is respected as-is.
    """
    all_dbs = ("default", "geo")
    for item in items:
        marker = item.get_closest_marker("django_db")
        if marker is None or marker.kwargs.get("databases") is not None:
            continue
        kwargs = {**marker.kwargs, "databases": all_dbs}
        item.add_marker(pytest.mark.django_db(*marker.args, **kwargs), append=False)


@pytest.fixture(autouse=True)
def _isolate_route_cache():
    """The OSRM route memo is a process-global dict; clear it around every test so a
    result cached by one test can never leak into another (e.g. an ``osrm`` route
    bleeding into a test that pins ``CAR_ORDER_OSRM_URL=""``)."""
    routing.clear_route_cache()
    yield
    routing.clear_route_cache()


# --------------------------------------------------------------------------- #
# Permission role bundles                                                      #
#                                                                             #
# The codename sets mirror the seed access groups in                          #
# ``auth_core/migrations/0002_seed_permissions.py`` so a role is named once    #
# and reused across the permission-matrix tests. ``CUSTOMER`` is an           #
# authenticated user with no car-order permissions (the lower bound).         #
# --------------------------------------------------------------------------- #
DRIVER = ["driver:accept_order", "driver:trip_control"]
DISPATCHER = ["car_order:approve"]            # the backend's "OverlayDispatcher"
CREATOR = ["car_order:create", "car_order:list_own"]
GARAGE = [
    "garage:list",
    "garage:retrieve",
    "garage:create",
    "garage:update",
    "garage:delete",
]
DRIVER_ADMIN = ["driver:list", "driver:assign_to_user"]
REPORTER = ["vehicle_report:create", "vehicle_report:list_own", "vehicle_report:retrieve"]
ADMIN = ["administrator"]
HYBRID = DISPATCHER + DRIVER                   # a dispatcher who also drives
CUSTOMER: list[str] = []                       # authenticated, zero car-order perms


class _DemoResp:
    """Stand-in for the ``requests.Response`` returned by demo ``/auth/me/``."""

    def __init__(self, ok, data):
        self.ok = ok
        self._data = data

    def json(self):
        return self._data


@pytest.fixture
def auth_client(monkeypatch):
    """Factory → an ``APIClient`` authenticated as a demo-bridged user.

    Drives the real ``DemoTokenAuthentication`` path by faking the demo
    ``/auth/me/`` lookup, keyed by bearer token so several distinct clients can
    coexist in one test (e.g. a driver *and* a dispatcher). Usage::

        client = auth_client(perms=DISPATCHER)            # has car_order:approve
        anon_authed = auth_client(perms=CUSTOMER)         # authed, no perms
        same_driver = auth_client(perms=DRIVER, user_id=42)

    ``perms=None`` omits the permissions key entirely (demo returned no list);
    ``perms=[]`` (``CUSTOMER``) is an authenticated user with zero permissions.
    Clears the per-process token cache around the test.
    """
    demo_auth._CACHE.clear()
    registry: dict[str, dict] = {}

    def _fake_get(url, headers=None, timeout=None, **kwargs):
        token = ""
        auth = (headers or {}).get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        payload = registry.get(token)
        return _DemoResp(payload is not None, payload or {})

    monkeypatch.setattr(demo_auth.requests, "get", _fake_get)

    state = {"n": 0}

    def _make(perms=None, user_id=None, is_superuser=False):
        state["n"] += 1
        if user_id is None:
            user_id = state["n"]
        token = f"tok{state['n']}"
        payload = {"id": user_id, "username": f"u{user_id}"}
        if perms is not None:
            payload["permissions"] = list(perms)
        if is_superuser:
            payload["is_superuser"] = True
        registry[token] = payload
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    yield _make
    demo_auth._CACHE.clear()


@pytest.fixture
def make_user(db):
    """Factory → a real Django ``User`` holding ARK ``perms`` via an AccessGroup.

    For the *native* viewset tests (mounted under ``car_orders.tests.urls``) where
    permission checks resolve through the DB-backed ``user_has_permission`` /
    ``HasPermission``. Pair with ``APIClient().force_authenticate(user=...)`` —
    the native viewsets use the project's JWT default auth, which a fake bearer
    token can't satisfy.
    """
    from django.contrib.auth import get_user_model

    from auth_core.models import AccessGroup, Permission, UserAccessGroup

    User = get_user_model()
    state = {"n": 0}

    def _make(perms=None, is_superuser=False):
        state["n"] += 1
        user = User.objects.create(
            username=f"nu{state['n']}",
            is_superuser=is_superuser,
            is_staff=is_superuser,
        )
        if perms:
            group = AccessGroup.objects.create(name=f"grp{state['n']}")
            for codename in perms:
                perm, _ = Permission.objects.get_or_create(codename=codename)
                group.permissions.add(perm)
            UserAccessGroup.objects.create(user=user, group=group)
        return user

    return _make
