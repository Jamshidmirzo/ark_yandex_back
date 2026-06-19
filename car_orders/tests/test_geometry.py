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
    # Driver is near the LAST vertex → only the tail remains.
    out = geometry.trim_geometry(geom, 41.35, 69.29)
    assert out[0] == [69.29, 41.35]
    assert len(out) == 2  # pinned point + the nearest (last) vertex


def test_trim_skips_malformed_points():
    geom = [[69.24], [69.26, 41.33], [69.29, 41.35]]  # first point malformed
    out = geometry.trim_geometry(geom, 41.33, 69.26)
    assert out[0] == [69.26, 41.33]


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
