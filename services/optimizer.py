"""Minimum-cost refuelling along a fixed route.

THE PROBLEM
    A route of length D miles. Stations at positions 0 <= x_1 <= ... <= x_n <= D
    with prices p_i per gallon. Tank capacity C gallons, efficiency m mpg, so
    range R = C*m miles. The vehicle starts at mile 0 with s gallons and must
    reach mile D. Minimise the money spent on fuel.

THE ALGORITHM
    The classic greedy for minimum-cost refuelling, which is *provably optimal*
    for this problem, not a heuristic. At the station we are currently standing
    at, look ahead as far as the range allows:

      - If a strictly cheaper station is reachable, buy only enough fuel to reach
        the nearest such station. Carrying extra fuel bought here past a cheaper
        pump is never worth it.
      - Otherwise, fill the tank completely and drive to the cheapest station in
        range. Everything reachable costs at least as much as here, so buying
        here as much as possible is never worse.

PROOF OF OPTIMALITY (exchange argument)
    Claim: some optimal solution never carries a gallon past a strictly cheaper
    station.

    Suppose an optimal solution buys a gallon at station i for price p_i and
    burns it after passing station j > i where p_j < p_i. Construct a new
    solution that buys that gallon at j instead of at i.

      - Feasibility holds. The gallon was burned after j, so the vehicle reached
        j in the original solution and still reaches it now (it carries one
        gallon *less* over the segment i..j, and tank capacity is an upper bound,
        so removing fuel cannot violate it). Between i and j the vehicle has one
        gallon less but never needed it: it was burned later than j.
      - The tank at j has room for the gallon, because in the original solution
        the vehicle was carrying that same gallon through j.
      - Cost strictly falls by p_i - p_j > 0.

    That contradicts optimality, so no optimal solution carries fuel past a
    cheaper station. Hence buying only enough to reach the nearest cheaper
    station is safe. Symmetrically, when nothing cheaper is in range, every
    reachable station costs >= p_i, so deferring a purchase can only cost more;
    filling the tank is safe. The greedy therefore constructs an optimal
    solution.

    `tests/test_optimizer_property.py` checks the consequence of this empirically
    against a brute-force optimum on small random instances.

THE DESTINATION IS A PRICE-ZERO SENTINEL
    The destination is appended as a station at mile D with price 0. This is not
    a trick, it is the correct model: arriving with fuel in the tank is money
    spent on fuel you did not use, so the destination genuinely is the cheapest
    place to "buy" the rest of the trip.

    It also makes the final-leg case disappear. Because the sentinel is cheaper
    than every real station, the rule "buy only enough to reach the nearest
    cheaper station" automatically caps the last purchase at what is needed to
    finish, instead of filling the tank. Without this, a 50-gallon top-up at the
    last station would overstate the headline total by up to 50 x price (around
    $170 at these prices) - the single easiest way to get this assignment wrong.

COMPLEXITY
    O(n) to precompute the next-strictly-cheaper station for every station with a
    monotonic stack, plus a bounded forward scan per *visited* station when
    filling. The number of visited stations is small (it is bounded by the number
    of purchases, roughly D/R when prices are flat), and n is the corridor size,
    measured at 188-460 for transcontinental routes. Total work is microseconds;
    see `manage.py benchmark`.

MONEY
    Costs are computed in float and rounded to cents at the boundary. Prices are
    stored as Decimal (exact, as supplied) but gallons are a *measurement*
    derived from route distance, so the product is an estimate whose error is
    dominated by the distance and mpg assumptions, not by float representation.
    Carrying Decimal through the loop would buy precision the inputs do not have.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Tolerance for float comparisons on miles/gallons. 1e-9 miles is 60 nanometres;
#: this exists to stop exact-boundary cases (a station precisely at the range
#: limit) from failing on representation error.
EPS = 1e-9


@dataclass(frozen=True)
class Station:
    """A fuel stop projected onto the route's one-dimensional distance axis."""

    stop_id: str
    name: str
    address: str
    city: str
    state: str
    lat: float
    lon: float
    #: Distance along the route, in miles, of the closest point to this stop.
    mile: float
    #: Straight-line distance from that closest route point to the stop.
    detour_miles: float
    #: USD per gallon.
    price: float


