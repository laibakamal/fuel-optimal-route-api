"""Latency benchmark over real long-haul routes.

    python manage.py benchmark
    python manage.py benchmark --repeats 20 --json bench.json
    python manage.py benchmark --offline            # replay cached routes only

HOW IT AVOIDS ABUSING A FREE SERVICE
    Each route is fetched from OSRM exactly ONCE, into the route cache. The
    repeated measurement then replays from cache. So --repeats 20 over 12 routes
    still costs 12 external calls, not 240. The cold external latency is reported
    separately from that warm-up pass.

WHAT THE NUMBERS MEAN
    local_compute_ms is the part we control: corridor search plus optimisation
    plus serialisation. external_ms is the routing provider's round trip, which is
    network-bound and varies with their load. Reporting one number that blends them
    would hide whether our code or the internet is the bottleneck.
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError

from fuelroute.indexes import get_place_index, get_stop_index
from fuelroute.planner import default_route_provider, plan_route
from services.corridor import find_corridor_stops, sample_route
from services.geo import haversine_miles
from services.optimizer import RouteInfeasibleError
from services.route_fixtures import FixtureBackedProvider

#: Twelve real long-haul US routes, chosen to span the country and to include
#: sparse corridors (Portland-Salt Lake, Kansas City-Las Vegas) as well as dense
#: ones (Dallas-Chicago), plus one short trip inside a single tank.
ROUTES = [
    ("Dallas, TX", "Chicago, IL"),
    ("Los Angeles, CA", "New York, NY"),
    ("Seattle, WA", "Miami, FL"),
    ("Houston, TX", "Atlanta, GA"),
    ("Denver, CO", "Phoenix, AZ"),
    ("Portland, OR", "Salt Lake City, UT"),
    ("Minneapolis, MN", "Dallas, TX"),
    ("Boston, MA", "Washington, DC"),
    ("Detroit, MI", "Nashville, TN"),
    ("Kansas City, MO", "Las Vegas, NV"),
    ("Jacksonville, FL", "Memphis, TN"),
    ("Indianapolis, IN", "Denver, CO"),
]


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. No interpolation: with small samples an
    interpolated p95 invents a value that was never measured."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def naive_corridor(route_points, index, max_detour_miles):
    """The implementation that was replaced, kept ONLY as a measurement baseline.

    Indexes nothing: for every stop, scan every route sample. This is the honest
    O(S x P) comparison for the speedup claimed in ARCHITECTURE.md, and running it
    here is what makes that claim reproducible rather than remembered.
    """
    samples, _ = sample_route(route_points, 1.0)
    found = 0
    for stop in index._stops:  # noqa: SLF001 - benchmark baseline, not production
        best = float("inf")
        for sample in samples:
            distance = haversine_miles(stop.lat, stop.lon, sample.lat, sample.lon)
            if distance < best:
                best = distance
        if best <= max_detour_miles:
            found += 1
    return found


class Command(BaseCommand):
    help = "Measure p50/p95 latency over real long-haul routes."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--repeats", type=int, default=10,
                            help="Warm measurement passes per route (default 10).")
        parser.add_argument("--detour", type=float, default=None,
                            help="max_detour_miles override.")
        parser.add_argument("--offline", action="store_true",
                            help="Use only recorded route fixtures; error on a miss.")
        parser.add_argument("--fixtures", type=Path, default=None,
                            help="Route fixture directory (default .cache/route_fixtures).")
        parser.add_argument("--compare-naive", action="store_true",
                            help="Also time the O(SxP) baseline corridor search.")
        parser.add_argument("--json", type=Path, default=None,
                            help="Write the full results as JSON.")

    def handle(self, *args, **opts) -> None:
        repeats = max(1, opts["repeats"])
        detour = opts["detour"]

        # Route responses are recorded to disk on first run, so repeat benchmarks
        # are free and reproducible. The in-process cache dies with the process;
        # this does not.
        from django.conf import settings
        fixture_dir = opts["fixtures"] or (settings.CACHE_DIR / "route_fixtures")
        provider = FixtureBackedProvider(
            default_route_provider(), fixture_dir, offline=opts["offline"]
        )

        started = time.perf_counter()
        place_index = get_place_index()
        stop_index = get_stop_index()
        warmup_ms = (time.perf_counter() - started) * 1000
        self.stdout.write(
            f"Indexes: {len(stop_index)} stops in {stop_index.cell_count} cells, "
            f"{len(place_index)} places, built in {warmup_ms:.0f} ms"
        )
        self.stdout.write(f"Routes: {len(ROUTES)}   repeats: {repeats}\n")

        # --- Pass 1: one external call per route, into the cache --------------
        self.stdout.write(self.style.MIGRATE_HEADING(
            "Pass 1 - cold: one external routing call per route"
        ))
        cold: list[dict] = []
        for origin, destination in ROUTES:
            try:
                result = plan_route(
                    origin, destination, max_detour_miles=detour,
                    route_provider=provider, use_cache=False,
                )
            except RouteInfeasibleError as exc:
                self.stdout.write(self.style.WARNING(
                    f"   {origin:>20s} -> {destination:<20s} INFEASIBLE ({exc.reason})"
                ))
                cold.append({"origin": origin, "destination": destination,
                             "infeasible": exc.reason})
                continue
            except Exception as exc:  # noqa: BLE001
                if opts["offline"]:
                    raise CommandError(
                        f"--offline but {origin} -> {destination} is not cached: {exc}"
                    ) from exc
                self.stdout.write(self.style.ERROR(
                    f"   {origin:>20s} -> {destination:<20s} FAILED: {exc}"
                ))
                cold.append({"origin": origin, "destination": destination,
                             "error": str(exc)})
                continue

            timings = result.timings.as_dict()
            cold.append({
                "origin": origin,
                "destination": destination,
                "miles": round(result.route.total_miles, 1),
                "route_points": len(result.route.points),
                "corridor_stops": len(result.corridor_stops),
                "fuel_stops": len(result.plan.purchases),
                "estimated_total_usd": round(result.plan.estimated_total_cost, 2),
                "external_ms": timings["external_ms"],
                "local_compute_ms": timings["local_compute_ms"],
                "routing_calls": result.calls.routing,
                "geocoding_calls": result.calls.geocoding,
            })
            self.stdout.write(
                f"   {origin:>20s} -> {destination:<20s} "
                f"{result.route.total_miles:7.1f} mi  "
                f"{len(result.route.points):6d} pts  "
                f"{len(result.corridor_stops):4d} corridor  "
                f"{len(result.plan.purchases):2d} stops  "
                f"ext {timings['external_ms']:7.1f} ms  "
                f"local {timings['local_compute_ms']:6.2f} ms  "
                f"calls {result.calls.total}"
            )

        usable = [row for row in cold if "miles" in row]
        if not usable:
            raise CommandError("No routes succeeded; cannot report latency.")

        total_calls = sum(row["routing_calls"] + row["geocoding_calls"] for row in usable)
        self.stdout.write(
            f"\n   external calls for the whole cold pass: {total_calls} "
            f"({len(usable)} routes)"
        )
        self.stdout.write(
            f"   provider network calls: {provider.network_calls}   "
            f"fixture hits: {provider.fixture_hits}   dir: {fixture_dir}"
        )

        # --- Pass 2: warm, cache hits, repeated ------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nPass 2 - warm: {repeats} passes per route, served from the route cache"
        ))
        per_route: dict[str, list[float]] = {}
        all_local: list[float] = []
        warm_calls = 0
        for origin, destination in ROUTES:
            key = f"{origin} -> {destination}"
            if not any(r["origin"] == origin and r["destination"] == destination
                       for r in usable):
                continue
            samples: list[float] = []
            for _ in range(repeats):
                result = plan_route(
                    origin, destination, max_detour_miles=detour,
                    route_provider=provider, use_cache=True,
                )
                warm_calls += result.calls.total
                samples.append(result.timings.local_compute_ms)
            per_route[key] = samples
            all_local.extend(samples)
            self.stdout.write(
                f"   {key:<44s} p50 {percentile(samples, 0.50):6.2f} ms   "
                f"p95 {percentile(samples, 0.95):6.2f} ms   "
                f"min {min(samples):6.2f}   max {max(samples):6.2f}"
            )

        cold_external = [row["external_ms"] for row in usable]
        p50_local, p95_local = percentile(all_local, 0.50), percentile(all_local, 0.95)

        self.stdout.write(self.style.MIGRATE_HEADING("\nSummary"))
        self.stdout.write(f"   samples                        {len(all_local)}")
        self.stdout.write(
            f"   LOCAL COMPUTE  p50            {p50_local:8.2f} ms\n"
            f"   LOCAL COMPUTE  p95            {p95_local:8.2f} ms\n"
            f"   LOCAL COMPUTE  mean / max     {statistics.fmean(all_local):8.2f} / "
            f"{max(all_local):.2f} ms"
        )
        self.stdout.write(
            f"   external (cold) p50           {percentile(cold_external, 0.50):8.1f} ms\n"
            f"   external (cold) p95           {percentile(cold_external, 0.95):8.1f} ms"
        )
        self.stdout.write(
            f"   external calls, warm passes   {warm_calls}  "
            f"(expected 0: every route cached)"
        )

        target = 200.0
        if p95_local <= target:
            self.stdout.write(self.style.SUCCESS(
                f"\n   TARGET MET: local compute p95 {p95_local:.2f} ms <= {target:.0f} ms"
            ))
        else:
            self.stdout.write(self.style.ERROR(
                f"\n   TARGET MISSED: local compute p95 {p95_local:.2f} ms > {target:.0f} ms"
            ))

        # --- Optional: the O(SxP) baseline, for the speedup claim ------------
        naive_rows = []
        if opts["compare_naive"]:
            self.stdout.write(self.style.MIGRATE_HEADING(
                "\nBaseline comparison - naive O(S x P) corridor search"
            ))
            detour_value = detour if detour is not None else None
            from django.conf import settings
            effective_detour = settings.MAX_DETOUR_MILES if detour_value is None else detour_value
            for origin, destination in ROUTES[:3]:
                result = plan_route(
                    origin, destination, max_detour_miles=detour,
                    route_provider=provider, use_cache=True,
                )
                points = result.route.points
                t0 = time.perf_counter()
                fast = find_corridor_stops(points, stop_index, effective_detour)
                fast_ms = (time.perf_counter() - t0) * 1000
                t0 = time.perf_counter()
                naive_count = naive_corridor(points, stop_index, effective_detour)
                naive_ms = (time.perf_counter() - t0) * 1000
                naive_rows.append({
                    "route": f"{origin} -> {destination}",
                    "fast_ms": round(fast_ms, 2), "naive_ms": round(naive_ms, 2),
                    "speedup": round(naive_ms / fast_ms, 1) if fast_ms else None,
                    "fast_stops": len(fast), "naive_stops": naive_count,
                })
                self.stdout.write(
                    f"   {origin} -> {destination}: "
                    f"indexed {fast_ms:7.2f} ms ({len(fast)} stops)   "
                    f"naive {naive_ms:8.2f} ms ({naive_count} stops)   "
                    f"speedup {naive_ms / fast_ms:5.1f}x"
                )

        if opts["json"]:
            payload = {
                "repeats": repeats,
                "max_detour_miles": detour,
                "index_build_ms": round(warmup_ms, 1),
                "cold": cold,
                "warm_local_compute_ms": per_route,
                "summary": {
                    "samples": len(all_local),
                    "local_p50_ms": round(p50_local, 2),
                    "local_p95_ms": round(p95_local, 2),
                    "local_mean_ms": round(statistics.fmean(all_local), 2),
                    "local_max_ms": round(max(all_local), 2),
                    "external_cold_p50_ms": round(percentile(cold_external, 0.50), 1),
                    "external_cold_p95_ms": round(percentile(cold_external, 0.95), 1),
                    "warm_external_calls": warm_calls,
                    "target_met": bool(p95_local <= target),
                },
                "naive_comparison": naive_rows,
            }
            opts["json"].write_text(json.dumps(payload, indent=2))
            self.stdout.write(f"\n   wrote {opts['json']}")
