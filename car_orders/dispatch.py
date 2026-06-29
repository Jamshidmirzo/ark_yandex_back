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
from django.contrib.gis.geos import Point
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from car_orders.geometry import MAX_LEG_KM, downsample, haversine_km, trim_geometry
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
        .not_terminal()
        .values_list("driver_id", flat=True)
    ):
        counts[did] = counts.get(did, 0) + 1
    return counts


def rank_drivers(car_type_id, pickup, shifts, positions, load, max_load=1, excluded=None):
    """Rank on-shift drivers for one order. Returns a list of
    ``(driver_id, car_id, car_label, distance_km, note)`` best-first; ``note == ""``
    marks an IDEAL (auto-assignable) candidate. Tagged candidates (wrong type /
    overloaded / unknown type / reassigned-off) sort last and are never
    auto-assigned. ``excluded`` is the set of driver ids the dispatcher took this
    order off — they must never become an ideal candidate again."""
    excluded = excluded or set()
    out = []
    for s in shifts:
        n = load.get(s.driver_id, 0)
        pos = positions.get(s.driver_id)
        dist = haversine_km(pos[0], pos[1], pickup[0], pickup[1]) if (pos and pickup) else None
        if s.driver_id in excluded:
            note = "reassigned-off"
        elif car_type_id is not None and s.car_type_id != car_type_id:
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