@dataclass(frozen=True)
class Purchase:
    station: Station
    gallons: float
    cost: float
    #: Fuel in the tank on arrival at this station, and on departure.
    arrive_gallons: float
    depart_gallons: float


@dataclass(frozen=True)
class FuelPlan:
    purchases: list[Purchase] = field(default_factory=list)
    total_gallons_purchased: float = 0.0
    total_cost: float = 0.0
    total_miles: float = 0.0
    #: Total fuel the trip burns: total_miles / mpg. Differs from
    #: total_gallons_purchased by the starting fuel plus the unpriced origin leg.
    gallons_consumed: float = 0.0
    #: Miles driven before the first purchase. With start_gallons=0 this fuel is
    #: assumed to be aboard already and is therefore NOT in total_cost. Reported
    #: so the gap is visible rather than quietly understating the total.
    #:
    #: This is not a corner case. On Los Angeles -> New York the nearest usable
    #: stop is 238.7 mi in, because all 8 California stops in the dataset sit in
    #: the Imperial Valley while the route runs north-east on I-15. That leaves
    #: 23.87 gal unpriced - $82.56, or 10.6% of the reported total. Far too large
    #: to leave implicit, which is why `estimated_total_cost` exists.
    unpriced_origin_miles: float = 0.0
    ending_gallons: float = 0.0
    #: mpg, carried so the derived properties below need no extra argument.
    mpg: float = 0.0

    @property
    def unpriced_origin_gallons(self) -> float:
        return self.unpriced_origin_miles / self.mpg if self.mpg else 0.0

    @property
    def origin_leg_estimated_cost(self) -> float:
        """Cost of the unpriced origin leg, valued at the first pump we do buy at.

        An estimate, labelled as one. It is the best available basis: the nearest
        station to the origin is the one whose price region the vehicle is
        actually starting in.
        """
        if not self.purchases:
            return 0.0
        return self.unpriced_origin_gallons * self.purchases[0].station.price

    @property
    def estimated_total_cost(self) -> float:
        """What the whole trip's fuel costs, including the origin leg estimate.

        `total_cost` is what the plan provably spends at pumps. This is the number
        that answers "total money spent on fuel" for the entire distance. Both are
        returned by the API, because reporting only the first understates long
        trips out of sparse regions and reporting only the second blurs a measured
        figure with an estimated one.
        """
        return self.total_cost + self.origin_leg_estimated_cost


class RouteInfeasibleError(Exception):
    """No refuelling plan exists.

    Carries structured data so the API can name the offending leg rather than
    returning a generic failure. Two distinct reasons, because they mean
    different things to the caller:

      GAP_EXCEEDS_RANGE  stops exist, but two consecutive ones are further apart
                         than the vehicle can travel on one tank.
      NO_STOPS           no usable stop was found on the route at all, so there
                         is nowhere to buy fuel.

    Conflating the two produced a nonsense message - "10.0 mi exceeds the 500 mi
    range" for a 10-mile route with no stops - which a property test caught.
    """

    GAP_EXCEEDS_RANGE = "gap_exceeds_range"
    NO_STOPS = "no_fuel_stops_on_route"

    def __init__(
        self,
        reason: str,
        gap_miles: float,
        range_miles: float,
        from_label: str,
        from_mile: float,
        to_label: str,
        to_mile: float,
        available_range_miles: float | None = None,
    ) -> None:
        self.reason = reason
        self.gap_miles = gap_miles
        self.range_miles = range_miles
        self.from_label = from_label
        self.from_mile = from_mile
        self.to_label = to_label
        self.to_mile = to_mile
        #: Range actually available at the point of failure. When there is
        #: nowhere to buy fuel this is the starting tank only, not a full tank.
        self.available_range_miles = (
            range_miles if available_range_miles is None else available_range_miles
        )

        if reason == self.NO_STOPS:
            message = (
                f"No usable fuel stop found on this route. The {gap_miles:.1f} mi "
                f"trip exceeds the {self.available_range_miles:.1f} mi of range "
                f"available at the origin, so it cannot be completed without "
                f"refuelling. Try increasing max_detour_miles."
            )
        else:
            message = (
                f"No fuel stop within range: the {gap_miles:.1f} mi leg from "
                f"{from_label} (mile {from_mile:.1f}) to {to_label} "
                f"(mile {to_mile:.1f}) exceeds the {range_miles:.0f} mi vehicle "
                f"range. Try increasing max_detour_miles."
            )
        super().__init__(message)

    def as_dict(self) -> dict:
        return {
            "reason": self.reason,
            "gap_miles": round(self.gap_miles, 1),
            "vehicle_range_miles": round(self.range_miles, 1),
            "available_range_miles": round(self.available_range_miles, 1),
            "from": {"label": self.from_label, "mile": round(self.from_mile, 1)},
            "to": {"label": self.to_label, "mile": round(self.to_mile, 1)},
        }


