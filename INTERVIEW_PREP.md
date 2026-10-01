# Interview prep

Fifteen questions a sharp reviewer would ask, with honest answers — including the
ones that expose weaknesses. The soft spots are marked **⚠**. Know where they are
before you walk in.

---

### 1. Why city centroids? Isn't that too imprecise to be useful? ⚠

**It is imprecise, and the honest answer is that the data forces it.** There are
no coordinates in the file and 99.9% of the addresses are highway-exit
descriptors, not street addresses. City + State is the only reliably geocodable
signal present.

I measured the cost rather than hand-waving it. Using Census land area as a proxy
for city radius: median 1.59 mi, p90 4.54 mi, p99 11.22 mi.

The important part is that this error lands unevenly:

- **`detour_miles` is advisory, not navigable** — accurate to roughly ±5 miles.
- **Total cost is robust.** Cost = gallons × price. Gallons depends on route
  distance, which is exact from the routing engine and completely independent of
  where I think the stops are. Stop position only perturbs *which* of several
  nearby stops gets chosen, and nearby stops have similar prices.

So the dollar figure — which is what the brief actually asks for — is far more
trustworthy than any individual detour number. I say this in the API response, in
the README, and in the Loom.

**The fix, with time:** 56.2% of rows carry `EXIT <n>` and 54.7% carry `I-<n>`.
Resolving those against OSM `highway=motorway_junction` nodes would place more
than half the stops within a few hundred metres.

---

### 2. Your detour default is 10 miles. The brief said 5. Why did you change it? ⚠

Because 5 would be filtering at a finer resolution than the coordinates have.
Measured geocoding error is median 1.59 mi and **p90 4.54 mi** — the same order as
a 5-mile threshold. At 5 miles the filter is partly sorting noise: it excludes
genuine on-route stops and includes off-route ones, roughly at random.

10 miles exceeds p90 error, so the filter dominates its own uncertainty.

I checked this was a judgment call and not a feasibility one: all twelve benchmark
routes are feasible at 3, 5, 10 and 25 miles. And it's a request parameter, so
you can set it to 5 and compare.

**If you push back:** this is a defensible disagreement, not a correctness issue.
Changing one default is a one-line change. The reasoning is what I'd want
credited.

---

### 3. Prove the optimiser is actually optimal.

The exchange argument, written out in `services/optimizer.py`:

Suppose an optimal solution buys a gallon at station `i` for `pᵢ` and burns it
after passing station `j > i` where `pⱼ < pᵢ`. Move that purchase to `j`.
Feasibility holds — the gallon was burned *after* `j`, so the vehicle reached `j`
regardless, and it now carries one gallon *less* over `i…j`, which can't violate a
capacity upper bound. There's room at `j`, because the original solution was
carrying that same gallon through `j`. And cost strictly falls by `pᵢ − pⱼ > 0`.
Contradiction. So no optimal solution carries fuel past a cheaper station — which
is exactly the greedy's rule.

**And because a proof in a comment is just a claim:** a property test checks the
greedy's cost *equals* an exhaustive DP optimum over 400 generated instances. The
brief asked only for "never worse than always filling at the nearest station" —
that's tested too, but it's much weaker.

---

### 4. What happens on the last leg? Don't you overbuy?

No, and this is the part I'd most want you to look at. The destination is modelled
as a **station at mile `D` with price 0**.

That's not a trick to make the code shorter — it's the correct model. Arriving
with fuel in the tank is money spent on fuel you didn't burn, so the destination
genuinely is the cheapest place to "buy" the remainder.

It also makes the final-leg case disappear structurally. Because the sentinel is
cheaper than everything, the rule "buy only enough to reach the nearest cheaper
station" automatically caps the last purchase. Without it you'd fill the tank at
the last stop and overstate the headline number by up to 50 gal × price ≈ **$170**
— on a trip whose answer is a few hundred dollars.

The brief's own phrasing of the greedy omits this. It's the single easiest way to
get this exercise wrong.

---

### 5. Why two cost numbers? That looks like hedging. ⚠

It looks like hedging until you see the magnitude.

- `total_fuel_cost_usd` — what the plan provably spends at pumps.
- `estimated_total_fuel_cost_usd` — the whole trip, with the leg before the first
  stop valued at that first stop's price.

With an empty starting tank, miles driven before the first purchase are fuel we
didn't buy. Usually that's nothing — on Dallas → Chicago it's zero.

