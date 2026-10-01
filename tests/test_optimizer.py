"""Optimiser tests against fixtures whose optimal answer is known by inspection.

Every expected number below was computed by hand before the implementation was
written, and the arithmetic is shown in the docstring so a reviewer can check it
without trusting the code.

Vehicle throughout: 50 gal tank, 10 mpg => 500 mile range.
"""
from __future__ import annotations

import pytest

from services.optimizer import (
    RouteInfeasibleError,
    Station,
    solve_refuelling,
)

TANK = 50.0
MPG = 10.0
RANGE = TANK * MPG  # 500


def station(mile: float, price: float, name: str = "") -> Station:
    return Station(
        stop_id=name or f"s{mile:g}",
        name=name or f"Station@{mile:g}",
        address="",
        city="",
        state="",
        lat=0.0,
        lon=0.0,
        mile=mile,
        detour_miles=0.0,
        price=price,
    )


def solve(stations, total_miles, start_gallons=0.0):
    return solve_refuelling(
        stations,
        total_miles=total_miles,
        tank_gallons=TANK,
        mpg=MPG,
        start_gallons=start_gallons,
    )


# --------------------------------------------------------------------------
# Core greedy behaviour
# --------------------------------------------------------------------------

def test_single_station_short_trip_buys_only_what_the_trip_needs():
    """One station at mile 0 at $3.00, destination at mile 100.

    The trip needs 100/10 = 10 gal. 10 x $3.00 = $30.00.

    This is the regression test for the final-leg bug: a greedy that "fills the
    tank when nothing cheaper is in range" would buy 50 gal for $150.00 and
    overstate the answer by $120.00.
    """
    plan = solve([station(0, 3.00)], total_miles=100)
    assert plan.total_gallons_purchased == pytest.approx(10.0)
    assert plan.total_cost == pytest.approx(30.00)
    assert len(plan.purchases) == 1


def test_buys_minimum_to_reach_a_cheaper_station_ahead():
    """Stations: mile 0 @ $4.00, mile 100 @ $3.00. Destination mile 150.

    At mile 0 a strictly cheaper station is reachable, so buy only enough to
    reach it: 100/10 = 10 gal x $4.00 = $40.00.
    At mile 100 nothing is cheaper except the destination: 50/10 = 5 gal
    x $3.00 = $15.00.
    Total 15 gal, $55.00.

    Filling at mile 0 instead would cost 15 x $4.00 = $60.00.
    """
    plan = solve([station(0, 4.00), station(100, 3.00)], total_miles=150)
    assert [p.gallons for p in plan.purchases] == pytest.approx([10.0, 5.0])
    assert plan.total_cost == pytest.approx(55.00)
    assert plan.total_gallons_purchased == pytest.approx(15.0)


def test_fills_tank_when_nothing_cheaper_is_in_range():
    """Stations: mile 0 @ $3.00, mile 400 @ $5.00. Destination mile 900.

    At mile 0 the destination is 900 mi away, beyond the 500 mi range, and the
    only station in range is dearer. So fill: 50 gal x $3.00 = $150.00, and
    drive to the cheapest station in range (mile 400).
    Arrive at mile 400 with 50 - 40 = 10 gal. The destination is now exactly
    500 mi away, so buy 50 - 10 = 40 gal x $5.00 = $200.00.
    Total 90 gal, $350.00.

    90 gal is also the exact trip consumption (900/10), so nothing is wasted.
    """
    plan = solve([station(0, 3.00), station(400, 5.00)], total_miles=900)
    assert [p.gallons for p in plan.purchases] == pytest.approx([50.0, 40.0])
    assert plan.total_cost == pytest.approx(350.00)
    assert plan.total_gallons_purchased == pytest.approx(90.0)


