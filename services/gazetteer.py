"""Offline place-coordinate index: the answer to "8,100 stops, no coordinates".

Two tiers, queried in order:

  Tier 1  US Census Bureau Gazetteer "Places" national file.
          US Government work -> public domain, no attribution required.
          Measured: resolves 94.78% of distinct US stops.

  Tier 2  GeoNames US populated places (feature class P, excluding PPLQ
          "historical"). CC-BY 4.0 -> attribution required, see README.
          Measured: resolves a further 4.94%.

  Tier 3  GeoNames *landmarks* - post offices, parks and civil divisions - with
          the landmark suffix stripped ("Crescent Post Office" -> "Crescent").
          A post office is named after the community it serves, which makes it a
          reliable position proxy for an unincorporated place that no gazetteer
          lists as a populated place. Measured: resolves a further 0.11%.

Census is tier 1 because it is public domain, authoritative for incorporated
places, and ships an ALAND_SQMI column we use both to break ties and to estimate
our own positional error. GeoNames is tier 2 because it covers the
unincorporated communities and census-designated hamlets that truck stops
actually sit next to (Breezewood PA, Heiskell TN, Ruther Glen VA) which are
absent from the Census Places file.

Nothing in this module touches the network at query time. Downloads happen once,
in the offline pipeline, into a gitignored cache directory.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

from services.geo import haversine_miles
from services.normalize import US_STATES, place_keys

logger = logging.getLogger(__name__)

CENSUS_YEAR = 2025
CENSUS_URL = (
    f"https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    f"{CENSUS_YEAR}_Gazetteer/{CENSUS_YEAR}_Gaz_place_national.zip"
)
CENSUS_MEMBER = f"{CENSUS_YEAR}_Gaz_place_national.txt"
GEONAMES_URL = "https://download.geonames.org/export/dump/US.zip"
GEONAMES_MEMBER = "US.txt"

SOURCE_CENSUS = "census_gazetteer"
SOURCE_GEONAMES = "geonames"
SOURCE_GEONAMES_LANDMARK = "geonames_landmark"

# Tier-3 allowlist. Deliberately narrow: these are feature types whose name
# identifies a *community*, not an arbitrary nearby object. Schools, dams and
# buildings are excluded - "Crescent Elementary School" is weak evidence of where
# Crescent is, whereas "Crescent Post Office" is strong evidence.
LANDMARK_FEATURE_CODES = frozenset({"PO", "PRK", "ADMD", "ADM3", "ADM4"})

# Suffixes stripped from a landmark name to recover the community name.
_LANDMARK_SUFFIXES = (
    " Post Office",
    " Township",
    " Borough",
    " Town",
)

# Two candidates for one (city, state) further apart than this are genuinely
# ambiguous (different towns of the same name), not just centroid jitter. We
# still pick one, but we flag the stop so the count is reportable.
AMBIGUITY_THRESHOLD_MILES = 10.0


@dataclass(frozen=True)
class Candidate:
    lat: float
    lon: float
    # Census: land area in sq mi. GeoNames: population. Both are "bigger means
    # more likely to be the place a truck stop address refers to", which is why
    # one field serves as the tie-break weight for both sources.
    weight: float


@dataclass(frozen=True)
class Resolution:
    lat: float
    lon: float
    source: str
    #: Max separation among candidates for this key, in miles. 0.0 when unique.
    ambiguity_miles: float

    @property
    def is_ambiguous(self) -> bool:
        return self.ambiguity_miles > AMBIGUITY_THRESHOLD_MILES


def _spread_miles(candidates: list[Candidate]) -> float:
    if len(candidates) < 2:
        return 0.0
    return max(
        haversine_miles(a.lat, a.lon, b.lat, b.lon)
        for i, a in enumerate(candidates)
        for b in candidates[i + 1 :]
    )


def load_census_places(zip_path: Path) -> dict[tuple[str, str], list[Candidate]]:
    """Parse the Census Gazetteer Places file into {(name_key, state): [...]}."""
    index: dict[tuple[str, str], list[Candidate]] = {}
    with zipfile.ZipFile(zip_path) as zf:
        raw = zf.read(CENSUS_MEMBER)
    # The file is pipe-delimited and latin-1 (it contains Spanish place names
    # from Puerto Rico that are not valid UTF-8).
    reader = csv.DictReader(io.StringIO(raw.decode("latin-1")), delimiter="|")
    for row in reader:
        state = (row.get("USPS") or "").strip().upper()
        try:
            lat = float(row["INTPTLAT"])
            lon = float(row["INTPTLONG"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            weight = float(row.get("ALAND_SQMI") or 0.0)
        except ValueError:
            weight = 0.0
        cand = Candidate(lat, lon, weight)
        for key in place_keys(row.get("NAME", "")):
            index.setdefault((key, state), []).append(cand)
    logger.info("census gazetteer: %d keys", len(index))
    return index


def load_geonames(
    zip_path: Path,
) -> tuple[dict[tuple[str, str], list[Candidate]], dict[tuple[str, str], list[Candidate]]]:
    """Parse GeoNames US.txt once, returning (populated_places, landmarks).

    One pass, two indexes: the extracted file is ~300 MB and reading it twice
    would double the slowest step in the pipeline for no benefit.
    """
    populated: dict[tuple[str, str], list[Candidate]] = {}
    landmarks: dict[tuple[str, str], list[Candidate]] = {}

    with zipfile.ZipFile(zip_path) as zf, zf.open(GEONAMES_MEMBER) as fh:
        for raw_line in io.TextIOWrapper(fh, encoding="utf-8"):
            parts = raw_line.rstrip("\n").split("\t")
            if len(parts) < 15:
                continue
            # Column order is fixed by the GeoNames 'geoname' table schema.
            feature_class, feature_code = parts[6], parts[7]
            state = parts[10].strip().upper()
            if state not in US_STATES:
                continue
            try:
                lat, lon = float(parts[4]), float(parts[5])
            except ValueError:
                continue

            if feature_class == "P":
                if feature_code == "PPLQ":
                    continue  # PPLQ = place that no longer exists
                try:
                    weight = float(parts[14] or 0)  # population
                except ValueError:
                    weight = 0.0
                cand = Candidate(lat, lon, weight)
                # Index both the UTF-8 and ASCII names; they differ for places
                # with accents and either may match the OPIS spelling.
                for name in {parts[1], parts[2]}:
                    for key in place_keys(name):
                        populated.setdefault((key, state), []).append(cand)

            elif feature_code in LANDMARK_FEATURE_CODES:
                cand = Candidate(lat, lon, 0.0)
                for name in {parts[1], parts[2]}:
                    if not name:
                        continue
                    community = name
                    for suffix in _LANDMARK_SUFFIXES:
                        if community.endswith(suffix):
                            community = community[: -len(suffix)]
                            break
                    else:
                        # No recognised suffix: only index parks, whose own name
                        # IS the place name ("Hot Springs National Park").
                        if feature_code != "PRK":
                            continue
                    for key in place_keys(community):
                        landmarks.setdefault((key, state), []).append(cand)

    logger.info(
        "geonames: %d populated-place keys, %d landmark keys",
        len(populated),
        len(landmarks),
    )
    return populated, landmarks


class GazetteerIndex:
    """Tiered (city, state) -> coordinate lookup. Pure in-memory, no I/O."""

    def __init__(self, tiers: list[tuple[str, dict[tuple[str, str], list[Candidate]]]]) -> None:
        #: Ordered highest-confidence first. Order is the whole design: an
        #: authoritative public-domain place centroid beats a crowd-sourced one,
        #: which beats a landmark proxy.
        self._tiers = [(name, index) for name, index in tiers if index]

    def resolve(self, city: str, state: str) -> Resolution | None:
        """Best coordinate for a (city, state) pair, or None if unmatched.

        Longer keys are tried first so a full form ("KANSAS CITY CITY") wins over
        a suffix-stripped one, and the spaced form wins over the de-spaced form.
        That ordering keeps the de-spaced fallback from creating spurious matches
        between genuinely different names.
        """
        state_code = (state or "").strip().upper()
        keys = sorted(place_keys(city), key=len, reverse=True)
        for source, index in self._tiers:
            for key in keys:
                candidates = index.get((key, state_code))
                if candidates:
                    best = max(candidates, key=lambda c: c.weight)
                    return Resolution(
                        lat=best.lat,
                        lon=best.lon,
                        source=source,
                        ambiguity_miles=_spread_miles(candidates),
                    )
        return None


# ---------------------------------------------------------------------------
# Place index for resolving user-supplied free-text locations
# ---------------------------------------------------------------------------

#: GeoNames population floor for inclusion in the committed place index. Census
#: Places already covers every incorporated place and CDP (32,058 records); this
#: threshold adds the populated places Census omits without dragging in 163,000
#: hamlets. Measured artefact size: 407 KiB for Census alone, 672 KiB with this
#: threshold, 2.4 MiB with no threshold.
PLACE_INDEX_MIN_POPULATION = 500


def build_place_records(
    census_zip: Path, geonames_zip: Path | None
) -> list[tuple[str, str, float, float]]:
    """(name, state, lat, lon) for every place a user might type as an endpoint.

    Separate from the truck-stop resolution tiers because the job is different:
    there we matched ~3,800 known (city, state) pairs as accurately as possible,
    here we need broad coverage of whatever a caller types. Shipping this as a
    committed artefact is what lets free-text input cost zero geocoding calls.
    """
    records: list[tuple[str, str, float, float]] = []

    with zipfile.ZipFile(census_zip) as zf:
        raw = zf.read(CENSUS_MEMBER)
    for row in csv.DictReader(io.StringIO(raw.decode("latin-1")), delimiter="|"):
        state = (row.get("USPS") or "").strip().upper()
        if state not in US_STATES:
            continue
        try:
            records.append(
                (row["NAME"].strip(), state, round(float(row["INTPTLAT"]), 4),
                 round(float(row["INTPTLONG"]), 4))
            )
        except (KeyError, TypeError, ValueError):
            continue

    if geonames_zip is not None:
        with zipfile.ZipFile(geonames_zip) as zf, zf.open(GEONAMES_MEMBER) as fh:
            for raw_line in io.TextIOWrapper(fh, encoding="utf-8"):
                parts = raw_line.rstrip("\n").split("\t")
                if len(parts) < 15 or parts[6] != "P" or parts[7] == "PPLQ":
                    continue
                state = parts[10].strip().upper()
                if state not in US_STATES:
                    continue
                try:
                    if int(parts[14] or 0) < PLACE_INDEX_MIN_POPULATION:
                        continue
                    records.append(
                        (parts[1], state, round(float(parts[4]), 4), round(float(parts[5]), 4))
                    )
                except ValueError:
                    continue

    logger.info("place index: %d records", len(records))
    return records
