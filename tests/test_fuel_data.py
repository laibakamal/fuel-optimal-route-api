"""CSV parsing, deduplication, price aggregation and name normalisation.

Run against the real supplied CSV where the expected numbers come from it, so a
change to the source file that would alter the documented match rate shows up as a
test failure rather than a quiet difference.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest
from django.conf import settings

from services.fuel_data import (
    PRICE_BASIS_MEAN,
    PRICE_BASIS_MEDIAN,
    PRICE_BASIS_MIN,
    CsvSchemaError,
    RawStop,
    parse_fuel_csv,
)
from services.normalize import country_for_state, normalize_place, place_keys


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Effingham                    ", "EFFINGHAM"),
        ("St. Louis", "ST LOUIS"),
        ("Saint Johns", "ST JOHNS"),
        ("Sainte Genevieve", "ST GENEVIEVE"),
        ("Fort Worth", "FT WORTH"),
        ("Mount Vernon", "MT VERNON"),
        ("O'Fallon", "O FALLON"),
        ("Winston-Salem", "WINSTON SALEM"),
        ("  double   spaces  ", "DOUBLE SPACES"),
    ],
)
def test_normalize_place(raw, expected):
    assert normalize_place(raw) == expected


def test_lsad_suffix_is_stripped_once_not_repeatedly():
    """'Kansas City city' must yield 'KANSAS CITY'. Stripping repeatedly would
    reduce it to 'KANSAS' and lose the match - a bug this caught during Phase 1."""
    keys = place_keys("Kansas City city")
    assert "KANSAS CITY" in keys
    assert "KANSAS" not in keys


def test_despaced_variant_is_indexed():
    """OPIS writes 'Mc Calla', Census writes 'McCalla'."""
    assert place_keys("Mc Calla") & place_keys("McCalla")


def test_sainte_is_normalised_so_sault_sainte_marie_matches():
    assert place_keys("Sault Sainte Marie") & place_keys("Sault Ste. Marie city")


@pytest.mark.parametrize(
    "code,country",
    [("TX", "US"), ("DC", "US"), ("ON", "CA"), ("AB", "CA"), ("ZZ", "XX"), ("", "XX")],
)
def test_country_for_state(code, country):
    assert country_for_state(code) == country


# --------------------------------------------------------------------------
# Price aggregation
# --------------------------------------------------------------------------

def test_price_bases():
    stop = RawStop("1", "N", "A", "C", "TX", "1", "US", prices=[3.0, 4.0, 10.0])
    assert stop.price(PRICE_BASIS_MEDIAN) == pytest.approx(4.0)
    assert stop.price(PRICE_BASIS_MEAN) == pytest.approx(5.6667, abs=1e-4)
    assert stop.price(PRICE_BASIS_MIN) == pytest.approx(3.0)


def test_median_resists_an_outlier_that_drags_the_mean():
    stop = RawStop("1", "N", "A", "C", "TX", "1", "US", prices=[3.0, 3.0, 3.0, 9.0])
    assert stop.price(PRICE_BASIS_MEDIAN) == pytest.approx(3.0)
    assert stop.price(PRICE_BASIS_MEAN) == pytest.approx(4.5)


# --------------------------------------------------------------------------
# Parsing the real file
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def parsed():
    return parse_fuel_csv(settings.FUEL_CSV)


def test_real_csv_row_and_dedup_counts(parsed):
    """These are the numbers the README and ARCHITECTURE documents quote. If the
    source file changes, this fails rather than letting the docs go stale."""
    stops, stats = parsed
    assert stats["rows_read"] == 8151
    assert stats["distinct_stops"] == 6738
    assert stats["stops_with_multiple_observations"] == 678
    assert stats["rows_skipped_bad_price"] == 0
    assert stats["rows_skipped_missing_id"] == 0


def test_opis_id_alone_is_a_sufficient_dedup_key():
    """The documented justification: adding Address, City or State to the ID does
    not change the group count, so the ID functionally determines location."""
    rows = list(csv.DictReader(
        settings.FUEL_CSV.open(newline="", encoding="utf-8-sig")
    ))
    def norm(value):
        return " ".join((value or "").split()).upper()

    by_id = {norm(r["OPIS Truckstop ID"]) for r in rows}
    by_id_addr = {(norm(r["OPIS Truckstop ID"]), norm(r["Address"])) for r in rows}
    by_id_city_state = {
        (norm(r["OPIS Truckstop ID"]), norm(r["City"]), norm(r["State"])) for r in rows
    }
    assert len(by_id) == len(by_id_addr) == len(by_id_city_state) == 6738


def test_country_split_of_the_real_file(parsed):
    stops, _ = parsed
    counts: dict[str, int] = {}
    for stop in stops.values():
        counts[stop.country] = counts.get(stop.country, 0) + 1
    assert counts == {"US": 6626, "CA": 112}


def test_city_padding_is_stripped(parsed):
    stops, _ = parsed
    assert all(stop.city == stop.city.strip() for stop in stops.values())
    assert any(stop.city == "Effingham" for stop in stops.values())


def test_repeated_ids_collect_their_observations(parsed):
    stops, _ = parsed
    # Stop 105 carries six price readings in the supplied file.
    assert stops["105"].observation_count == 6
    assert stops["105"].name == "TA SAGINAW I 75 TRAVEL CENTER"


def test_some_stops_legitimately_repeat_an_identical_price(parsed):
    """Why there is no unique constraint on (stop, price): a uniqueness rule would
    silently discard real observations."""
    stops, _ = parsed
    repeated = [
        s for s in stops.values()
        if len(s.prices) != len(set(s.prices))
    ]
    assert len(repeated) > 0


def test_unexpected_header_is_rejected(tmp_path: Path):
    bad = tmp_path / "bad.csv"
    bad.write_text("id,name,price\n1,x,3.00\n")
    with pytest.raises(CsvSchemaError, match="missing expected column"):
        parse_fuel_csv(bad)


def test_bad_and_nonpositive_prices_are_counted_not_silently_dropped(tmp_path: Path):
    path = tmp_path / "f.csv"
    path.write_text(
        "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
        "1,A,ADDR,City,TX,1,3.00\n"
        "2,B,ADDR,City,TX,1,notanumber\n"
        "3,C,ADDR,City,TX,1,0\n"
        "4,D,ADDR,City,TX,1,-1.5\n"
    )
    stops, stats = parse_fuel_csv(path)
    assert stats["rows_read"] == 4
    assert stats["rows_skipped_bad_price"] == 3
    assert set(stops) == {"1"}
