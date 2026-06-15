"""API tests for the car-orders block: the full workflow plus the permission
and Р1/Р3 invariants. Run with ``pytest``."""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from auth_core.models import AccessGroup, UserAccessGroup
from car_orders.models import Car, CarType

User = get_user_model()

# Run these against the standalone wiring (the router mounted locally) — see
# car_orders/tests/urls.py. Without this the gateway proxies CRUD to upstream.
pytestmark = pytest.mark.urls("car_orders.tests.urls")


def _user(username, *groups):
    u = User.objects.create_user(username=username, password="pw")
    u.is_active = True
    u.save()
    for name in groups:
        UserAccessGroup.objects.create(user=u, group=AccessGroup.objects.get(name=name))
    return u


def _client(u):
    # force_authenticate, not a real login: ``/api/v1/auth/login/`` is proxied to the
    # upstream demo backend (which doesn't have the test users), and the car-orders
    # views' DemoTokenAuthentication likewise can't validate a locally-minted token.
    # Auth/login isn't car_orders' concern — bypass it and exercise the business logic.
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
    return {
        "req": _client(req),
        "disp": _client(disp),
        "drv": _client(drv),
        "ct": ct,
        "car": car,
    }


def _new_order(client, ct):
    r = client.post(
        "/api/v1/car-orders/",
        {"address": "Ул. Амир Темур 67", "car_type_id": ct.id},
        format="json",
    )
    assert r.status_code == 201, r.content
    return r.data["id"]


@pytest.mark.django_db
def test_full_workflow_with_shift_and_tracking(env):
    oid = _new_order(env["req"], env["ct"])

    assert env["req"].post(f"/api/v1/car-orders/{oid}/submit/").data["status"] == "pending"
    approved = env["disp"].post(f"/api/v1/car-orders/{oid}/admin-approve/").data
    assert approved["status"] == "awaiting_driver"

    # Р1: driver goes on shift with a car; feed is filtered to that car's type.
    assert (
        env["drv"]
        .patch("/api/v1/car-orders/drivers/me/shift/", {"car_id": env["car"].id}, format="json")
        .status_code
        == 200
    )
    feed = env["drv"].get("/api/v1/car-orders/").data
    assert any(o["id"] == oid for o in feed["results"])

    # claim reserves the window into the driver's schedule (shift car, Р1).
    claimed = env["drv"].post(f"/api/v1/car-orders/{oid}/claim/").data
    assert claimed["status"] == "scheduled"
    assert claimed["car"]["plate_number"] == "01A001AA"

    # start begins the trip → in_progress.
    started = env["drv"].post(f"/api/v1/car-orders/{oid}/start/").data
    assert started["status"] == "in_progress"

    # Р3: heartbeat then the order author sees the live position.
    env["drv"].post(
        "/api/v1/car-orders/drivers/me/location/", {"lat": 41.31, "lng": 69.27}, format="json"
    )
    track = env["req"].get(f"/api/v1/car-orders/{oid}/").data["driver_location"]
    assert track and track["lat"] == 41.31 and track["lng"] == 69.27

    assert env["drv"].post(f"/api/v1/car-orders/{oid}/complete/").data["status"] == "completed"


@pytest.mark.django_db
def test_requester_cannot_approve(env):
    oid = _new_order(env["req"], env["ct"])
    env["req"].post(f"/api/v1/car-orders/{oid}/submit/")
    assert env["req"].post(f"/api/v1/car-orders/{oid}/admin-approve/").status_code == 403


@pytest.mark.django_db
def test_claim_requires_active_shift(env):
    oid = _new_order(env["req"], env["ct"])
    env["req"].post(f"/api/v1/car-orders/{oid}/submit/")
    env["disp"].post(f"/api/v1/car-orders/{oid}/admin-approve/")
    r = env["drv"].post(f"/api/v1/car-orders/{oid}/claim/")
    assert r.status_code == 400
    assert r.data["error"]["code"] == "NO_SHIFT"


@pytest.mark.django_db
def test_draft_edit_only_by_creator(env):
    oid = _new_order(env["req"], env["ct"])
    # dispatcher (not the author) may not edit the draft
    r = env["disp"].patch(f"/api/v1/car-orders/{oid}/", {"address": "X"}, format="json")
    assert r.status_code in (403, 404)


@pytest.mark.django_db
def test_only_assigned_driver_completes(env):
    other = _client(_user("drv2", "Driver"))
    car2 = Car.objects.create(model="Faw", plate_number="01A002AA", type=env["ct"], status="active")
    car2.drivers.add(User.objects.get(username="drv2"))

    oid = _new_order(env["req"], env["ct"])
    env["req"].post(f"/api/v1/car-orders/{oid}/submit/")
    env["disp"].post(f"/api/v1/car-orders/{oid}/admin-approve/")
    env["drv"].patch(
        "/api/v1/car-orders/drivers/me/shift/", {"car_id": env["car"].id}, format="json"
    )
    env["drv"].post(f"/api/v1/car-orders/{oid}/claim/")

    # a different driver cannot complete someone else's trip
    r = other.post(f"/api/v1/car-orders/{oid}/complete/")
    assert r.status_code == 403


