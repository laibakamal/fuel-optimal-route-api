"""Property tests for the optimiser.

Two independent checks:

1. The brief's requirement: the greedy never costs more than the obvious
   baseline of always filling up at the nearest station in range.

2. A stronger check on optimality: the greedy's cost equals the exhaustive
   dynamic-programming optimum. Instances are generated with distances that are
   multiples of the mpg and integer prices, so every purchase that matters lands
   on a whole-gallon lattice and the DP over (station, integer gallons) is an
   exhaustive search of that lattice. The greedy's own solution also lies on the
   lattice, so equality means the greedy attains the optimum, not merely that it
   beats a baseline.
"""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from services.optimizer import (
    RouteInfeasibleError,
    Station,
    solve_refuelling,
)

TANK = 50
MPG = 10
RANGE = TANK * MPG


def mk(mile: float, price: float) -> Station:
    return Station(
        stop_id=f"s{mile:g}", name=f"S{mile:g}", address="", city="", state="",
        lat=0.0, lon=0.0, mile=float(mile), detour_miles=0.0, price=float(price),
    )


# --------------------------------------------------------------------------
# Baseline: always fill the tank at the nearest station you can reach.
# --------------------------------------------------------------------------

def nearest_station_fill_baseline(stations, total_miles, tank, mpg):
    """Naive strategy: drive to the next station, fill up, repeat.

    Deliberately includes the final-leg mistake of filling rather than buying
    only what is needed, because that is the realistic naive implementation the
    greedy has to beat.
    """
    gallons = 0.0
    position = 0.0
    cost = 0.0
    index = 0
    while position < total_miles - 1e-9:
        if gallons * mpg >= total_miles - position - 1e-9:
            break
        if index >= len(stations):
            return None
        station = stations[index]
        if station.mile < position - 1e-9:
            index += 1
            continue
        if station.mile - position > gallons * mpg + 1e-9:
            return None  # cannot reach it
        gallons -= (station.mile - position) / mpg
        position = station.mile
        buy = tank - gallons
        if buy > 1e-9:
            cost += buy * station.price
            gallons = tank
        index += 1
    return cost


# --------------------------------------------------------------------------
# Exhaustive DP optimum over a whole-gallon lattice.
# --------------------------------------------------------------------------

def dp_optimum(stations, total_miles, tank, mpg):
    """Exact minimum cost over integer gallon levels. O(n * tank^2)."""
    miles = [s.mile for s in stations] + [float(total_miles)]
    prices = [s.price for s in stations]
    n = len(stations)

    INF = float("inf")
    # best[g] = min cost to be at station i holding g gallons on arrival.
    best = [INF] * (tank + 1)
    first_leg = int(round(miles[0] / mpg)) if n else 0
    if n == 0:
        return 0.0 if total_miles <= 1e-9 else None
    # Arrive at station 0 empty: the origin leg is assumed covered (see
    # FuelPlan.unpriced_origin_miles) and costs nothing here, matching the
    # optimiser's accounting.
    best[0] = 0.0

    for i in range(n):
        leg = int(round((miles[i + 1] - miles[i]) / mpg))
        if leg > tank:
            return None  # gap exceeds range
        nxt = [INF] * (tank + 1)
        for arrive in range(tank + 1):
            if best[arrive] == INF:
                continue
            for depart in range(arrive, tank + 1):
                if depart < leg:
                    continue  # cannot cross this leg
                cost = best[arrive] + (depart - arrive) * prices[i]
                remaining = depart - leg
                if cost < nxt[remaining]:
                    nxt[remaining] = cost
        best = nxt

    # Any fuel left at the destination was wasted money, but it is still a
    # feasible outcome, so take the min over all ending levels.
    answer = min(best)
    return None if answer == INF else answer


# --------------------------------------------------------------------------
# Instance generation: distances are multiples of MPG, prices whole dollars.
# --------------------------------------------------------------------------