def test_skips_a_dearer_station_entirely():
    """Stations: mile 0 @ $3.00, mile 50 @ $9.99. Destination mile 100.

    The $9.99 station is never worth stopping at: buy all 10 gal at mile 0.
    """
    plan = solve([station(0, 3.00), station(50, 9.99)], total_miles=100)
    assert len(plan.purchases) == 1
    assert plan.purchases[0].station.mile == 0
    assert plan.total_cost == pytest.approx(30.00)


def test_prefers_the_cheapest_reachable_station_when_filling():
    """Stations: 0 @ $4.00, 100 @ $5.00, 200 @ $4.50, 600 @ $1.00. Dest 1000.

    At mile 0 nothing cheaper than $4.00 is within 500 mi (600 is out of range),
    so fill 50 gal x $4.00 = $200.00 and go to the cheapest in range, which is
    mile 200 @ $4.50 (not mile 100 @ $5.00).
    Arrive mile 200 with 50 - 20 = 30 gal. Now mile 600 @ $1.00 is 400 mi away,
    in range and cheaper, so buy only enough to reach it: 400/10 = 40 gal,
    minus the 30 in the tank = 10 gal x $4.50 = $45.00.
    Arrive mile 600 with 0 gal. Destination is 400 mi on: 40 gal x $1.00 =
    $40.00.
    Total 100 gal, $285.00.
    """
    plan = solve(
        [station(0, 4.00), station(100, 5.00), station(200, 4.50), station(600, 1.00)],
        total_miles=1000,
    )
    assert [p.station.mile for p in plan.purchases] == [0, 200, 600]
    assert [p.gallons for p in plan.purchases] == pytest.approx([50.0, 10.0, 40.0])
    assert plan.total_cost == pytest.approx(285.00)


def test_descending_prices_buy_hand_to_mouth():
    """Stations at 0,100,200,300 priced 6,5,4,3. Destination mile 350.

    Each station has a cheaper one 100 mi ahead, so each purchase is exactly the
    10 gal needed to reach the next: 10x6 + 10x5 + 10x4 = $150.00, then the last
    leg 50 mi => 5 gal x $3.00 = $15.00. Total 35 gal, $165.00.
    """
    plan = solve(
        [station(0, 6.0), station(100, 5.0), station(200, 4.0), station(300, 3.0)],
        total_miles=350,
    )
    assert [p.gallons for p in plan.purchases] == pytest.approx([10.0, 10.0, 10.0, 5.0])
    assert plan.total_cost == pytest.approx(165.00)


# --------------------------------------------------------------------------
# Tank capacity and range boundaries
# --------------------------------------------------------------------------

def test_station_exactly_at_range_limit_is_reachable():
    """A station exactly 500 mi ahead is reachable on a full tank, not an error."""
    plan = solve([station(0, 3.00), station(500, 2.00)], total_miles=600)
    assert plan.total_gallons_purchased == pytest.approx(60.0)
    # 50 gal @ $3 to cross the 500 mi, then 10 gal @ $2 for the last 100 mi.
    assert plan.total_cost == pytest.approx(150.00 + 20.00)


def test_never_exceeds_tank_capacity():
    plan = solve(
        [station(0, 3.0), station(450, 3.0), station(900, 3.0)], total_miles=1300
    )
    for purchase in plan.purchases:
        assert purchase.depart_gallons <= TANK + 1e-9
        assert purchase.arrive_gallons >= -1e-9


def test_starting_fuel_reduces_the_first_purchase():
    """Start with 4 gal (40 mi of range). One station at mile 0, dest mile 100.

    Trip needs 10 gal; 4 are already aboard, so buy 6 x $3.00 = $18.00.
    """
    plan = solve([station(0, 3.00)], total_miles=100, start_gallons=4.0)
    assert plan.total_gallons_purchased == pytest.approx(6.0)
    assert plan.total_cost == pytest.approx(18.00)


def test_no_purchase_needed_when_starting_fuel_covers_the_trip():
    plan = solve([station(0, 3.00)], total_miles=100, start_gallons=10.0)
    assert plan.purchases == []
    assert plan.total_cost == pytest.approx(0.0)