# --- Scheduling: non-overlapping windows, buffer, lifecycle -----------------

from datetime import timedelta  # noqa: E402

from django.core.management import call_command  # noqa: E402
from django.test import override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402

from car_orders import scheduling  # noqa: E402
from car_orders.models import CarOrder, DriverShift  # noqa: E402


def _on_shift(env):
    env["drv"].patch(
        "/api/v1/car-orders/drivers/me/shift/", {"car_id": env["car"].id}, format="json"
    )


def _windowed_order(env, start, duration=120, latest_start=None):
    """Create → submit → approve an order with a planned window. ``start`` is a
    datetime; returns the order id, ready for a driver to claim."""
    body = {
        "address": "Ул. Амир Темур 67",
        "car_type_id": env["ct"].id,
        "planned_datetime": start.isoformat(),
        "estimated_duration": duration,
    }
    if latest_start is not None:
        body["latest_start"] = latest_start.isoformat()
    r = env["req"].post("/api/v1/car-orders/", body, format="json")
    assert r.status_code == 201, r.content
    oid = r.data["id"]
    env["req"].post(f"/api/v1/car-orders/{oid}/submit/")
    env["disp"].post(f"/api/v1/car-orders/{oid}/admin-approve/")
    return oid


@pytest.mark.django_db
def test_non_overlapping_windows_allowed(env):
    """The 5h + 2h scenario: a driver holds two orders in separate windows."""
    _on_shift(env)
    base = timezone.now() + timedelta(days=1)
    a = _windowed_order(env, base, duration=300)  # 10:00–15:00
    b = _windowed_order(env, base + timedelta(hours=6), duration=120)  # 16:00–18:00

    assert env["drv"].post(f"/api/v1/car-orders/{a}/claim/").data["status"] == "scheduled"
    rb = env["drv"].post(f"/api/v1/car-orders/{b}/claim/")
    assert rb.status_code == 200
    assert rb.data["status"] == "scheduled"


@pytest.mark.django_db
def test_overlapping_window_rejected(env):
    _on_shift(env)
    base = timezone.now() + timedelta(days=1)
    a = _windowed_order(env, base, duration=300)  # 10:00–15:00
    overlap = _windowed_order(env, base + timedelta(hours=2), duration=120)  # 12:00–14:00

    env["drv"].post(f"/api/v1/car-orders/{a}/claim/")
    r = env["drv"].post(f"/api/v1/car-orders/{overlap}/claim/")
    assert r.status_code == 409
    assert r.data["error"]["code"] == "TIME_CONFLICT"
    assert r.data["error"]["details"]["order_id"] == a


@pytest.mark.django_db
def test_travel_buffer_enforced(env):
    """A new order starting inside the 30-min travel buffer after A is blocked."""
    _on_shift(env)
    base = timezone.now() + timedelta(days=1)
    a = _windowed_order(env, base, duration=300)  # ends 15:00
    env["drv"].post(f"/api/v1/car-orders/{a}/claim/")

    too_soon = _windowed_order(env, base + timedelta(hours=5, minutes=20))  # 15:20 < 15:30
    assert env["drv"].post(f"/api/v1/car-orders/{too_soon}/claim/").status_code == 409

    far_enough = _windowed_order(env, base + timedelta(hours=6))  # 16:00
    assert env["drv"].post(f"/api/v1/car-orders/{far_enough}/claim/").status_code == 200


@pytest.mark.django_db
def test_cancel_frees_window(env):
    _on_shift(env)
    base = timezone.now() + timedelta(days=1)
    a = _windowed_order(env, base, duration=300)
    env["drv"].post(f"/api/v1/car-orders/{a}/claim/")

    cancelled = env["disp"].post(f"/api/v1/car-orders/{a}/cancel/", {"reason": "client off"})
    assert cancelled.data["status"] == "cancelled"

    # the freed window can now be taken by an overlapping order
    overlap = _windowed_order(env, base + timedelta(hours=2), duration=120)
    assert env["drv"].post(f"/api/v1/car-orders/{overlap}/claim/").status_code == 200


@pytest.mark.django_db
def test_release_returns_to_awaiting(env):
    _on_shift(env)
    base = timezone.now() + timedelta(days=1)
    a = _windowed_order(env, base)
    env["drv"].post(f"/api/v1/car-orders/{a}/claim/")

    released = env["drv"].post(f"/api/v1/car-orders/{a}/release/").data
    assert released["status"] == "awaiting_driver"
    assert released["driver"] is None


@pytest.mark.django_db
def test_reassign_by_dispatcher(env):
    _on_shift(env)
    base = timezone.now() + timedelta(days=1)
    a = _windowed_order(env, base)
    env["drv"].post(f"/api/v1/car-orders/{a}/claim/")

    r = env["disp"].post(f"/api/v1/car-orders/{a}/reassign/")
    assert r.status_code == 200
    assert r.data["status"] == "awaiting_driver"
    assert r.data["driver"] is None


