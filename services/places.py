"""Resolve a user-supplied location string to coordinates, offline.

This is what makes free-text input cost zero external calls. The brief allows
two or three calls to the map API; we spend one, on routing, because the endpoint
geocoding is answered from a committed 53,675-place index.

Accepted inputs:
    "32.7767,-96.7970"      explicit coordinates          -> 0 calls
    "Dallas, TX"            city and state                -> 0 calls
    "Dallas TX"             without the comma             -> 0 calls
    "Dallas"                city alone, if unambiguous    -> 0 calls

A bare city name is resolved only when the index has a single clear winner;
otherwise the caller is told it is ambiguous and asked to add a state, rather than
being silently given one of several Springfields.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from services.geo import in_us
from services.normalize import US_STATES, normalize_place, place_keys

logger = logging.getLogger(__name__)

#: "32.7767,-96.7970" or "32.7767 -96.7970", with optional whitespace.
_COORD_RE = re.compile(
    r"^\s*(?P<lat>[-+]?\d{1,3}(?:\.\d+)?)\s*[,\s]\s*(?P<lon>[-+]?\d{1,3}(?:\.\d+)?)\s*$"
)

STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE",
    "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
    "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "DISTRICT OF COLUMBIA": "DC",
}


class LocationError(ValueError):
    """The input could not be turned into a usable US coordinate."""

    def __init__(self, message: str, *, code: str = "unresolvable_location") -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ResolvedLocation:
    lat: float
    lon: float
    #: Echo of what the caller sent, so the response is self-describing.
    query: str
    #: "coordinates", "place_index", or "nominatim".
    source: str
    label: str = ""
    external_calls: int = 0


class PlaceIndex:
    """Name -> coordinate lookup for user-entered locations.

    Keyed on (normalised name, state) and also on normalised name alone, the
    latter only where exactly one state claims that name - which is what lets
    "Dallas" resolve while "Springfield" is correctly reported ambiguous.
    """

    __slots__ = ("_by_name_state", "_by_name", "_count")

    def __init__(self, records: list[tuple[str, str, float, float]]) -> None:
        by_name_state: dict[tuple[str, str], tuple[float, float]] = {}
        name_states: dict[str, set[str]] = {}
        by_name: dict[str, tuple[float, float]] = {}

        for name, state, lat, lon in records:
            for key in place_keys(name):
                by_name_state.setdefault((key, state), (lat, lon))
                name_states.setdefault(key, set()).add(state)
                by_name.setdefault(key, (lat, lon))

        self._by_name_state = by_name_state
        # Drop any bare name claimed by more than one state: answering those
        # silently would hand back an arbitrary Springfield.
        self._by_name = {
            key: coords
            for key, coords in by_name.items()
            if len(name_states.get(key, ())) == 1
        }
        self._count = len(records)

    def __len__(self) -> int:
        return self._count

    @classmethod
    def from_artefact(cls, path: Path) -> PlaceIndex:
        with gzip.open(path, "rb") as fh:
            payload = json.load(fh)
        return cls([tuple(row) for row in payload["places"]])

    def lookup(self, name: str, state: str | None) -> tuple[float, float] | None:
        keys = sorted(place_keys(name), key=len, reverse=True)
        if state:
            for key in keys:
                hit = self._by_name_state.get((key, state))
                if hit:
                    return hit
            return None
        for key in keys:
            hit = self._by_name.get(key)
            if hit:
                return hit
        return None

    def states_for(self, name: str) -> list[str]:
        """Every state that has a place of this name, sorted.

        Used to make the ambiguity error name real states. Suggesting an
        arbitrary state code would be worse than saying nothing: an earlier
        version told callers to try "Springfield, TX" purely because that is
        where set iteration happened to land.
        """
        keys = place_keys(name)
        return sorted(
            {
                state
                for (key, state) in self._by_name_state
                if key in keys
            }
        )


def split_city_state(text: str) -> tuple[str, str | None]:
    """Pull a trailing state out of a free-text location.

    Handles "Dallas, TX", "Dallas TX", "Dallas, Texas" and "Dallas". Also tolerates
    a trailing country: "Dallas, TX, USA".
    """
    cleaned = normalize_place(text)
    if not cleaned:
        return "", None

    tokens = cleaned.split(" ")
    # Strip a trailing country token so "Dallas, TX, USA" still finds TX.
    while tokens and tokens[-1] in {"USA", "US", "UNITED STATES", "STATES", "UNITED"}:
        tokens.pop()
    if not tokens:
        return "", None

    # Two-word state names first ("NEW YORK", "NORTH DAKOTA"), then one word.
    for span in (3, 2, 1):
        if len(tokens) > span:
            candidate = " ".join(tokens[-span:])
            if candidate in STATE_NAMES:
                return " ".join(tokens[:-span]), STATE_NAMES[candidate]
    if len(tokens) > 1 and tokens[-1] in US_STATES:
        return " ".join(tokens[:-1]), tokens[-1]
    return " ".join(tokens), None


def resolve_location(
    text: str,
    index: PlaceIndex,
    geocoder=None,
) -> ResolvedLocation:
    """Turn a location string into a US coordinate.

    `geocoder`, if given, is a last-resort callable (city, state) -> (lat, lon)
    used only when the offline index fails. It is the only path that can cost an
    external call, and the count is reported on the result.
    """
    raw = (text or "").strip()
    if not raw:
        raise LocationError("Location is empty.", code="empty_location")

    match = _COORD_RE.match(raw)
    if match:
        lat, lon = float(match.group("lat")), float(match.group("lon"))
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise LocationError(
                f"{lat},{lon} is not a valid coordinate.", code="invalid_coordinate"
            )
        if not in_us(lat, lon):
            raise LocationError(
                f"{lat},{lon} is outside the United States. "
                "Both start and finish must be within the USA.",
                code="outside_us",
            )
        return ResolvedLocation(lat=lat, lon=lon, query=raw, source="coordinates",
                                label=f"{lat},{lon}")

    city, state = split_city_state(raw)
    if not city:
        raise LocationError(f"Could not read a place name from {raw!r}.")

    hit = index.lookup(city, state)
    if hit:
        label = f"{city.title()}, {state}" if state else city.title()
        return ResolvedLocation(lat=hit[0], lon=hit[1], query=raw,
                                source="place_index", label=label)

    if state is None:
        states = index.states_for(city)
        if len(states) > 1:
            shown = ", ".join(f"{city.title()}, {code}" for code in states[:3])
            more = f" and {len(states) - 3} more" if len(states) > 3 else ""
            raise LocationError(
                f"{city.title()!r} is in {len(states)} states. Add a state code - "
                f"for example {shown}{more}.",
                code="ambiguous_location",
            )

    if geocoder is not None:
        coords = geocoder(city, state or "")
        if coords and in_us(*coords):
            return ResolvedLocation(lat=coords[0], lon=coords[1], query=raw,
                                    source="nominatim",
                                    label=f"{city.title()}, {state}" if state else city.title(),
                                    external_calls=1)

    raise LocationError(
        f"Could not find {raw!r} in the United States. Try 'City, ST' "
        "or explicit 'lat,lon' coordinates."
    )
