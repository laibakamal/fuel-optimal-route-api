"""Shared test fixtures.

The network block is the important one. "No test hits the network" is easy to
intend and easy to break by accident, so it is enforced at the socket layer
rather than trusted: any test that tries to open a connection fails with a clear
message naming this fixture.
"""
from __future__ import annotations

import socket
from decimal import Decimal

import pytest

from services.geo import MILES_PER_DEGREE_LAT
from services.routing import Route, RoutePoint


class NetworkAccessAttempted(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Fail any test that attempts an outbound connection.

    Patches the connection *attempt* rather than the whole socket module, so that
    unrelated uses of socket (hostname lookups inside Django, for instance) keep
    working and only real egress is stopped.
    """

    def deny(*args, **kwargs):
        raise NetworkAccessAttempted(
            "A test tried to open a network connection. Tests must mock the "
            "routing provider; see tests/conftest.py::block_network."
        )

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    yield


@pytest.fixture(autouse=True)
def fresh_indexes():
    """Drop the process-level index singletons around every test.

    Without this, the first test to build an index would pin its data for the
    whole session and later tests would silently assert against stale stops.
    """
    from fuelroute.indexes import reset_indexes

    # stops_only: the place index is immutable data from a committed artefact, so
    # reloading it per test would add ~250 ms each for no possible change in
    # behaviour. The stop index must be rebuilt because the database changes.
    reset_indexes(stops_only=True)
    yield
    reset_indexes(stops_only=True)


@pytest.fixture(autouse=True)
def clear_route_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
# Synthetic geometry: a route due north along a meridian.
#
# A meridian is the one direction where distance is exactly proportional to
# degrees, so expected mileages are computable by hand: one degree of latitude is
# MILES_PER_DEGREE_LAT, everywhere. Anything along a parallel would need a cosine
# and make every expected value an approximation.
# ---------------------------------------------------------------------------

MERIDIAN_LON = -100.0
MERIDIAN_START_LAT = 35.0


def meridian_route(length_miles: float, step_miles: float = 0.5) -> Route:
    """A straight route running north from (35, -100) for `length_miles`."""
    points: list[RoutePoint] = []
    mile = 0.0
    while mile < length_miles:
        points.append(
            RoutePoint(
                lat=MERIDIAN_START_LAT + mile / MILES_PER_DEGREE_LAT,
                lon=MERIDIAN_LON,
                mile=mile,
            )
        )
        mile += step_miles
    points.append(
        RoutePoint(
            lat=MERIDIAN_START_LAT + length_miles / MILES_PER_DEGREE_LAT,
            lon=MERIDIAN_LON,
            mile=length_miles,
        )
    )
    return Route(
        points=points,
        total_miles=length_miles,
        duration_seconds=length_miles * 60.0,
        provider="synthetic",
        external_calls=0,
    )


def point_at(mile: float, east_miles: float = 0.0) -> tuple[float, float]:
    """Coordinates `mile` along the meridian route and `east_miles` to its east."""
    import math

    lat = MERIDIAN_START_LAT + mile / MILES_PER_DEGREE_LAT
    miles_per_degree_lon = MILES_PER_DEGREE_LAT * math.cos(math.radians(lat))
    return lat, MERIDIAN_LON + east_miles / miles_per_degree_lon


@pytest.fixture
def fake_provider():
    """A RouteProvider that returns a fixed Route and counts its calls."""

    class FakeProvider:
        def __init__(self, route: Route | None = None) -> None:
            self.route = route or meridian_route(600.0)
            self.calls = 0
            #: OsrmRouteProvider exposes this so FixtureBackedProvider can record
            #: a fixture. A fake without it silently defeats the fixture layer,
            #: which is how a benchmark assertion went wrong once.
            self.last_payload = {"synthetic": True}

        def fetch(self, origin, destination):
            self.calls += 1
            return Route(
                points=self.route.points,
                total_miles=self.route.total_miles,
                duration_seconds=self.route.duration_seconds,
                provider="fake",
                external_calls=1,
            )

    return FakeProvider


@pytest.fixture
def seeded_stops(db):
    """A handful of stops placed on the synthetic meridian route.

    Deliberately tiny and hand-placed: an assertion against six known stops is
    checkable by a reader, whereas one against the real 6,626 is not.
    """
    from fuelroute.models import PriceObservation, TruckStop

    specs = [
        # (mile along route, miles east of route, price, name, city, state)
        (0.0, 0.5, "3.00", "ORIGIN TRUCK STOP", "Alpha", "KS"),
        (100.0, 1.0, "4.00", "DEAR PUMP", "Bravo", "KS"),
        (200.0, 2.0, "2.50", "CHEAP PUMP", "Charlie", "NE"),
        (300.0, 3.0, "3.50", "MIDDLING PUMP", "Delta", "NE"),
        (450.0, 40.0, "1.00", "FAR OFF ROUTE", "Echo", "NE"),
        (500.0, 1.5, "3.25", "LAST PUMP", "Foxtrot", "SD"),
    ]
    created = []
    for index, (mile, east, price, name, city, state) in enumerate(specs, start=1):
        lat, lon = point_at(mile, east)
        stop = TruckStop.objects.create(
            opis_id=f"T{index:04d}",
            name=name,
            address=f"I-00, EXIT {index}",
            city=city,
            state=state,
            country=TruckStop.Country.US,
            rack_id="1",
            latitude=lat,
            longitude=lon,
            geocode_source=TruckStop.GeocodeSource.CENSUS,
        )
        PriceObservation.objects.create(
            stop=stop, price_usd_per_gallon=Decimal(price)
        )
        created.append(stop)
    return created
