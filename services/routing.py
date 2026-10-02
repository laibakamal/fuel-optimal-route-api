"""Route acquisition. Exactly one external HTTP call per uncached route.

WHY OSRM
    The decisive feature is `annotations=distance`. One request returns the full
    road geometry *and* the length of every segment of it, so we can build the
    (lat, lon, cumulative_miles) polyline the optimiser needs by prefix-summing
    the annotations. No second call, and no re-measuring distances ourselves with
    haversine (which would under-report, because it chords every curve).

    Verified against the live service on Dallas -> Chicago: 9,161 coordinates,
    9,160 annotation values, and sum(annotations) == route.distance exactly
    (1,555,627.1 m). See ARCHITECTURE.md.

CALL ACCOUNTING
    `fetch` returns the Route together with the number of HTTP calls it made, so
    the API can report a measured count rather than a claim. A fallback attempt
    after a primary failure counts as a second call and is reported as such.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

import requests

from services.geo import METERS_PER_MILE

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoutePoint:
    lat: float
    lon: float
    #: Cumulative driving distance from the origin, in miles.
    mile: float


@dataclass(frozen=True)
class Route:
    points: list[RoutePoint]
    total_miles: float
    duration_seconds: float
    provider: str
    #: HTTP calls this route cost to obtain. 0 when served from cache.
    external_calls: int = 0

    @property
    def geojson_coordinates(self) -> list[list[float]]:
        """GeoJSON order is [lon, lat], which is the opposite of how humans say
        it. Converting in one place stops that mistake spreading."""
        return [[p.lon, p.lat] for p in self.points]


class RoutingError(Exception):
    """The routing provider could not supply a route."""

    def __init__(self, message: str, *, code: str = "routing_failed") -> None:
        self.code = code
        super().__init__(message)


class NoRouteFoundError(RoutingError):
    """The provider answered, but no road route connects the two points."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="no_route_found")


class RouteProvider(Protocol):
    def fetch(self, origin: tuple[float, float], destination: tuple[float, float]) -> Route: ...


@dataclass
class OsrmRouteProvider:
    """OSRM `route` service client with a keyless fallback endpoint.

    The fallback (FOSSGIS `routed-car`) runs the same OSRM version and returns an
    identical response shape - verified: byte-identical distance, point count and
    annotation count on Dallas -> Chicago - so failover needs no second parser.
    """

    base_url: str
    fallback_url: str = ""
    timeout_seconds: float = 20.0
    user_agent: str = "fuel-optimal-route-api/1.0"
    session: requests.Session | None = None
    #: Mutable so tests can assert on it without reaching into the transport.
    last_provider: str = field(default="", init=False)
    #: Raw provider JSON from the most recent successful fetch. Exposed so the
    #: benchmark's fixture recorder can persist exactly what the service returned,
    #: rather than re-serialising our parsed form and losing fidelity.
    last_payload: dict | None = field(default=None, init=False)

    def _client(self) -> requests.Session:
        if self.session is None:
            self.session = requests.Session()
        return self.session

    def _request(self, base: str, origin, destination) -> dict:
        # OSRM takes coordinates as lon,lat.
        coords = f"{origin[1]:.6f},{origin[0]:.6f};{destination[1]:.6f},{destination[0]:.6f}"
        url = f"{base}/route/v1/driving/{coords}"
        params = {
            "overview": "full",          # full-resolution geometry, not simplified
            "geometries": "geojson",
            "annotations": "distance",   # per-segment lengths: the whole point
            "steps": "false",            # we need distances, not turn instructions
            "alternatives": "false",
            "generate_hints": "false",   # trims ~30% off the response size
        }
        response = self._client().get(
            url,
            params=params,
            timeout=self.timeout_seconds,
            headers={"User-Agent": self.user_agent},
        )
        response.raise_for_status()
        return response.json()

    def fetch(self, origin: tuple[float, float], destination: tuple[float, float]) -> Route:
        """One HTTP call on the happy path; a second only if the primary fails."""
        calls = 0
        endpoints = [("osrm-demo", self.base_url)]
        if self.fallback_url:
            endpoints.append(("osrm-fossgis", self.fallback_url))

        last_error: Exception | None = None
        for name, base in endpoints:
            try:
                calls += 1
                payload = self._request(base, origin, destination)
            except requests.RequestException as exc:
                logger.warning("routing provider %s failed: %s", name, exc)
                last_error = exc
                continue

            code = payload.get("code")
            if code == "NoRoute":
                # A definitive answer, not a transport failure: do not retry the
                # fallback, it will say the same thing.
                raise NoRouteFoundError(
                    "No drivable route connects these two locations. "
                    "Check that both are on the road network and in the USA."
                )
            if code != "Ok" or not payload.get("routes"):
                last_error = RoutingError(f"{name} returned code={code!r}")
                logger.warning("routing provider %s returned code=%r", name, code)
                continue

            self.last_provider = name
            self.last_payload = payload
            return _parse_route(payload, provider=name, external_calls=calls)

        raise RoutingError(
            f"All routing providers failed ({len(endpoints)} tried). Last error: {last_error}"
        )


def _parse_route(payload: dict, provider: str, external_calls: int) -> Route:
    route = payload["routes"][0]
    coordinates = route["geometry"]["coordinates"]
    if len(coordinates) < 2:
        raise RoutingError("Routing provider returned a degenerate geometry.")

    legs = route.get("legs") or []
    segments: list[float] = []
    for leg in legs:
        annotation = leg.get("annotation") or {}
        segments.extend(annotation.get("distance") or [])

    if len(segments) != len(coordinates) - 1:
        # Without per-segment distances we would have to re-measure the geometry
        # with haversine, which chords every curve and under-reports. Fail loudly
        # instead of silently returning a short route and an undercharged total.
        raise RoutingError(
            "Routing response is missing per-segment distance annotations "
            f"({len(segments)} annotations for {len(coordinates)} coordinates). "
            "The request must include annotations=distance."
        )

    points = [RoutePoint(lat=coordinates[0][1], lon=coordinates[0][0], mile=0.0)]
    cumulative = 0.0
    for index, meters in enumerate(segments):
        cumulative += meters / METERS_PER_MILE
        lon, lat = coordinates[index + 1][0], coordinates[index + 1][1]
        points.append(RoutePoint(lat=lat, lon=lon, mile=cumulative))

    return Route(
        points=points,
        # Prefer the provider's own total over our prefix sum; they agree to the
        # metre in practice, and the provider's value is authoritative.
        total_miles=route["distance"] / METERS_PER_MILE,
        duration_seconds=route.get("duration", 0.0),
        provider=provider,
        external_calls=external_calls,
    )


def simplify_coordinates(
    coordinates: list[list[float]], every_nth: int, precision: int = 5
) -> list[list[float]]:
    """Decimate and round a coordinate list for transport.

    Measured payloads for the raw geometry: 203.8 KiB (Dallas-Chicago) and
    830.8 KiB (LA-New York). At every_nth=10 the LA-New York geometry falls to
    76.5 KiB, a 10.9x reduction that is visually identical at map zoom levels.
    5 decimal places is ~1.1 m of precision, far finer than the route needs.

    The first and last points are always kept so the line still starts and ends
    in the right place.
    """
    if every_nth <= 1:
        return [[round(lon, precision), round(lat, precision)] for lon, lat in coordinates]
    kept = coordinates[::every_nth]
    if kept[-1] is not coordinates[-1]:
        kept = kept + [coordinates[-1]]
    return [[round(lon, precision), round(lat, precision)] for lon, lat in kept]
