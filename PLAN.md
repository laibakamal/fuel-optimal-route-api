# PLAN.md — Fuel-Optimal Route API

Phase 0 deliverable. Everything numeric below was measured by a script in this
session, not estimated. Re-run commands are given so each number can be checked.

---

## 1. What the data actually is

`fuel-prices-for-be-assessment.csv` — 8,151 data rows, 7 columns, no nulls anywhere.

```
OPIS Truckstop ID, Truckstop Name, Address, City, State, Rack ID, Retail Price
```

### 1.1 Row counts and identity

| Fact | Value |
|---|---|
| Data rows | 8,151 |
| Distinct `OPIS Truckstop ID` | 6,738 |
| IDs appearing more than once | 678 (2,091 rows involved) |
| Max repeats for one ID | 6 |

**The duplicate behaviour is not what it looks like.** I tested six candidate
dedup keys:

| Key | Distinct groups |
|---|---|
| `OPIS Truckstop ID` alone | **6,738** |
| ID + Address | **6,738** |
| ID + City + State | **6,738** |
| ID + Address + City + State | **6,738** |
| Name + Address + City + State | 6,964 |
| Address + City + State | 6,337 |

Adding Address, City or State to the ID changes nothing. The ID *functionally
determines* location. So `OPIS Truckstop ID` is a clean primary key, and the
repeated rows are repeated **price observations of one physical stop**, e.g.:

```
ID 105  TA SAGINAW I 75 TRAVEL CENTER  I-75, EXIT 144-B  Bridgeport MI
        prices: 3.269, 3.339, 3.429, 3.289, 3.399, 3.299
```

Of the 678 repeated IDs: 597 differ in `Retail Price`, 226 differ in
`Truckstop Name` (cosmetic variants only). **There is no date column**, so the
observations are undated and cannot be ordered. Within-stop spread: median
$0.060, p90 $0.240, max $0.900.

> This is the finding that justifies the Phase 2 schema split: **TruckStop**
> (identity + location, keyed on OPIS ID) and **PriceObservation** (many per
> stop). It is a real normalisation driven by the data, not schema theatre.

### 1.2 The CSV is not US-only

57 distinct state codes = 48 US + **9 Canadian provinces**.

| | Rows | Median price |
|---|---|---|
| US (48 codes) | 7,531 | 3.399 |
| Canada (ON, AB, BC, MB, SK, YT, QC, NS, NB) | **620** | **4.454** |

Canadian prices sit ~31% higher, which is consistent with a different currency
and/or unit. **The CSV does not say which.** The assignment says both endpoints
are "within the USA", so Canadian stops are out of scope for the optimiser — and
excluding them also sidesteps a currency ambiguity I cannot resolve from the file.

US states absent from the data: **AK, HI, DC**.

### 1.3 Coverage is very uneven — this is a feasibility risk

Distinct stops per state, sparsest first:

```
RI 2    CA 8    DE 15   VT 16   NH 23   CT 25   OR 29
ME 30   WV 38   MT 44   MA 44   WA 52   ND 59   ID 61
```

versus TX 790 rows, IL 774. **California has 8 truck stops.** I treated this as a
blocking risk and tested it directly in §4.3 rather than assuming.

### 1.4 Price distribution (US rows)

```
min 2.687   p25 3.199   median 3.399   p75 3.616   max 6.399
mean 3.432  stdev 0.418
```

No zero, negative or absurd values. Cheapest states TX 3.075, OK 3.132, SD 3.159.
Dearest CA 4.794, WA 3.989, CT 3.909. A real west-to-south price gradient exists,
so cost-optimal refuelling is a genuine optimisation here, not a rounding error.

### 1.5 Addresses are highway descriptors, and there are no coordinates

This is **the central engineering problem**. No latitude or longitude column, and
the addresses are not geocodable street addresses:

| Pattern | Rows | Share |
|---|---|---|
| Contains a highway/exit token | 7,859 | 96.4% |
| Contains `EXIT <n>` | 4,582 | 56.2% |
| Contains `I-<n>` | 4,456 | 54.7% |
| Looks like a street address | **5** | **0.1%** |

