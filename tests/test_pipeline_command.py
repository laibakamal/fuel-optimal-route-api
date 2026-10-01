"""End-to-end tests for the offline pipeline and the benchmark command.

build_fuel_index is a graded deliverable, so it is tested as a whole against
synthetic inputs: a small CSV plus synthetic gazetteer archives, with the
downloader stubbed. That exercises parsing, deduplication, the tier cascade, bbox
validation, exclusion reporting and artefact writing together, which is where the
interesting bugs live.
"""
from __future__ import annotations

import gzip
import json
import zipfile
from pathlib import Path

import pytest
from django.core.management import call_command

from services import gazetteer
from tests.test_gazetteer import CENSUS_HEADER, census_row, geonames_row

CSV_HEADER = (
    "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price"
)


@pytest.fixture
def pipeline_inputs(tmp_path: Path, monkeypatch):
    """Synthetic CSV + gazetteers, with the downloader stubbed out."""
    csv_path = tmp_path / "fuel.csv"
    csv_path.write_text(
        "\n".join([
            CSV_HEADER,
            # Resolvable by Census, two price observations for one stop.
            '1,DALLAS STOP,"I-20, EXIT 1",Dallas,TX,10,3.00',
            '1,DALLAS STOP ALT NAME,"I-20, EXIT 1",Dallas,TX,10,3.50',
            # Resolvable only by the GeoNames populated-place tier.
            '2,BREEZEWOOD STOP,"I-70, EXIT 12",Breezewood,PA,20,3.90',
            # Resolvable only by the landmark (post office) tier.
            '3,CRESCENT STOP,ST 51,Crescent,PA,30,3.80',
            # Canadian: out of scope by design, never geocoded.
            '4,ONTARIO STOP,TCH-16,Kitchener,ON,40,4.45',
            # Unresolvable anywhere: must be excluded and named.
            '5,MYSTERY STOP,US-1,Zzyzxville,KS,50,3.10',
            # Bad price: must be skipped and counted.
            '6,BAD PRICE STOP,US-2,Dallas,TX,60,notanumber',
        ])
    )

    census = tmp_path / "census.zip"
    with zipfile.ZipFile(census, "w") as zf:
        zf.writestr(gazetteer.CENSUS_MEMBER, "\n".join([
            CENSUS_HEADER,
            census_row("TX", "Dallas city", 32.7933, -96.7665, 340.0),
        ]))

    geonames = tmp_path / "geonames.zip"
    with zipfile.ZipFile(geonames, "w") as zf:
        zf.writestr(gazetteer.GEONAMES_MEMBER, "\n".join([
            geonames_row("Breezewood", "PA", 39.9987, -78.2461, population=200),
            geonames_row("Crescent Post Office", "PA", 40.5598, -80.2235,
                         feature_class="S", feature_code="PO"),
        ]))

    def fake_download(url, dest, user_agent, force=False):
        return census if "census" in url.lower() else geonames

    monkeypatch.setattr(
        "fuelroute.management.commands.build_fuel_index.download_if_missing",
        fake_download,
    )
    return {
        "csv": csv_path,
        "out": tmp_path / "stops.json.gz",
        "places_out": tmp_path / "places.json.gz",
    }


def run_pipeline(inputs, **extra):
    call_command(
        "build_fuel_index",
        csv=inputs["csv"],
        out=inputs["out"],
        places_out=inputs["places_out"],
        no_geocode_fallback=True,
        verbosity=0,
        **extra,
    )
    with gzip.open(inputs["out"], "rb") as fh:
        return json.load(fh)


def test_pipeline_resolves_through_all_three_tiers(pipeline_inputs):
    artefact = run_pipeline(pipeline_inputs)
    by_id = {s["id"]: s for s in artefact["stops"]}
    assert by_id["1"]["geocode_source"] == "census_gazetteer"
    assert by_id["2"]["geocode_source"] == "geonames"
    assert by_id["3"]["geocode_source"] == "geonames_landmark"


def test_pipeline_deduplicates_on_opis_id_and_keeps_both_prices(pipeline_inputs):
    artefact = run_pipeline(pipeline_inputs)
    by_id = {s["id"]: s for s in artefact["stops"]}
    assert sorted(by_id["1"]["prices"]) == pytest.approx([3.00, 3.50])
    assert artefact["stats"]["name_variants_collapsed"] == 1


def test_pipeline_excludes_non_us_and_reports_it_separately(pipeline_inputs):
    artefact = run_pipeline(pipeline_inputs)
    stats = artefact["stats"]
    assert "4" not in {s["id"] for s in artefact["stops"]}
    out_of_scope = {row["id"] for row in stats["excluded_out_of_scope"]}
    assert out_of_scope == {"4"}
    # The Canadian stop must NOT be reported as an unresolved failure.
    assert all(row["id"] != "4" for row in stats["excluded_unresolved"])


