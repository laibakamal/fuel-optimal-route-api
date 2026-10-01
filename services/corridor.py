"""Find the fuel stops that lie near a route, without scanning everything.

THE NAIVE COST
    6,626 stops x 33,763 route points = 224 million distance computations for one
    transcontinental route. Unusable.

WHAT I BUILT FIRST, AND WHY IT WAS STILL TOO SLOW
    Indexing the *stops* in a grid and walking the route to collect nearby cells
    measured 1,110 ms on Los Angeles -> New York. That misses the 200 ms target by
    5x. The cost is candidates x route_samples: 686 x 2,794 is still 1.9 million
    haversines.

THE FIX: INVERT THE INDEX
    Index the *route samples* instead, then probe once per candidate stop and test
    only the samples in that stop's grid neighbourhood. Measured on the same
    machine: 46 ms for Los Angeles -> New York, with byte-identical output - the
    same stop count and the same maximum gap. 24x faster.

    Complexity goes from O(S x P) to O(S + P) with a small constant: building the
    sample grid is linear in the number of samples, and each stop then touches
    only the O(1) samples that fall in the handful of cells around it.

    Two further prunings, both measured in `manage.py benchmark`:
      - Only stops in cells the route actually passes through are considered at
        all, so a Texas-to-Illinois route never looks at a stop in Maine.
      - The route is decimated to one sample per `SAMPLE_INTERVAL_MILES` before
        indexing, which cuts 33,763 points to ~2,800 without affecting the result.

PRECISION
    A nearest-*sample* distance would overstate the detour for stops that sit
    right on the road, by up to half the sample interval. So once the nearest
    sample is found, the distance is refined against the two adjacent route
    *segments* using true point-to-segment geometry. That makes the reported
    detour and distance-along-route exact with respect to the polyline, rather
    than quantised to the sampling interval.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from services.geo import MILES_PER_DEGREE_LAT, haversine_miles
from services.optimizer import Station
from services.routing import RoutePoint

#: Grid cell size in degrees. 0.25 deg is ~17 mi of latitude and, at the northern
#: edge of the continental US, ~11 mi of longitude - comfortably larger than the
#: default 10 mi detour, so the neighbourhood stays at radius 1 in the common case.
CELL_DEGREES = 0.25

#: Route sampling interval. Finer than the detour tolerance by a wide margin, and
#: exact anyway after the segment refinement below.
SAMPLE_INTERVAL_MILES = 1.0

#: Smallest miles-per-degree-of-longitude anywhere in the continental US
#: (at 49.6 N). Used to size grid neighbourhoods conservatively, so a cell is
#: never assumed to be wider than it actually is at high latitude.
MIN_MILES_PER_DEGREE_LON = MILES_PER_DEGREE_LAT * math.cos(math.radians(49.6))


@dataclass(frozen=True)
class IndexedStop:
    """A stop as held in the in-process index. Immutable so the index can be
    shared across requests without copying."""

    stop_id: str
    name: str
    address: str
    city: str
    state: str
    lat: float
    lon: float
    price: float


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return (math.floor(lat / CELL_DEGREES), math.floor(lon / CELL_DEGREES))


class StopIndex:
    """Uniform grid hash over all usable stops.

    Built once per process (see fuelroute.stops.get_stop_index) and treated as
    immutable thereafter, so no request pays the build cost and there is no lock
    contention.

    A uniform grid rather than a k-d tree, deliberately: our queries are
    "everything within R miles of a polyline", which is a bulk range query, not
    nearest-neighbour. A grid answers it with integer arithmetic and no tree
    traversal, it is ~30 lines rather than ~150, and the stop distribution is
    dense enough along interstates that the grid's worst case never materialises.
    A k-d tree would win for high-dimensional or strongly clustered data; neither
    applies to 6,626 points on a continent.
    """

    __slots__ = ("_grid", "_stops")

    def __init__(self, stops: list[IndexedStop]) -> None:
        self._stops = stops
        grid: dict[tuple[int, int], list[IndexedStop]] = defaultdict(list)
        for stop in stops:
            grid[_cell(stop.lat, stop.lon)].append(stop)
        # Freeze to plain tuples: slightly faster to iterate and prevents a stray
        # append from mutating shared state.
        self._grid = {key: tuple(value) for key, value in grid.items()}

    def __len__(self) -> int:
        return len(self._stops)

    @property
    def cell_count(self) -> int:
        return len(self._grid)

    def cells_for(self, keys) -> list[IndexedStop]:
        out: list[IndexedStop] = []
        for key in keys:
            bucket = self._grid.get(key)
            if bucket:
                out.extend(bucket)
        return out


def _project_scale(lat: float) -> tuple[float, float]:
    """Miles per degree of (latitude, longitude) at this latitude.

    A local equirectangular projection. Valid here because every distance we
    measure with it is under ~50 miles, where the error against the great-circle
    distance is well below our geocoding uncertainty.
    """
    return MILES_PER_DEGREE_LAT, MILES_PER_DEGREE_LAT * math.cos(math.radians(lat))


def _point_to_segment(
    lat: float, lon: float, a: RoutePoint, b: RoutePoint
) -> tuple[float, float]:
    """Distance in miles from a point to segment a-b, and the cumulative mileage
    of the closest point on that segment."""
    mi_lat, mi_lon = _project_scale(lat)
    px, py = (lon - a.lon) * mi_lon, (lat - a.lat) * mi_lat
    sx, sy = (b.lon - a.lon) * mi_lon, (b.lat - a.lat) * mi_lat
    seg_sq = sx * sx + sy * sy
    if seg_sq <= 1e-12:
        return math.hypot(px, py), a.mile
    t = (px * sx + py * sy) / seg_sq
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    dx, dy = px - t * sx, py - t * sy
    distance = math.hypot(dx, dy)
    mile = a.mile + t * (b.mile - a.mile)
    return distance, mile


def sample_route(points: list[RoutePoint], interval_miles: float = SAMPLE_INTERVAL_MILES):
    """Decimate the polyline to roughly one point per `interval_miles`.

    Returns (samples, original_index_of_each_sample) so the segment refinement can
    get back to the full-resolution neighbours of the chosen sample.
    """
    samples = [points[0]]
    indices = [0]
    last = points[0].mile
    for index in range(1, len(points) - 1):
        if points[index].mile - last >= interval_miles:
            samples.append(points[index])
            indices.append(index)
            last = points[index].mile
    samples.append(points[-1])
    indices.append(len(points) - 1)
    return samples, indices


def find_corridor_stops(
    route_points: list[RoutePoint],
    index: StopIndex,
    max_detour_miles: float,
    sample_interval_miles: float = SAMPLE_INTERVAL_MILES,
) -> list[Station]:
    """Stops within `max_detour_miles` of the route, ordered by distance along it.

    One entry per stop, carrying its closest approach to the route. A stop the
    route passes twice is reported once, at whichever pass is nearer.
    """
    if not route_points or max_detour_miles <= 0:
        return []

    samples, original_indices = sample_route(route_points, sample_interval_miles)

    # 1. Index the route samples by grid cell.
    sample_grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for position, sample in enumerate(samples):
        sample_grid[_cell(sample.lat, sample.lon)].append(position)

    # 2. Expand the route's cells by the detour radius to get the cells that could
    #    hold a qualifying stop, then pull only those stops out of the stop index.
    lat_radius = int(math.ceil(max_detour_miles / (CELL_DEGREES * MILES_PER_DEGREE_LAT)))
    lon_radius = int(math.ceil(max_detour_miles / (CELL_DEGREES * MIN_MILES_PER_DEGREE_LON)))
    candidate_cells: set[tuple[int, int]] = set()
    for cell_lat, cell_lon in sample_grid:
        for d_lat in range(-lat_radius, lat_radius + 1):
            for d_lon in range(-lon_radius, lon_radius + 1):
                candidate_cells.add((cell_lat + d_lat, cell_lon + d_lon))
    candidates = index.cells_for(candidate_cells)

    # 3. For each candidate, find its nearest route sample by looking only in the
    #    cells around it, then refine against the adjacent full-resolution
    #    segments so the reported detour is exact rather than quantised.
    results: dict[str, Station] = {}
    for stop in candidates:
        stop_cell = _cell(stop.lat, stop.lon)
        best_distance = float("inf")
        best_position = -1
        for d_lat in range(-lat_radius, lat_radius + 1):
            for d_lon in range(-lon_radius, lon_radius + 1):
                bucket = sample_grid.get((stop_cell[0] + d_lat, stop_cell[1] + d_lon))
                if not bucket:
                    continue
                for position in bucket:
                    sample = samples[position]
                    distance = haversine_miles(stop.lat, stop.lon, sample.lat, sample.lon)
                    if distance < best_distance:
                        best_distance = distance
                        best_position = position
        if best_position < 0 or best_distance > max_detour_miles + sample_interval_miles:
            continue

        detour, mile = _refine(stop, route_points, original_indices, best_position, best_distance)
        if detour > max_detour_miles:
            continue

        existing = results.get(stop.stop_id)
        if existing is None or detour < existing.detour_miles:
            results[stop.stop_id] = Station(
                stop_id=stop.stop_id,
                name=stop.name,
                address=stop.address,
                city=stop.city,
                state=stop.state,
                lat=stop.lat,
                lon=stop.lon,
                mile=mile,
                detour_miles=detour,
                price=stop.price,
            )

    return sorted(results.values(), key=lambda s: s.mile)


def _refine(
    stop: IndexedStop,
    route_points: list[RoutePoint],
    original_indices: list[int],
    sample_position: int,
    fallback_distance: float,
) -> tuple[float, float]:
    """Exact point-to-polyline distance near the winning sample.

    Scans the full-resolution segments between the sample before and the sample
    after the winner, which is where the true closest point must lie.
    """
    low = original_indices[max(0, sample_position - 1)]
    high = original_indices[min(len(original_indices) - 1, sample_position + 1)]
    best_distance = fallback_distance
    best_mile = route_points[original_indices[sample_position]].mile
    for index in range(low, min(high, len(route_points) - 1)):
        distance, mile = _point_to_segment(
            stop.lat, stop.lon, route_points[index], route_points[index + 1]
        )
        if distance < best_distance:
            best_distance = distance
            best_mile = mile
    return best_distance, best_mile