@st.composite
def instances(draw):
    n = draw(st.integers(min_value=1, max_value=7))
    # Gaps in whole tens of miles, each within range so most instances are
    # feasible; infeasible ones are handled by the test.
    gaps = draw(
        st.lists(
            st.integers(min_value=1, max_value=RANGE // MPG).map(lambda g: g * MPG),
            min_size=n,
            max_size=n,
        )
    )
    prices = draw(
        st.lists(st.integers(min_value=1, max_value=9), min_size=n, max_size=n)
    )
    miles = [0]
    for gap in gaps[:-1]:
        miles.append(miles[-1] + gap)
    total = miles[-1] + gaps[-1]
    return [mk(m, p) for m, p in zip(miles, prices)], float(total)


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(instances())
def test_greedy_is_never_worse_than_the_nearest_station_baseline(instance):
    stations, total = instance
    plan = solve_refuelling(
        stations, total_miles=total, tank_gallons=TANK, mpg=MPG, start_gallons=0.0
    )
    baseline = nearest_station_fill_baseline(stations, total, TANK, MPG)
    if baseline is not None:
        assert plan.total_cost <= baseline + 1e-6


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(instances())
def test_greedy_attains_the_exhaustive_dp_optimum(instance):
    stations, total = instance
    optimum = dp_optimum(stations, total, TANK, MPG)
    plan = solve_refuelling(
        stations, total_miles=total, tank_gallons=TANK, mpg=MPG, start_gallons=0.0
    )
    assert optimum is not None
    assert plan.total_cost == pytest.approx(optimum, abs=1e-6)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(instances())
def test_plan_invariants_always_hold(instance):
    stations, total = instance
    plan = solve_refuelling(
        stations, total_miles=total, tank_gallons=TANK, mpg=MPG, start_gallons=0.0
    )
    for purchase in plan.purchases:
        assert purchase.gallons > 0
        assert purchase.depart_gallons <= TANK + 1e-9
        assert purchase.arrive_gallons >= -1e-9
        assert purchase.cost == pytest.approx(purchase.gallons * purchase.station.price)
    # Purchases are in route order.
    assert [p.station.mile for p in plan.purchases] == sorted(
        p.station.mile for p in plan.purchases
    )
    # Accounting reconciles exactly.
    assert plan.total_cost == pytest.approx(sum(p.cost for p in plan.purchases))
    assert plan.gallons_consumed == pytest.approx(total / MPG)
    assert plan.total_gallons_purchased + plan.unpriced_origin_miles / MPG == pytest.approx(
        plan.gallons_consumed + plan.ending_gallons
    )


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=300).map(lambda m: m * MPG),
            st.integers(min_value=1, max_value=9),
        ),
        min_size=0,
        max_size=8,
    ),
    st.integers(min_value=0, max_value=400).map(lambda m: m * MPG),
)
def test_never_returns_a_plan_for_an_infeasible_route(stops, total):
    """Unconstrained generation: whatever comes out, either it is a valid plan or
    a RouteInfeasibleError. It must never be a plan that runs the tank dry."""
    stations = sorted({m: mk(m, p) for m, p in stops}.values(), key=lambda s: s.mile)
    stations = [s for s in stations if s.mile <= total]
    try:
        plan = solve_refuelling(
            stations, total_miles=float(total), tank_gallons=TANK, mpg=MPG, start_gallons=0.0
        )
    except RouteInfeasibleError as exc:
        if exc.reason == RouteInfeasibleError.NO_STOPS:
            # Nowhere to buy fuel: the trip must exceed the range available at
            # the origin, which with no starting fuel is zero.
            assert not stations
            assert exc.gap_miles > exc.available_range_miles - 1e-9
        else:
            assert exc.gap_miles > RANGE - 1e-9
        return
    # If a plan came back, simulate it and confirm the tank never goes negative.
    gallons = 0.0
    position = stations[0].mile if stations else 0.0
    for purchase in plan.purchases:
        gallons -= (purchase.station.mile - position) / MPG
        assert gallons >= -1e-6
        position = purchase.station.mile
        gallons += purchase.gallons
        assert gallons <= TANK + 1e-6
    gallons -= (total - position) / MPG
    assert gallons >= -1e-6
