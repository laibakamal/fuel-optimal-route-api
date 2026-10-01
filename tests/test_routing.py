"""Route parsing and provider behaviour. No network: every response is a literal.

The parser is where a silent data error would do the most damage, because a short
route produces a confidently undercharged total. These tests pin the failure modes
rather than only the happy path.
"""
from __future__ import annotations

import pytest
import requests

from services.geo import METERS_PER_MILE
from services.routing import (
    NoRouteFoundError,
    OsrmRouteProvider,
    RoutingError,
    _parse_route,
    simplify_coordinates,
)


def osrm_payload(coordinates, segment_metres, total_metres=None, code="Ok"):
    return {
        "code": code,
        "routes": [
            {
                "distance": total_metres if total_metres is not None else sum(segment_metres),
                "duration": 3600.0,
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "legs": [{"annotation": {"distance": segment_metres}}],
            }
        ],
    }


def test_cumulative_mileage_is_the_prefix_sum_of_the_annotations():
    coordinates = [[-100.0, 35.0], [-100.0, 36.0], [-100.0, 37.0], [-100.0, 38.0]]
    segments = [1609.344, 3218.688, 1609.344]  # 1, 2, 1 miles
    route = _parse_route(osrm_payload(coordinates, segments), "test", 1)
    assert [p.mile for p in route.points] == pytest.approx([0.0, 1.0, 3.0, 4.0])
    assert route.total_miles == pytest.approx(4.0)


def test_latitude_and_longitude_are_not_transposed():
    """GeoJSON is [lon, lat]. Swapping them would put the whole route in the wrong
    hemisphere, so it is worth one explicit test."""
    route = _parse_route(
        osrm_payload([[-96.797, 32.776], [-87.63, 41.878]], [1609.344]), "test", 1
    )
    assert route.points[0].lat == pytest.approx(32.776)
    assert route.points[0].lon == pytest.approx(-96.797)
    # And the GeoJSON helper must put them back the other way round.
    assert route.geojson_coordinates[0] == [-96.797, 32.776]


def test_provider_distance_is_preferred_over_our_prefix_sum():
    """The provider's total is authoritative. We should report it even if our own
    sum differs slightly, rather than quietly substituting our arithmetic."""
    route = _parse_route(
        osrm_payload([[-100.0, 35.0], [-100.0, 36.0]], [1609.344], total_metres=5000.0),
        "test", 1,
    )
    assert route.total_miles == pytest.approx(5000.0 / METERS_PER_MILE)


def test_missing_distance_annotations_is_a_hard_error():
    """Without per-segment distances we would have to re-measure the geometry with
    haversine, which chords curves and under-reports. Failing loudly is correct:
    a short route means an undercharged total."""
    payload = {
        "code": "Ok",
        "routes": [{
            "distance": 1000.0, "duration": 60.0,
            "geometry": {"type": "LineString", "coordinates": [[-100.0, 35.0], [-100.0, 36.0]]},
            "legs": [{}],
        }],
    }
    with pytest.raises(RoutingError, match="annotations"):
        _parse_route(payload, "test", 1)


def test_annotation_count_mismatch_is_a_hard_error():
    coordinates = [[-100.0, 35.0], [-100.0, 36.0], [-100.0, 37.0]]
    with pytest.raises(RoutingError, match="annotations"):
        _parse_route(osrm_payload(coordinates, [1609.344]), "test", 1)


def test_degenerate_geometry_is_rejected():
    with pytest.raises(RoutingError, match="degenerate"):
        _parse_route(osrm_payload([[-100.0, 35.0]], []), "test", 1)


def test_multi_leg_annotations_are_concatenated():
    payload = {
        "code": "Ok",
        "routes": [{
            "distance": 3 * 1609.344, "duration": 60.0,
            "geometry": {"type": "LineString",
                         "coordinates": [[-100.0, 35.0], [-100.0, 36.0],
                                         [-100.0, 37.0], [-100.0, 38.0]]},
            "legs": [{"annotation": {"distance": [1609.344]}},
                     {"annotation": {"distance": [1609.344, 1609.344]}}],
        }],
    }
    route = _parse_route(payload, "test", 1)
    assert [p.mile for p in route.points] == pytest.approx([0.0, 1.0, 2.0, 3.0])