def is_due(meta, first_seen, now, lead_min, stale_sec, has_ideal=False):
    """Is it time to auto-assign this order? Urgent → now; scheduled → within
    ``lead_min`` of the pickup; ASAP (no time) → the moment an IDEAL driver is
    already free (``has_ideal``), otherwise after waiting ``stale_sec``.

    The ASAP stale wait exists to give a closer driver time to come online before we
    grab the first one — but when a suitable car is ALREADY sitting idle there's
    nothing to wait for, so we assign at once (a freshly-created «сейчас» order
    shouldn't sit 3 min while the dispatcher sees the driver as «подходит»)."""
    if meta.is_urgent:
        return True
    if meta.planned_datetime:
        return meta.planned_datetime - now <= timedelta(minutes=lead_min)
    if has_ideal:
        return True
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
        # The dispatcher took this order off this driver — never auto-assign it back
        # (final invariant check under the row lock, in case the exclusion list grew
        # between ranking and claim).
        if driver_id in (meta.excluded_driver_ids or []):
            return False
        # One active order per driver (matches OverlayClaimView / demo).
        busy = (
            OrderMeta.objects.active_for_driver(driver_id)
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


def _is_abandoned(m, now, cutoff, last_seen) -> bool:
    """A driver is ABANDONED on order ``m`` when ALL hold:
      • the order is still ASSIGNED — claimed but the driver NEVER set off. We never
        reap a started order (to_client/at_client/in_trip/at_destination/waiting): once
        the driver has engaged the customer, only a human unwinds it. Crucially this
        also means we never yank a driver legitimately parked for hours during a shoot
        (at_destination / waiting) on a benign GPS gap.
      • it isn't a scheduled order still waiting for a FUTURE pickup (assigned early
        within the lead window) — only reap once its pickup time has arrived/passed.
      • it's been stuck in ASSIGNED ≥ ``abandon_sec`` (updated_at ≤ cutoff), so a fresh
        assignment isn't reaped before the driver can react.
      • the driver is DARK — no GPS fix for ≥ ``abandon_sec`` (an online driver, who
        heartbeats even while parked, is never reaped; a present-but-idle driver is left
        for the dispatcher to reassign by hand)."""
    if m.trip_state != OrderMeta.TripState.ASSIGNED:
        return False
    if m.planned_datetime is not None and m.planned_datetime > now:
        return False
    if m.updated_at is not None and m.updated_at > cutoff:
        return False
    seen = last_seen.get(m.driver_id)
    return seen is None or seen <= cutoff


def reap_abandoned(now=None, *, abandon_sec=None):
    """Free drivers pinned by an order they've ABANDONED, so a long-dead claim can't
    keep a driver «busy» forever (the «водитель свободен/подходит, но новый заказ не
    назначается» trap). An ASSIGNED-but-never-started order whose driver has gone dark
    for ``abandon_sec`` is requeued — the driver is unpinned and the order goes back to
    the queue for someone online. Returns the list of ``(order_id, driver_id)`` freed.
    ``abandon_sec=0`` disables it. See :func:`_is_abandoned` for the exact (deliberately
    conservative) rule.

    Requeues with a plain ``release`` (no permanent driver exclusion): a dark driver
    ranks LAST anyway (no fresh GPS → unknown distance), so when any live driver exists
    the order goes to them; only when the dark driver is the sole candidate does it bounce
    back — and the ``updated_at`` guard then keeps it from re-reaping for another full
    window, so there's no thrash and no single-driver starvation."""
    from car_orders import concurrency
    from car_orders.services import overlay

    now = now or timezone.now()
    if abandon_sec is None:
        abandon_sec = getattr(settings, "CAR_ORDER_ABANDON_SEC", 60 * 60)
    if not abandon_sec:
        return []
    cutoff = now - timedelta(seconds=abandon_sec)
    last_seen = dict(DriverPosition.objects.values_list("driver_id", "last_seen"))
    candidates = [
        (m.order_id, m.driver_id)
        for m in OrderMeta.objects.filter(
            trip_state=OrderMeta.TripState.ASSIGNED, driver_id__isnull=False
        )
        if _is_abandoned(m, now, cutoff, last_seen)
    ]
    freed = []
    for order_id, did in candidates:
        # Re-validate under the SAME per-driver advisory lock claim/advance take, so a
        # concurrent «to_client» tap (or a claim) committed since we scanned isn't lost
        # — only requeue if the order is STILL an abandoned ASSIGNED pin on this driver.
        with transaction.atomic():
            concurrency.lock_driver(did)
            m = OrderMeta.objects.select_for_update().filter(order_id=order_id).first()
            # Re-read this driver's GPS recency too — they may have come back online
            # between the scan and now (which would make the order no longer abandoned).
            ls = DriverPosition.objects.filter(driver_id=did).values_list("last_seen", flat=True).first()
            if m is None or m.driver_id != did or not _is_abandoned(m, now, cutoff, {did: ls}):
                continue
            overlay.release(order_id, requeue=True, is_dispatcher=True)
        freed.append((order_id, did))
    return freed


def fill_missing_addresses(limit=2):
    """Reverse-geocode «откуда / куда» onto overlay orders that have coords but no
    address text yet, so every client shows the route as TEXT (not «—») — including an
    overlay-only order with no demo CarOrder body to read the address from (the
    «в активном заказе откуда/куда стоит —» bug). Idempotent and bounded per pass; the
    geocoder is cached, so each point costs one lookup once. Returns the count filled.

    Kept SMALL per pass and run AFTER the assign pass so the (throttled) Nominatim
    lookups never delay auto-dispatch."""
    from car_orders.services import geocode

    # Includes TERMINAL orders: a finished/cancelled ride still needs its «откуда/куда»
    # for the driver's history list (it never auto-dispatches again, so leaving it blank
    # is fine for dispatch but shows «—» in history). Bounded + idempotent.
    qs = OrderMeta.objects.filter(
        Q(origin_address="", origin_lat__isnull=False, origin_lng__isnull=False)
        | Q(dest_address="", address_lat__isnull=False, address_lng__isnull=False)
    )[:limit]
    filled = 0
    for m in qs:
        changed = []
        if not m.origin_address and m.origin_lat is not None and m.origin_lng is not None:
            label = geocode.reverse(m.origin_lat, m.origin_lng)
            if label:
                m.origin_address = label[:500]
                changed.append("origin_address")
        if not m.dest_address and m.address_lat is not None and m.address_lng is not None:
            label = geocode.reverse(m.address_lat, m.address_lng)
            if label:
                m.dest_address = label[:500]
                changed.append("dest_address")
        if changed:
            m.save(update_fields=changed)
            filled += 1
    return filled


def fresh_positions(max_age_sec, now=None, *, near=None, within_m=None):
    """driver_id → (lat, lng) for drivers with a GPS fix newer than max_age.

    When ``near=(lat, lng)`` is given, candidates are pre-filtered/ordered by
    proximity via the PostGIS GiST index (DriverPositionQuerySet.near) — so a caller
    that knows the pickup only materialises drivers actually near it. ``within_m``
    optionally caps the search radius (metres). Ranking itself stays in Python
    (rank_drivers) for parity with the frontend, so this is purely a candidate
    pre-filter; the default (no ``near``) keeps the full-snapshot behaviour the
    auto-dispatch pass relies on.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=max_age_sec)
    qs = DriverPosition.objects.fresh(cutoff)
    if near is not None:
        qs = qs.near(Point(near[1], near[0], srid=4326), within_m=within_m)  # (lng, lat)
    return {p.driver_id: (p.lat, p.lng) for p in qs}


def queue_orders():
    """Approved, driverless, non-terminal orders with a pickup — ready to dispatch."""
    return list(OrderMeta.objects.dispatch_queue())


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


def push_order_route(meta, driver_pos=None, bearing=None):
    """Compute the current leg's route (OSRM) and broadcast its geometry + store it
    on OrderLiveLocation, so the map always shows where the driver should go — the
    server controls navigation. Called on assignment and every trip-state change.
    Returns the geometry, or None when the stage has no moving leg.

    ``bearing`` (deg) is the driver's travel direction; passed to OSRM so a live leg
    that starts at the driver's moving position snaps to the correct (not oncoming)
    carriageway. Applied only when the leg actually starts at ``driver_pos``."""
    if meta is None:
        return None
    if driver_pos is None:
        driver_pos = _latest_pos(meta.driver_id)
    leg = order_leg(meta, driver_pos)
    from car_orders.models import OrderLiveLocation
    from car_orders.ws import broadcast_location

    if not leg:
        return None
    (slat, slng), (elat, elng) = leg

    if haversine_km(slat, slng, elat, elng) > MAX_LEG_KM:
        # Implausible leg (stale/bogus GPS — e.g. a fix in another city). Don't route
        # it, and CLEAR any stale geometry (e.g. an old straight-line fallback) so the
        # map stops showing a wrong line for this order until a sane fix arrives.
        existing = OrderLiveLocation.objects.filter(order_id=meta.order_id).first()
        if existing is not None and existing.geometry:
            existing.geometry = []
            existing.save(update_fields=["geometry"])
        return None
    from car_orders import services

    # Constrain OSRM's start-snap to the driver's heading ONLY when the leg actually
    # starts at the driver's live position (true for every MOVING leg; the
    # at_destination return preview starts at the fixed destination → unconstrained).
    leg_bearing = (
        bearing
        if (
            bearing is not None
            and driver_pos
            and driver_pos[0] is not None
            and abs(slat - driver_pos[0]) < 1e-9
            and abs(slng - driver_pos[1]) < 1e-9
        )
        else None
    )
    try:
        result = services.estimate_route(slat, slng, elat, elng, bearing=leg_bearing)
    except Exception:
        result = None
    geom = result.get("geometry") if result else None
    source = result.get("source") if result else None
    if not geom:
        return None
    existing = OrderLiveLocation.objects.filter(order_id=meta.order_id).first()
    # A transient OSRM outage falls back to a 2-point straight line (source
    # «haversine») that cuts across roads/houses — NEVER show it as the route:
    #   • if a good road route is already on the map, keep it, but RE-ANCHOR its start
    #     to the driver's current point so it doesn't appear to run back to where they
    #     used to be; the next successful OSRM re-route restores the full live line.
    #   • if there's no route yet (first push), draw NOTHING rather than a line through
    #     buildings — the next GPS fix re-routes (empty geometry counts as «deviated»).
    if source != "osrm":
        if existing is not None and existing.geometry:
            if driver_pos and driver_pos[0] is not None:
                kept = trim_geometry(existing.geometry, driver_pos[0], driver_pos[1])
                broadcast_location(meta.order_id, {"geometry": kept})
                return kept
            return existing.geometry
        return None
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
    # Only draw a real road route. The straight-line haversine fallback (OSRM down)
    # would cut A→B through buildings, so when it's not an OSRM result show pins only
    # — a later OSRM success (cached) fills the line in.
    if not result or result.get("source") != "osrm":
        return None
    geom = result.get("geometry")
    return downsample(geom) if geom else None


def run_once(first_seen, now=None, *, lead_min, stale_sec, pos_max_age, max_load=1):
    """One dispatch pass. Returns a list of (order_id, driver_id) just assigned.
    `first_seen` is a mutable {order_id: datetime} carried across passes."""
    now = now or timezone.now()
    shifts = list(DriverShiftState.objects.all())
    positions = fresh_positions(pos_max_age, now)
    load = active_count_by_driver()
    assigned = []
    # Claim scarce ideal drivers in PRIORITY order: urgent first, then scheduled by
    # soonest pickup, then ASAP oldest-first. Without this, now that an ASAP order with
    # a free ideal driver assigns immediately (Fix A), a fresh ASAP order earlier in the
    # default scan order could grab the only suitable car ahead of a due urgent/scheduled
    # one. (DB-agnostic Python sort — SQLite/Postgres order NULLs differently.)
    def _priority(meta):
        if meta.is_urgent:
            return (0, meta.updated_at or now)
        if meta.planned_datetime:
            return (1, meta.planned_datetime)
        return (2, meta.updated_at or now)

    for meta in sorted(queue_orders(), key=_priority):
        # AUDIT M6: seed «first seen» from the order's persisted updated_at (≈ when it
        # entered the queue) rather than `now`, so a worker restart doesn't reset every
        # ASAP order's stale clock and indefinitely defer its dispatch.
        first_seen.setdefault(meta.order_id, meta.updated_at or now)
        # Rank BEFORE the due-gate: knowing an IDEAL driver is already free lets an
        # ASAP order skip the stale wait and assign on this very pass.
        ranked = rank_drivers(
            meta.car_type_id, (meta.origin_lat, meta.origin_lng), shifts, positions, load, max_load,
            excluded=set(meta.excluded_driver_ids or []),
        )
        has_ideal = bool(ranked) and ranked[0][4] == ""
        if not is_due(meta, first_seen, now, lead_min, stale_sec, has_ideal=has_ideal):
            continue
        if not has_ideal:  # no candidate, or best isn't ideal
            continue
        driver_id, car_id, car_label, _dist, _note = ranked[0]
        if claim(meta.order_id, driver_id, car_id, car_label):
            load[driver_id] = load.get(driver_id, 0) + 1  # reflect within this pass
            assigned.append((meta.order_id, driver_id))
    return assigned