```
'I-44, EXIT 283 & US-69'
'I-94, EXIT 143 & US-12 & SR-21'
'I-81, EXIT 273 & SR-703/SR-292'
'US-46'
```

Street-address geocoding is therefore useless on 99.9% of rows. `City` + `State`
is the only reliably geocodable signal. `City` also carries trailing whitespace
padding (`'Effingham                '`) and needs normalisation.

---

## 2. Geocoding strategy — measured, not guessed

**Decision: a two-tier offline join on (City, State). Zero geocoding API calls.**

| Tier | Source | Licence |
|---|---|---|
| 1 | US Census Bureau 2025 Gazetteer **Places** (`2025_Gaz_place_national.txt`) | US Government work — public domain |
| 2 | GeoNames US populated places (`US.txt`, feature class `P`, excluding `PPLQ` historical) | CC-BY 4.0 (attribution required) |

Name normalisation applied to both sides: Unicode fold, uppercase, strip
punctuation, `SAINT|STE→ST`, `FORT→FT`, `MOUNT→MT`, remove one trailing LSAD
token (`CITY|TOWN|CDP|VILLAGE|BOROUGH|TOWNSHIP|…`), and index both spaced and
de-spaced forms so `MC CALLA` matches `MCCALLA`.

### Measured match rate against 6,626 distinct US stops

| Tier | Stops matched | Share |
|---|---|---|
| 1 — Census Places | 6,280 | 94.78% |
| 2 — GeoNames | 328 | 4.95% |
| **Combined** | **6,608** | **99.73%** |
| Unresolved | **18** | 0.27% |

All 6,608 resolved coordinates fall inside the CONUS bounding box — zero outliers.

The 18 stragglers are individually identifiable (`Sault Sainte Marie` MI,
`S Coffeyville` OK, `Hot Springs National Park` AR, `Willington`/`East Lyme` CT,
`Derby` VT, `Baileyville`/`Corinth` ME, `Bronx` NY, `Willow Beach` AZ,
`Pueblo Of Acoma` NM, `Dundee` IL, `Crescent` PA). 18 calls to a rate-limited
geocoder with an on-disk cache clears them; whatever still fails gets excluded
with the count printed and recorded in the README. Nothing is dropped silently.

> **The headline: the pipeline needs zero geocoding API calls.** An
> 8,100-row geocoding job becomes a hash join against two public-domain files.

### Positional error — and the problem it creates

City centroids are not truck stops. Using Census `ALAND_SQMI` to derive an
implied city radius as a proxy for centroid error:

```
median 1.59 mi   p75 2.70 mi   p90 4.54 mi   p99 11.22 mi   max 15.42 mi
```

Plus 56 stops whose (City, State) matches multiple distinct places more than
10 miles apart (tie-broken on largest land area / population).

**How this affects answer quality, plainly:** each stop's true position is
typically 1.5–5 miles from where we place it. Two consequences:

1. Reported **detour distance is advisory, not navigable.** It is accurate to
   roughly ±5 miles.
2. Reported **total cost is robust.** Cost depends on gallons × price. Gallons
   depends on total route distance (exact, from the routing API) — *not* on stop
   positions. Stop position only perturbs *which* station is chosen when two are
   close together, and nearby stations have similar prices. The dollar total is
   therefore far more trustworthy than any individual detour figure.

**⚠ This creates a real tension with `MAX_DETOUR_MILES = 5`** — see §6, Q3. A
5-mile corridor is the same order of magnitude as the geocoding error, so at that
setting the filter is partly sorting noise.

### What I rejected

- **8,100 live geocoding calls.** Slow, rate-limited, abusive of free services,
  non-reproducible. The whole point is to avoid this.
- **Parsing interstate + exit number to snap stops onto the road.** Tempting, and
  56% of rows carry `EXIT <n>`. But exit numbers are not a coordinate — resolving
  them needs an exit database I do not have, or many more API calls. The
  interstate token is still worth storing as a sanity-check and a future hook.
  Noted in ARCHITECTURE.md as "what I'd do with a week and a budget".

