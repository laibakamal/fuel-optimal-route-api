"""Geodesy primitives. Miles throughout - the brief is stated in miles and mpg,
so converting once at the API boundary and staying in miles avoids a class of
unit bugs."""
from __future__ import annotations

import math

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344

# Degrees of latitude per mile is very nearly constant; longitude shrinks with
# latitude. Used to size grid-cell neighbourhoods, never for final distances.
MILES_PER_DEGREE_LAT = 69.047

# Generous CONUS + Alaska-free bounding box, used to reject bad joins rather
# than trust the gazetteer blindly.
US_BBOX = (24.0, -125.5, 49.6, -66.5)      # (min_lat, min_lon, max_lat, max_lon)
# Canada is accepted by the validator (the CSV contains 620 Canadian rows) but
# those stops are excluded from optimisation; see PLAN.md Q2.
CANADA_BBOX = (41.6, -141.1, 73.0, -52.5)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles.

    Haversine rather than Vincenty/geodesic: at the scales that matter here
    (sub-50-mile detours) the ellipsoidal correction is well under 0.5%, far
    smaller than our city-centroid geocoding error of ~1.6 miles median. Paying
    for more precision than the input data has would be false rigour.
    """
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    h = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_MILES * math.asin(math.sqrt(h))


def in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    min_lat, min_lon, max_lat, max_lon = bbox
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def in_us(lat: float, lon: float) -> bool:
    return in_bbox(lat, lon, US_BBOX)


def in_us_or_canada(lat: float, lon: float) -> bool:
    return in_bbox(lat, lon, US_BBOX) or in_bbox(lat, lon, CANADA_BBOX)