@pytest.mark.django_db
def test_only_one_active_trip(env):
    _on_shift(env)
    base = timezone.now() + timedelta(days=1)
    a = _windowed_order(env, base, duration=120)
    b = _windowed_order(env, base + timedelta(hours=4), duration=120)
    env["drv"].post(f"/api/v1/car-orders/{a}/claim/")
    env["drv"].post(f"/api/v1/car-orders/{b}/claim/")

    assert env["drv"].post(f"/api/v1/car-orders/{a}/start/").data["status"] == "in_progress"
    r = env["drv"].post(f"/api/v1/car-orders/{b}/start/")
    assert r.status_code == 400
    assert r.data["error"]["code"] == "DRIVER_BUSY"


@pytest.mark.django_db
def test_extend_flags_conflict_with_next(env):
    _on_shift(env)
    base = timezone.now() + timedelta(days=1)
    a = _windowed_order(env, base, duration=120)  # 10:00–12:00
    b = _windowed_order(env, base + timedelta(hours=3), duration=120)  # 13:00–15:00
    env["drv"].post(f"/api/v1/car-orders/{a}/claim/")
    env["drv"].post(f"/api/v1/car-orders/{b}/claim/")

    # extend A by 2h → 10:00–14:00, now collides with B's window.
    r = env["drv"].post(f"/api/v1/car-orders/{a}/extend/", {"minutes": 120})
    assert r.status_code == 200
    assert r.data["schedule_conflict"] is not None
    assert r.data["schedule_conflict"]["order_id"] == b


@pytest.mark.django_db
def test_estimate_returns_duration_and_geometry(env):
    r = env["disp"].post(
        "/api/v1/car-orders/estimate/",
        {"origin_lat": 41.31, "origin_lng": 69.24, "dest_lat": 41.35, "dest_lng": 69.29},
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["duration_minutes"] > 0
    assert len(r.data["geometry"]) >= 2
    assert r.data["source"] in ("osrm", "haversine")


@pytest.mark.django_db
def test_schedule_endpoint_lists_committed(env):
    _on_shift(env)
    base = timezone.now() + timedelta(days=1)
    a = _windowed_order(env, base, duration=120)
    env["drv"].post(f"/api/v1/car-orders/{a}/claim/")
    sched = env["drv"].get("/api/v1/car-orders/drivers/me/schedule/").data
    assert [o["id"] for o in sched] == [a]
    assert sched[0]["status"] == "scheduled"


@pytest.mark.django_db
def test_needs_reassign_when_overrunning_past_latest_start():
    """Driver on an overrunning trip → the next order's projected start blows
    past its latest_start, so it must be reassigned."""
    requester = User.objects.create_user(username="r0", password="pw")
    drv = User.objects.create_user(username="d0", password="pw")
    now = timezone.now()
    # current trip was due to finish 1h ago — still in progress (overrunning).
    CarOrder.objects.create(
        created_by=requester,
        driver=drv,
        status=CarOrder.Status.IN_PROGRESS,
        planned_datetime=now - timedelta(hours=3),
        estimated_duration=timedelta(hours=2),
    )
    nxt = CarOrder.objects.create(
        created_by=requester,
        driver=drv,
        status=CarOrder.Status.SCHEDULED,
        planned_datetime=now - timedelta(minutes=30),
        estimated_duration=timedelta(hours=1),
        latest_start=now - timedelta(minutes=10),
    )
    assert scheduling.needs_reassign(nxt, now) is True

    nxt.latest_start = now + timedelta(hours=5)
    nxt.save(update_fields=["latest_start"])
    assert scheduling.needs_reassign(nxt, now) is False


@pytest.mark.django_db
@override_settings(CAR_ORDER_OSRM_URL="")  # force the deterministic offline path
def test_simulator_moves_driver(env):
    _on_shift(env)
    base = timezone.now() + timedelta(days=1)
    r = env["req"].post(
        "/api/v1/car-orders/",
        {
            "address": "Ул. Амир Темур 67",
            "car_type_id": env["ct"].id,
            "planned_datetime": base.isoformat(),
            "estimated_duration": 120,
            "origin_lat": 41.31,
            "origin_lng": 69.24,
            "address_lat": 41.35,
            "address_lng": 69.29,
        },
        format="json",
    )
    oid = r.data["id"]
    env["req"].post(f"/api/v1/car-orders/{oid}/submit/")
    env["disp"].post(f"/api/v1/car-orders/{oid}/admin-approve/")
    env["drv"].post(f"/api/v1/car-orders/{oid}/claim/")

    call_command("simulate_driver", order=oid, steps=4, interval=0)

    shift = DriverShift.objects.get(driver__username="drv", ended_at__isnull=True)
    assert shift.last_seen is not None
    # offline geometry is exactly [origin, dest]; the car ends at the destination.
    assert abs(shift.lat - 41.35) < 1e-6
    assert abs(shift.lng - 69.29) < 1e-6
