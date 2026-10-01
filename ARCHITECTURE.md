# Architecture

Why this is built the way it is. Numbers here were measured in this repo; the
commands to re-measure them are given.

Companion documents: `README.md` (what it does, how to run it), `PLAN.md` (the
Phase 0 analysis this design came out of).

---

## 1. The shape of the problem

The brief looks like a routing exercise. It isn't. Routing is one HTTP call to a
free service. The actual problem is in the supplied data:

> **8,151 fuel stops, no latitude, no longitude, and addresses that are highway
> descriptors rather than street addresses.**

```
I-44, EXIT 283 & US-69          96.4% of rows carry a highway/exit token
I-81, EXIT 273 & SR-703/SR-292  56.2% carry EXIT <n>
US-46                            0.1% look like a street address
```

Everything else follows from that. You cannot geocode a street address that isn't
there, and you cannot make 8,100 API calls inside a 3-day exercise and call it
reproducible. So the first design decision is: **resolve coordinates offline,
once, and commit the result.**

---

## 2. Geocoding: three offline tiers, zero API calls

**Decision: join City + State against public gazetteer files, in tiers.**

| Tier | Source | Licence | Resolves |
|---|---|---|---|
| 1 | US Census Bureau 2025 Gazetteer, Places file | US Government work — **public domain** | 6,283 (94.823%) |
| 2 | GeoNames US, feature class `P` (populated places), excluding `PPLQ` | CC-BY 4.0 | 327 (4.935%) |
| 3 | GeoNames US **landmarks**: post offices, parks, civil divisions | CC-BY 4.0 | 6 (0.091%) |
| 4 | Nominatim, rate-limited, committed cache | ODbL | 10 (0.151%) |
| | | **Total** | **6,626 (100.000%)** |

Re-measure: `make pipeline`.

**Why Census is tier 1.** Public domain (no attribution obligation), authoritative
for incorporated places and CDPs, and it ships `ALAND_SQMI`, which does double
duty: it breaks ties between same-named places and it gives us an estimate of our
own positional error (§7).

**Why GeoNames is tier 2.** Census Places covers incorporated places. Truck stops
sit next to unincorporated communities that Census does not list — Breezewood PA,
Heiskell TN, Ruther Glen VA. GeoNames has them.

**Why tier 3 exists, and why it is my favourite part.** Six stops resolved nowhere.
The obvious move is to call a geocoder. I did, and **Nominatim placed `Crescent, PA`
at 39.896, −75.175 — near Chester, 270 miles from the real Crescent Township in
Allegheny County.** A state bounding-box check does not catch this, because the
wrong point is still inside Pennsylvania.

What does catch it: GeoNames has a `Crescent Post Office` at 40.5598, −80.2235.
**A post office is named after the community it serves**, which makes it a sound
position proxy for a place no gazetteer lists as populated. So tier 3 indexes post
offices, parks and civil divisions with the suffix stripped.

Cross-checked against Nominatim on the seven cases where both have an answer, the
landmark tier agrees within 1.6 miles on six and is right about Crescent by 270
miles. It also removed six network calls. The allowlist is deliberately narrow —
`PO`, `PRK`, `ADMD`, `ADM3`, `ADM4` — because `Crescent Elementary School` is weak
evidence of where Crescent is, while `Crescent Post Office` is strong. Tested in
`tests/test_gazetteer.py`.

**Name normalisation** is what lifts tier 1 from 90.5% to 94.8%: Unicode folding,
punctuation stripping, `SAINTE|SAINT|STE→ST`, `FORT→FT`, `MOUNT→MT`, removal of
**one** trailing LSAD token, and a de-spaced variant so OPIS `Mc Calla` matches
Census `McCalla`.

> One subtle bug worth recording: stripping the LSAD suffix *repeatedly* turns
> `Kansas City city` into `Kansas`, losing the match. It must be stripped once.
> `tests/test_fuel_data.py::test_lsad_suffix_is_stripped_once_not_repeatedly`.

### What I rejected

| Option | Why not |
|---|---|
| 8,100 live geocoding calls | Slow, rate-limited, abusive of a free service, unreproducible. This is the thing the whole design exists to avoid. |
| Parsing interstate + exit number to snap stops onto the road | Tempting — 56% of rows carry `EXIT <n>`. But an exit number is not a coordinate; resolving it needs an exit database I don't have. See §9. |
| A paid geocoder | Would work, costs money, and hides the interesting problem. |

