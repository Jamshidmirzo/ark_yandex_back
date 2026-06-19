"""API tests for native CarOrderViewSet endpoints the original workflow suite
skipped: reject, submit guards, draft destroy, the activity trail, and
me/active-order. Standalone wiring (router mounted locally) — see tests/urls.py.
"""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from auth_core.models import AccessGroup, UserAccessGroup
from car_orders.models import Car, CarOrder, CarType

User = get_user_model()

pytestmark = pytest.mark.urls("car_orders.tests.urls")


def _user(username, *groups):
    u = User.objects.create_user(username=username, password="pw")
    for name in groups:
        UserAccessGroup.objects.create(user=u, group=AccessGroup.objects.get(name=name))
    return u


def _client(u):
    c = APIClient()
    c.force_authenticate(user=u)
    return c


@pytest.fixture
def env(db):
    req = _user("req", "Car Requester")
    disp = _user("disp", "Car Admin")
    drv = _user("drv", "Driver")
    ct = CarType.objects.create(name="Легковая")
    car = Car.objects.create(model="Damas", plate_number="01A001AA", type=ct, status="active")
    car.drivers.add(drv)
    return {"req_user": req, "req": _client(req), "disp": _client(disp), "drv": _client(drv),
            "ct": ct, "car": car}


def _new_order(env):
    r = env["req"].post(
        "/api/v1/car-orders/", {"address": "Ул. Амир Темур 67", "car_type_id": env["ct"].id},
        format="json",
    )
    assert r.status_code == 201, r.content
    return r.data["id"]


def _submit(env, oid):
    env["req"].post(f"/api/v1/car-orders/{oid}/submit/")


def _approve(env, oid):
    env["disp"].post(f"/api/v1/car-orders/{oid}/admin-approve/")


# ---- reject ----------------------------------------------------------------

@pytest.mark.django_db
def test_reject_by_author_records_reason(env):
    oid = _new_order(env)
    _submit(env, oid)
    r = env["req"].post(f"/api/v1/car-orders/{oid}/reject/", {"reason": "changed mind"})
    assert r.status_code == 200
    assert r.data["status"] == "rejected"
    assert r.data["reject_reason"] == "changed mind"
    assert r.data["rejected_by"]["id"] == env["req_user"].id


@pytest.mark.django_db
def test_reject_by_dispatcher_on_awaiting(env):
    oid = _new_order(env)
    _submit(env, oid)
    _approve(env, oid)
    r = env["disp"].post(f"/api/v1/car-orders/{oid}/reject/")
    assert r.status_code == 200
    assert r.data["status"] == "rejected"


@pytest.mark.django_db
def test_reject_rejects_wrong_status(env):
    oid = _new_order(env)  # still a DRAFT — can't be rejected
    r = env["disp"].post(f"/api/v1/car-orders/{oid}/reject/")
    assert r.status_code == 400
    assert r.data["error"]["code"] == "INVALID_STATUS"


# ---- admin-approve / submit guards ----------------------------------------

@pytest.mark.django_db
def test_admin_approve_rejects_non_pending(env):
    oid = _new_order(env)  # DRAFT
    r = env["disp"].post(f"/api/v1/car-orders/{oid}/admin-approve/")
    assert r.status_code == 400
    assert r.data["error"]["code"] == "INVALID_STATUS"


@pytest.mark.django_db
def test_submit_forbidden_for_non_creator(env):
    oid = _new_order(env)
    r = env["disp"].post(f"/api/v1/car-orders/{oid}/submit/")
    assert r.status_code == 403


@pytest.mark.django_db
def test_submit_rejects_non_draft(env):
    oid = _new_order(env)
    _submit(env, oid)  # now pending
    r = env["req"].post(f"/api/v1/car-orders/{oid}/submit/")
    assert r.status_code == 400
    assert r.data["error"]["code"] == "INVALID_STATUS"


# ---- destroy ---------------------------------------------------------------

@pytest.mark.django_db
def test_destroy_draft_by_creator(env):
    oid = _new_order(env)
    r = env["req"].delete(f"/api/v1/car-orders/{oid}/")
    assert r.status_code == 204
    assert not CarOrder.objects.filter(pk=oid).exists()


@pytest.mark.django_db
def test_destroy_rejects_non_draft(env):
    oid = _new_order(env)
    _submit(env, oid)
    r = env["req"].delete(f"/api/v1/car-orders/{oid}/")
    assert r.status_code == 400
    assert r.data["error"]["code"] == "INVALID_STATUS"


# ---- activity + my/active-order -------------------------------------------

@pytest.mark.django_db
def test_activity_lists_the_audit_trail(env):
    oid = _new_order(env)
    _submit(env, oid)
    _approve(env, oid)
    acts = env["disp"].get(f"/api/v1/car-orders/{oid}/activity/").data
    kinds = {a["kind"] for a in acts}
    assert {"created", "sent", "approved"} <= kinds


@pytest.mark.django_db
def test_my_active_order_null_then_value(env):
    assert env["drv"].get("/api/v1/car-orders/me/active-order/").data is None
    oid = _new_order(env)
    _submit(env, oid)
    _approve(env, oid)
    env["drv"].patch(
        "/api/v1/car-orders/drivers/me/shift/", {"car_id": env["car"].id}, format="json"
    )
    env["drv"].post(f"/api/v1/car-orders/{oid}/claim/")
    env["drv"].post(f"/api/v1/car-orders/{oid}/start/")
    data = env["drv"].get("/api/v1/car-orders/me/active-order/").data
    assert data["id"] == oid and data["status"] == "in_progress"
