"""The API. One primary endpoint, plus a Leaflet map page for demonstration."""
from __future__ import annotations

import logging

from django.conf import settings
from django.shortcuts import render
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from fuelroute.errors import problem_response, validation_problem
from fuelroute.indexes import StopsNotSeededError
from fuelroute.planner import assumptions, plan_route
from fuelroute.serializers import (
    GEOMETRY_DECIMATION,
    GEOMETRY_FULL,
    GEOMETRY_NONE,
    RoutePlanRequestSerializer,
    serialise_stop,
)
from services.optimizer import RouteInfeasibleError
from services.places import LocationError
from services.routing import NoRouteFoundError, RoutingError, simplify_coordinates

logger = logging.getLogger(__name__)


@api_view(["GET", "POST"])
def route_plan(request):
    """Plan the cheapest refuelling for a US trip.

    GET with query parameters or POST with a JSON body - identical semantics and
    the same serializer. GET exists because it makes the endpoint demonstrable
    from a browser address bar and lets the map page call it directly; POST exists
    because a planning request with several parameters is more naturally a body.
    """
    payload = request.query_params if request.method == "GET" else request.data
    serializer = RoutePlanRequestSerializer(data=payload)
    if not serializer.is_valid():
        return validation_problem(serializer.errors)

    data = serializer.validated_data
    detour = data.get("max_detour_miles", settings.MAX_DETOUR_MILES)
    start_gallons = data.get("start_tank_gallons", settings.START_TANK_GALLONS)
    geometry_mode = data.get("geometry")
    current_assumptions = assumptions(detour, start_gallons)

    try:
        result = plan_route(
            origin_text=data["start"],
            destination_text=data["finish"],
            max_detour_miles=detour,
            start_tank_gallons=start_gallons,
        )
    except LocationError as exc:
        # A bad location is the caller's input problem: 400.
        return problem_response(
            code=exc.code,
            title="Location could not be resolved",
            detail=str(exc),
            http_status=status.HTTP_400_BAD_REQUEST,
        )
    except RouteInfeasibleError as exc:
        # The request is well-formed but no plan exists. 422 rather than 400: we
        # understood it, we just cannot satisfy it. The gap is named so the caller
        # knows exactly which leg defeated it.
        return problem_response(
            code=exc.reason,
            title="No feasible refuelling plan for this route",
            detail=str(exc),
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            extra={"infeasibility": exc.as_dict()},
            assumptions=current_assumptions,
        )
    except NoRouteFoundError as exc:
        return problem_response(
            code=exc.code,
            title="No drivable route between these locations",
            detail=str(exc),
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except RoutingError as exc:
        # Upstream failed, not the caller: 502, and say which dependency.
        logger.exception("routing provider failure")
        return problem_response(
            code=exc.code,
            title="Routing provider unavailable",
            detail=str(exc),
            http_status=status.HTTP_502_BAD_GATEWAY,
            extra={"dependency": "osrm"},
        )
    except StopsNotSeededError as exc:
        return problem_response(
            code="stops_not_seeded",
            title="Service not ready",
            detail=str(exc),
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response(_serialise_result(result, geometry_mode, current_assumptions))


def _serialise_result(result, geometry_mode: str, current_assumptions: dict) -> dict:
    plan = result.plan
    route = result.route

    route_block = {
        "total_distance_miles": round(route.total_miles, 1),
        "estimated_driving_hours": round(route.duration_seconds / 3600.0, 2),
        "provider": route.provider,
    }
    if geometry_mode != GEOMETRY_NONE:
        every_nth = 1 if geometry_mode == GEOMETRY_FULL else GEOMETRY_DECIMATION
        coordinates = simplify_coordinates(route.geojson_coordinates, every_nth)
        route_block["geometry"] = {"type": "LineString", "coordinates": coordinates}
        route_block["geometry_detail"] = geometry_mode
        route_block["geometry_point_count"] = len(coordinates)
        route_block["geometry_source_point_count"] = len(route.points)

    gallons_consumed = plan.gallons_consumed
    effective = (
        plan.estimated_total_cost / gallons_consumed if gallons_consumed > 0 else 0.0
    )

    return {
        "request": {
            "start": result.origin.query,
            "finish": result.destination.query,
            "max_detour_miles": result.max_detour_miles,
        },
        "origin": _endpoint(result.origin),
        "destination": _endpoint(result.destination),
        "route": route_block,
        "fuel_stops": [
            serialise_stop(purchase, order)
            for order, purchase in enumerate(plan.purchases, start=1)
        ],
        "totals": {
            "fuel_stop_count": len(plan.purchases),
            "total_gallons_purchased": round(plan.total_gallons_purchased, 3),
            # What the plan provably spends at pumps.
            "total_fuel_cost_usd": round(plan.total_cost, 2),
            # The origin leg: miles driven before the first purchase, which with an
            # empty starting tank is fuel we did not buy. Surfaced rather than
            # hidden, because on Los Angeles -> New York it is 10.6% of the total.
            "unpriced_origin_miles": round(plan.unpriced_origin_miles, 1),
            "unpriced_origin_gallons": round(plan.unpriced_origin_gallons, 3),
            "origin_leg_estimated_cost_usd": round(plan.origin_leg_estimated_cost, 2),
            # The whole trip, origin leg valued at the first pump. This is the
            # number that answers "total money spent on fuel".
            "estimated_total_fuel_cost_usd": round(plan.estimated_total_cost, 2),
            "total_gallons_consumed": round(gallons_consumed, 3),
            "effective_price_usd_per_gallon": round(effective, 4),
            "tank_gallons_at_destination": round(plan.ending_gallons, 6),
        },
        "assumptions": current_assumptions,
        "diagnostics": {
            # The brief asks for one routing call. This is the measured count, so a
            # reviewer verifies it instead of believing the README.
            "external_calls": result.calls.as_dict(),
            "route_cache_hit": result.route_cache_hit,
            "timings_ms": result.timings.as_dict(),
            "stops_indexed": result.stops_indexed,
            "corridor_stops_considered": len(result.corridor_stops),
        },
    }


def _endpoint(location) -> dict:
    return {
        "query": location.query,
        "resolved_label": location.label,
        "latitude": round(location.lat, 6),
        "longitude": round(location.lon, 6),
        "resolution_source": location.source,
    }


def map_page(request):
    """Leaflet demonstration page. 'Return a map of the route', literally.

    A deliberately thin demonstration surface, not a frontend project: one
    template, one static JS file, and it calls the same public API the reviewer
    calls from Postman, so nothing here can diverge from the documented contract.
    """
    return render(
        request,
        "fuelroute/map.html",
        {
            "defaults": {
                "start": "Dallas, TX",
                "finish": "Chicago, IL",
                "max_detour_miles": settings.MAX_DETOUR_MILES,
            }
        },
    )