---

## 3. Routing: OSRM, one call

**Decision: OSRM demo server, with FOSSGIS as a keyless fallback.**

```
GET /route/v1/driving/{lon1},{lat1};{lon2},{lat2}
      ?overview=full&geometries=geojson&annotations=distance&steps=false
```

The decisive parameter is **`annotations=distance`**. Verified live on
Dallas → Chicago:

| | |
|---|---|
| Geometry | 9,161 coordinates |
| `annotation.distance` | **9,160** values = coords − 1 |
| Sum of annotations | 1,555,627.1 m — **exactly** `route.distance` |

One call returns the geometry *and* the length of every segment of it, so the
`(lat, lon, cumulative_miles)` polyline the optimiser needs is a prefix sum. No
second call for distances, and — importantly — **no re-measuring the geometry
ourselves with haversine**, which chords every curve and under-reports. A missing
annotation array is treated as a hard error rather than silently falling back to
our own arithmetic, because a short route means a confidently undercharged total
(`tests/test_routing.py::test_missing_distance_annotations_is_a_hard_error`).

### Provider comparison

| Candidate | Verdict |
|---|---|
| **OSRM demo** (`router.project-osrm.org`) | No key, full geometry + per-segment distances in one call, BSD engine, self-hostable later. **Chosen.** |
| **FOSSGIS** (`routing.openstreetmap.de/routed-car`) | Tested: *identical* 966.6 mi / 9,161 pts / 9,160 annotations. Same response shape, so failover costs one config line and no second parser. **Chosen as fallback.** |
| OpenRouteService | Needs an API key — conflicts with "no secrets in the repo" for a clean clone. |
| GraphHopper / Geoapify | Key required, tighter free tiers. |
| Valhalla (FOSSGIS) | Viable, but a different response shape means a second adapter for no gain. |

### Usage policy, quoted not paraphrased

The OSRM wiki's *Demo server* page states, in full:

> "FOSSGIS kindly sponsors an OSRM demo server running worldwide car, foot and
> bike profiles. The server is available at router.project-osrm.org and
> routing.openstreetmap.de."

**It publishes no rate limit or quota.** I will not invent one. Because no quota
is published, the correct posture is to minimise calls regardless: exactly one per
uncached request, a route cache keyed on rounded coordinates, a descriptive
`User-Agent`, and a benchmark that records responses to disk so repeat runs cost
nothing. This is a demo-grade dependency. The production answer is self-hosted
OSRM (BSD licence, official Docker image), and I would rather say that than imply
a free demo server is production infrastructure.

### Why the endpoint geocoding is also free

A user typing `Dallas, TX` could cost a geocoding call. It doesn't: endpoints
resolve against the same committed offline index (53,675 places, 672 KiB). The
only way to spend a geocoding call is to name somewhere that index has never heard
of. A bare city name is resolved only when unambiguous — `Springfield` reports the
25 states it appears in rather than silently picking one.

---

## 4. Spatial index: a uniform grid, and how it got fast

The naive cost is **6,626 stops × 33,763 route points ≈ 224 million** distance
computations for one transcontinental route.

**Decision: a uniform grid hash, not a k-d tree.** The query here is "everything
within R miles of a polyline" — a bulk range query, not nearest-neighbour. A grid
answers it with integer arithmetic and no tree traversal, in ~30 lines rather than
~150. A k-d tree wins for high-dimensional or strongly clustered data; neither
applies to 6,626 points spread along interstates.

### Three versions, measured

**v1 — index the stops, walk the route.** For each route cell, collect nearby
stops, then measure each candidate against the sampled polyline. Measured
**1,110 ms** on LA → New York. Still `candidates × samples`: 686 × 2,794 ≈ 1.9M
haversines. Five times over the target.

**v2 — invert the index.** Hash the *route samples* instead, then probe once per
stop and test only the samples in that stop's grid neighbourhood. **46 ms.**

**v3 — prune the candidate set too.** Only consider stops in cells the route
actually passes through (expanded by the detour radius), so a Texas-to-Illinois
route never looks at a stop in Maine. **14.4 ms.**

Against a true no-index scan, measured by `make bench`:

