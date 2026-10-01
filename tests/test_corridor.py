"""Corridor-search tests on a synthetic route with known geometry.

The route runs due north along the -100 meridian, where distance is exactly
proportional to degrees of latitude. That makes every expected mileage and detour
computable by hand instead of approximated, so these tests pin down real numbers
rather than just checking that something came back.
"""
from __future__ import annotations

import pytest

from services.corridor import IndexedStop, StopIndex, find_corridor_stops, sample_route
from tests.conftest import meridian_route, point_at


def stop(stop_id: str, mile: float, east_miles: float, price: float = 3.0) -> IndexedStop:
    lat, lon = point_at(mile, east_miles)
    return IndexedStop(
        stop_id=stop_id, name=f"Stop {stop_id}", address="", city="C", state="KS",
        lat=lat, lon=lon, price=price,
    )


# --------------------------------------------------------------------------
# Distance along route and detour must both be accurate
# --------------------------------------------------------------------------

def test_finds_a_stop_at_a_known_mile_and_detour():
    route = meridian_route(500.0)
    index = StopIndex([stop("A", 250.0, 3.0)])
    found = find_corridor_stops(route.points, index, max_detour_miles=10.0)
    assert len(found) == 1
    assert found[0].mile == pytest.approx(250.0, abs=0.05)
    assert found[0].detour_miles == pytest.approx(3.0, abs=0.05)


def test_detour_is_accurate_across_a_range_of_offsets():
    route = meridian_route(400.0)
    offsets = [0.0, 0.25, 1.0, 2.5, 5.0, 9.0]
    index = StopIndex([stop(f"S{i}", 200.0, d) for i, d in enumerate(offsets)])
    found = {s.stop_id: s for s in find_corridor_stops(route.points, index, 10.0)}
    assert len(found) == len(offsets)
    for i, expected in enumerate(offsets):
        assert found[f"S{i}"].detour_miles == pytest.approx(expected, abs=0.05)


def test_a_stop_directly_on_the_route_has_near_zero_detour():
    """Sampling every 1 mile would quantise this to up to 0.5 mi without the
    segment refinement, so this test is what keeps that refinement honest."""
    route = meridian_route(300.0)
    # 150.3 miles: deliberately not a multiple of the sampling interval.
    index = StopIndex([stop("ON", 150.3, 0.0)])
    found = find_corridor_stops(route.points, index, 10.0)
    assert found[0].detour_miles == pytest.approx(0.0, abs=0.01)
    assert found[0].mile == pytest.approx(150.3, abs=0.05)


def test_stops_are_returned_ordered_by_distance_along_route():
    route = meridian_route(500.0)
    index = StopIndex([stop("C", 400.0, 1.0), stop("A", 50.0, 1.0), stop("B", 220.0, 1.0)])
    found = find_corridor_stops(route.points, index, 10.0)
    assert [s.stop_id for s in found] == ["A", "B", "C"]
    assert [round(s.mile) for s in found] == [50, 220, 400]


# --------------------------------------------------------------------------
# The detour threshold must be respected exactly
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "offset,tolerance,expected",
    [
        (4.0, 5.0, True),    # comfortably inside
        (5.5, 5.0, False),   # comfortably outside
        (9.5, 10.0, True),
        (12.0, 10.0, False),
        (30.0, 25.0, False),
        (20.0, 25.0, True),
    ],
)
def test_detour_threshold_includes_and_excludes_correctly(offset, tolerance, expected):
    route = meridian_route(300.0)
    index = StopIndex([stop("X", 150.0, offset)])
    found = find_corridor_stops(route.points, index, tolerance)
    assert bool(found) is expected


def test_widening_the_tolerance_never_loses_a_stop():
    """Monotonicity: the set found at a wider tolerance must be a superset."""
    route = meridian_route(400.0)
    index = StopIndex(
        [stop(f"S{i}", 100.0 + i * 20.0, float(i)) for i in range(12)]
    )
    previous: set[str] = set()
    for tolerance in (1.0, 3.0, 6.0, 12.0, 25.0):
        current = {s.stop_id for s in find_corridor_stops(route.points, index, tolerance)}
        assert previous <= current, f"tolerance {tolerance} lost stops"
        previous = current
    assert len(previous) == 12


def test_stops_far_from_the_route_are_excluded():
    route = meridian_route(300.0)
    # 500 miles east of the corridor, and 10 degrees of latitude away.
    far = IndexedStop("FAR", "Far", "", "C", "KS", 45.0, -90.0, 3.0)
    index = StopIndex([stop("NEAR", 150.0, 1.0), far])
    found = find_corridor_stops(route.points, index, 10.0)
    assert [s.stop_id for s in found] == ["NEAR"]


def test_stops_beyond_the_route_ends_are_excluded():
    """A stop 50 miles past the destination is within 50 mi of the polyline's end
    but is not on the route, so a 10 mi corridor must not pick it up."""
    route = meridian_route(300.0)
    index = StopIndex([stop("PAST", 350.0, 0.0), stop("BEFORE", -50.0, 0.0)])
    found = find_corridor_stops(route.points, index, 10.0)
    assert found == []


# --------------------------------------------------------------------------
# Degenerate inputs
# --------------------------------------------------------------------------

def test_empty_index_returns_nothing():
    route = meridian_route(200.0)
    assert find_corridor_stops(route.points, StopIndex([]), 10.0) == []


def test_zero_tolerance_returns_nothing():
    route = meridian_route(200.0)
    index = StopIndex([stop("A", 100.0, 0.5)])
    assert find_corridor_stops(route.points, index, 0.0) == []


def test_empty_route_returns_nothing():
    index = StopIndex([stop("A", 100.0, 0.5)])
    assert find_corridor_stops([], index, 10.0) == []


def test_one_entry_per_stop_even_when_the_route_passes_twice():
    """An out-and-back route passes the same stop twice. It must appear once,
    at its nearest approach, or the optimiser would see a phantom station."""
    out = meridian_route(200.0)
    # Return leg: same geometry back down, mileage continuing to 400.
    back = [
        type(p)(lat=p.lat, lon=p.lon, mile=400.0 - p.mile)
        for p in reversed(out.points)
    ]
    points = list(out.points) + back[1:]
    index = StopIndex([stop("A", 100.0, 2.0)])
    found = find_corridor_stops(points, index, 10.0)
    assert len(found) == 1
    assert found[0].detour_miles == pytest.approx(2.0, abs=0.05)


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------

def test_sampling_keeps_both_endpoints_and_thins_the_middle():
    route = meridian_route(100.0, step_miles=0.1)
    samples, indices = sample_route(route.points, interval_miles=1.0)
    assert samples[0] is route.points[0]
    assert samples[-1] is route.points[-1]
    assert indices[0] == 0 and indices[-1] == len(route.points) - 1
    # ~100 one-mile samples out of ~1000 points, plus the endpoints.
    assert 95 <= len(samples) <= 110
    assert all(
        later.mile >= earlier.mile for earlier, later in zip(samples, samples[1:])
    )


def test_index_reports_its_size_and_cell_count():
    index = StopIndex([stop(f"S{i}", i * 30.0, 1.0) for i in range(10)])
    assert len(index) == 10
    assert index.cell_count >= 1