# --------------------------------------------------------------------------
# Degenerate and infeasible cases
# --------------------------------------------------------------------------

def test_same_start_and_finish_is_free_not_an_error():
    plan = solve([station(0, 3.00)], total_miles=0.0)
    assert plan.purchases == []
    assert plan.total_cost == pytest.approx(0.0)
    assert plan.total_gallons_purchased == pytest.approx(0.0)


def test_trip_within_starting_range_needs_no_station_at_all():
    plan = solve([], total_miles=100.0, start_gallons=50.0)
    assert plan.purchases == []
    assert plan.total_cost == pytest.approx(0.0)


def test_gap_between_consecutive_stations_longer_than_range_is_infeasible():
    """Stations at mile 0 and mile 600: a 600 mi gap exceeds the 500 mi range.

    The error must name the gap rather than return a plausible wrong answer.
    """
    with pytest.raises(RouteInfeasibleError) as exc:
        solve([station(0, 3.00, "Alpha"), station(600, 3.00, "Omega")], total_miles=700)
    err = exc.value
    assert err.gap_miles == pytest.approx(600.0)
    assert err.range_miles == pytest.approx(500.0)
    assert "Alpha" in err.from_label and "Omega" in err.to_label
    assert err.from_mile == pytest.approx(0.0)
    assert err.to_mile == pytest.approx(600.0)


def test_gap_from_origin_to_first_station_longer_than_range_is_infeasible():
    with pytest.raises(RouteInfeasibleError) as exc:
        solve([station(600, 3.00, "Far")], total_miles=700)
    assert exc.value.gap_miles == pytest.approx(600.0)
    assert exc.value.from_mile == pytest.approx(0.0)


def test_gap_from_last_station_to_destination_longer_than_range_is_infeasible():
    with pytest.raises(RouteInfeasibleError) as exc:
        solve([station(0, 3.00), station(100, 3.00, "Last")], total_miles=900)
    # 900 - 100 = 800 > 500
    assert exc.value.gap_miles == pytest.approx(800.0)
    assert "Last" in exc.value.from_label


def test_no_stations_on_a_long_route_is_infeasible():
    with pytest.raises(RouteInfeasibleError) as exc:
        solve([], total_miles=900.0)
    assert exc.value.gap_miles == pytest.approx(900.0)
    assert exc.value.reason == RouteInfeasibleError.NO_STOPS


def test_no_stations_reports_its_own_reason_not_a_range_gap():
    """A route with no stops in the corridor is a different failure from a gap
    that is too long, and must not claim a short trip "exceeds the range"."""
    with pytest.raises(RouteInfeasibleError) as exc:
        solve([], total_miles=10.0)
    err = exc.value
    assert err.reason == RouteInfeasibleError.NO_STOPS
    assert err.available_range_miles == pytest.approx(0.0)
    assert "No usable fuel stop" in str(err)
    assert "exceeds the 500 mi vehicle range" not in str(err)
    assert "max_detour_miles" in str(err)


def test_gap_failures_report_the_gap_reason():
    with pytest.raises(RouteInfeasibleError) as exc:
        solve([station(0, 3.0, "A"), station(600, 3.0, "B")], total_miles=700)
    assert exc.value.reason == RouteInfeasibleError.GAP_EXCEEDS_RANGE


def test_infeasible_error_serialises_for_the_api():
    with pytest.raises(RouteInfeasibleError) as exc:
        solve([station(0, 3.0, "Alpha"), station(600, 3.0, "Omega")], total_miles=700)
    payload = exc.value.as_dict()
    assert payload["reason"] == RouteInfeasibleError.GAP_EXCEEDS_RANGE
    assert payload["gap_miles"] == pytest.approx(600.0)
    assert payload["vehicle_range_miles"] == pytest.approx(500.0)
    assert payload["from"]["label"] == "Alpha"
    assert payload["to"]["label"] == "Omega"


