"""Pure geometry helpers shared across the car-orders module.

Single source of truth for the great-circle distance and the polyline
trim / downsample maths used to keep live-tracking WebSocket frames small.
These were previously copied between :mod:`car_orders.dispatch`,
:mod:`car_orders.services` and the driver socket; they live here so there is
exactly one implementation to reason about (and unit-test).

No Django imports — keep this module dependency-free and trivially testable.
"""

import math

# A leg longer than this is almost certainly stale/bogus GPS (e.g. a San-Francisco
# fix vs a Tashkent pickup → an 11 000 km route, a 1.4 MB polyline that overflows
# the 1 MB WebSocket frame). Skip routing it.
MAX_LEG_KM = 300
# Cap the polyline so a single order's geometry never blows the WS frame; 500 points
# is smooth enough for tracking.
MAX_GEOM_POINTS = 500
# Per-frame streamed (trimmed) line — lighter than the full route since it's resent
# every GPS fix; 160 points is smooth for the «line shrinks as the car moves» effect.
MAX_STREAM_POINTS = 160
# Dead-zone (metres): if the car moved less than this since the last SHOWN point it's
# GPS jitter / standing still — don't move the marker or redraw the line (kills the
# in-place flicker). >80 m deviation is impossible without crossing this first.
MIN_MOVE_M = 12
# Snap-to-route (map-matching) corridor: when the live fix is within this many metres
# of the route AND heading roughly along it, the DISPLAYED marker is projected onto
# the line so the dot rides the route instead of floating beside it on biased GPS
# (urban multipath / walking the sidewalk, not the road centre). Beyond the corridor
# it's a real detour → show the raw fix and let the deviation re-route take over.
SNAP_CORRIDOR_M = 70
# Heading gate (deg): only snap when the matched segment's bearing is within this of
# the driver's travel bearing, so a fix near a parallel/oncoming street isn't yanked
# onto the wrong carriageway. No travel bearing known (first fix) → distance-only.
SNAP_BEARING_TOL_DEG = 50


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two ``(lat, lng)`` points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Initial great-circle bearing (deg, 0-360) from point 1 → point 2.

    Used to derive the driver's travel direction from two consecutive GPS fixes, so
    the live re-route can tell OSRM which way the driver is going and stop it snapping
    the route onto the oncoming carriageway."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def project_to_polyline(lat, lng, geom):
    """Project ``(lat, lng)`` onto a route polyline's nearest SEGMENT.

    Returns ``(plat, plng, dist_km, seg_bearing_deg, seg_index)`` — the nearest
    on-route point, its cross-track distance (km), the bearing of the matched
    segment (the route's travel direction there, 0–360), and the index of that
    segment's start vertex — or ``None`` when there's no usable segment (empty /
    single point). ``geom`` is the canonical ``[[lng, lat], ...]``.

    Single source of the segment-projection maths: :func:`trim_geometry` (where the
    line starts) and :func:`min_dist_km_to_polyline` (how far off-route) both delegate
    here, as does :func:`snap_to_route`. Local equirectangular projection around the
    query latitude — accurate for the short legs in tracking, far cheaper than a true
    geodesic.
    """
    if not geom:
        return None
    pts = [p for p in geom if len(p) >= 2]
    if len(pts) < 2:
        return None
    klat = 111.32  # km per degree latitude
    klng = 111.32 * math.cos(math.radians(lat))  # per degree longitude at this lat
    best_i, best_d2 = 0, float("inf")
    best_proj = (pts[0][0], pts[0][1])  # (lng, lat)
    for i in range(len(pts) - 1):
        alng, alat = pts[i][0], pts[i][1]
        blng, blat = pts[i + 1][0], pts[i + 1][1]
        ax, ay = (alng - lng) * klng, (alat - lat) * klat
        bx, by = (blng - lng) * klng, (blat - lat) * klat
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else -(ax * dx + ay * dy) / seg2
        t = max(0.0, min(1.0, t))  # clamp the projection to the segment
        px, py = ax + t * dx, ay + t * dy
        d2 = px * px + py * py
        if d2 < best_d2:
            best_d2 = d2
            best_i = i
            best_proj = (alng + (blng - alng) * t, alat + (blat - alat) * t)
    a, b = pts[best_i], pts[best_i + 1]
    seg_bearing = bearing_deg(a[1], a[0], b[1], b[0])
    return (best_proj[1], best_proj[0], math.sqrt(best_d2), seg_bearing, best_i)


