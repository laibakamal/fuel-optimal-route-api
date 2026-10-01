"""Location resolution: coordinate parsing, city/state splitting, offline lookup.

These run against the real committed place artefact, which is a local file read
with no network, so they test what production actually uses.
"""
from __future__ import annotations

import pytest
from django.conf import settings

from services.places import (
    LocationError,
    PlaceIndex,
    resolve_location,
    split_city_state,
)


@pytest.fixture(scope="module")
def index():
    return PlaceIndex.from_artefact(settings.PLACES_ARTEFACT)


# --------------------------------------------------------------------------
# Coordinate input
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,lat,lon",
    [
        ("32.7767,-96.7970", 32.7767, -96.7970),
        ("32.7767, -96.7970", 32.7767, -96.7970),
        ("  32.7767 -96.7970  ", 32.7767, -96.7970),
        ("40,-100", 40.0, -100.0),
        ("+40.5,-100.5", 40.5, -100.5),
    ],
)
def test_coordinate_forms_parse(index, text, lat, lon):
    resolved = resolve_location(text, index)
    assert resolved.source == "coordinates"
    assert resolved.external_calls == 0
    assert (resolved.lat, resolved.lon) == pytest.approx((lat, lon))


@pytest.mark.parametrize("text", ["51.5074,-0.1278", "19.4326,-99.1332", "-33.8,151.2"])
def test_non_us_coordinates_are_rejected(index, text):
    with pytest.raises(LocationError) as exc:
        resolve_location(text, index)
    assert exc.value.code == "outside_us"


def test_impossible_coordinates_are_rejected(index):
    with pytest.raises(LocationError) as exc:
        resolve_location("991,-100", index)
    assert exc.value.code in {"invalid_coordinate", "unresolvable_location"}


# --------------------------------------------------------------------------
# City/state splitting
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,city,state",
    [
        ("Dallas, TX", "DALLAS", "TX"),
        ("Dallas TX", "DALLAS", "TX"),
        ("dallas, texas", "DALLAS", "TX"),
        ("Dallas, TX, USA", "DALLAS", "TX"),
        ("New York, NY", "NEW YORK", "NY"),
        ("New York, New York", "NEW YORK", "NY"),
        ("Salt Lake City, UT", "SALT LAKE CITY", "UT"),
        ("Washington, District of Columbia", "WASHINGTON", "DC"),
        ("St. Louis, MO", "ST LOUIS", "MO"),
        ("Fort Worth, Texas", "FT WORTH", "TX"),
        ("Dallas", "DALLAS", None),
    ],
)
def test_city_state_splitting(text, city, state):
    assert split_city_state(text) == (city, state)


def test_two_word_state_is_not_eaten_as_part_of_the_city():
    """'New York, New York' must not become city='NEW YORK NEW', state='YORK'."""
    assert split_city_state("New York, New York") == ("NEW YORK", "NY")


# --------------------------------------------------------------------------
# Offline lookup against the real artefact
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Dallas, TX", "Chicago, IL", "Los Angeles, CA", "New York, NY",
        "Seattle, WA", "Miami, FL", "Houston, TX", "Denver, CO",
        "Phoenix, AZ", "Portland, OR", "Salt Lake City, UT",
        "Minneapolis, MN", "Boston, MA", "Washington, DC", "Detroit, MI",
        "Nashville, TN", "Kansas City, MO", "Las Vegas, NV",
        "Jacksonville, FL", "Memphis, TN", "Indianapolis, IN", "Atlanta, GA",
    ],
)
def test_benchmark_cities_all_resolve_offline(index, text):
    """Every city the benchmark uses must resolve with zero external calls, or the
    benchmark's call accounting would be wrong."""
    resolved = resolve_location(text, index)
    assert resolved.source == "place_index"
    assert resolved.external_calls == 0
    assert 24.0 <= resolved.lat <= 49.6
    assert -125.5 <= resolved.lon <= -66.5


def test_ambiguous_bare_city_names_the_real_states(index):
    with pytest.raises(LocationError) as exc:
        resolve_location("Springfield", index)
    assert exc.value.code == "ambiguous_location"
    message = str(exc.value)
    # Must name states that actually have a Springfield, not an arbitrary one.
    assert "Springfield," in message
    for code in message.replace(",", " ").split():
        if len(code) == 2 and code.isupper():
            assert index.lookup("Springfield", code) is not None


def test_unambiguous_bare_city_resolves(index):
    resolved = resolve_location("Kalamazoo", index)
    assert resolved.source == "place_index"


def test_unknown_place_raises(index):
    with pytest.raises(LocationError) as exc:
        resolve_location("Nowhereville, ZZ", index)
    assert exc.value.code == "unresolvable_location"


def test_empty_input_raises(index):
    with pytest.raises(LocationError) as exc:
        resolve_location("   ", index)
    assert exc.value.code == "empty_location"


def test_geocoder_fallback_is_used_only_when_the_index_misses(index):
    calls = []

    def geocoder(city, state):
        calls.append((city, state))
        return (39.0, -100.0)

    # A hit must not call the geocoder.
    resolve_location("Dallas, TX", index, geocoder=geocoder)
    assert calls == []

    # A miss must, and the result must be attributed and counted.
    resolved = resolve_location("Zzyzxville, KS", index, geocoder=geocoder)
    assert calls == [("ZZYZXVILLE", "KS")]
    assert resolved.source == "nominatim"
    assert resolved.external_calls == 1


def test_geocoder_result_outside_the_us_is_rejected(index):
    resolved = None
    with pytest.raises(LocationError):
        resolved = resolve_location(
            "Zzyzxville, KS", index, geocoder=lambda c, s: (51.5, -0.12)
        )
    assert resolved is None
