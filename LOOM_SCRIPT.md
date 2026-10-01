# Loom script — 5 minutes

Written to be read aloud. Plain, specific, no selling. Timings are cumulative.

## Before you hit record

- [ ] `make setup` has run; `make run` is up on :8000
- [ ] **Warm the cache for the routes you'll demo** — the OSRM demo server has
      swung between 390 ms and 1.3 s between runs, and you don't want to sit
      watching a spinner. Run requests 1 and 5 in Postman once, then stop and
      restart the server so request 1 is a genuine cache miss again, or just
      accept the ~1 s and say what it is.
- [ ] Postman open with `api/postman_collection.json` imported
- [ ] Two browser tabs: the map at `http://127.0.0.1:8000`, and the repo
- [ ] Editor open with `services/optimizer.py`
- [ ] A terminal with `make bench --offline` output already on screen, scrolled
      to the summary — do not run it live, it takes 20 seconds

---

## 0:00 – 1:00 — The problem, and the thing that isn't obvious

> This is a fuel-routing API. You give it a start and finish in the US, it gives
> you the route, where to buy diesel, and what it costs for a truck with a
> 500-mile range at 10 miles a gallon.
>
> The routing part is one API call. The actual problem is in the data file you
> sent.

**Open the CSV. Scroll so the Address column is visible.**

> Eight thousand one hundred and fifty-one fuel stops. No latitude. No longitude.
> And the addresses aren't street addresses — they're highway exit descriptors.
> "I-44, Exit 283 and US-69." I measured it: ninety-six per cent of rows have a
> highway or exit token, and **one tenth of one per cent** look like a street
> address you could geocode.
>
> So you can't geocode these the normal way. And you can't make eight thousand
> geocoding calls in a three-day exercise and call it reproducible.
>
> What I did instead: join city and state against the US Census Bureau's
> Gazetteer, which is public domain, and GeoNames. Offline, once, and I committed
> the result. That resolves **a hundred per cent** of the US stops with **zero**
> geocoding API calls.

**Show the pipeline output block in the README.**

> Ninety-five per cent from Census, five per cent from GeoNames, and then six
> stops from a third tier I'll come back to, because it's the bit I'd actually
> want to talk about.

---

## 1:00 – 2:30 — The demo

**Postman, request 1: Dallas to Chicago. Send.**

> Dallas to Chicago. Nine hundred and sixty-one miles, six fuel stops, two
> hundred and seventy-four dollars sixty-six.

**Scroll to `fuel_stops`.**

> Each stop has the name, the address, the city and state, the price per gallon,
> how many gallons to buy there, the cost, how far along the route it is, and how
> far off-route it is.
>
> Look at the gallons: one point one five at the first stop, then two point eight,
> then a full fifty-gallon fill. It's not filling up every time. It buys just
> enough to reach a cheaper pump when there is one, and fills up when there isn't.
> That's the optimiser, and I'll show you why it's provably right in a minute.

**Scroll to `totals`.**

> Effective price, two dollars eighty-six a gallon. The median price in your file
> is three thirty-nine. So it's about sixteen per cent under the median — it's
> genuinely finding the cheap Texas fuel.
>
> And the tank reaches Chicago at exactly zero. It doesn't buy fuel it doesn't
> burn.

**Scroll to `diagnostics`.**

> You asked for one call to the routing API. Here's the count, measured, in the
> response: routing one, geocoding zero. You don't have to take my word for it —
> every response tells you what it spent.

**Send request 2 — the identical request.**

> Same request again. Routing calls: zero. Cache hit.

**Switch to the map tab, hit Plan route.**

> And "return a map of the route", literally — Leaflet, OpenStreetMap tiles, the
> route and the numbered stops. It calls the same public endpoint, so it can't
> show you anything the API wouldn't return.

**Postman, request 10 — the 0.1 mile corridor.**

> One error case, because I think this matters more than the happy path. If I
> squeeze the detour tolerance to a tenth of a mile, there's a nine-hundred-mile
> gap with no reachable fuel. It returns a 422 and it **names the gap** — from
> this stop at mile zero to that stop at mile nine sixty-one, exceeds the
> five-hundred-mile range. It does not return a plausible wrong answer.

---

## 2:30 – 4:30 — The code

**Editor. Repo root first.**

> Structure: `services/` is pure Python, no Django imports at all. That's the
> routing, the corridor search and the optimiser. `fuelroute/` is the Django app
> — models, the endpoint, the management commands. The split means the part
> that's actually being graded is unit-testable without a database.

**Open `services/routing.py`, show the request params.**

