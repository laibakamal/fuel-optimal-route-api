"""Request validation and response shaping."""
from __future__ import annotations

from django.conf import settings
from rest_framework import serializers

GEOMETRY_SIMPLIFIED = "simplified"
GEOMETRY_FULL = "full"
GEOMETRY_NONE = "none"

#: Keep roughly every 10th coordinate. Measured: the LA->New York geometry goes
#: from 830.8 KiB to 76.5 KiB, a 10.9x reduction, and is visually identical at any
#: map zoom. Callers who want the raw polyline ask for geometry=full.
GEOMETRY_DECIMATION = 10


class RoutePlanRequestSerializer(serializers.Serializer):
    """Validates the one endpoint's input.

    `start` and `finish` accept either free text ("Dallas, TX") or explicit
    coordinates ("32.7767,-96.7970"). Parsing and US-bounds checking happen in
    services.places, because they are domain rules rather than HTTP concerns and
    need to be testable without a request.
    """

    start = serializers.CharField(
        max_length=200,
        trim_whitespace=True,
        help_text="US location: 'Dallas, TX' or 'lat,lon'.",
    )
    finish = serializers.CharField(
        max_length=200,
        trim_whitespace=True,
        help_text="US location: 'Chicago, IL' or 'lat,lon'.",
    )
    max_detour_miles = serializers.FloatField(
        required=False,
        min_value=0.1,
        max_value=settings.MAX_DETOUR_MILES_LIMIT,
        help_text=(
            f"How far off-route a stop may be. Default {settings.MAX_DETOUR_MILES:g}, "
            f"max {settings.MAX_DETOUR_MILES_LIMIT:g}."
        ),
    )
    start_tank_gallons = serializers.FloatField(
        required=False,
        min_value=0.0,
        max_value=settings.TANK_CAPACITY_GALLONS,
        help_text=(
            f"Fuel aboard at the origin. Default {settings.START_TANK_GALLONS:g} "
            f"(empty), max {settings.TANK_CAPACITY_GALLONS:g}."
        ),
    )
    geometry = serializers.ChoiceField(
        choices=[GEOMETRY_SIMPLIFIED, GEOMETRY_FULL, GEOMETRY_NONE],
        required=False,
        default=GEOMETRY_SIMPLIFIED,
        help_text=(
            "simplified (default, ~10x smaller), full (every coordinate), "
            "or none (omit geometry entirely)."
        ),
    )

    def validate_start(self, value: str) -> str:
        return self._non_blank(value, "start")

    def validate_finish(self, value: str) -> str:
        return self._non_blank(value, "finish")

    @staticmethod
    def _non_blank(value: str, field: str) -> str:
        if not value.strip():
            raise serializers.ValidationError(f"{field} must not be blank.")
        return value.strip()


def serialise_stop(purchase, order: int) -> dict:
    """One fuel stop in the plan, with everything needed to act on it."""
    station = purchase.station
    return {
        "order": order,
        "opis_truckstop_id": station.stop_id,
        "name": station.name,
        "address": station.address,
        "city": station.city,
        "state": station.state,
        "latitude": round(station.lat, 6),
        "longitude": round(station.lon, 6),
        "price_usd_per_gallon": round(station.price, 4),
        "gallons_purchased": round(purchase.gallons, 3),
        "cost_usd": round(purchase.cost, 2),
        "distance_along_route_miles": round(station.mile, 1),
        "detour_miles": round(station.detour_miles, 2),
        "tank_gallons_on_arrival": round(purchase.arrive_gallons, 3),
        "tank_gallons_on_departure": round(purchase.depart_gallons, 3),
    }
