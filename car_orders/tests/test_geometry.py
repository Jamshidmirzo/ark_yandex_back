"""Unit tests for the pure geometry helpers (``car_orders.geometry``).

These maths functions (great-circle distance, polyline downsample/trim, deviation
distance) underpin live tracking and dispatch ranking but had no direct tests.
No Django, no DB — just the numbers and the degenerate inputs.
"""

from car_orders import geometry


# ---- haversine_km ----------------------------------------------------------

def test_haversine_zero_for_identical_points():
    assert geometry.haversine_km(41.31, 69.24, 41.31, 69.24) == 0.0


def test_haversine_one_degree_of_longitude_at_equator():
    # 1° of longitude at the equator ≈ 111.19 km.
    km = geometry.haversine_km(0.0, 0.0, 0.0, 1.0)
    assert 111.0 < km < 111.4


# ---- downsample ------------------------------------------------------------

def test_downsample_passthrough_when_small():
    geom = [[0, 0], [1, 1]]
    assert geometry.downsample(geom, 5) == geom


def test_downsample_empty():
    assert geometry.downsample([], 5) == []


def test_downsample_caps_and_keeps_ends():
    geom = [[i, i] for i in range(1000)]
    out = geometry.downsample(geom, 100)
    assert len(out) <= 101  # n points, plus the explicit last if it slipped off
    assert out[0] == [0, 0]
    assert out[-1] == [999, 999]


# ---- trim_geometry ---------------------------------------------------------

def test_trim_empty_returns_input():
    assert geometry.trim_geometry([], 41.31, 69.24) == []


def test_trim_pins_start_to_driver_point():
    # Geometry is [[lng, lat], ...]; trim starts the line at the driver's position.
    geom = [[69.24, 41.31], [69.26, 41.33], [69.29, 41.35]]
    out = geometry.trim_geometry(geom, 41.33, 69.26)
    assert out[0] == [69.26, 41.33]  # pinned to the driver
    assert out[-1] == [69.29, 41.35]  # the rest of the route ahead


def test_trim_drops_already_passed_vertices():
    geom = [[69.24, 41.31], [69.26, 41.33], [69.29, 41.35]]
    # Driver is on the LAST vertex → only the tail (its projection + that vertex)
    # remains; the earlier vertices are dropped.
    out = geometry.trim_geometry(geom, 41.35, 69.29)
    assert out[0] == [69.29, 41.35]
    assert out[-1] == [69.29, 41.35]
    assert len(out) == 2  # projection onto the last segment + its end vertex


def test_trim_skips_malformed_points():
    geom = [[69.24], [69.26, 41.33], [69.29, 41.35]]  # first point malformed
    out = geometry.trim_geometry(geom, 41.33, 69.26)
    assert out[0] == [69.26, 41.33]


def test_trim_projects_onto_segment_not_a_far_vertex():
    # A long, sparsely-sampled leg: the driver is mid-segment, far from BOTH vertices.
    # Nearest-vertex snapping would jump the start to a vertex; segment projection
    # keeps it on the road between them (this is what stops the «ahead» line flipping
    # to the oncoming side where a route runs near itself).
    geom = [[69.20, 41.30], [69.40, 41.30]]  # a 0.20° east-west segment
    out = geometry.trim_geometry(geom, 41.30, 69.30)  # driver halfway along it
    assert abs(out[0][0] - 69.30) < 1e-6  # start projected to the midpoint lng
    assert abs(out[0][1] - 41.30) < 1e-6
    assert out[-1] == [69.40, 41.30]


# ---- bearing_deg -----------------------------------------------------------

def test_bearing_due_north_and_east():
    assert abs(geometry.bearing_deg(41.30, 69.24, 41.40, 69.24) - 0.0) < 1.0  # north
    assert abs(geometry.bearing_deg(41.30, 69.24, 41.30, 69.34) - 90.0) < 1.0  # east