| Route | Indexed | Naive `O(S×P)` | Speedup | Stops found |
|---|---|---|---|---|
| Dallas→Chicago | 5.51 ms | 1221.24 ms | **221.5×** | 189 / 189 |
| LA→New York | 15.35 ms | 3748.01 ms | **244.2×** | 460 / 460 |
| Seattle→Miami | 12.35 ms | 4431.04 ms | **358.8×** | 389 / 389 |

Identical stop counts in every pair — a pure speedup, not a different answer.
Complexity goes from `O(S × P)` to `O(S + P)` with a small constant.

> Keep the baselines straight: 221–359× is against a *true* no-index scan. The
> jump from v1 (which already had a grid) to v3 is about 77×. Quoting the bigger
> number against the smaller comparison would be dishonest.

### Precision

Taking the nearest *sample* would overstate the detour for stops sitting on the
road, by up to half the sampling interval. So once the nearest sample is found,
the distance is refined against the two adjacent full-resolution **segments**
using true point-to-segment geometry. That makes `detour_miles` and
`distance_along_route_miles` exact with respect to the polyline rather than
quantised. Tested with a stop at mile 150.3 on a 1-mile sampling grid
(`tests/test_corridor.py::test_a_stop_directly_on_the_route_has_near_zero_detour`).

Short-range work uses a local equirectangular projection; distances use haversine.
`MILES_PER_DEGREE_LAT` is **derived** from `EARTH_RADIUS_MILES` rather than
hardcoded, so the two cannot disagree. (They did, by 0.067%, until a test caught
it.)

---

## 5. The optimiser, and why it is optimal

**Problem.** A route of length `D`. Stations at `0 ≤ x₁ ≤ … ≤ xₙ ≤ D` with prices
`pᵢ`. Tank `C` gallons, `m` mpg, range `R = C·m`. Start at mile 0 with `s`
gallons. Minimise money spent on fuel.

**Algorithm.** At the station you are standing at, look ahead as far as the range
allows:

- If a **strictly cheaper** station is reachable, buy **only enough to reach the
  nearest such station**.
- Otherwise, **fill the tank** and drive to the **cheapest station in range**.

This is the classic minimum-cost refuelling greedy. It is provably optimal, not a
heuristic.

### Proof (exchange argument)

*Claim: some optimal solution never carries a gallon past a strictly cheaper
station.*

Suppose an optimal solution buys a gallon at station `i` for `pᵢ` and burns it
after passing station `j > i` where `pⱼ < pᵢ`. Construct a new solution that buys
that gallon at `j` instead.

- **Feasibility holds.** The gallon was burned after `j`, so the vehicle reached
  `j` in the original solution and still reaches it now — it carries one gallon
  *less* over the segment `i…j`, and tank capacity is an upper bound, so removing
  fuel cannot violate it.
- **There is room at `j`.** In the original solution the vehicle carried that same
  gallon through `j`, so the tank had space for it there.
- **Cost strictly falls** by `pᵢ − pⱼ > 0`.

That contradicts optimality. So no optimal solution carries fuel past a cheaper
station, and buying only enough to reach the nearest cheaper one is safe.
Symmetrically, when nothing cheaper is in range every reachable station costs at
least `pᵢ`, so deferring a purchase can only cost more and filling is safe. ∎

### Verification, beyond the proof

A proof in a comment is a claim. `tests/test_optimizer_property.py` checks the
greedy's cost **equals an exhaustive dynamic-programming optimum** over 400
generated instances, with distances on a whole-gallon lattice so the DP is an
exhaustive search of that lattice. The brief asked only for "never worse than
always filling at the nearest station" — that is tested too, but it is a much
weaker claim. Plus 27 hand-computed fixtures whose arithmetic is written out in
each docstring.

### The destination is a price-zero sentinel station

Arriving with fuel in the tank is money spent on fuel you did not burn, so the
destination genuinely *is* the cheapest place to buy the remainder. Modelling it
as a station at mile `D` with price 0 is not a trick — it is the correct model.

It also makes the final-leg case disappear. Because the sentinel is cheaper than
every real station, the rule "buy only enough to reach the nearest cheaper
station" **automatically caps the last purchase** at what is needed to finish. The
brief's phrasing of the greedy omits this; filling the tank at the last stop
instead would overstate the headline total by up to 50 gal × price ≈ **$170**.
This is the single easiest way to get the assignment wrong, and the sentinel makes
it structurally impossible rather than a case someone has to remember.