---

## 3. Routing API — chosen and tested

**Primary: OSRM demo server, `router.project-osrm.org`.**
**Fallback: FOSSGIS, `routing.openstreetmap.de/routed-car`.**

Single request:

```
GET /route/v1/driving/{lon1},{lat1};{lon2},{lat2}
      ?overview=full&geometries=geojson&annotations=distance&steps=false
```

Verified live (Dallas → Chicago):

| | |
|---|---|
| HTTP | 200 in **1.37 s** |
| Distance | 1,555,627.1 m = **966.6 mi** |
| Geometry | GeoJSON LineString, **9,161** coordinates |
| `annotation.distance` | **9,160** values = coords − 1 |
| Sum of annotations | 1,555,627.1 m — **exactly** the route distance |

That last row is the decisive property. `annotations=distance` gives the length
of every segment, so one call yields the full
`(lat, lon, cumulative_miles)` polyline the optimiser needs, with cumulative
distance derived by prefix-sum rather than re-measured by me. **One call. No
second call for distances.**

Why OSRM over the alternatives:

| Candidate | Verdict |
|---|---|
| **OSRM demo** | No key, full geometry + per-segment distances in one call, sub-2s, BSD engine, self-hostable later. **Chosen.** |
| **FOSSGIS `routed-car`** | Tested: returned *identical* 966.6 mi / 9,161 pts / 9,160 annotations in 1.03 s. Same API shape ⇒ fallback costs one config line. **Chosen as fallback.** |
| OpenRouteService | Needs an API key; conflicts with "no secrets in the repo" for a reviewer running a clean clone. Third option only. |
| GraphHopper / Geoapify | Key required, free tiers tighter. |
| Valhalla (FOSSGIS) | Viable, but response shape differs ⇒ a second adapter for no gain. |

### Usage policy — quoted, not paraphrased

The OSRM wiki's *Demo server* page states in full:

> "FOSSGIS kindly sponsors an OSRM demo server running worldwide car, foot and
> bike profiles. The server is available at router.project-osrm.org and
> routing.openstreetmap.de."

**It publishes no rate limit or quota.** I will not invent one. The honest
position for ARCHITECTURE.md: because no quota is published, the correct posture
is to minimise calls regardless — we make exactly one per uncached request, cache
aggressively, and set a descriptive `User-Agent`. This is a demo-grade
dependency; the production answer is self-hosted OSRM (BSD licence, official
Docker image), which I will say explicitly rather than implying the demo server
is production infrastructure.

### Geocoding the user's own origin/destination

`lat,lon` input ⇒ **0 external calls**. Free-text ⇒ resolved against **the same
offline Census/GeoNames index** we already load for the truck stops, so
`"Dallas, TX"` also costs **0 calls**. Nominatim is the last resort for input the
offline index cannot resolve, rate-limited and disk-cached.

**Call accounting per uncached request: 1 (route) + 0 (geocoding, normal path).**
The response will carry a live counter so the reviewer verifies this rather than
believing it.

---

## 4. Architecture and data flow

```
                        OFFLINE, ONCE (committed to the repo)
  fuel CSV ─┐
  Census    ├─► build_fuel_index ──► stops.json.gz  ──► seed_stops ──► DB
  GeoNames ─┘   (normalise, dedup,     (committed:        (idempotent)
                 join, validate)        id, name, addr,
                                        city, state,
                                        lat, lon, price)

                        PER REQUEST
  POST /api/v1/route
      │
      ├─ serializer validates, rejects non-US coords
      ├─ resolve endpoints  (offline index → 0 calls)
      ├─ route cache hit?  ──yes──► cached polyline
      │        └──no──► OSRM: ONE call ──► polyline + cumulative miles
      ├─ corridor search   (grid hash over route samples — see §4.2)
      ├─ project stops to 1-D axis, order by distance-along-route
      ├─ min-cost refuelling greedy (50 gal tank, 10 mpg)
      └─ response: geometry, stops, totals, assumptions, counters, timings
```

