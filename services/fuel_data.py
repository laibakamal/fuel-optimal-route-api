"""Parsing, deduplication and price aggregation for the OPIS fuel-price CSV.

Deduplication key: OPIS Truckstop ID, alone.

Justification is measured, not assumed. Distinct-group counts on the supplied
file for six candidate keys:

    OPIS Truckstop ID                    6738
    ID + Address                         6738
    ID + City + State                    6738
    ID + Address + City + State          6738
    Truckstop Name + Address + City + State   6964
    Address + City + State               6337

Adding Address, City or State to the ID changes nothing: the ID functionally
determines location, so it is a sound primary key. Name is *not* in the key
because 226 IDs carry cosmetic name variants, and keying on name would split one
physical stop into several. Address is *not* in the key because it adds no
discrimination (6738 either way) while risking splits on whitespace variants.

What the repeated IDs actually are: 678 IDs appear more than once, and 597 of
those differ in Retail Price. They are repeated *price observations* of one
physical truck stop - which is why the schema separates TruckStop from
PriceObservation. There is no date column, so observations cannot be ordered and
"latest price" is not available from this file; we aggregate instead.
"""
from __future__ import annotations

import csv
import statistics
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from services.normalize import country_for_state

PRICE_BASIS_MEDIAN = "median"
PRICE_BASIS_MEAN = "mean"
PRICE_BASIS_MIN = "min"

_AGGREGATORS = {
    PRICE_BASIS_MEDIAN: statistics.median,
    PRICE_BASIS_MEAN: statistics.fmean,
    PRICE_BASIS_MIN: min,
}

EXPECTED_COLUMNS = (
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
)


@dataclass
class RawStop:
    """One physical truck stop, with every price observation seen for it."""

    opis_id: str
    name: str
    address: str
    city: str
    state: str
    rack_id: str
    country: str
    prices: list[float] = field(default_factory=list)

    def price(self, basis: str = PRICE_BASIS_MEDIAN) -> float:
        """Aggregate the undated observations into one usable price.

        Median by default: within-stop spread is median $0.060 but reaches $0.900,
        and the median is not dragged by those outliers. The chosen basis is
        reported in the API's `assumptions` object rather than left implicit -
        this is an aggregate of undated observations, not a live price.
        """
        return round(_AGGREGATORS[basis](self.prices), 4)

    @property
    def observation_count(self) -> int:
        return len(self.prices)


class CsvSchemaError(ValueError):
    pass


def _clean(value: str | None) -> str:
    """Collapse internal runs of whitespace and strip. The City column is padded
    to a fixed width in the source file ('Effingham                ')."""
    return " ".join((value or "").split())


def parse_fuel_csv(path: Path) -> tuple[dict[str, RawStop], dict[str, int]]:
    """Read the CSV into {opis_id: RawStop} plus a stats dict.

    Returns stops keyed by the dedup key, so the caller gets deduplication for
    free. Raises CsvSchemaError if the header is not the expected one - a silent
    column rename would otherwise produce a confidently wrong answer.
    """
    stats = {
        "rows_read": 0,
        "rows_skipped_bad_price": 0,
        "rows_skipped_missing_id": 0,
        "name_variants_collapsed": 0,
    }
    stops: dict[str, RawStop] = {}

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in EXPECTED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise CsvSchemaError(
                f"{path.name} is missing expected column(s): {missing}. "
                f"Found: {reader.fieldnames}"
            )

        for row in reader:
            stats["rows_read"] += 1
            opis_id = _clean(row["OPIS Truckstop ID"])
            if not opis_id:
                stats["rows_skipped_missing_id"] += 1
                continue
            try:
                price = float(row["Retail Price"])
            except (TypeError, ValueError):
                stats["rows_skipped_bad_price"] += 1
                continue
            if price <= 0:
                stats["rows_skipped_bad_price"] += 1
                continue

            state = _clean(row["State"]).upper()
            existing = stops.get(opis_id)
            if existing is None:
                stops[opis_id] = RawStop(
                    opis_id=opis_id,
                    name=_clean(row["Truckstop Name"]),
                    address=_clean(row["Address"]),
                    city=_clean(row["City"]),
                    state=state,
                    rack_id=_clean(row["Rack ID"]),
                    country=country_for_state(state),
                    prices=[price],
                )
            else:
                existing.prices.append(price)
                # Keep the first name seen and count the divergence rather than
                # pick arbitrarily and say nothing.
                if _clean(row["Truckstop Name"]) != existing.name:
                    stats["name_variants_collapsed"] += 1

    stats["distinct_stops"] = len(stops)
    stats["stops_with_multiple_observations"] = sum(
        1 for s in stops.values() if s.observation_count > 1
    )
    return stops, stats


def iter_by_country(stops: dict[str, RawStop], country: str) -> Iterator[RawStop]:
    return (s for s in stops.values() if s.country == country)
