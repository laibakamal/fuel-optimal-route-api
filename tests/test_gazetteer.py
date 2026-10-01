"""The three-tier offline geocoder.

Tested against synthetic archives built in tmp_path rather than the real 70 MiB
downloads, so these run offline and fast while still exercising the actual parsers
and the tier-ordering logic that resolves 100% of US stops.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from services import gazetteer
from services.gazetteer import (
    SOURCE_CENSUS,
    SOURCE_GEONAMES,
    SOURCE_GEONAMES_LANDMARK,
    Candidate,
    GazetteerIndex,
    build_place_records,
    load_census_places,
    load_geonames,
)

CENSUS_HEADER = (
    "USPS|GEOID|GEOIDFQ|ANSICODE|NAME|LSAD|FUNCSTAT|ALAND|AWATER|"
    "ALAND_SQMI|AWATER_SQMI|INTPTLAT|INTPTLONG"
)


def census_row(state, name, lat, lon, land_sqmi=1.0):
    return (
        f"{state}|0100100|1600000US0100100|02582661|{name}|25|A|1000|0|"
        f"{land_sqmi}|0.0|{lat}|{lon}"
    )


def geonames_row(name, state, lat, lon, feature_class="P", feature_code="PPL", population=0):
    columns = [""] * 19
    columns[0] = "1"
    columns[1] = name
    columns[2] = name
    columns[4] = str(lat)
    columns[5] = str(lon)
    columns[6] = feature_class
    columns[7] = feature_code
    columns[8] = "US"
    columns[10] = state
    columns[14] = str(population)
    return "\t".join(columns)


@pytest.fixture
def census_zip(tmp_path: Path) -> Path:
    rows = [
        CENSUS_HEADER,
        census_row("TX", "Dallas city", 32.7933, -96.7665, 340.0),
        census_row("KS", "Kansas City city", 39.1155, -94.6267, 124.0),
        census_row("AL", "Abanda CDP", 33.0916, -85.5270, 3.0),
        census_row("MI", "Sault Ste. Marie city", 46.4817, -84.3453, 15.0),
        census_row("AL", "McCalla CDP", 33.3, -87.0, 5.0),
        # Two same-named places far apart, to exercise ambiguity reporting.
        census_row("MO", "Springfield city", 37.1944, -93.2919, 82.0),
        census_row("IL", "Springfield city", 39.7639, -89.6708, 60.0),
    ]
    path = tmp_path / "census.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(gazetteer.CENSUS_MEMBER, "\n".join(rows))
    return path


@pytest.fixture
def geonames_zip(tmp_path: Path) -> Path:
    rows = [
        geonames_row("Breezewood", "PA", 39.9987, -78.2461, population=200),
        geonames_row("Bigtown", "PA", 40.0, -78.0, population=5000),
        # Historical places must be ignored.
        geonames_row("Ghosttown", "PA", 41.0, -79.0, feature_code="PPLQ", population=0),
        # Non-US rows must be ignored.
        geonames_row("Toronto", "ON", 43.65, -79.38, population=2000000),
        # Landmarks: the post office is the tier-3 signal.
        geonames_row("Crescent Post Office", "PA", 40.5598, -80.2235,
                     feature_class="S", feature_code="PO"),
        geonames_row("Hot Springs National Park", "AR", 34.5143, -93.0508,
                     feature_class="L", feature_code="PRK"),
        # A school named after the same place must NOT become a tier-3 signal.
        geonames_row("Crescent Elementary School", "PA", 40.4542, -79.8808,
                     feature_class="S", feature_code="SCH"),
    ]
    path = tmp_path / "geonames.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(gazetteer.GEONAMES_MEMBER, "\n".join(rows))
    return path


# --------------------------------------------------------------------------
# Census tier
# --------------------------------------------------------------------------

def test_census_indexes_both_full_and_suffix_stripped_names(census_zip):
    index = load_census_places(census_zip)
    assert ("DALLAS", "TX") in index
    assert ("DALLAS CITY", "TX") in index


def test_census_does_not_over_strip_kansas_city(census_zip):
    """'Kansas City city' must index as KANSAS CITY, never reduced to KANSAS."""
    index = load_census_places(census_zip)
    assert ("KANSAS CITY", "KS") in index
    assert ("KANSAS", "KS") not in index


def test_census_handles_cdp_and_abbreviations(census_zip):
    index = load_census_places(census_zip)
    assert ("ABANDA", "AL") in index
    assert ("SAULT ST MARIE", "MI") in index
    assert ("MCCALLA", "AL") in index


# --------------------------------------------------------------------------
# GeoNames tiers
# --------------------------------------------------------------------------

def test_geonames_splits_populated_places_from_landmarks(geonames_zip):
    populated, landmarks = load_geonames(geonames_zip)
    assert ("BREEZEWOOD", "PA") in populated
    assert ("CRESCENT", "PA") in landmarks
    assert ("HOT SPRINGS NATIONAL PARK", "AR") in landmarks


def test_geonames_excludes_historical_places(geonames_zip):
    populated, _ = load_geonames(geonames_zip)
    assert ("GHOSTTOWN", "PA") not in populated


def test_geonames_excludes_non_us_rows(geonames_zip):
    populated, _ = load_geonames(geonames_zip)
    assert ("TORONTO", "ON") not in populated


def test_landmark_tier_excludes_schools_and_buildings(geonames_zip):
    """'Crescent Elementary School' is weak evidence of where Crescent is;
    'Crescent Post Office' is strong. Only the latter may be indexed."""
    _, landmarks = load_geonames(geonames_zip)
    candidates = landmarks[("CRESCENT", "PA")]
    assert len(candidates) == 1
    assert candidates[0].lat == pytest.approx(40.5598)


# --------------------------------------------------------------------------
# Tier ordering
# --------------------------------------------------------------------------

def test_tiers_are_consulted_in_order(census_zip, geonames_zip):
    census = load_census_places(census_zip)
    populated, landmarks = load_geonames(geonames_zip)
    index = GazetteerIndex([
        (SOURCE_CENSUS, census),
        (SOURCE_GEONAMES, populated),
        (SOURCE_GEONAMES_LANDMARK, landmarks),
    ])
    assert index.resolve("Dallas", "TX").source == SOURCE_CENSUS
    assert index.resolve("Breezewood", "PA").source == SOURCE_GEONAMES
    assert index.resolve("Crescent", "PA").source == SOURCE_GEONAMES_LANDMARK
    assert index.resolve("Nowhere", "ZZ") is None


def test_census_wins_when_a_name_is_in_both_tiers(census_zip, geonames_zip):
    census = load_census_places(census_zip)
    populated, _ = load_geonames(geonames_zip)
    # Add a competing GeoNames entry for Dallas, TX at a wrong location.
    populated[("DALLAS", "TX")] = [Candidate(1.0, 1.0, 999999.0)]
    index = GazetteerIndex([(SOURCE_CENSUS, census), (SOURCE_GEONAMES, populated)])
    hit = index.resolve("Dallas", "TX")
    assert hit.source == SOURCE_CENSUS
    assert hit.lat == pytest.approx(32.7933)


def test_crescent_pa_resolves_to_allegheny_county_not_philadelphia(geonames_zip):
    """The regression this tier exists for: Nominatim placed Crescent, PA 270 mi
    away near Chester. The post-office landmark puts it in Allegheny County."""
    _, landmarks = load_geonames(geonames_zip)
    index = GazetteerIndex([(SOURCE_GEONAMES_LANDMARK, landmarks)])
    hit = index.resolve("Crescent", "PA")
    assert hit.lat == pytest.approx(40.56, abs=0.02)
    assert hit.lon == pytest.approx(-80.22, abs=0.02)


# --------------------------------------------------------------------------
# Tie-breaking and ambiguity
# --------------------------------------------------------------------------

def test_largest_candidate_wins_a_tie():
    index = GazetteerIndex([(SOURCE_CENSUS, {
        ("TWINS", "TX"): [Candidate(30.0, -97.0, 5.0), Candidate(31.0, -98.0, 500.0)],
    })])
    assert index.resolve("Twins", "TX").lat == pytest.approx(31.0)


def test_ambiguity_distance_is_reported(census_zip):
    """Two Springfields in different states are not ambiguous for a (city, state)
    lookup; ambiguity is only within one state."""
    index = GazetteerIndex([(SOURCE_CENSUS, load_census_places(census_zip))])
    assert index.resolve("Springfield", "MO").ambiguity_miles == pytest.approx(0.0)


def test_ambiguity_flag_trips_past_the_threshold():
    far = GazetteerIndex([(SOURCE_CENSUS, {
        ("X", "TX"): [Candidate(30.0, -97.0, 1.0), Candidate(35.0, -97.0, 2.0)],
    })])
    hit = far.resolve("X", "TX")
    assert hit.ambiguity_miles > gazetteer.AMBIGUITY_THRESHOLD_MILES
    assert hit.is_ambiguous

    near = GazetteerIndex([(SOURCE_CENSUS, {
        ("Y", "TX"): [Candidate(30.0, -97.0, 1.0), Candidate(30.01, -97.0, 2.0)],
    })])
    assert not near.resolve("Y", "TX").is_ambiguous


def test_empty_tiers_are_dropped():
    index = GazetteerIndex([(SOURCE_CENSUS, {}), (SOURCE_GEONAMES, {
        ("A", "TX"): [Candidate(30.0, -97.0, 1.0)]
    })])
    assert index.resolve("A", "TX").source == SOURCE_GEONAMES


# --------------------------------------------------------------------------
# Place index for user input
# --------------------------------------------------------------------------

def test_place_records_include_census_and_populous_geonames(census_zip, geonames_zip):
    records = build_place_records(census_zip, geonames_zip)
    names = {(name, state) for name, state, _, _ in records}
    assert ("Dallas city", "TX") in names
    # Bigtown has population 5000, above the threshold.
    assert ("Bigtown", "PA") in names
    # Breezewood has population 200, below it.
    assert ("Breezewood", "PA") not in names
    # Landmarks and non-US rows never belong in the user-input index.
    assert ("Crescent Post Office", "PA") not in names
    assert ("Toronto", "ON") not in names


def test_place_records_work_without_geonames(census_zip):
    records = build_place_records(census_zip, None)
    assert records
    assert all(state != "ON" for _, state, _, _ in records)