### 4.1 Layering

`services/` is **pure Python, zero Django imports** — `route.py`, `corridor.py`,
`optimizer.py`, `geo.py`. The optimiser is a function over a list of
`(mile, price)` tuples. That makes Phase 6's hand-computed fixtures trivial and
keeps the graded algorithm testable without a database.

### 4.2 Corridor search — I built it wrong first, measured it, and fixed it

Naive approach (index the *stops*, scan the polyline per candidate):

| Route | Points | Stops found | Time |
|---|---|---|---|
| Dallas→Chicago | 9,161 | 157 | 283 ms |
| LA→NY | 33,763 | 351 | **1,110 ms** |
| Seattle→Miami | 35,624 | 344 | 954 ms |

That misses the 200 ms target by 5×. The cost is `candidates × route_samples` —
686 × 2,794 ≈ 1.9M haversines for LA→NY.

**Fix: invert the index.** Decimate the polyline to ~1 point per mile, hash
*those* samples into a 0.25° grid, then probe once per stop and test only samples
in neighbouring cells. Measured, same machine, median of 5 runs:

| Route | Naive | Inverted | Stops | Max gap |
|---|---|---|---|---|
| Dallas→Chicago | 283 ms | **30 ms** | 157 | 62.7 mi |
| LA→NY | 1,110 ms | **46 ms** | 351 | 238.3 mi |
| Seattle→Miami | 954 ms | **42 ms** | 344 | 216.6 mi |

**24× faster, byte-identical output** (same stop counts, same gaps). Complexity
goes from `O(S × P)` to `O(S + P)` with a small constant — each stop touches only
the O(1) samples in its grid neighbourhood. Versus the truly naive
`8,151 × 33,763 ≈ 275M` distance computations, this is ~4 orders of magnitude
less work.

Reproduce: `python manage.py benchmark` (Phase 5).

### 4.3 Feasibility — the California question, answered

I was worried 8 stops in California made LA→NY infeasible. Measured instead of
assumed, at several detour tolerances (tank range 500 mi):

| Route | Total | Detour ≤3mi | ≤5mi | ≤10mi | ≤25mi | Max gap | Verdict |
|---|---|---|---|---|---|---|---|
| Dallas→Chicago | 966.6 mi | 130 | 157 | 188 | 320 | 62.7 mi | **FEASIBLE** |
| LA→NY | 2,793.7 mi | 274 | 351 | 460 | 686 | 238.3 mi | **FEASIBLE** |
| Seattle→Miami | 3,302.5 mi | 298 | 344 | 389 | 523 | 216.6 mi | **FEASIBLE** |

All three are feasible **even at a 3-mile corridor** — worst gap 238 mi against a
500 mi range, comfortable margin. The CA sparsity turned out not to bite because
I-40 east out of LA runs through AZ/NM where coverage is dense. Good news, and
now it is a measurement rather than a hope.

Infeasible routes must still be handled correctly (short coastal/New England
hops will trip it), so Phase 3 implements and tests the structured-error path
regardless.

---

## 5. The refuelling algorithm

Inputs: stations as `(mile_along_route, price_per_gallon)` sorted ascending;
`TANK = 50 gal`; `MPG = 10`; `RANGE = 500 mi`; `start_gallons` (setting).

**Classic min-cost refuelling greedy, provably optimal:**

At the current station, look ahead within remaining range.
- If a **strictly cheaper** station is reachable → buy *only* enough to reach the
  **nearest** such cheaper station.
- Otherwise → **fill the tank**, and drive to the **cheapest** station in range.

**Exchange argument** (to be written as a comment at the implementation):
Suppose an optimal solution buys a gallon at price `p` at station `i` that is
consumed after passing a station `j > i` with `p_j < p`. Move that gallon's
purchase from `i` to `j`: the tank-capacity constraint is not violated (the
gallon is consumed later than `j`, so the tank had room at `j`), the vehicle
still completes the route, and the cost strictly falls by `p − p_j > 0`,
contradicting optimality. Hence in some optimal solution no gallon is ever
carried past a strictly cheaper station — which is exactly the greedy's rule.
Symmetrically, when nothing cheaper is in range, deferring any purchase can only
raise cost, so filling is safe.