# --------------------------------------------------------------------------
# Provider: failover and call counting
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    """Returns queued results in order. Each entry is a payload or an Exception."""

    def __init__(self, results):
        self.results = list(results)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


def payload_ok():
    return osrm_payload([[-100.0, 35.0], [-100.0, 36.0]], [1609.344])


def test_happy_path_makes_exactly_one_call():
    session = FakeSession([payload_ok()])
    provider = OsrmRouteProvider("https://primary", "https://fallback", session=session)
    route = provider.fetch((35.0, -100.0), (36.0, -100.0))
    assert route.external_calls == 1
    assert len(session.urls) == 1
    assert session.urls[0].startswith("https://primary")


def test_coordinates_are_sent_as_lon_lat():
    session = FakeSession([payload_ok()])
    provider = OsrmRouteProvider("https://primary", session=session)
    provider.fetch((32.7767, -96.7970), (41.8781, -87.6298))
    # OSRM path order is lon,lat;lon,lat.
    assert "-96.797000,32.776700;-87.629800,41.878100" in session.urls[0]


def test_falls_back_to_the_second_endpoint_and_counts_both_calls():
    session = FakeSession([requests.ConnectionError("down"), payload_ok()])
    provider = OsrmRouteProvider("https://primary", "https://fallback", session=session)
    route = provider.fetch((35.0, -100.0), (36.0, -100.0))
    assert route.external_calls == 2, "a fallback attempt must be counted"
    assert route.provider == "osrm-fossgis"
    assert session.urls[1].startswith("https://fallback")


def test_no_route_does_not_retry_the_fallback():
    """'NoRoute' is a definitive answer, not a transport failure. Retrying would
    waste a call on a service that will say the same thing."""
    session = FakeSession([{"code": "NoRoute", "routes": []}])
    provider = OsrmRouteProvider("https://primary", "https://fallback", session=session)
    with pytest.raises(NoRouteFoundError):
        provider.fetch((35.0, -100.0), (36.0, -100.0))
    assert len(session.urls) == 1


def test_all_endpoints_failing_raises():
    session = FakeSession([requests.ConnectionError("a"), requests.ConnectionError("b")])
    provider = OsrmRouteProvider("https://primary", "https://fallback", session=session)
    with pytest.raises(RoutingError, match="All routing providers failed"):
        provider.fetch((35.0, -100.0), (36.0, -100.0))


def test_request_asks_for_distance_annotations():
    """The one-call design depends on this parameter. If it were ever dropped the
    parser would raise, but asserting it here localises the failure."""
    session = FakeSession([payload_ok()])
    captured = {}

    def capture(url, **kwargs):
        captured.update(kwargs.get("params", {}))
        return FakeResponse(payload_ok())

    session.get = capture
    provider = OsrmRouteProvider("https://primary", session=session)
    provider.fetch((35.0, -100.0), (36.0, -100.0))
    assert captured["annotations"] == "distance"
    assert captured["overview"] == "full"
    assert captured["geometries"] == "geojson"


# --------------------------------------------------------------------------
# Simplification
# --------------------------------------------------------------------------

def test_simplify_keeps_first_and_last_and_rounds():
    coordinates = [[-100.0 + i / 100000.0, 35.0] for i in range(100)]
    kept = simplify_coordinates(coordinates, every_nth=10)
    assert kept[0] == [round(coordinates[0][0], 5), 35.0]
    assert kept[-1] == [round(coordinates[-1][0], 5), 35.0]
    assert len(kept) <= 12


def test_simplify_with_every_nth_one_only_rounds():
    coordinates = [[-100.123456789, 35.987654321]]
    assert simplify_coordinates(coordinates, every_nth=1) == [[-100.12346, 35.98765]]