_ORIGIN_LABEL = "route origin"
_DESTINATION_LABEL = "route destination"


def _next_strictly_cheaper(prices: list[float]) -> list[int]:
    """For each index, the next index with a strictly lower price, or len(prices).

    Standard next-smaller-element scan with a monotonic stack: each index is
    pushed and popped at most once, so this is O(n).
    """
    n = len(prices)
    result = [n] * n
    stack: list[int] = []
    for i, price in enumerate(prices):
        while stack and prices[stack[-1]] > price:
            result[stack.pop()] = i
        stack.append(i)
    return result


def _assert_feasible(
    stations: list[Station], total_miles: float, range_miles: float, start_gallons: float, mpg: float
) -> None:
    """Raise if any leg exceeds the range. Checked before planning so an
    infeasible route fails loudly instead of producing a confident wrong answer.
    """
    start_range = min(start_gallons * mpg, range_miles)

    if not stations:
        # No stations at all: only feasible if the starting fuel covers the trip.
        # This is NOT a range-gap failure - there is nowhere to buy fuel - so it
        # reports its own reason and the range actually available at the origin.
        if total_miles > start_range + EPS:
            raise RouteInfeasibleError(
                RouteInfeasibleError.NO_STOPS,
                total_miles,
                range_miles,
                _ORIGIN_LABEL,
                0.0,
                _DESTINATION_LABEL,
                total_miles,
                available_range_miles=start_range,
            )
        return

    # Origin -> first station. The starting tank covers at most start_range; the
    # brief's assumption is that the first station is reachable, but a leg longer
    # than a full tank could not be covered by any tank, so it is still infeasible.
    first = stations[0]
    if first.mile > range_miles + EPS:
        raise RouteInfeasibleError(
            RouteInfeasibleError.GAP_EXCEEDS_RANGE,
            first.mile, range_miles, _ORIGIN_LABEL, 0.0, first.name, first.mile,
        )

    for current, nxt in zip(stations, stations[1:]):
        gap = nxt.mile - current.mile
        if gap > range_miles + EPS:
            raise RouteInfeasibleError(
                RouteInfeasibleError.GAP_EXCEEDS_RANGE,
                gap, range_miles, current.name, current.mile, nxt.name, nxt.mile,
            )

    last = stations[-1]
    tail = total_miles - last.mile
    if tail > range_miles + EPS:
        raise RouteInfeasibleError(
            RouteInfeasibleError.GAP_EXCEEDS_RANGE,
            tail, range_miles, last.name, last.mile, _DESTINATION_LABEL, total_miles,
        )