**On Los Angeles → New York it's 238.7 miles**, because all 8 California stops in
the dataset sit in the Imperial Valley while the route runs north-east on I-15.
That's 23.87 gallons — **$82.56, or 10.6% of the total**.

Reporting only the first number understates that trip by a tenth. Reporting only
the second blends a measured figure with an estimated one. So I return both plus
`unpriced_origin_miles`, and the caller can see exactly where the difference comes
from.

---

### 6. Why a uniform grid and not a k-d tree or PostGIS?

The query is "every stop within R miles of a polyline" — a **bulk range query**,
not nearest-neighbour. A grid answers that with integer arithmetic and no tree
traversal, in about 30 lines versus 150. A k-d tree wins on high-dimensional or
strongly clustered data; 6,626 points spread along interstates is neither.

PostGIS with a GiST index and `ST_DWithin` is the right answer at a different
scale — millions of stops that won't fit in memory. It's the same algorithm with
the index maintained by the database. At 6,626 stops the in-memory grid is
correct, and I'd rather state the crossover than pretend either is universally
better.

**Measured:** 221–359× faster than a no-index scan, with identical stop counts.

---

### 7. How do you know it's really one API call?

Don't take my word for it — **the response reports its own measured count** in
`diagnostics.external_calls`. The benchmark prints the total for a 12-route pass:
12 calls, then 0 on the warm passes.

The reason one call suffices is `annotations=distance`. OSRM returns the geometry
*and* every segment's length in one response, so the cumulative-mileage polyline
is a prefix sum. Verified: 9,161 coordinates, 9,160 annotations, summing to
exactly the reported route distance.

Free-text endpoints cost nothing extra because they resolve against a committed
offline place index. The only way to spend a geocoding call is to name somewhere
that index has never heard of.

---

### 8. The OSRM demo server has no SLA. Is this production-ready? ⚠

**No, and I wouldn't claim it is.** That's the one genuinely production-blocking
dependency.

The OSRM wiki publishes no rate limit or quota — I quoted the page verbatim in
`ARCHITECTURE.md` rather than inventing a number. External latency swung between
390 ms and 1.3 s between two runs of the same twelve routes.

What I did about it: exactly one call per uncached request, a route cache, a
descriptive User-Agent, and a verified keyless fallback (`routing.openstreetmap.de`)
that returns a byte-identical response shape, so failover is one config line and
no second parser.

**The production answer is self-hosted OSRM** — BSD licence, official Docker
image, a North America extract. That removes the dependency and cuts external
latency to tens of milliseconds. It's item 2 on the "with a week" list.

---

### 9. Your PostgreSQL setup — did you actually run it? ⚠

**No.** Docker isn't installed on the machine I built this on, so
`docker-compose.yml` is authored but never executed, and it says exactly that in a
comment at the top.

What *is* verified: the settings switch selects the `postgresql` backend,
`makemigrations --check` is clean under both backends, and I generated the real
PostgreSQL DDL offline via `collect_sql` — `numeric(12,8)`, `double precision`,
both CHECK constraints and the composite index all translate correctly.

What isn't: `migrate` and `seed_stops` against a running server.

SQLite is the path tested end to end, and it's the default so a reviewer gets a
working API with no Docker. I'd rather ship an honest caveat than an untested
claim. It's maybe an hour to close.

---

### 10. Why exclude the Canadian stops? Isn't that dropping data?

They're excluded from the *optimiser*, not dropped. All 112 are loaded with a
`country` field and reported separately in the pipeline output.

Two reasons. First, the brief routes between two US locations. Second — and this
is the one I'd lead with — **the Canadian prices have a median of 4.454 against
3.399 for the US, about 31% higher, and the file has no currency column.** I can't
tell whether that's CAD per gallon, CAD per litre, or genuinely higher USD
pricing. Mixing an unknown currency into a dollar total would be worse than
excluding it.

The pipeline reports "excluded: out of scope (non-US) 112" separately from
"excluded: UNRESOLVED (US) 0", because "deliberately out of scope" and "we failed"
are different facts and conflating them would hide a real failure.

---

### 11. Why is `OPIS Truckstop ID` the dedup key? How do you know it's safe?

I counted, rather than assuming. Distinct groups under six candidate keys:

| Key | Groups |
|---|---|
| `OPIS Truckstop ID` | **6,738** |
| ID + Address | **6,738** |
| ID + City + State | **6,738** |
| ID + Address + City + State | **6,738** |
| Name + Address + City + State | 6,964 |

Adding Address, City or State changes nothing — the ID functionally determines
location, so it's a sound primary key.

