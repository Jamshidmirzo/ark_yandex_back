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


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two ``(lat, lng)`` points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


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
    ``[[lng, lat], ...]``; returns a trimmed copy starting at ``(lng, lat)``.

    The caller keeps the canonical route untouched (deviation is still measured
    against it); this is only the per-frame *view* of the line ahead.
    """
    if not geom:
        return geom
    # Nearest vertex to the driver = how far along the route they already are.
    best_i, best_d = 0, float("inf")
    for i, p in enumerate(geom):
        if len(p) < 2:
            continue
        d = haversine_km(lat, lng, p[1], p[0])
        if d < best_d:
            best_d, best_i = d, i
    ahead = geom[best_i:]
    if not ahead:
        return [[lng, lat]]
    return downsample([[lng, lat]] + ahead, max_points)


def _point_segment_km(plat, plng, alat, alng, blat, blng) -> float:
    """Distance (km) from point P to the SEGMENT A–B, via a local equirectangular
    projection around P's latitude — accurate enough for the short legs used in the
    deviation check, and far cheaper than a true geodesic."""
    klat = 111.32  # km per degree of latitude
    klng = 111.32 * math.cos(math.radians(plat))  # per degree of longitude at this lat
    ax, ay = (alng - plng) * klng, (alat - plat) * klat
    bx, by = (blng - plng) * klng, (blat - plat) * klat
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 == 0:  # degenerate segment → distance to the point
        return math.hypot(ax, ay)
    t = -(ax * dx + ay * dy) / seg2  # projection of the origin onto A–B
    t = max(0.0, min(1.0, t))  # clamp to the segment
    return math.hypot(ax + t * dx, ay + t * dy)


def min_dist_km_to_polyline(lat, lng, geom) -> float:
    """Min distance (km) from a point to the polyline's SEGMENTS — a «how far off the
    route am I» check for re-routing on deviation. Measuring to segments (not just
    vertices, AUDIT H4) avoids a false «off-route» when the driver is mid-way along a
    long, sparsely-sampled leg."""
    if not geom:
        return float("inf")
    pts = [p for p in geom if len(p) >= 2]
    if not pts:
        return float("inf")
    if len(pts) == 1:
        return haversine_km(lat, lng, pts[0][1], pts[0][0])
    return min(
        _point_segment_km(lat, lng, a[1], a[0], b[1], b[0]) for a, b in zip(pts, pts[1:])
    )
