"""Schema constraints, the seed command, and the in-memory index built from the DB."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from fuelroute.indexes import StopsNotSeededError, build_stop_index
from fuelroute.models import PriceObservation, TruckStop


def make_stop(**overrides) -> TruckStop:
    defaults = dict(
        opis_id="X1", name="N", address="A", city="C", state="TX",
        country=TruckStop.Country.US, rack_id="1",
        latitude=32.0, longitude=-96.0,
        geocode_source=TruckStop.GeocodeSource.CENSUS,
    )
    defaults.update(overrides)
    return TruckStop.objects.create(**defaults)


# --------------------------------------------------------------------------
# Database-level constraints
# --------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize("field,value", [("latitude", 91.0), ("latitude", -91.0),
                                         ("longitude", 181.0), ("longitude", -181.0)])
def test_out_of_range_coordinates_are_rejected_by_the_database(field, value):
    with pytest.raises(IntegrityError), transaction.atomic():
        make_stop(**{field: value})


@pytest.mark.django_db
def test_nonpositive_price_is_rejected_by_the_database():
    stop = make_stop()
    for bad in (Decimal("0"), Decimal("-1.5")):
        with pytest.raises(IntegrityError), transaction.atomic():
            PriceObservation.objects.create(stop=stop, price_usd_per_gallon=bad)


@pytest.mark.django_db
def test_opis_id_is_unique():
    make_stop(opis_id="DUP")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_stop(opis_id="DUP")


@pytest.mark.django_db
def test_the_same_price_may_be_recorded_twice_for_one_stop():
    """104 stops in the real file do exactly this. A unique constraint here would
    silently discard observations."""
    stop = make_stop()
    PriceObservation.objects.create(stop=stop, price_usd_per_gallon=Decimal("3.899"))
    PriceObservation.objects.create(stop=stop, price_usd_per_gallon=Decimal("3.899"))
    assert stop.price_observations.count() == 2


@pytest.mark.django_db
def test_deleting_a_stop_removes_its_observations():
    stop = make_stop()
    PriceObservation.objects.create(stop=stop, price_usd_per_gallon=Decimal("3.0"))
    stop.delete()
    assert PriceObservation.objects.count() == 0


@pytest.mark.django_db
def test_price_keeps_full_source_precision():
    """The source carries up to 8 decimal places (3.00733333). Rounding on the way
    in would lose the observation as supplied."""
    stop = make_stop()
    PriceObservation.objects.create(stop=stop, price_usd_per_gallon=Decimal("3.00733333"))
    assert PriceObservation.objects.get().price_usd_per_gallon == Decimal("3.00733333")


# --------------------------------------------------------------------------
# Query paths
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_in_bbox_filters_correctly():
    inside = make_stop(opis_id="IN", latitude=35.0, longitude=-100.0)
    make_stop(opis_id="OUT", latitude=45.0, longitude=-70.0)
    found = TruckStop.objects.in_bbox(34.0, -101.0, 36.0, -99.0)
    assert [s.opis_id for s in found] == [inside.opis_id]


@pytest.mark.django_db
def test_for_optimizer_avoids_an_n_plus_one():
    for index in range(25):
        stop = make_stop(opis_id=f"S{index}", latitude=32.0 + index * 0.01)
        PriceObservation.objects.create(stop=stop, price_usd_per_gallon=Decimal("3.0"))
    with CaptureQueriesContext(connection) as queries:
        stops = list(TruckStop.objects.for_optimizer())
        total = sum(len(s.price_observations.all()) for s in stops)
    assert total == 25
    # Two queries regardless of row count: one for stops, one for observations.
    assert len(queries) == 2


@pytest.mark.django_db
def test_for_optimizer_excludes_non_us_stops():
    make_stop(opis_id="US1", country=TruckStop.Country.US)
    make_stop(opis_id="CA1", country=TruckStop.Country.CA, latitude=45.0, longitude=-75.0)
    assert [s.opis_id for s in TruckStop.objects.for_optimizer()] == ["US1"]


# --------------------------------------------------------------------------
# Index building
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_build_stop_index_aggregates_observations_with_the_configured_basis():
    stop = make_stop(opis_id="AGG")
    for price in ("3.00", "4.00", "10.00"):
        PriceObservation.objects.create(stop=stop, price_usd_per_gallon=Decimal(price))
    median = build_stop_index(price_basis="median")
    assert median._stops[0].price == pytest.approx(4.0)  # noqa: SLF001
    mean = build_stop_index(price_basis="mean")
    assert mean._stops[0].price == pytest.approx(5.6667, abs=1e-3)  # noqa: SLF001
    cheapest = build_stop_index(price_basis="min")
    assert cheapest._stops[0].price == pytest.approx(3.0)  # noqa: SLF001


@pytest.mark.django_db
def test_build_stop_index_rejects_an_unknown_basis():
    stop = make_stop()
    PriceObservation.objects.create(stop=stop, price_usd_per_gallon=Decimal("3.0"))
    with pytest.raises(ValueError, match="PRICE_BASIS"):
        build_stop_index(price_basis="geometric_mean")


@pytest.mark.django_db
def test_build_stop_index_on_an_empty_database_raises_a_clear_error():
    with pytest.raises(StopsNotSeededError, match="seed_stops"):
        build_stop_index()


@pytest.mark.django_db
def test_build_stop_index_skips_a_stop_with_no_observations():
    good = make_stop(opis_id="GOOD")
    PriceObservation.objects.create(stop=good, price_usd_per_gallon=Decimal("3.0"))
    make_stop(opis_id="NOPRICE", latitude=33.0)
    index = build_stop_index()
    assert [s.stop_id for s in index._stops] == ["GOOD"]  # noqa: SLF001


# --------------------------------------------------------------------------
# seed_stops
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_seed_loads_the_committed_artefact():
    call_command("seed_stops", verbosity=0)
    assert TruckStop.objects.count() == 6626
    assert PriceObservation.objects.count() == 7531
    assert TruckStop.objects.in_country(TruckStop.Country.US).count() == 6626


@pytest.mark.django_db
def test_seed_is_idempotent():
    call_command("seed_stops", verbosity=0)
    call_command("seed_stops", verbosity=0)
    call_command("seed_stops", verbosity=0)
    assert TruckStop.objects.count() == 6626
    assert PriceObservation.objects.count() == 7531


@pytest.mark.django_db
def test_every_seeded_coordinate_is_inside_the_us_bounding_box():
    call_command("seed_stops", verbosity=0)
    assert all(stop.is_in_us_bbox for stop in TruckStop.objects.all())


@pytest.mark.django_db
def test_verify_stops_passes_on_the_seeded_data():
    call_command("seed_stops", verbosity=0)
    # verify_stops exits non-zero on failure, so completing is the assertion.
    call_command("verify_stops", verbosity=0)


@pytest.mark.django_db
def test_seed_refuses_a_missing_artefact(tmp_path):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="not found"):
        call_command("seed_stops", artefact=tmp_path / "nope.json.gz", verbosity=0)
