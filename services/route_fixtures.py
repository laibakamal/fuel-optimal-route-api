"""Disk-backed route cache, for reproducible benchmarking and offline runs.

The in-process route cache (locmem) dies with the process, so a fresh
`manage.py benchmark` always starts cold and must call OSRM again. That makes the
benchmark unreproducible without network access and repeatedly bills a free
service for the same twelve answers.

This wraps a RouteProvider and persists each raw provider response under
.cache/route_fixtures/. On a hit nothing leaves the machine. In `offline` mode a
miss is an error rather than a silent network call - which is the bug this fixes:
an earlier --offline flag only *claimed* not to touch the network.

Deliberately NOT used by the API itself: a stale route on disk should never be
served to a caller. This is a benchmarking and test aid.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from services.routing import Route, RouteProvider, RoutingError, _parse_route

logger = logging.getLogger(__name__)


class FixtureBackedProvider:
    def __init__(
        self,
        inner: RouteProvider,
        directory: Path,
        offline: bool = False,
    ) -> None:
        self.inner = inner
        self.directory = directory
        self.offline = offline
        self.network_calls = 0
        self.fixture_hits = 0
        directory.mkdir(parents=True, exist_ok=True)

    def _path(self, origin: tuple[float, float], destination: tuple[float, float]) -> Path:
        key = f"{origin[0]:.5f},{origin[1]:.5f};{destination[0]:.5f},{destination[1]:.5f}"
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        return self.directory / f"{digest}.json"

    def fetch(self, origin, destination) -> Route:
        path = self._path(origin, destination)
        if path.exists():
            self.fixture_hits += 1
            payload = json.loads(path.read_text())
            # external_calls=0: this cost nothing, and the counter must not claim
            # a call that never happened.
            return _parse_route(payload["response"], provider="fixture", external_calls=0)

        if self.offline:
            raise RoutingError(
                f"offline mode: no route fixture for {origin} -> {destination} "
                f"({path.name}). Run once without --offline to record it."
            )

        route = self.inner.fetch(origin, destination)
        self.network_calls += route.external_calls
        raw = getattr(self.inner, "last_payload", None)
        if raw is not None:
            path.write_text(json.dumps({"key": f"{origin};{destination}", "response": raw}))
        return route
