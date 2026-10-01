# Fuel-Optimal Route API

Given a start and finish in the USA, this returns the driving route, the
cost-optimal places to buy diesel along it for a 500-mile-range truck, and what
the fuel will cost.

Built for the Spotter backend assessment. Django 6.1.1 on Python 3.13.

---

## The problem worth knowing about

The supplied price file has **8,151 rows and no coordinates**. Its addresses are
highway-exit descriptors, not street addresses:

```
I-44, EXIT 283 & US-69
I-81, EXIT 273 & SR-703/SR-292
```

96.4% of rows carry a highway or exit token; **0.1% look like a street address**.
So street-level geocoding is useless here, and geocoding 8,100 stops through an
API is slow, rate-limited and unreproducible.

**This project resolves 100% of in-scope stops with zero geocoding API calls**, by
joining City + State against public-domain gazetteer files offline. The resolved
coordinates are committed, so a reviewer never runs that pipeline.

---

## Quick start

Requires Python 3.12+ (Django 6.1 requires it). No Docker, no PostgreSQL.

```bash
make setup     # install, migrate, seed            (~16s from a clean clone)
make run       # start the API and map page on :8000
```

Then open <http://127.0.0.1:8000> for the map, or:

```bash
curl "http://127.0.0.1:8000/api/v1/route?start=Dallas,+TX&finish=Chicago,+IL"
```

If `python3.13` is not on your PATH: `make setup PYTHON=python3.12`, or
`brew install python@3.13` on macOS.

| Command | What it does |
|---|---|
| `make setup` | Install dependencies, run migrations, seed 6,626 stops |
| `make run` | Start the server |
| `make test` | 239 tests with coverage |
| `make bench` | Measure p50/p95 latency over 12 real long-haul routes |
| `make verify` | Validate the seeded coordinates and prices |
| `make pipeline` | Regenerate the stop artefact from the CSV (**not needed**) |

---

## The API

### `GET` or `POST` `/api/v1/route`

| Parameter | Required | Default | Notes |
|---|---|---|---|
| `start` | yes | — | `Dallas, TX` or `32.7767,-96.7970` |
| `finish` | yes | — | Same forms |
| `max_detour_miles` | no | `10` | How far off-route a stop may be. Max 50 |
| `start_tank_gallons` | no | `0` | Fuel aboard at the origin. Max 50 |
| `geometry` | no | `simplified` | `simplified`, `full`, or `none` |

### Example

```bash
curl "http://127.0.0.1:8000/api/v1/route?start=Dallas,+TX&finish=Chicago,+IL&geometry=none"
```

```jsonc
{
  "request": { "start": "Dallas, TX", "finish": "Chicago, IL", "max_detour_miles": 10.0 },
  "origin": {
    "query": "Dallas, TX", "resolved_label": "Dallas, TX",
    "latitude": 32.7933, "longitude": -96.7665,
    "resolution_source": "place_index"        // offline: no geocoding call
  },
  "destination": { "...": "..." },
  "route": {
    "total_distance_miles": 961.0,
    "estimated_driving_hours": 17.04,
    "provider": "osrm-demo"
  },
  "fuel_stops": [
    {
      "order": 1,
      "opis_truckstop_id": "72773",
      "name": "RaceTrac #2626",
      "address": "I-20 Exit 472",
      "city": "Dallas", "state": "TX",
      "latitude": 32.793333, "longitude": -96.766513,
      "price_usd_per_gallon": 2.864,
      "gallons_purchased": 1.153,
      "cost_usd": 3.3,
      "distance_along_route_miles": 0.0,
      "detour_miles": 0.01,
      "tank_gallons_on_arrival": 0.0,
      "tank_gallons_on_departure": 1.153
    }
    // ... 5 more
  ],
  "totals": {
    "fuel_stop_count": 6,
    "total_gallons_purchased": 96.103,
    "total_fuel_cost_usd": 274.66,          // provably spent at pumps
    "unpriced_origin_miles": 0.0,
    "unpriced_origin_gallons": 0.0,
    "origin_leg_estimated_cost_usd": 0.0,
    "estimated_total_fuel_cost_usd": 274.66, // whole trip - see "Two cost numbers"
    "total_gallons_consumed": 96.103,
    "effective_price_usd_per_gallon": 2.8579,
    "tank_gallons_at_destination": 0.0
  },
  "assumptions": {
    "tank_capacity_gallons": 50.0,
    "miles_per_gallon": 10.0,
    "vehicle_range_miles": 500.0,
    "start_tank_gallons": 0.0,
    "max_detour_miles": 10.0,
    "price_basis": "median_of_undated_observations",
    "currency": "USD"
    // plus explanatory notes on each
  },
  "diagnostics": {
    "external_calls": { "routing_api": 1, "geocoding_api": 0, "total": 1 },
    "route_cache_hit": false,
    "timings_ms": { "corridor_search": 5.16, "optimisation": 0.09,
                    "external_ms": 1286.21, "local_compute_ms": 5.73 },
    "stops_indexed": 6626,
    "corridor_stops_considered": 189
  }
}
```