**⚠ One thing the brief's phrasing omits:** on the **final leg**, the purchase
must be capped at the fuel needed to *reach the destination*, not a full tank.
Filling at the last station overstates "total money spent on fuel" by up to
50 gal × price ≈ $170. Since the deliverable is literally the total spent, I will
cap the last purchase and test it. Flagging it because it is a silent
off-by-$170 if missed.

**Edge cases, each with a test:**

| Case | Behaviour |
|---|---|
| Gap > 500 mi between consecutive usable stops | Structured `RouteInfeasible` error **naming the gap** (both station names, both mileposts, gap length). Never a wrong answer. |
| Gap > 500 mi from origin to first station | Same error, flagged as the origin leg. |
| Trip shorter than one tank | Zero or one refuel; correct, not an error. |
| Start == finish | Zero distance, zero cost, empty stop list, `200 OK`. |
| No stops in corridor | Infeasible unless the trip is within starting range; explicit distinct error. |

Complexity: `O(n)` amortised with a monotonic deque over the look-ahead window;
`n` ≤ ~700 in the worst measured case, so this is microseconds.

---

## 6. Decisions (resolved 2026-10-01)

| # | Question | **Decision** |
|---|---|---|
| Q1 | Price basis for 678 stops with undated repeat observations | **Median**, surfaced as `price_basis: "median_of_undated_observations"` |
| Q2 | 620 Canadian rows | **Load all rows with a `country` field; exclude non-US from the optimiser; report excluded count in README** |
| Q3 | `MAX_DETOUR_MILES` default | **10** (exceeds p90 geocode error of 4.54 mi), exposed as a request parameter |
| Q4 | `START_TANK_GALLONS` | **0**, with the semantics stated explicitly in `assumptions`: tank empty at origin, first station reachable by assumption |
| Env | Python / database | **`brew install python@3.13`; SQLite is the verified default path; `docker-compose.yml` provided for Postgres but marked unverified on this machine** |

### Original framing of those questions

These change the output materially, so per your rules of engagement I am not
assuming.

**Q1 — Price basis for stops with multiple undated observations (678 stops).**
No date column exists, so "latest price" is not available. Options: `median`
(robust to the one $0.90 outlier spread), `mean`, or `min` (optimistic).
→ **My recommendation: `median`**, stated explicitly in the `assumptions` object
as `"price_basis": "median_of_undated_observations"`. Configurable.

**Q2 — Canadian stops (620 rows).**
Spec says both endpoints are in the USA, and CAD/unit ambiguity is unresolvable
from the file.
→ **My recommendation: load all rows with a `country` field, exclude non-US from
the optimiser, and report the excluded count in the README.** Nothing dropped
silently, spec honoured, currency ambiguity avoided.

**Q3 — `MAX_DETOUR_MILES` default.** This is the one I most want you to decide.
You specified 5. But measured city-centroid error is median 1.59 / p90 4.54 mi,
so a 5-mile corridor filters on a quantity of the same magnitude as its own
error. §4.3 shows all three long-hauls stay feasible at 3, 5, 10 and 25 mi, so
this is a defensibility choice, not a feasibility one:
- **Keep 5** — matches your brief, tighter detours, but a reviewer who spots the
  error analysis may ask why we filter at a resolution we don't have.
- **Raise to 10** — exceeds p90 geocoding error, so the filter dominates its own
  noise; yields 188–460 stops (more optimisation headroom, better prices).
→ **My recommendation: default 10, documented with exactly this reasoning, and
expose it as a request parameter so the Loom can demo 5 and 10 side by side.**
Turning a weakness into a visible, deliberate decision is the stronger interview
position.