### Complexity

`O(n)` to precompute the next-strictly-cheaper station for every station with a
monotonic stack, plus a bounded forward scan per *visited* station when filling.
`n` is the corridor size — measured at 57–460 on real routes — so the optimiser
runs in **0.09–0.19 ms**. It is not the bottleneck and was never going to be; the
corridor search is.

### Infeasibility is a first-class outcome

A gap longer than the range means no plan exists. The API returns 422 with the
offending leg named at both ends, never a plausible wrong answer. Two distinct
reasons, because they call for different responses from the caller:

- `gap_exceeds_range` — stops exist but two consecutive ones are too far apart.
- `no_fuel_stops_on_route` — nothing in the corridor at all.

Conflating them produced the message *"10.0 mi exceeds the 500 mi range"* for a
10-mile route with no stops. A property test caught it.

---

## 6. Schema

Two tables, because the data has two entities.

```
TruckStop         identity + resolved location.  PK: OPIS Truckstop ID
PriceObservation  one observed price.            FK → TruckStop
```

**The split is measured, not stylistic.** 678 of 6,738 distinct OPIS IDs appear on
more than one row, and 597 of those differ *only* in price — they are repeated
readings of one physical stop. Flattening price onto `TruckStop` would discard 905
observations and hide that the quoted price is an aggregate.

**Dedup key: `OPIS Truckstop ID` alone.** Justified by counting:

| Key | Distinct groups |
|---|---|
| `OPIS Truckstop ID` | **6,738** |
| ID + Address | **6,738** |
| ID + City + State | **6,738** |
| ID + Address + City + State | **6,738** |
| Name + Address + City + State | 6,964 |

Adding Address, City or State changes nothing — the ID functionally determines
location. Name is deliberately *excluded*: 258 cosmetic name variants would split
one physical stop into several.

Three decisions I checked against the data before committing to them:

- **No `unique_together` on (stop, price).** 104 stops legitimately record the
  same price twice. A uniqueness constraint would have silently discarded real
  observations.
- **No date column on `PriceObservation`.** The source has none. Inventing one
  would let callers believe we know which price is current.
- **`opis_id` is text, not an integer.** Every value in this file is numeric, but
  it is an opaque external identifier we never do arithmetic on, and text survives
  leading zeros and an upstream format change.

**Indexes: `(latitude, longitude)` and the FK.** Deliberately *not* `country`
(98.3% one value) or `state` (48 values over 6,738 rows) — at that cardinality the
planner correctly prefers a sequential scan and the index would be write-time dead
weight. `EXPLAIN QUERY PLAN` confirms the composite index is used;
`for_optimizer()` loads 6,626 stops and 7,531 observations in **2 queries**, not
6,627.

**Floats for coordinates, Decimal for money.** Coordinates are measurements with
~1.6 mi of uncertainty of our own, so fixed-point would imply precision the data
lacks. Prices are `numeric(12,8)`, storing the source's 8 decimal places exactly.
The optimiser then works in float, because gallons are derived from a measured
distance and the product's error is dominated by the mpg and distance assumptions,
not by float representation. Rounding happens once, at the response boundary.

---

## 7. What the geocoding precision actually costs

Using Census `ALAND_SQMI` to derive an implied city radius as a proxy for centroid
error:

```
median 1.59 mi   p75 2.70 mi   p90 4.54 mi   p99 11.22 mi   max 15.42 mi
```

Two consequences, and they are very different:

**`detour_miles` is advisory, not navigable.** Accurate to roughly ±5 miles. Do
not hand it to a driver.

**Total cost is robust.** Cost = gallons × price. Gallons depends on total route
distance, which is exact from the routing engine and *not* a function of stop
positions. Stop position only perturbs *which* of several nearby stops is chosen,
and nearby stops have similar prices. The dollar total is far more trustworthy
than any individual detour figure.

**This is why `MAX_DETOUR_MILES` defaults to 10, not the 5 the brief suggested.**
A 5-mile corridor filters at a finer resolution than the coordinates actually
have — it would be sorting noise. 10 exceeds p90 error. All twelve benchmark
routes are feasible at 3, 5, 10 and 25 miles, so this is a defensibility choice,
not a feasibility one. It is a request parameter; try both.

---

## 8. Caching and process startup

