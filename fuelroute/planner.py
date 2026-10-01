"""Orchestration: resolve endpoints, get the route, find stops, optimise.

This is the only place that knows the whole sequence. It also owns the two things
the brief asks to be measurable rather than claimed:

  external call counting   every call to a third-party service is counted and
                           returned, so "one routing call per request" is proven
                           by the response, not asserted by the README.
  stage timings            each phase is timed separately, so the split between
                           external latency and local compute is visible.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache

from fuelroute.indexes import get_place_index, get_stop_index
from services.corridor import find_corridor_stops
from services.optimizer import FuelPlan, Station, solve_refuelling
from services.places import LocationError, ResolvedLocation, resolve_location
from services.routing import OsrmRouteProvider, Route, RouteProvider

logger = logging.getLogger(__name__)

#: Coordinates are rounded to this many decimals for the cache key. 3 dp is about
#: 110 m, which is finer than the routing engine's own snapping to the road
#: network, so two requests that round together would have produced the same route.
CACHE_COORD_PRECISION = 3


@dataclass
class CallCounters:
    routing: int = 0
    geocoding: int = 0

    @property
    def total(self) -> int:
        return self.routing + self.geocoding

    def as_dict(self) -> dict:
        return {
            "routing_api": self.routing,
            "geocoding_api": self.geocoding,
            "total": self.total,
        }


@dataclass
class Timings:
    """Milliseconds per stage. `local_compute_ms` deliberately excludes external
    latency, because that is the only part we control."""

    _marks: dict[str, float] = field(default_factory=dict)

    def record(self, name: str, seconds: float) -> None:
        self._marks[name] = round(seconds * 1000, 2)

    @property
    def external_ms(self) -> float:
        return round(self._marks.get("routing", 0.0) + self._marks.get("geocoding", 0.0), 2)

    @property
    def local_compute_ms(self) -> float:
        return round(sum(self._marks.values()) - self.external_ms, 2)

    def as_dict(self) -> dict:
        return {
            **self._marks,
            "external_ms": self.external_ms,
            "local_compute_ms": self.local_compute_ms,
            "total_ms": round(sum(self._marks.values()), 2),
        }


@dataclass
class PlanResult:
    origin: ResolvedLocation
    destination: ResolvedLocation
    route: Route
    plan: FuelPlan
    corridor_stops: list[Station]
    max_detour_miles: float
    calls: CallCounters
    timings: Timings
    route_cache_hit: bool
    stops_indexed: int


def _cache_key(origin: tuple[float, float], destination: tuple[float, float]) -> str:
    return (
        "route:v1:"
        f"{round(origin[0], CACHE_COORD_PRECISION)},{round(origin[1], CACHE_COORD_PRECISION)}:"
        f"{round(destination[0], CACHE_COORD_PRECISION)},{round(destination[1], CACHE_COORD_PRECISION)}"
    )


def default_route_provider() -> RouteProvider:
    return OsrmRouteProvider(
        base_url=settings.OSRM_BASE_URL,
        fallback_url=settings.OSRM_FALLBACK_BASE_URL,
        timeout_seconds=settings.OSRM_TIMEOUT_SECONDS,
        user_agent=settings.HTTP_USER_AGENT,
    )


def plan_route(
    origin_text: str,
    destination_text: str,
    max_detour_miles: float | None = None,
    start_tank_gallons: float | None = None,
    route_provider: RouteProvider | None = None,
    use_cache: bool = True,
) -> PlanResult:
    """Plan the cheapest refuelling for a trip. Raises LocationError /
    RoutingError / RouteInfeasibleError, all of which the view maps to a problem
    document."""
    detour = settings.MAX_DETOUR_MILES if max_detour_miles is None else max_detour_miles
    start_gallons = (
        settings.START_TANK_GALLONS if start_tank_gallons is None else start_tank_gallons
    )
    provider = route_provider or default_route_provider()
    calls = CallCounters()
    timings = Timings()

    # Resolve the indexes BEFORE the first timer starts. They are process-level
    # singletons warmed at startup, but on a cold process the first caller would
    # otherwise pay the build cost inside a timed stage - which is exactly how an
    # earlier version reported a 132 ms "corridor_search" that was really a 127 ms
    # index build. Timings must measure the work they name.
    place_index = get_place_index()
    stop_index = get_stop_index()

    # --- 1. Endpoints. Offline for coordinates and known places: 0 calls. ---
    started = time.perf_counter()
    origin = resolve_location(origin_text, place_index)
    destination = resolve_location(destination_text, place_index)
    calls.geocoding += origin.external_calls + destination.external_calls
    timings.record("geocoding", time.perf_counter() - started)

    # --- 2. Route. Exactly one call, or zero on a cache hit. ---
    started = time.perf_counter()
    key = _cache_key((origin.lat, origin.lon), (destination.lat, destination.lon))
    route: Route | None = cache.get(key) if use_cache else None
    cache_hit = route is not None
    if route is None:
        route = provider.fetch((origin.lat, origin.lon), (destination.lat, destination.lon))
        calls.routing += route.external_calls
        if use_cache:
            cache.set(key, route)
    timings.record("routing", time.perf_counter() - started)

    # --- 3. Corridor search. Local. ---
    started = time.perf_counter()
    corridor_stops = find_corridor_stops(route.points, stop_index, detour)
    timings.record("corridor_search", time.perf_counter() - started)

    # --- 4. Optimise. Local. ---
    started = time.perf_counter()
    plan = solve_refuelling(
        corridor_stops,
        total_miles=route.total_miles,
        tank_gallons=settings.TANK_CAPACITY_GALLONS,
        mpg=settings.MILES_PER_GALLON,
        start_gallons=start_gallons,
    )
    timings.record("optimisation", time.perf_counter() - started)

    return PlanResult(
        origin=origin,
        destination=destination,
        route=route,
        plan=plan,
        corridor_stops=corridor_stops,
        max_detour_miles=detour,
        calls=calls,
        timings=timings,
        route_cache_hit=cache_hit,
        stops_indexed=len(stop_index),
    )


def assumptions(max_detour_miles: float, start_gallons: float) -> dict:
    """Everything a caller needs to interpret the numbers.

    Returned on every response, including errors where it applies. Whether the
    tank starts full or empty changes the total materially, and the price basis is
    an aggregate of undated observations rather than a live quote - burying either
    would make the headline figure look more authoritative than it is.
    """
    return {
        "tank_capacity_gallons": settings.TANK_CAPACITY_GALLONS,
        "miles_per_gallon": settings.MILES_PER_GALLON,
        "vehicle_range_miles": settings.VEHICLE_RANGE_MILES,
        "start_tank_gallons": start_gallons,
        "start_tank_note": (
            "The tank is assumed empty at the origin, so the reported cost covers "
            "all fuel the trip consumes. The vehicle is assumed able to reach the "
            "first usable stop; the miles before that first purchase are reported "
            "as unpriced_origin_miles and valued at the first pump's price in "
            "estimated_total_fuel_cost_usd."
        ),
        "max_detour_miles": max_detour_miles,
        "price_basis": f"{settings.PRICE_BASIS}_of_undated_observations",
        "price_basis_note": (
            "The source file has no date column, so prices cannot be ordered or "
            "dated. Each stop's price is the "
            f"{settings.PRICE_BASIS} of its observations. This is not a live price."
        ),
        "currency": "USD",
        "geocoding_precision_note": (
            "Stop coordinates are city/place centroids, not surveyed pump "
            "locations. Measured positional error is ~1.6 mi median and ~4.5 mi "
            "at p90, so detour_miles is advisory, not navigable. Total cost is "
            "far more robust: it depends on route distance, which is exact."
        ),
        "scope_note": (
            "Only US truck stops are considered. 112 Canadian stops in the source "
            "file are excluded: the brief routes between US locations, and their "
            "prices are ~31% higher with no currency column to disambiguate."
        ),
    }