def test_bearing_is_in_0_360():
    b = geometry.bearing_deg(41.40, 69.34, 41.30, 69.24)  # heading south-west
    assert 0.0 <= b < 360.0
    assert 180.0 < b < 270.0


# ---- min_dist_km_to_polyline ----------------------------------------------

def test_min_dist_empty_is_infinite():
    assert geometry.min_dist_km_to_polyline(41.31, 69.24, []) == float("inf")


def test_min_dist_zero_on_a_vertex():
    geom = [[69.24, 41.31], [69.29, 41.35]]
    assert geometry.min_dist_km_to_polyline(41.31, 69.24, geom) < 1e-6


def test_min_dist_skips_malformed_points():
    geom = [[69.24], [69.29, 41.35]]  # first malformed, second is the only real vertex
    d = geometry.min_dist_km_to_polyline(41.35, 69.29, geom)
    assert d < 1e-6


# ---- project_to_polyline ---------------------------------------------------

# A due-NORTH segment (constant lng 69.30, lat 41.30 → 41.40) used by the projection
# and snap tests. ~60 m east of it at lat 41.35 is one km-per-degree-longitude step:
#   klng = 111.32 * cos(41.35°) ≈ 83.55 km/deg → 0.060 km ≈ 0.000718°.
_NS_SEGMENT = [[69.30, 41.30], [69.30, 41.40]]
_LNG_60M_EAST = 69.30 + 0.060 / (111.32 * 0.75046)  # ≈ 69.300718


def test_project_returns_none_without_a_segment():
    assert geometry.project_to_polyline(41.31, 69.24, []) is None
    assert geometry.project_to_polyline(41.31, 69.24, [[69.24, 41.31]]) is None


def test_project_onto_segment_gives_point_distance_and_bearing():
    plat, plng, dist_km, seg_bearing, seg_i = geometry.project_to_polyline(
        41.35, _LNG_60M_EAST, _NS_SEGMENT
    )
    assert abs(plat - 41.35) < 1e-4  # projects to the driver's latitude on the line
    assert abs(plng - 69.30) < 1e-4  # onto the segment's longitude
    assert abs(dist_km - 0.060) < 0.005  # ~60 m cross-track
    assert abs(seg_bearing - 0.0) < 1.0  # the segment heads due north
    assert seg_i == 0


# ---- snap_to_route ---------------------------------------------------------

def test_snap_projects_when_inside_corridor_and_heading_aligned():
    # 60 m off, travelling north (≈ the segment bearing) → dot rides the line.
    lat, lng = geometry.snap_to_route(41.35, _LNG_60M_EAST, _NS_SEGMENT, travel_bearing=0.0)
    assert abs(lat - 41.35) < 1e-4
    assert abs(lng - 69.30) < 1e-4


def test_snap_keeps_raw_beyond_corridor():
    # ~100 m east is outside the 70 m corridor → show the real (off-route) fix.
    far_lng = 69.30 + 0.100 / (111.32 * 0.75046)
    lat, lng = geometry.snap_to_route(41.35, far_lng, _NS_SEGMENT, travel_bearing=0.0)
    assert (lat, lng) == (41.35, far_lng)


def test_snap_keeps_raw_when_heading_crosses_the_route():
    # Inside the corridor but heading EAST across a north-south road → don't yank the
    # marker onto a road the driver isn't on (parallel/oncoming-street guard).
    lat, lng = geometry.snap_to_route(41.35, _LNG_60M_EAST, _NS_SEGMENT, travel_bearing=90.0)
    assert (lat, lng) == (41.35, _LNG_60M_EAST)


def test_snap_is_distance_only_without_a_travel_bearing():
    # First fix (no known heading): snap on distance alone.
    lat, lng = geometry.snap_to_route(41.35, _LNG_60M_EAST, _NS_SEGMENT)
    assert abs(lng - 69.30) < 1e-4


def test_snap_passthrough_without_geometry():
    assert geometry.snap_to_route(41.35, 69.30, []) == (41.35, 69.30)