**Q4 — `START_TANK_GALLONS = 0`.** Agreed, and it makes the reported total cover
all fuel the trip consumes, which is what the brief asks for. The wrinkle: a
literally empty tank cannot move, so the semantics must be "tank empty at origin;
the first station is reachable by assumption." I will state that in
`assumptions` rather than leave it implicit. Confirm that reading is what you
want.

---

## 7. Risks, and things I flag as problems with the brief

| # | Risk / concern | Mitigation |
|---|---|---|
| R1 | **Python 3.12+ is not installed here.** System Python is **3.9.6**; no pyenv, no uv, no Homebrew Python. Django 6.1.1 requires `>=3.12` (verified from PyPI metadata). | `brew install python@3.13` as step 0. Blocking — flagged below. |
| R2 | **Docker is not installed** (`docker`, `docker-compose` absent) and there is no local PostgreSQL. I can author `docker-compose.yml` but **cannot verify it runs.** | SQLite becomes the *verified* default path; Postgres via one env var, compose file provided and clearly marked as unverified-on-this-machine. I will not claim it works untested. |
| R3 | Full GeoJSON is **heavy**: measured 203.8 KiB (Dallas→Chicago), **830.8 KiB** (LA→NY), 876.3 KiB (Seattle→Miami). Directly fights "the quicker the better". | Default to coordinates rounded to 5 dp and decimated (step 10 ⇒ 76.5 KiB for LA→NY, a **10.9×** reduction, visually identical at map zoom). `?geometry=full` opts into the raw polyline. Both numbers in the README. |
| R4 | OSRM demo server has **no published SLA or quota** and could rate-limit or go down mid-demo. | Verified keyless fallback with an identical response shape; route cache; **pre-warm the cache for the Loom routes before recording.** |
| R5 | City-centroid geocoding is the core accuracy limitation. | Quantified in §2, stated in the response `assumptions`, and given its own README "known limitations" section. Not buried. |
| R6 | Canadian rows and the CAD ambiguity. | Q2. |
| R7 | Undated duplicate prices mean "current price" is not knowable. | Q1; stated in `assumptions` as a basis, never claimed to be live. |
| R8 | The brief's greedy phrasing omits capping the final purchase — a silent ~$170 overstatement of the headline number. | §5; capped and unit-tested. |

### Things in the brief I think are worth arguing about

1. **`MAX_DETOUR_MILES = 5` is finer than the data's resolution.** Q3. The brief
   asks for 5 while also asking for honest error analysis; those two pull against
   each other and I would rather settle it now than in the interview.
2. **"LA to NY" as a benchmark route is fine** — I checked, it is feasible. But
   it is also the 830 KiB response. Keep it in the benchmark set, demo
   Dallas→Chicago in the Loom.
3. **The 3-day limit vs. the scope.** Phases 0–7 including a benchmark harness, a
   Leaflet page, a Postman collection and four documents is a lot. If time runs
   short my cut order is: Postman collection (curl commands suffice) → Docker
   Postgres (SQLite verified instead) → benchmark breadth (10 routes → 5). I will
   not cut tests or the optimality proof, since those are what is being graded.
4. **"One call to the routing API" is satisfied, but note the honest caveat** that
   a route *cache hit* makes zero calls — so the counter in the response reports
   actual calls made, and I will demo a cache miss first so the reviewer sees the 1.

---

## 8. Phase 1 entry criteria

On your approval of §6 Q1–Q4, Phase 1 will:

1. `brew install python@3.13`, create the venv, pin Django 6.1.1, confirm
   `django-admin --version` → `6.1.1`.
2. `build_fuel_index` management command: CSV → normalise → dedup on OPIS ID →
   two-tier offline join → CONUS/Canada bbox validation → `stops.json.gz`,
   printing match rate and every excluded row with its reason.
3. `seed_stops` command loading the committed artefact so the reviewer never
   re-runs the join.
4. Report: match rate, excluded count, rows loaded.

Checkpoint target, from this session's measurements: **99.73% resolved, 18
unresolved before fallback geocoding, 6,608 US stops loaded.**