def solve_refuelling(
    stations: list[Station],
    total_miles: float,
    tank_gallons: float,
    mpg: float,
    start_gallons: float = 0.0,
) -> FuelPlan:
    """Cheapest way to buy the fuel for `total_miles` given `stations`.

    `stations` must be sorted by `mile` ascending. Raises RouteInfeasibleError if
    no plan exists.
    """
    if mpg <= 0:
        raise ValueError("mpg must be positive")
    if tank_gallons <= 0:
        raise ValueError("tank_gallons must be positive")

    range_miles = tank_gallons * mpg
    gallons_consumed = total_miles / mpg

    if total_miles <= EPS:
        # Start == finish. Zero distance, zero cost; not an error.
        return FuelPlan(total_miles=0.0, ending_gallons=start_gallons, mpg=mpg)

    usable = [s for s in stations if 0.0 - EPS <= s.mile <= total_miles + EPS]
    usable.sort(key=lambda s: s.mile)
    _assert_feasible(usable, total_miles, range_miles, start_gallons, mpg)

    # Trip already covered by the fuel aboard: buy nothing.
    if total_miles <= start_gallons * mpg + EPS:
        return FuelPlan(
            total_miles=total_miles,
            gallons_consumed=gallons_consumed,
            ending_gallons=start_gallons - gallons_consumed,
            mpg=mpg,
        )

    # The destination joins the station list as a price-zero sentinel, so the
    # "nearest cheaper station" rule caps the final purchase automatically.
    sentinel = Station(
        stop_id="", name=_DESTINATION_LABEL, address="", city="", state="",
        lat=0.0, lon=0.0, mile=total_miles, detour_miles=0.0, price=0.0,
    )
    nodes = usable + [sentinel]
    prices = [s.price for s in nodes]
    next_cheaper = _next_strictly_cheaper(prices)
    destination_index = len(nodes) - 1

    purchases: list[Purchase] = []
    # The vehicle is assumed to reach the first station on the fuel aboard; any
    # shortfall is reported as unpriced_origin_miles rather than silently costed.
    unpriced_origin_miles = 0.0
    if usable:
        reach = start_gallons * mpg
        if usable[0].mile > reach + EPS:
            unpriced_origin_miles = usable[0].mile - reach

    index = 0
    # Fuel on arrival at nodes[0], after burning what the origin leg could use.
    gallons = max(0.0, start_gallons - max(0.0, nodes[0].mile - unpriced_origin_miles) / mpg)

    while index < destination_index:
        here = nodes[index]
        # Reachable set on a full tank from here.
        limit = here.mile + range_miles + EPS

        target = next_cheaper[index]
        if target <= destination_index and nodes[target].mile <= limit:
            # A strictly cheaper pump (possibly the destination) is reachable:
            # buy the minimum that gets us there, and nothing more.
            needed = (nodes[target].mile - here.mile) / mpg
            buy = max(0.0, needed - gallons)
            next_index = target
        else:
            # Nothing cheaper in range: fill up, then continue to the cheapest
            # station we can reach. Ties go to the nearer station, which keeps the
            # remaining range as large as possible.
            buy = max(0.0, tank_gallons - gallons)
            next_index = -1
            best_price = float("inf")
            for j in range(index + 1, destination_index + 1):
                if nodes[j].mile > limit:
                    break
                if nodes[j].price < best_price - EPS:
                    best_price = nodes[j].price
                    next_index = j
            if next_index == -1:
                # _assert_feasible guarantees a reachable next node, so this is a
                # genuine invariant violation rather than a user-facing error.
                raise AssertionError(
                    f"no reachable node from mile {here.mile} despite feasibility check"
                )

        if buy > EPS:
            cost = buy * here.price
            purchases.append(
                Purchase(
                    station=here,
                    gallons=buy,
                    cost=cost,
                    arrive_gallons=gallons,
                    depart_gallons=gallons + buy,
                )
            )
            gallons += buy

        gallons -= (nodes[next_index].mile - here.mile) / mpg
        if gallons < -EPS:
            raise AssertionError(
                f"ran dry between mile {here.mile} and {nodes[next_index].mile}"
            )
        gallons = max(0.0, gallons)
        index = next_index

    total_gallons = sum(p.gallons for p in purchases)
    return FuelPlan(
        purchases=purchases,
        total_gallons_purchased=total_gallons,
        total_cost=sum(p.cost for p in purchases),
        total_miles=total_miles,
        gallons_consumed=gallons_consumed,
        unpriced_origin_miles=unpriced_origin_miles,
        ending_gallons=gallons,
        mpg=mpg,
    )