def _bearing_delta(a, b) -> float:
    """Smallest absolute difference between two bearings (deg), in 0–180."""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def snap_to_route(lat, lng, geom, travel_bearing=None,
                  corridor_m: float = SNAP_CORRIDOR_M,
                  bearing_tol_deg: float = SNAP_BEARING_TOL_DEG):
    """The marker position to DISPLAY: ``(lat, lng)`` projected onto the route when
    it's within ``corridor_m`` of the line AND (when a ``travel_bearing`` is known)
    heading within ``bearing_tol_deg`` of the matched segment; otherwise the raw
    point, unchanged. Display-only — callers keep the raw fix for the deviation /
    bearing decisions, so re-routing is unaffected. Returns ``(lat, lng)``."""
    proj = project_to_polyline(lat, lng, geom)
    if proj is None:
        return lat, lng
    plat, plng, dist_km, seg_bearing, _i = proj
    if dist_km * 1000.0 > corridor_m:
        return lat, lng
    if travel_bearing is not None and _bearing_delta(travel_bearing, seg_bearing) > bearing_tol_deg:
        return lat, lng
    return plat, plng


def downsample(geom, n: int = MAX_GEOM_POINTS):
    """Thin a polyline to at most ``n`` points (keeps the ends), so the WS frame
    stays small. OSRM ``overview=full`` can return tens of thousands of points."""
    if not geom or len(geom) <= n:
        return geom
    step = len(geom) / float(n)
    out = [geom[int(i * step)] for i in range(n)]
    if out[-1] != geom[-1]:
        out.append(geom[-1])
    return out


def trim_geometry(geom, lat, lng, max_points: int = MAX_STREAM_POINTS):
    """Drop the already-passed part of a route polyline and pin its start to the
    driver's current point, so the drawn line *shrinks smoothly* as they advance —
    sent on every GPS fix, NO OSRM call (cheap). ``geom`` is the canonical route
    ``[[lng, lat], ...]``; returns a trimmed copy starting at the driver's projection
    onto the route.

    Projects the driver onto the nearest SEGMENT (not just the nearest vertex) and
    starts the line at that projection — matching the web's ``routeAhead``. The old
    nearest-vertex snap could pick a vertex *across the road* where a route runs near
    itself (hairpin, return leg beside the outbound one), making the «ahead» line jump
    to the oncoming side; segment projection keeps it on the road the driver is on.

    The caller keeps the canonical route untouched (deviation is still measured
    against it); this is only the per-frame *view* of the line ahead.
    """
    if not geom:
        return geom
    pts = [p for p in geom if len(p) >= 2]
    if len(pts) < 2:
        return [[lng, lat]]
    plat, plng, _d, _b, best_i = project_to_polyline(lat, lng, geom)
    ahead = [[plng, plat]] + pts[best_i + 1:]
    return downsample(ahead, max_points)


def min_dist_km_to_polyline(lat, lng, geom) -> float:
    """Min distance (km) from a point to the polyline's SEGMENTS — a «how far off the
    route am I» check for re-routing on deviation. Measuring to segments (not just
    vertices, AUDIT H4) avoids a false «off-route» when the driver is mid-way along a
    long, sparsely-sampled leg. Delegates to :func:`project_to_polyline` (the single
    segment-projection implementation); the single-point geometry is the only case it
    can't (no segment), so it falls back to a great-circle distance there."""
    proj = project_to_polyline(lat, lng, geom)
    if proj is not None:
        return proj[2]
    pts = [p for p in geom if len(p) >= 2] if geom else []
    if len(pts) == 1:
        return haversine_km(lat, lng, pts[0][1], pts[0][0])
    return float("inf")
