"""API tests. No test reaches the network: the routing provider is always mocked
and tests/conftest.py blocks sockets outright.

Routes are the synthetic meridian from conftest, so the expected costs are
hand-computable. The seeded stops are:

    mile    east   price   name
       0     0.5   $3.00   ORIGIN TRUCK STOP
     100     1.0   $4.00   DEAR PUMP
     200     2.0   $2.50   CHEAP PUMP
     300     3.0   $3.50   MIDDLING PUMP
     450    40.0   $1.00   FAR OFF ROUTE      (outside a 10 mi corridor)
     500     1.5   $3.25   LAST PUMP
"""
from __future__ import annotations

import pytest

from tests.conftest import meridian_route

ENDPOINT = "/api/v1/route"


@pytest.fixture
def api(client, seeded_stops, monkeypatch, fake_provider):
    """Test client with the routing provider replaced by a counting fake."""
    provider = fake_provider(meridian_route(600.0))
    monkeypatch.setattr(
        "fuelroute.planner.default_route_provider", lambda: provider
    )
    client.provider = provider
    return client


def get(client, **params):
    params.setdefault("start", "39.0,-100.0")
    params.setdefault("finish", "41.0,-100.0")
    params.setdefault("geometry", "none")
    return client.get(ENDPOINT, params)


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_happy_path_returns_a_plan(api):
    response = get(api)
    assert response.status_code == 200
    body = response.json()
    assert body["route"]["total_distance_miles"] == pytest.approx(600.0)
    assert body["totals"]["fuel_stop_count"] >= 1
    assert body["fuel_stops"][0]["name"] == "ORIGIN TRUCK STOP"


@pytest.mark.django_db
def test_exactly_one_routing_call_per_uncached_request(api):
    body = get(api).json()
    assert body["diagnostics"]["external_calls"]["routing_api"] == 1
    assert body["diagnostics"]["external_calls"]["geocoding_api"] == 0
    assert body["diagnostics"]["external_calls"]["total"] == 1
    assert api.provider.calls == 1


@pytest.mark.django_db
def test_second_identical_request_makes_no_external_call(api):
    get(api)
    body = get(api).json()
    assert body["diagnostics"]["route_cache_hit"] is True
    assert body["diagnostics"]["external_calls"]["routing_api"] == 0
    assert api.provider.calls == 1, "cache did not prevent the second call"


@pytest.mark.django_db
def test_cost_matches_the_hand_computed_optimum(api):
    """600 mi at 10 mpg = 60 gal. Stops in a 10 mi corridor: $3.00 at 0,
    $4.00 at 100, $2.50 at 200, $3.50 at 300, $3.25 at 500.
    ($1.00 at mile 450 is 40 mi off route and must be excluded.)

    At mile 0 ($3.00) the cheapest thing in the 500 mi range is $2.50 at mile 200,
    so buy just enough to reach it: 20 gal x $3.00 = $60.00.
    At mile 200 ($2.50) nothing ahead is cheaper within range except the
    destination at mile 600, which is exactly 400 mi on: 40 gal x $2.50 = $100.00.
    Total 60 gal, $160.00 - and nothing is bought at the $4.00, $3.50 or $3.25
    pumps at all.
    """
    body = get(api).json()
    totals = body["totals"]
    assert totals["total_gallons_purchased"] == pytest.approx(60.0)
    assert totals["total_fuel_cost_usd"] == pytest.approx(160.00)
    assert [s["price_usd_per_gallon"] for s in body["fuel_stops"]] == [3.0, 2.5]
    assert [s["gallons_purchased"] for s in body["fuel_stops"]] == pytest.approx([20.0, 40.0])
    assert totals["tank_gallons_at_destination"] == pytest.approx(0.0)


@pytest.mark.django_db
def test_far_off_route_stop_is_excluded_at_the_default_tolerance(api):
    body = get(api).json()
    names = [s["name"] for s in body["fuel_stops"]]
    assert "FAR OFF ROUTE" not in names