> One call. The reason one call is enough is this parameter —
> `annotations=distance`. OSRM returns the full geometry *and* the length of every
> segment of it. So I prefix-sum those and I've got every point on the route with
> its cumulative mileage. No second call, and I'm not re-measuring the geometry
> myself with straight-line distance, which would cut every curve and
> under-report the trip.

**Open `services/corridor.py`, scroll to the module docstring.**

> Corridor search. Naively this is six and a half thousand stops against
> thirty-four thousand route points — two hundred and twenty million distance
> calculations.
>
> My first version indexed the stops and walked the route. Eleven hundred
> milliseconds on LA to New York. Five times over budget.
>
> The fix was to invert it: index the *route samples*, then probe once per stop
> and only look at samples in that stop's grid cell. Fourteen milliseconds, same
> answer — same stop count, same gaps. It's in the benchmark, you can re-run it.
>
> I used a uniform grid rather than a k-d tree deliberately. The query is
> "everything within ten miles of this line", which is a bulk range query, not
> nearest-neighbour. The grid is thirty lines of integer arithmetic.

**Open `services/optimizer.py`. Scroll to the proof.**

> The optimiser. At each station: if there's a strictly cheaper station in range,
> buy only enough fuel to reach it. If there isn't, fill up and drive to the
> cheapest one you can reach.
>
> That's provably optimal, not a heuristic, and the exchange argument is written
> out here. If an optimal solution carries a gallon past a cheaper station, you
> can move that purchase to the cheaper station — it's still feasible, the tank
> had room, and it costs strictly less. Contradiction. So no optimal solution ever
> carries fuel past a cheaper pump.

**Scroll to the sentinel.**

> One thing I want to point out because it's the easiest way to get this wrong.
> The destination is modelled as a station with price zero. That's not a trick —
> arriving with fuel in the tank is money you spent on fuel you didn't burn, so
> the destination really is the cheapest place to buy the rest.
>
> And it makes the last leg handle itself. Without it, you fill the tank at the
> final stop and overstate the answer by up to a hundred and seventy dollars.
> That's the headline number in this exercise, so it's worth being careful about.

**Open `tests/test_optimizer_property.py`.**

> And a proof in a comment is just a claim, so there's a property test that checks
> the greedy's cost equals an exhaustive dynamic-programming optimum across four
> hundred generated cases.

---

## 4:30 – 5:00 — Numbers, and what's wrong with it

**Terminal with the benchmark summary.**

> Performance: twelve real long-haul routes, twenty repeats. Local compute is
> four point three milliseconds at p50, fifteen at p95. The target was two
> hundred. The external routing call dominates everything and varies between four
> hundred milliseconds and one and a half seconds, which is why I report them
> separately.

**README, Known limitations.**

> And the honest part. The stops are placed at **city centroids**, not at pumps —
> because the file has no coordinates and no street addresses. Measured error is
> about one and a half miles median, four and a half at the ninetieth percentile.
>
> So the detour distance I report is advisory. It is not navigable. Don't give it
> to a driver.
>
> The total cost is much more solid, because cost is gallons times price, and
> gallons comes from the route distance, which is exact. Stop position only
> changes *which* nearby stop gets picked, and nearby stops have similar prices.
>
> That's also why the detour default is ten miles and not five — five would be
> filtering at a finer resolution than the coordinates actually have.
>
> If I had another week, the first thing I'd do is parse the interstate and exit
> number out of those addresses and resolve them against OSM's junction data.
> That would put more than half the stops within a few hundred metres instead of
> a few miles, and the detour number would become real.
>
> Everything's in the README — the measured numbers, the commands to re-run them,
> and the limitations. Thanks for looking at it.

---

## If you have 30 seconds spare

The Crescent, Pennsylvania story is the best thing in here and it's cut for time.
If you end up ahead of schedule:

> One more thing. Six stops wouldn't resolve offline, so I called a geocoder, and
> it put Crescent, Pennsylvania two hundred and seventy miles from where it is. A
> state bounding-box check doesn't catch that, because the wrong answer is still
> inside Pennsylvania.
>
> What caught it: GeoNames has a "Crescent Post Office" in the right place. A post
> office is named after the community it serves. So I added a third tier that
> indexes post offices, parks and townships. It fixed Crescent and removed six
> network calls. It's narrow on purpose — a post office is good evidence, an
> elementary school isn't.

## Things not to say

- Don't call it production-ready. It depends on a free demo server with no SLA.
- Don't claim the Postgres path works. It's configured and the DDL is verified to
  generate, but it has never run against a live server — Docker isn't installed
  on this machine, and the README says so.
- Don't round the numbers up. 90.7% coverage, 239 tests, 4.31 ms p50.