- **Stop index**: built once per process from the **database** (not the artefact —
  the DB is the source of truth after seeding, and reading the file here would
  make the models decorative). 6,626 stops into a 2,885-cell grid in ~130 ms.
- **Place index**: built once from the committed artefact, ~250 ms.
- Both warm at **WSGI load**, not in `AppConfig.ready()` — Django rightly warns
  against querying the database during app initialisation. So no request pays the
  build cost.
- **Route cache**: keyed on origin and destination rounded to 3 decimals (~110 m,
  finer than OSRM's own snapping to the road network). LocMem, because the cached
  value is derived data that is cheap to recompute on a cold process. Redis is a
  one-line change in `settings.CACHES`.

> An earlier version reported a 132 ms `corridor_search` that was really a 127 ms
> lazy index build happening inside the timed block. Timers must measure the work
> they name; the index lookups now happen before the first timer starts.

---

## 9. What I would do differently with a week and a budget

Ordered by how much they would actually improve the answer.

**1. Fix the geocoding properly — the single biggest win.** Everything in §7 is
downstream of city-centroid placement. Two routes:

- *Cheap:* parse `I-<n>, EXIT <m>` (56.2% of rows carry it) and resolve against an
  interchange database — OSM has `highway=motorway_junction` nodes with `ref`
  tags. That would place more than half the stops within a few hundred metres
  instead of a few miles, and `detour_miles` would become navigable.
- *Direct:* a commercial geocoder or the OPIS feed with coordinates. A few hundred
  dollars and the problem disappears.

**2. Self-host OSRM.** The demo server has no SLA and no published quota. The
official Docker image plus a North America extract removes the only
production-blocking dependency, cuts the 400–1400 ms external latency to tens of
milliseconds on a LAN, and makes the whole thing offline-capable.

**3. Real detour cost, not straight-line distance.** Today a "3-mile detour" is
3 miles as the crow flies from the polyline. The honest version routes to the stop
and back, which costs extra API calls — or, with self-hosted OSRM, costs nothing.
It also lets the optimiser trade detour *time* against price, which is what a
dispatcher actually cares about.

**4. Model the detour in the optimisation.** Right now the corridor filter is a
hard cutoff and detour distance does not enter the cost function. A stop 9 miles
off route saving $0.02/gal is probably a bad trade; the current model takes it.
Adding `2 × detour_miles / mpg × price` to each stop's effective cost is a small
change with a real effect on answer quality.

**5. Dated prices and a freshness policy.** With a dated feed, "current price"
becomes meaningful, and the API could return a confidence or an as-of timestamp
instead of an aggregation basis.

**6. Scale the stop index out of memory.** 6,626 stops fit comfortably in RAM. At
millions — every fuelling point in North America — I would use PostGIS with a
GiST index and `ST_DWithin` against the route geometry, which is the same
algorithm with the grid maintained by the database. The in-memory grid is right
for *this* size and I would not pretend otherwise in either direction.

**7. Operational hardening.** Per-IP rate limiting, a circuit breaker on the
routing provider, structured JSON logs with a request id, and an OpenAPI schema
(`drf-spectacular`) so the contract is machine-readable rather than described in a
README.

**8. Verify the PostgreSQL path.** It is configured and the DDL is verified to
generate, but `migrate` and `seed_stops` have never run against a live server
because Docker is not installed on this machine. That is an hour's work and I did
not want to claim it untested.

---

## 10. Things that went wrong

Recorded because the interesting part of a build is usually the corrections.

| What | How it was caught |
|---|---|
| Corridor search 5× too slow (1,110 ms) | Measured it in Phase 0 instead of assuming |
| Nominatim placed Crescent PA 270 mi out | Cross-checking against an offline source |
| Infeasibility message was nonsense for the no-stops case | A Hypothesis property test |
| LA→NY under-reported cost by 10.6% | Reconciling gallons purchased against gallons consumed |
| `--offline` benchmark flag silently used the network | Writing a test that asserted zero calls |
| DRF 3.16.1 is incompatible with Django 6.1 | `manage.py check` failing on import |
| Leaflet SRI hash was fabricated — would have broken the map page | Recomputing it from the CDN bytes |
| Two inconsistent miles-per-degree constants | Writing exact-value corridor tests |
| `.coverage` and `.hypothesis` committed by `git add -A` | Auditing the clean clone |