@pytest.mark.django_db
def test_widening_the_detour_reaches_the_cheap_far_stop(api):
    """At a 45 mi tolerance the $1.00 pump 40 mi off route becomes usable, and the
    total must fall. This is the corridor parameter actually changing the answer."""
    narrow = get(api).json()["totals"]["total_fuel_cost_usd"]
    wide = get(api, max_detour_miles=45).json()
    assert "FAR OFF ROUTE" in [s["name"] for s in wide["fuel_stops"]]
    assert wide["totals"]["total_fuel_cost_usd"] < narrow


@pytest.mark.django_db
def test_stop_payload_carries_every_documented_field(api):
    stop = get(api).json()["fuel_stops"][0]
    for field in (
        "order", "opis_truckstop_id", "name", "address", "city", "state",
        "latitude", "longitude", "price_usd_per_gallon", "gallons_purchased",
        "cost_usd", "distance_along_route_miles", "detour_miles",
        "tank_gallons_on_arrival", "tank_gallons_on_departure",
    ):
        assert field in stop, f"missing {field}"


@pytest.mark.django_db
def test_assumptions_are_returned_and_state_the_basis(api):
    assumptions = get(api).json()["assumptions"]
    assert assumptions["tank_capacity_gallons"] == 50.0
    assert assumptions["miles_per_gallon"] == 10.0
    assert assumptions["vehicle_range_miles"] == 500.0
    assert assumptions["start_tank_gallons"] == 0.0
    assert assumptions["max_detour_miles"] == 10.0
    assert "median" in assumptions["price_basis"]
    assert "not a live price" in assumptions["price_basis_note"]
    assert "centroid" in assumptions["geocoding_precision_note"]
    assert assumptions["currency"] == "USD"


@pytest.mark.django_db
def test_timings_separate_local_compute_from_external(api):
    timings = get(api).json()["diagnostics"]["timings_ms"]
    for key in ("corridor_search", "optimisation", "external_ms", "local_compute_ms", "total_ms"):
        assert key in timings
    assert timings["local_compute_ms"] >= 0
    assert timings["total_ms"] >= timings["local_compute_ms"]


# --------------------------------------------------------------------------
# Geometry modes
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_geometry_none_omits_the_geometry(api):
    assert "geometry" not in get(api, geometry="none").json()["route"]


@pytest.mark.django_db
def test_geometry_full_is_denser_than_simplified(api):
    full = get(api, geometry="full").json()["route"]
    simplified = get(api, geometry="simplified").json()["route"]
    assert full["geometry_point_count"] > simplified["geometry_point_count"]
    assert full["geometry"]["type"] == "LineString"
    # GeoJSON is [lon, lat], and the synthetic route sits on the -100 meridian.
    assert simplified["geometry"]["coordinates"][0][0] == pytest.approx(-100.0)


@pytest.mark.django_db
def test_simplified_geometry_keeps_the_final_point(api):
    route = get(api, geometry="simplified").json()["route"]
    last = route["geometry"]["coordinates"][-1]
    assert last[1] == pytest.approx(35.0 + 600.0 / 69.09341898553099, abs=1e-4)