def test_pipeline_names_every_unresolved_stop(pipeline_inputs):
    artefact = run_pipeline(pipeline_inputs)
    unresolved = {row["id"]: row for row in artefact["stats"]["excluded_unresolved"]}
    assert set(unresolved) == {"5"}
    assert unresolved["5"]["city"] == "Zzyzxville"


def test_pipeline_counts_the_bad_price_row(pipeline_inputs):
    artefact = run_pipeline(pipeline_inputs)
    assert artefact["stats"]["rows_skipped_bad_price"] == 1
    assert "6" not in {s["id"] for s in artefact["stops"]}


def test_pipeline_output_is_byte_identical_across_runs(pipeline_inputs):
    """Idempotence. A wall-clock timestamp in the artefact made every rebuild a
    spurious diff; provenance is a hash of the input instead."""
    run_pipeline(pipeline_inputs)
    first = pipeline_inputs["out"].read_bytes()
    run_pipeline(pipeline_inputs)
    assert pipeline_inputs["out"].read_bytes() == first


def test_pipeline_records_the_source_hash_not_a_timestamp(pipeline_inputs):
    import hashlib

    artefact = run_pipeline(pipeline_inputs)
    expected = hashlib.sha256(pipeline_inputs["csv"].read_bytes()).hexdigest()
    assert artefact["source_csv_sha256"] == expected
    assert "generated_at" not in artefact


def test_pipeline_writes_the_place_index(pipeline_inputs):
    run_pipeline(pipeline_inputs)
    with gzip.open(pipeline_inputs["places_out"], "rb") as fh:
        places = json.load(fh)
    assert places["columns"] == ["name", "state", "lat", "lon"]
    assert ["Dallas city", "TX", 32.7933, -96.7665] in places["places"]


def test_pipeline_can_run_without_the_geonames_tier(pipeline_inputs):
    artefact = run_pipeline(pipeline_inputs, no_geonames=True)
    sources = {s["geocode_source"] for s in artefact["stops"]}
    assert sources == {"census_gazetteer"}
    # Breezewood and Crescent now fail, and must be named rather than vanish.
    unresolved = {row["id"] for row in artefact["stats"]["excluded_unresolved"]}
    assert {"2", "3", "5"} == unresolved


def test_pipeline_rejects_a_missing_csv(tmp_path):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="CSV not found"):
        call_command("build_fuel_index", csv=tmp_path / "nope.csv", verbosity=0)


def test_every_artefact_coordinate_is_inside_the_validated_bbox(pipeline_inputs):
    from services.geo import in_us_or_canada

    artefact = run_pipeline(pipeline_inputs)
    assert artefact["stats"]["bbox_rejected_count"] == 0
    assert all(in_us_or_canada(s["lat"], s["lon"]) for s in artefact["stops"])


# --------------------------------------------------------------------------
# benchmark
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_benchmark_runs_and_reports_without_touching_the_network(
    tmp_path, monkeypatch, fake_provider, seeded_stops, capsys
):
    """Smoke test over a single route, with the provider faked. Proves the command
    completes and that its warm passes make no external calls."""
    from tests.conftest import meridian_route

    provider = fake_provider(meridian_route(600.0))
    monkeypatch.setattr("fuelroute.planner.default_route_provider", lambda: provider)
    monkeypatch.setattr(
        "fuelroute.management.commands.benchmark.ROUTES",
        [("39.0,-100.0", "41.0,-100.0")],
    )
    results = tmp_path / "bench.json"
    call_command(
        "benchmark", repeats=2, fixtures=tmp_path / "fx", json=results
    )
    out = capsys.readouterr().out
    assert "TARGET MET" in out
    payload = json.loads(results.read_text())
    assert payload["summary"]["samples"] == 2
    assert payload["summary"]["target_met"] is True
    assert payload["summary"]["warm_external_calls"] == 0
    assert payload["cold"][0]["routing_calls"] == 1


@pytest.mark.django_db
def test_benchmark_offline_errors_when_no_fixture_is_recorded(
    tmp_path, monkeypatch, fake_provider, seeded_stops
):
    from django.core.management.base import CommandError
    from tests.conftest import meridian_route

    monkeypatch.setattr(
        "fuelroute.planner.default_route_provider",
        lambda: fake_provider(meridian_route(600.0)),
    )
    monkeypatch.setattr(
        "fuelroute.management.commands.benchmark.ROUTES",
        [("39.0,-100.0", "41.0,-100.0")],
    )
    with pytest.raises(CommandError, match="not cached"):
        call_command("benchmark", repeats=1, offline=True, fixtures=tmp_path / "empty")
