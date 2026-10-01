"""Rate-limited Nominatim fallback with an on-disk cache.

Only reached for the handful of stops the offline gazetteers cannot resolve
(measured: 18 of 6,626 before this fallback, 0.27%). It exists so the pipeline
never silently drops a row, not as a bulk geocoding strategy - 8,100 live
geocoding calls is exactly what the offline join is designed to avoid.

Honours the Nominatim usage policy: max 1 request/second, descriptive
User-Agent, and results cached on disk so a re-run makes zero new calls.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
MIN_SECONDS_BETWEEN_CALLS = 1.1  # policy is 1/s; 1.1 leaves margin for clock skew


class CachedNominatim:
    def __init__(
        self, cache_path: Path, user_agent: str, offline_only: bool = False
    ) -> None:
        self.cache_path = cache_path
        self.user_agent = user_agent
        #: When True, answer only from the committed cache and never touch the
        #: network. Lets the pipeline run fully offline and still reproduce the
        #: complete artefact.
        self.offline_only = offline_only
        self.calls_made = 0
        self.cache_hits = 0
        self._last_call = 0.0
        self._cache: dict[str, list[float] | None] = {}
        if cache_path.exists():
            try:
                self._cache = json.loads(cache_path.read_text())
            except json.JSONDecodeError:
                logger.warning("geocode cache corrupt, starting empty: %s", cache_path)

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < MIN_SECONDS_BETWEEN_CALLS:
            time.sleep(MIN_SECONDS_BETWEEN_CALLS - elapsed)
        self._last_call = time.monotonic()

    def lookup(self, city: str, state: str) -> tuple[float, float] | None:
        key = f"{city.strip().upper()}|{state.strip().upper()}"
        if key in self._cache:
            self.cache_hits += 1
            hit = self._cache[key]
            return (hit[0], hit[1]) if hit else None
        if self.offline_only:
            return None

        self._throttle()
        params = urllib.parse.urlencode(
            {
                "q": f"{city}, {state}, USA",
                "format": "json",
                "limit": "1",
                "countrycodes": "us",
            }
        )
        request = urllib.request.Request(
            f"{NOMINATIM_URL}?{params}", headers={"User-Agent": self.user_agent}
        )
        result: list[float] | None = None
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            self.calls_made += 1
            if payload:
                result = [float(payload[0]["lat"]), float(payload[0]["lon"])]
        except Exception as exc:  # network/parse failures must not abort the run
            logger.warning("nominatim lookup failed for %s, %s: %s", city, state, exc)
            return None

        self._cache[key] = result
        return (result[0], result[1]) if result else None

    def flush(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache, indent=0, sort_keys=True))