def test_infeasible_error_message_is_actionable():
    with pytest.raises(RouteInfeasibleError) as exc:
        solve([station(0, 3.0, "Alpha"), station(600, 3.0, "Omega")], total_miles=700)
    message = str(exc.value)
    assert "600" in message and "500" in message
    assert "Alpha" in message and "Omega" in message


# --------------------------------------------------------------------------
# Accounting must reconcile
# --------------------------------------------------------------------------

def test_consumption_reconciles_with_purchases_and_origin_leg():
    """First station at mile 50, so 50 mi are driven before any purchase.

    Trip consumes 100/10 = 10 gal. We purchase (100-50)/10 = 5 gal. The 5 gal
    difference is the origin leg and must be reported, not hidden.
    """
    plan = solve([station(50, 3.00)], total_miles=100)
    assert plan.unpriced_origin_miles == pytest.approx(50.0)
    assert plan.gallons_consumed == pytest.approx(10.0)
    assert plan.total_gallons_purchased == pytest.approx(5.0)
    assert plan.gallons_consumed - plan.total_gallons_purchased == pytest.approx(
        plan.unpriced_origin_miles / MPG
    )


def test_arrives_at_destination_with_an_empty_tank():
    """Any fuel left in the tank at the destination is money wasted, so an
    optimal plan must finish empty (given start_gallons=0)."""
    plan = solve(
        [station(0, 3.0), station(300, 2.0), station(700, 4.0)], total_miles=1000
    )
    assert plan.ending_gallons == pytest.approx(0.0, abs=1e-9)


def test_cost_equals_sum_of_purchase_costs():
    plan = solve(
        [station(0, 4.0), station(200, 3.0), station(600, 5.0)], total_miles=1000
    )
    assert plan.total_cost == pytest.approx(sum(p.cost for p in plan.purchases))
    assert plan.total_gallons_purchased == pytest.approx(
        sum(p.gallons for p in plan.purchases)
    )


# --------------------------------------------------------------------------
# The unpriced origin leg must be visible, and priced
# --------------------------------------------------------------------------

def test_origin_leg_is_priced_at_the_first_pump_and_reported_separately():
    """First station at mile 100 @ $3.00, destination mile 200.

    Purchases cover miles 100..200 = 10 gal x $3.00 = $30.00.
    The origin leg is 100 mi = 10 gal, valued at the same $3.00 = $30.00.
    So total_cost is $30.00 and estimated_total_cost is $60.00.

    Reporting only total_cost would understate this trip by half.
    """
    plan = solve([station(100, 3.00)], total_miles=200)
    assert plan.total_cost == pytest.approx(30.00)
    assert plan.unpriced_origin_miles == pytest.approx(100.0)
    assert plan.unpriced_origin_gallons == pytest.approx(10.0)
    assert plan.origin_leg_estimated_cost == pytest.approx(30.00)
    assert plan.estimated_total_cost == pytest.approx(60.00)


def test_no_origin_leg_cost_when_a_station_sits_at_the_origin():
    plan = solve([station(0, 3.00)], total_miles=100)
    assert plan.unpriced_origin_miles == pytest.approx(0.0)
    assert plan.origin_leg_estimated_cost == pytest.approx(0.0)
    assert plan.estimated_total_cost == pytest.approx(plan.total_cost)


def test_origin_leg_cost_is_zero_when_nothing_is_purchased():
    plan = solve([station(0, 3.00)], total_miles=0.0)
    assert plan.origin_leg_estimated_cost == pytest.approx(0.0)
    assert plan.estimated_total_cost == pytest.approx(0.0)


def test_estimated_total_covers_the_whole_trip_consumption():
    """With the origin leg included, the gallons accounted for equal the gallons
    the trip actually burns."""
    plan = solve([station(50, 3.00), station(300, 2.00)], total_miles=400)
    accounted = plan.total_gallons_purchased + plan.unpriced_origin_gallons
    assert accounted == pytest.approx(plan.gallons_consumed)