That Dallas–Chicago plan buys 96.1 gallons at an effective **$2.858/gal** against
a dataset median of **$3.399** — and reaches Chicago with the tank at exactly
`0.0`.

### Errors

Every error is [RFC 7807](https://datatracker.ietf.org/doc/html/rfc7807)
`application/problem+json` with a stable `code`:

| Code | Status | When |
|---|---|---|
| `invalid_request` | 400 | Serializer validation failed; see `errors` |
| `outside_us` | 400 | A coordinate is not in the USA |
| `ambiguous_location` | 400 | Bare city name in several states |
| `unresolvable_location` | 400 | Place not found |
| `gap_exceeds_range` | 422 | Two consecutive stops are >500 mi apart |
| `no_fuel_stops_on_route` | 422 | Nothing in the corridor at all |
| `no_route_found` | 422 | No drivable route between the points |
| `routing_failed` | 502 | The routing provider is unavailable |
| `stops_not_seeded` | 503 | Database empty; run `make setup` |

An infeasible route names the offending leg rather than returning a plausible
wrong answer:

```json
{
  "code": "gap_exceeds_range",
  "detail": "No fuel stop within range: the 961.0 mi leg from RaceTrac #2626 (mile 0.0) to ROAD RANGER #187 (mile 961.0) exceeds the 500 mi vehicle range. Try increasing max_detour_miles.",
  "infeasibility": { "gap_miles": 961.0, "vehicle_range_miles": 500.0,
                     "from": {"label": "RaceTrac #2626", "mile": 0.0},
                     "to": {"label": "ROAD RANGER #187", "mile": 961.0} }
}
```

### The map

<http://127.0.0.1:8000> renders the route and numbered fuel stops on Leaflet with
OpenStreetMap tiles. It calls the same public endpoint, so it cannot show
anything the documented API would not return.

---

## External call accounting

**One routing call per uncached request. Zero geocoding calls on the normal path.**

Don't take my word for it — every response reports its own measured counts in
`diagnostics.external_calls`, and the benchmark prints the total:

```
external calls for the whole cold pass: 12 (12 routes)
external calls, warm passes   0  (the cold pass populated the route cache)
```

| Path | Routing | Geocoding |
|---|---|---|
| `lat,lon` input, cold cache | 1 | 0 |
| `City, ST` input, cold cache | 1 | 0 |
| Any input, warm cache | 0 | 0 |
| Place absent from the 53,675-entry index | 1 | 1 (Nominatim, cached) |

Free-text costs nothing because endpoint names resolve against the same committed
offline place index. The only way to spend a geocoding call is to ask for
somewhere that index has never heard of.

---

## Measured performance

```bash
make bench     # or: python manage.py benchmark --repeats 20 --offline --compare-naive
```

12 real long-haul routes × 20 repeats = **240 samples**, this machine
(Apple Silicon, Python 3.13.16):

| Metric | Value |
|---|---|
| Local compute **p50** | **4.31 ms** |
| Local compute **p95** | **14.91 ms** |
| Local compute mean / max | 5.72 / 15.58 ms |
| Target | 200 ms p95 — **met with 13× margin** |

Local compute is corridor search + optimisation + serialisation. External routing
latency is reported separately because it is not ours to control, and it is
**wildly variable**: two runs of the same 12 routes measured p50 **1264.7 ms** and
p50 **389.3 ms**. Blending that into one number would be meaningless.

**Corridor search versus a naive scan**, same command:

| Route | Indexed | Naive `O(S×P)` | Speedup | Stops found |
|---|---|---|---|---|
| Dallas→Chicago | 5.51 ms | 1221.24 ms | **221.5×** | 189 / 189 |
| LA→New York | 15.35 ms | 3748.01 ms | **244.2×** | 460 / 460 |
| Seattle→Miami | 12.35 ms | 4431.04 ms | **358.8×** | 389 / 389 |

Identical stop counts in every pair, so it is a pure speedup, not a different
answer. See `ARCHITECTURE.md` for how.

> The first `make bench` makes 12 routing calls and records them to
> `.cache/route_fixtures` (6.5 MB, not committed). Every run after that is free
> and reproducible; add `--offline` to guarantee no network access.

**Response sizes** (gzip is on):

| Route | `simplified` (default) | `full` |
|---|---|---|
| Dallas→Chicago | 23.7 KiB / **8.5 KiB gz** | 196.6 KiB / 56.7 gz |
| LA→New York | 79.9 KiB / **26.8 KiB gz** | 719.3 KiB / 198.8 gz |
| Seattle→Miami | 83.7 KiB / **28.4 KiB gz** | 747.6 KiB / 212.3 gz |

---

## Data pipeline

Run once, offline, by me. **A reviewer never needs this** — the output is
committed and `make setup` loads it.

```bash
make pipeline      # python manage.py build_fuel_index
```

Measured on the supplied CSV:

```
rows_read                            8151
distinct_stops                       6738      (dedup key: OPIS Truckstop ID)
stops_with_multiple_observations      678
stops by country                     {'US': 6626, 'CA': 112}

in-scope (US) stops                  6626
census_gazetteer                     6283  (94.823%)   public domain
geonames                              327  ( 4.935%)   CC-BY 4.0
geonames_landmark                       6  ( 0.091%)   CC-BY 4.0
nominatim                              10  ( 0.151%)   committed cache
TOTAL RESOLVED (of US)               6626  (100.000%)
ambiguous (>10mi apart, flagged)       56
rejected by bbox validation             0
excluded: out of scope (non-US)       112
excluded: UNRESOLVED (US)               0
```

**No row is ever dropped silently.** The two exclusion reasons are reported
separately and recorded in the artefact, because "Canadian, out of scope by
design" and "we failed to locate this" are different facts.

The pipeline is idempotent: three consecutive runs produce byte-identical output.
Provenance is a SHA-256 of the input CSV, not a timestamp, so regenerating
unchanged data is not a diff.

---

## Assumptions

Returned in every response under `assumptions`, not buried here.

| Assumption | Value | Why |
|---|---|---|
| Tank capacity | 50 gal | 500 mi range ÷ 10 mpg, from the brief |
| Fuel economy | 10 mpg | From the brief |
| Starting fuel | **0 gal** | So the reported cost covers all fuel the trip consumes |
| Max detour | **10 mi** | See "Known limitations" — 5 would be finer than the data's resolution |
| Price basis | **median** of each stop's undated observations | The file has no date column |
| Scope | US stops only | The brief routes between US locations |

### Two cost numbers, and why

- **`total_fuel_cost_usd`** — what the plan provably spends at pumps.
- **`estimated_total_fuel_cost_usd`** — the whole trip, including the leg before
  the first stop, valued at that first stop's price.

With an empty starting tank, the miles before the first purchase are fuel we did
not buy. Usually that's nothing. **On Los Angeles → New York it is 238.7 miles**,
because all 8 California stops in the dataset sit in the Imperial Valley while the
route runs north-east on I-15. That's 23.87 gallons, **$82.56, or 10.6% of the
total**. Reporting only the first number would understate that trip; reporting
only the second would blend a measured figure with an estimated one. So both are
returned, plus `unpriced_origin_miles`.

---

## Known limitations

Honest list. These are real, and I'd rather state them than have them found.

**1. Stop coordinates are city centroids, not pumps.** This is the big one. The
CSV has no coordinates and no street addresses, so each stop is placed at the
centroid of its city. Measured positional error, using Census land area as a
proxy for city radius:

```
median 1.59 mi   p75 2.70 mi   p90 4.54 mi   p99 11.22 mi   max 15.42 mi
```

Consequences:
- **`detour_miles` is advisory, not navigable.** It is accurate to roughly ±5 mi.
- **Total cost is far more robust.** Cost = gallons × price; gallons depends on
  route distance, which is exact from the routing engine. Stop position only
  perturbs *which* of several nearby stops is chosen, and nearby stops have
  similar prices.
- In the densest city, **22 stops share one coordinate**. Among stops genuinely in
  the same town, price is the correct tiebreak, so this is benign — but they are
  not really at the same place.

**2. `MAX_DETOUR_MILES` defaults to 10, not 5.** The brief suggested 5. Measured
geocoding error is median 1.59 mi and p90 4.54 mi, so a 5-mile corridor filters at
a finer resolution than the coordinates actually have. 10 exceeds p90. It is a
request parameter, so try both.

**3. Prices are undated aggregates, not live quotes.** The file has no date
column. 678 stops carry several observations (within-stop spread: median $0.06,
max $0.90) and the median is reported. This is not a current price and the API
says so.

**4. 112 Canadian stops are excluded.** Their prices run ~31% higher with no
currency column to disambiguate, and the brief routes between US locations. They
are loaded with a `country` field and reported, not dropped.

**5. The routing provider is a free demo server with no published SLA.** The OSRM
wiki states only that FOSSGIS sponsors it; it publishes no rate limit. A keyless
fallback with an identical response shape is configured, but the production answer
is self-hosted OSRM. Pre-warm the cache before any live demo.

**6. The PostgreSQL path is configured but unverified.** Docker is not installed
on the machine this was built on, so `docker-compose.yml` is authored but never
executed, and it says so in a comment. What *is* verified: the settings switch
selects the `postgresql` backend, migrations are backend-agnostic
(`makemigrations --check` is clean under both), and the PostgreSQL DDL generates
correctly offline with every constraint and index intact. **Not verified:**
`migrate` and `seed_stops` against a running server. SQLite is the path tested end
to end.

**7. Alaska, Hawaii and DC have no stops in the file**, so routes there will be
infeasible. That is the data, not a bug — but the error says `gap_exceeds_range`,
which is accurate rather than illuminating.

**8. The map page has not been opened in a real browser by me.** Its logic is
executed under Node against real API responses (polyline, markers, totals, error
path), the JS parses, the SRI hashes are verified against the live CDN bytes, and
the static file serves. Visual layout is unverified.

---

## Testing

```bash
make test      # python -m pytest tests/ -q --cov --cov-report=term-missing
```

**239 tests. 90.7% coverage.** That is what the tool prints; `.coveragerc` sets no
`fail_under` and does not skip covered files.

| Module | | Module | |
|---|---|---|---|
| `services/geo.py` | 100.0% | `fuelroute/views.py` | 100.0% |
| `services/corridor.py` | 98.2% | `fuelroute/planner.py` | 97.7% |
| `services/routing.py` | 96.7% | `services/places.py` | 96.2% |
| `services/normalize.py` | 95.7% | `services/optimizer.py` | 94.8% |
| `services/fuel_data.py` | 94.8% | `services/gazetteer.py` | 90.0% |

What is *not* well covered: the `benchmark` command's reporting branches (65.8%)
and `verify_stops`' failure paths (67.0%). Both are diagnostic output. I would
rather say that than write assertions on log formatting to inflate a number.

**No test touches the network, and it is enforced, not intended.** `conftest.py`
patches `socket.connect`/`connect_ex`/`create_connection` to raise, and
`tests/test_no_network.py` proves it by attempting a raw socket, a `requests`
call, and a fetch through the real OSRM provider.

The optimiser is checked against 27 hand-computed fixtures *and* against an
exhaustive dynamic-programming optimum over 400 generated instances — a stronger
claim than merely beating a baseline, which is also tested.

---

## Configuration

Everything has a working default; `.env` is optional. See `.env.example`.

| Variable | Default | |
|---|---|---|
| `DB_ENGINE` | `sqlite` | or `postgres` |
| `MAX_DETOUR_MILES` | `10` | |
| `START_TANK_GALLONS` | `0` | |
| `TANK_CAPACITY_GALLONS` / `MILES_PER_GALLON` | `50` / `10` | Range is derived |
| `PRICE_BASIS` | `median` | or `mean`, `min` |
| `OSRM_BASE_URL` | `https://router.project-osrm.org` | |
| `OSRM_FALLBACK_BASE_URL` | `https://routing.openstreetmap.de/routed-car` | |

No secrets in the repo. `.env` is gitignored.

---

## Attribution

- Routing: [OSRM](https://project-osrm.org/) demo server, sponsored by FOSSGIS.
- Map tiles and routing data: © [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors.
- Place coordinates: US Census Bureau 2025 Gazetteer (public domain) and
  [GeoNames](https://www.geonames.org/) (CC-BY 4.0).
- Fuel prices: OPIS file supplied with the assessment.

See `ARCHITECTURE.md` for why each of these was chosen, and `PLAN.md` for the
Phase 0 analysis the design came from.