Name is deliberately *excluded*: 258 rows carry cosmetic name variants, and keying
on name would split one physical truck stop into several.

**Note the brief's framing was slightly off here.** It described the duplicates as
name variants. They're mostly not — 597 of the 678 repeated IDs differ in *price*.
They're repeated price observations of one stop, which is precisely what drove the
two-table schema.

---

### 12. There's no date on `PriceObservation`. Isn't that a modelling gap?

It's deliberate. **The source file has no date column**, so the observations
cannot be ordered or dated. Adding a timestamp would mean inventing data, and it
would let callers believe we know which price is current. We don't.

Instead, each stop's price is the **median** of its observations, and the API
states that explicitly in `assumptions.price_basis` as
`median_of_undated_observations`, with a note saying it is not a live price.

Median rather than mean because the within-stop spread reaches $0.90 and the
median isn't dragged by that. It's a setting — `mean` and `min` work too.

With a dated feed I'd add the column, index `(stop, observed_at desc)`, and return
an as-of timestamp instead of an aggregation basis.

---

### 13. 90.7% coverage — what's the missing 9%? ⚠

Mostly the `benchmark` command's reporting branches (65.8%) and `verify_stops`'
failure paths (67.0%). Both are diagnostic output — print formatting and exit
codes.

I'd rather say that plainly than write assertions on log strings to push the
number up. The modules that matter are all 90–100%: optimiser 94.8%, corridor
98.2%, routing 96.7%, places 96.2%, views 100%, planner 97.7%.

`.coveragerc` sets no `fail_under` and doesn't skip covered files, so the number
is whatever the tool prints.

**The thing I'd defend harder than the percentage:** no test touches the network,
and that's *enforced*, not intended — `conftest.py` patches the socket layer to
raise, and there are three meta-tests that prove it by trying.

---

### 14. What's the worst bug you found, and how?

Two candidates.

**The one with the biggest blast radius:** the Leaflet subresource-integrity hash
in the map template was **fabricated** — I'd written a plausible-looking hash
rather than computing one. SRI mismatch *blocks the script*, so the map page would
have silently failed to load Leaflet. On camera. I caught it by recomputing both
hashes from the actual CDN bytes and comparing.

**The one that's more interesting technically:** Nominatim placed `Crescent, PA`
270 miles from the real Crescent Township, near Chester instead of Allegheny
County. A state bounding-box check doesn't catch it, because the wrong point is
still inside Pennsylvania.

The fix became a feature. GeoNames has a `Crescent Post Office` at the right spot,
and **a post office is named after the community it serves** — so it's a sound
position proxy for a place no gazetteer lists as populated. I added a third
offline tier indexing post offices, parks and civil divisions. Cross-checked
against Nominatim on the seven cases where both have an answer, it agrees within
1.6 miles on six and is right about Crescent by 270 miles. It also removed six
network calls.

The allowlist is narrow on purpose: `Crescent Post Office` is strong evidence,
`Crescent Elementary School` isn't.

`ARCHITECTURE.md` §10 lists all nine things that went wrong and how each was
caught.

---

### 15. What would you do differently if you started again?

**Measure the corridor search before writing the second version.** I built a
grid-indexed version, measured 1,110 ms, and had to invert the whole thing. Twenty
minutes of measurement in Phase 0 would have saved rewriting it — which is why I
now benchmark the naive baseline in the shipped command, so the claim is
reproducible rather than remembered.

**Verify dependency compatibility before pinning.** I pinned DRF 3.16.1 without
checking, and it's incompatible with Django 6.1 — it imports `cc_delim_re`, which
Django 6 removed. Thirty seconds of checking classifiers would have caught it.

**Write the property tests earlier.** Both the nonsense infeasibility message and
the benchmark's fake `--offline` mode were found by tests that asserted a property
rather than an example. The hand-computed fixtures all passed first time; the
property tests found real bugs.

---

## Questions to ask them

- How do you currently geocode fuel stops in production — is there a coordinate
  feed, or is this the same problem internally?
- Is detour measured as straight-line distance or actual routed distance? That
  changes the optimisation materially.
- Do you self-host routing, and on what engine?
- How current do fuel prices need to be for this to be useful to a dispatcher —
  hourly, daily, weekly?

## One-line summary if they ask for one

> 8,100 fuel stops with no coordinates became a hash join against two public-domain
> files instead of 8,100 API calls. One routing call per request, a provably
> optimal refuelling greedy, 4.3 ms p50 local compute, and a README that says
> where it's weak.
