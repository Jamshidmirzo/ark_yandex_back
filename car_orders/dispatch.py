"""Server-side auto-dispatch — the brain of the backend worker
(`manage.py auto_dispatch`), so auto-assignment runs even when no dispatcher tab
is open (the logic used to live only in the browser).

Operates entirely on OUR overlay (no demo calls — like order_watchdog):
  • queue     = dispatchable, driverless, non-terminal OrderMeta that has coords
  • drivers   = on-shift DriverShiftState (carries the car + its type)
  • positions = DriverPosition (latest GPS per driver)
  • load      = a driver's active (non-terminal) order count (cap «1 активный»)

Mirrors the frontend dispatchSuggest.rankDrivers + the auto-loop due-rules:
auto-assign only an IDEAL candidate (on shift, right type, free), urgent now /
scheduled within the lead window / ASAP after it has waited long enough.
"""

import math
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from car_orders.models import DriverPosition, DriverShiftState, OrderMeta

TERMINAL = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)


def _haversine_km(a, b):
    """Great-circle distance in km between (lat, lng) tuples."""
    r = 6371.0
    p = math.pi / 180
    lat1, lng1 = a
    lat2, lng2 = b
    dlat = (lat2 - lat1) * p
    dlng = (lng2 - lng1) * p
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlng / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(h), math.sqrt(1 - h))


def active_count_by_driver():
    """driver_id → number of active (non-terminal) orders they hold (load cap)."""
    counts: dict[int, int] = {}
    for did in (
        OrderMeta.objects.filter(driver_id__isnull=False)
        .exclude(trip_state__in=TERMINAL)
        .values_list("driver_id", flat=True)
    ):
        counts[did] = counts.get(did, 0) + 1
    return counts


def rank_drivers(car_type_id, pickup, shifts, positions, load, max_load=1):
    """Rank on-shift drivers for one order. Returns a list of
    ``(driver_id, car_id, car_label, distance_km, note)`` best-first; ``note == ""``
    marks an IDEAL (auto-assignable) candidate. Tagged candidates (wrong type /
    overloaded / unknown type) sort last and are never auto-assigned."""
    out = []
    for s in shifts:
        n = load.get(s.driver_id, 0)
        pos = positions.get(s.driver_id)
        dist = _haversine_km(pos, pickup) if (pos and pickup) else None
        if car_type_id is None:
            note = "no-type"
        elif s.car_type_id != car_type_id:
            note = "wrong-type"
        elif n >= max_load:
            note = "overloaded"
        else:
            note = ""
        out.append((s.driver_id, s.car_id, f"{s.car_model} ({s.car_plate})".strip(), dist, note))

    def key(c):
        dist = c[3] if c[3] is not None else float("inf")
        load_n = load.get(c[0], 0)
        return (0 if c[4] == "" else 1, dist, load_n)

    out.sort(key=key)
    return out


def is_due(meta, first_seen, now, lead_min, stale_sec):
    """Is it time to auto-assign this order? Urgent → now; scheduled → within
    ``lead_min`` of the pickup; ASAP (no time) → after waiting ``stale_sec``."""
    if meta.is_urgent:
        return True
    if meta.planned_datetime:
        return meta.planned_datetime - now <= timedelta(minutes=lead_min)
    seen = first_seen.get(meta.order_id, now)
    return (now - seen) >= timedelta(seconds=stale_sec)


def claim(order_id, driver_id, car_id, car_label):
    """Assign an order to a driver in our overlay — the worker's equivalent of
    overlay-claim, with the same «1 водитель = 1 активный заказ» guard. Returns
    True on success, False if the order is already taken or the driver is busy."""
    from car_orders.ws import notify_order_status

    with transaction.atomic():
        meta = OrderMeta.objects.select_for_update().filter(order_id=order_id).first()
        if meta is None:
            return False
        # Already held by a driver and still active → don't steal it.
        if meta.driver_id is not None and meta.trip_state not in TERMINAL:
            return False
        # One active order per driver (matches OverlayClaimView / demo).
        busy = (
            OrderMeta.objects.filter(driver_id=driver_id)
            .exclude(trip_state__in=TERMINAL)
            .exclude(order_id=order_id)
            .exists()
        )
        if busy:
            return False
        meta.driver_id = driver_id
        meta.car_id = car_id
        meta.car_label = car_label
        meta.overlay_claimed = True
        if meta.trip_state in TERMINAL:
            meta.returning = False
        meta.trip_state = OrderMeta.TripState.ASSIGNED
        meta.save()
    notify_order_status(meta, OrderMeta.TripState.ASSIGNED)
    return True


def fresh_positions(max_age_sec, now=None):
    """driver_id → (lat, lng) for drivers with a GPS fix newer than max_age."""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=max_age_sec)
    return {
        p.driver_id: (p.lat, p.lng)
        for p in DriverPosition.objects.filter(last_seen__gte=cutoff)
    }


def queue_orders():
    """Approved, driverless, non-terminal orders with a pickup — ready to dispatch."""
    return list(
        OrderMeta.objects.filter(
            dispatchable=True,
            driver_id__isnull=True,
            origin_lat__isnull=False,
            origin_lng__isnull=False,
        ).exclude(trip_state__in=TERMINAL)
    )


def run_once(first_seen, now=None, *, lead_min, stale_sec, pos_max_age, max_load=1):
    """One dispatch pass. Returns a list of (order_id, driver_id) just assigned.
    `first_seen` is a mutable {order_id: datetime} carried across passes."""
    now = now or timezone.now()
    shifts = list(DriverShiftState.objects.all())
    positions = fresh_positions(pos_max_age, now)
    load = active_count_by_driver()
    assigned = []
    for meta in queue_orders():
        first_seen.setdefault(meta.order_id, now)
        if not is_due(meta, first_seen, now, lead_min, stale_sec):
            continue
        ranked = rank_drivers(
            meta.car_type_id, (meta.origin_lat, meta.origin_lng), shifts, positions, load, max_load
        )
        if not ranked or ranked[0][4] != "":  # no candidate, or best isn't ideal
            continue
        driver_id, car_id, car_label, _dist, _note = ranked[0]
        if claim(meta.order_id, driver_id, car_id, car_label):
            load[driver_id] = load.get(driver_id, 0) + 1  # reflect within this pass
            assigned.append((meta.order_id, driver_id))
    return assigned
