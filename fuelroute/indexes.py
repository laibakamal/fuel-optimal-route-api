"""Process-level in-memory indexes, built once and reused by every request.

Why these are module-level singletons rather than per-request work: building the
stop index takes a database read of 6,626 rows plus 7,531 observations, and the
place index is a 672 KiB gzipped artefact. Paying either per request would dwarf
the actual routing work. They are immutable once built, so sharing them across
threads needs no lock.

The stop index is built from the DATABASE, not from the committed artefact. The
database is the source of truth after seeding; reading the artefact here would
make the models decorative and let the two drift apart silently.

The place index is built from the artefact, because it is reference data for
parsing user input rather than domain data - it has no relationships, nothing
updates it, and putting 53,675 rows in a table we would only ever read whole
would buy nothing.
"""
from __future__ import annotations

import logging
import statistics
import threading
import time

from django.conf import settings

from fuelroute.models import TruckStop
from services.corridor import IndexedStop, StopIndex
from services.fuel_data import PRICE_BASIS_MEAN, PRICE_BASIS_MEDIAN, PRICE_BASIS_MIN
from services.places import PlaceIndex

logger = logging.getLogger(__name__)

_AGGREGATORS = {
    PRICE_BASIS_MEDIAN: statistics.median,
    PRICE_BASIS_MEAN: statistics.fmean,
    PRICE_BASIS_MIN: min,
}

_stop_index: StopIndex | None = None
_place_index: PlaceIndex | None = None
_lock = threading.Lock()


class StopsNotSeededError(RuntimeError):
    """The database has no stops. The API cannot answer anything useful."""


def build_stop_index(price_basis: str | None = None) -> StopIndex:
    """Load every usable stop and aggregate its price observations.

    Aggregation happens here, once per process, rather than in SQL. Doing it in
    Python keeps the basis (median/mean/min) a single configurable setting instead
    of three hand-written SQL expressions, and median in particular is awkward and
    non-portable across SQLite and PostgreSQL.
    """
    basis = price_basis or settings.PRICE_BASIS
    try:
        aggregate = _AGGREGATORS[basis]
    except KeyError:
        raise ValueError(
            f"PRICE_BASIS={basis!r} is not one of {sorted(_AGGREGATORS)}"
        ) from None

    started = time.perf_counter()
    stops: list[IndexedStop] = []
    for stop in TruckStop.objects.for_optimizer():
        prices = [
            float(observation.price_usd_per_gallon)
            for observation in stop.price_observations.all()
        ]
        if not prices:
            # verify_stops treats this as a failure; skip defensively so a partial
            # seed degrades rather than crashing every request.
            logger.warning("stop %s has no price observations, skipping", stop.opis_id)
            continue
        stops.append(
            IndexedStop(
                stop_id=stop.opis_id,
                name=stop.name,
                address=stop.address,
                city=stop.city,
                state=stop.state,
                lat=stop.latitude,
                lon=stop.longitude,
                price=round(float(aggregate(prices)), 4),
            )
        )

    if not stops:
        raise StopsNotSeededError(
            "No truck stops in the database. Run: python manage.py seed_stops"
        )

    index = StopIndex(stops)
    logger.info(
        "stop index built: %d stops, %d grid cells, basis=%s, %.0f ms",
        len(index),
        index.cell_count,
        basis,
        (time.perf_counter() - started) * 1000,
    )
    return index


def get_stop_index() -> StopIndex:
    global _stop_index
    if _stop_index is None:
        with _lock:
            if _stop_index is None:  # re-check: another thread may have won the race
                _stop_index = build_stop_index()
    return _stop_index


def get_place_index() -> PlaceIndex:
    global _place_index
    if _place_index is None:
        with _lock:
            if _place_index is None:
                started = time.perf_counter()
                _place_index = PlaceIndex.from_artefact(settings.PLACES_ARTEFACT)
                logger.info(
                    "place index built: %d records, %.0f ms",
                    len(_place_index),
                    (time.perf_counter() - started) * 1000,
                )
    return _place_index


def reset_indexes(stops_only: bool = False) -> None:
    """Drop the cached indexes. For tests, and after a re-seed.

    `stops_only` keeps the place index, which is immutable reference data loaded
    from a committed artefact. Rebuilding it between tests costs ~250 ms each time
    and can never change the outcome, whereas the stop index must be rebuilt
    because the database does change.
    """
    global _stop_index, _place_index
    with _lock:
        _stop_index = None
        if not stops_only:
            _place_index = None