# --------------------------------------------------------------------------
# POST, and parameter handling
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_post_body_behaves_like_get(api):
    get_body = get(api).json()
    response = api.post(
        ENDPOINT,
        {"start": "39.0,-100.0", "finish": "41.0,-100.0", "geometry": "none"},
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.json()["totals"] == get_body["totals"]


@pytest.mark.django_db
def test_start_tank_reduces_the_total(api):
    empty = get(api).json()["totals"]
    full = get(api, start_tank_gallons=50).json()["totals"]
    assert full["total_gallons_purchased"] < empty["total_gallons_purchased"]
    assert full["total_fuel_cost_usd"] < empty["total_fuel_cost_usd"]


@pytest.mark.django_db
def test_free_text_locations_resolve_without_a_geocoding_call(api):
    body = get(api, start="Dallas, TX", finish="Chicago, IL").json()
    assert body["origin"]["resolution_source"] == "place_index"
    assert body["destination"]["resolution_source"] == "place_index"
    assert body["diagnostics"]["external_calls"]["geocoding_api"] == 0


# --------------------------------------------------------------------------
# Errors: all RFC 7807
# --------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize(
    "params,status,code",
    [
        ({"start": ""}, 400, "invalid_request"),
        ({"start": "   "}, 400, "invalid_request"),
        ({"max_detour_miles": 500}, 400, "invalid_request"),
        ({"max_detour_miles": -1}, 400, "invalid_request"),
        ({"start_tank_gallons": 999}, 400, "invalid_request"),
        ({"geometry": "wibble"}, 400, "invalid_request"),
        ({"start": "51.5074,-0.1278"}, 400, "outside_us"),
        ({"start": "Nowhereville, ZZ"}, 400, "unresolvable_location"),
        ({"start": "Springfield"}, 400, "ambiguous_location"),
    ],
)
def test_bad_requests_return_problem_documents(api, params, status, code):
    response = get(api, **params)
    assert response.status_code == status
    assert response["Content-Type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == code
    assert body["status"] == status
    assert body["type"].startswith("https://")
    assert body["detail"]


@pytest.mark.django_db
def test_missing_required_field_names_the_field(api):
    response = api.get(ENDPOINT, {"finish": "41.0,-100.0"})
    assert response.status_code == 400
    assert "start" in response.json()["errors"]


@pytest.mark.django_db
def test_disallowed_method_is_also_a_problem_document(api):
    response = api.delete(ENDPOINT)
    assert response.status_code == 405
    assert response["Content-Type"].startswith("application/problem+json")
    assert response.json()["code"] == "method_not_allowed"


@pytest.mark.django_db
def test_infeasible_route_returns_422_naming_the_gap(api, monkeypatch, fake_provider):
    """A 4,000 mi route over stops that stop at mile 500 leaves a 3,500 mi tail."""
    monkeypatch.setattr(
        "fuelroute.planner.default_route_provider",
        lambda: fake_provider(meridian_route(4000.0)),
    )
    response = get(api)
    assert response.status_code == 422
    assert response["Content-Type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "gap_exceeds_range"
    gap = body["infeasibility"]
    assert gap["gap_miles"] > 500
    assert gap["vehicle_range_miles"] == 500.0
    assert gap["from"]["label"] == "LAST PUMP"
    assert gap["to"]["label"] == "route destination"
    # The assumptions must come back too: infeasibility is relative to them.
    assert body["assumptions"]["vehicle_range_miles"] == 500.0


@pytest.mark.django_db
def test_no_stops_in_corridor_reports_its_own_reason(api):
    """A 0.01 mi corridor finds nothing, which is a different failure from a gap."""
    response = get(api, max_detour_miles=0.1)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "no_fuel_stops_on_route"
    assert "No usable fuel stop" in body["detail"]


@pytest.mark.django_db
def test_routing_provider_failure_is_a_502_not_a_500(api, monkeypatch):
    from services.routing import RoutingError

    class Broken:
        def fetch(self, origin, destination):
            raise RoutingError("upstream exploded")

    monkeypatch.setattr("fuelroute.planner.default_route_provider", lambda: Broken())
    response = get(api)
    assert response.status_code == 502
    assert response.json()["dependency"] == "osrm"


@pytest.mark.django_db
def test_no_route_found_is_422(api, monkeypatch):
    from services.routing import NoRouteFoundError

    class NoRoute:
        def fetch(self, origin, destination):
            raise NoRouteFoundError("nope")

    monkeypatch.setattr("fuelroute.planner.default_route_provider", lambda: NoRoute())
    assert get(api).status_code == 422


@pytest.mark.django_db
def test_unseeded_database_returns_503(client, monkeypatch, fake_provider):
    """No stops at all is a service-readiness problem, not the caller's fault."""
    monkeypatch.setattr(
        "fuelroute.planner.default_route_provider",
        lambda: fake_provider(meridian_route(600.0)),
    )
    response = get(client)
    assert response.status_code == 503
    assert response.json()["code"] == "stops_not_seeded"
    assert "seed_stops" in response.json()["detail"]


# --------------------------------------------------------------------------
# Map page
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_map_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.content.decode()
    assert "leaflet" in html
    assert "/static/fuelroute/map.js" in html
    # Subresource integrity must be present on both CDN assets.
    assert html.count("integrity=\"sha512-") == 2
