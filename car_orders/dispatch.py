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

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from car_orders.geometry import MAX_LEG_KM, downsample, haversine_km
from car_orders.models import DispatchSettings, DriverPosition, DriverShiftState, OrderMeta

logger = logging.getLogger(__name__)

TERMINAL = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)


def auto_enabled():
    """Is auto-dispatch live right now? The env var ``AUTO_DISPATCH_ENABLED`` is the
    hard ops kill-switch; the dispatcher's runtime toggle (``DispatchSettings``) is
    the in-app switch. Both must be on. Defaults safe-off if the DB row is missing."""
    if not getattr(settings, "AUTO_DISPATCH_ENABLED", True):
        return False
    try:
        return DispatchSettings.load().auto_enabled
    except Exception:
        # AUDIT M1: don't let a real DB error masquerade as «auto-dispatch off» — fail
        # safe-off (the worker must keep running) but LOG it so the outage is visible.
        logger.exception("car_orders: auto_enabled() check failed — defaulting to OFF")
        return False

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
        dist = haversine_km(pos[0], pos[1], pickup[0], pickup[1]) if (pos and pickup) else None
        if car_type_id is not None and s.car_type_id != car_type_id:
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
    from car_orders import concurrency
    from car_orders.ws import notify_order_status

    with transaction.atomic():
        # Serialise concurrent claims for THIS driver (worker vs API, two passes) so
        # the «busy?» check below can't race two orders onto one driver — AUDIT C1.
        concurrency.lock_driver(driver_id)
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
    push_order_route(meta)  # send the approach route the moment it's assigned
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


def _latest_pos(driver_id):
    if driver_id is None:
        return None
    p = DriverPosition.objects.filter(driver_id=driver_id).first()
    return (p.lat, p.lng) if p else None


def order_leg(meta, driver_pos=None):
    """The ``((slat,slng),(elat,elng))`` the driver should drive at THIS stage, or
    None. We always describe what the driver should do NEXT, so the server owns the
    route at every stage — not only once the trip is started:
      assigned / to_client → (driver's position → pickup)   approach
      at_client / in_trip  → (pickup → destination)          the trip
      in_trip (returning)  → (destination → return point)    the way back
      at_destination + has_return & !returning → (dest → return)  preview of return
      waiting / at_destination(final) / terminal → None      (parked)
    """
    ts = meta.trip_state
    if ts in TERMINAL:
        return None
    o = (meta.origin_lat, meta.origin_lng)
    d = (meta.address_lat, meta.address_lng)
    if None in o or None in d:
        return None
    r = (
        (meta.return_lat, meta.return_lng)
        if meta.return_lat is not None and meta.return_lng is not None
        else o
    )
    TS = OrderMeta.TripState

    def _start(fallback):
        """Moving legs start from the driver's CURRENT position (so a re-route
        follows the road they actually took), falling back to the leg origin."""
        return driver_pos if (driver_pos and driver_pos[0] is not None) else fallback

    if ts == TS.IN_TRIP:
        end = r if meta.returning else d
        return (_start(d if meta.returning else o), end)
    if ts == TS.AT_DESTINATION:
        return (d, r) if (meta.has_return and not meta.returning) else None
    if ts == TS.AT_CLIENT:
        return (_start(o), d)
    if ts in (TS.ASSIGNED, TS.TO_CLIENT):
        if not driver_pos or driver_pos[0] is None:
            return None
        if abs(driver_pos[0] - o[0]) < 1e-6 and abs(driver_pos[1] - o[1]) < 1e-6:
            return None  # already on the pickup point → no line
        return (driver_pos, o)
    return None  # waiting → parked, no moving route


def push_order_route(meta, driver_pos=None):
    """Compute the current leg's route (OSRM) and broadcast its geometry + store it
    on OrderLiveLocation, so the map always shows where the driver should go — the
    server controls navigation. Called on assignment and every trip-state change.
    Returns the geometry, or None when the stage has no moving leg."""
    if meta is None:
        return None
    if driver_pos is None:
        driver_pos = _latest_pos(meta.driver_id)
    leg = order_leg(meta, driver_pos)
    if not leg:
        return None
    (slat, slng), (elat, elng) = leg

    if haversine_km(slat, slng, elat, elng) > MAX_LEG_KM:
        return None
    from car_orders import services
    from car_orders.models import OrderLiveLocation
    from car_orders.ws import broadcast_location

    try:
        result = services.estimate_route(slat, slng, elat, elng)
    except Exception:
        result = None
    geom = result.get("geometry") if result else None
    source = result.get("source") if result else None
    if not geom:
        return None
    # A transient OSRM outage falls back to a 2-point straight line (source
    # «haversine») that cuts across roads/houses. Don't let it overwrite a good road
    # route that's already on the map — keep the last canonical polyline; the next
    # successful re-route restores the live line. (When there's no route yet we still
    # draw the fallback, so the map isn't blank on first assignment.)
    existing = OrderLiveLocation.objects.filter(order_id=meta.order_id).first()
    if source != "osrm" and existing is not None and existing.geometry:
        return existing.geometry
    geom = downsample(geom)  # keep the WS frame well under the 1 MB limit
    loc, created = OrderLiveLocation.objects.get_or_create(
        order_id=meta.order_id,
        defaults={"lat": slat, "lng": slng, "last_seen": timezone.now(), "geometry": geom},
    )
    if not created:
        loc.geometry = geom
        loc.save(update_fields=["geometry"])
    broadcast_location(meta.order_id, {"geometry": geom, "source": source})
    return geom


def planned_route_geometry(meta):
    """The client's ORDERED trip A→B (pickup → destination) as a downsampled
    polyline, independent of any driver.

    Used for an order that has no live leg yet — e.g. it's still awaiting a driver,
    so :func:`order_leg` returns None (no driver position to approach from). Without
    this the map only has the A/B pins and draws no line; this gives it the route
    the requester actually asked for. Returns the geometry, or None when the coords
    are missing or the leg is implausibly long (same sanity bound as a live leg)."""
    if meta is None:
        return None
    o = (meta.origin_lat, meta.origin_lng)
    d = (meta.address_lat, meta.address_lng)
    if None in o or None in d:
        return None
    if haversine_km(o[0], o[1], d[0], d[1]) > MAX_LEG_KM:
        return None
    from car_orders import services

    try:
        result = services.estimate_route(o[0], o[1], d[0], d[1])
    except Exception:
        return None
    geom = result.get("geometry") if result else None
    return downsample(geom) if geom else None


def run_once(first_seen, now=None, *, lead_min, stale_sec, pos_max_age, max_load=1):
    """One dispatch pass. Returns a list of (order_id, driver_id) just assigned.
    `first_seen` is a mutable {order_id: datetime} carried across passes."""
    now = now or timezone.now()
    shifts = list(DriverShiftState.objects.all())
    positions = fresh_positions(pos_max_age, now)
    load = active_count_by_driver()
    assigned = []
    for meta in queue_orders():
        # AUDIT M6: seed «first seen» from the order's persisted updated_at (≈ when it
        # entered the queue) rather than `now`, so a worker restart doesn't reset every
        # ASAP order's stale clock and indefinitely defer its dispatch.
        first_seen.setdefault(meta.order_id, meta.updated_at or now)
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
