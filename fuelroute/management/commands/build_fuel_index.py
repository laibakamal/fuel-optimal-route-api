"""Offline pipeline: fuel CSV + public gazetteers -> data/stops.json.gz

Idempotent and re-runnable. A reviewer never needs to run this: the derived
artefact is committed, and `seed_stops` loads it. Run it to regenerate after
changing the CSV or the normalisation rules.

    python manage.py build_fuel_index
    python manage.py build_fuel_index --no-geocode-fallback   # fully offline
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from services import gazetteer
from services.download import download_if_missing
from services.fuel_data import parse_fuel_csv
from services.geo import in_us, in_us_or_canada
from services.geocode_fallback import CachedNominatim

ARTEFACT_VERSION = 1


class Command(BaseCommand):
    help = "Resolve coordinates for every fuel stop and write data/stops.json.gz"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--csv", type=Path, default=settings.FUEL_CSV, help="Source fuel-price CSV."
        )
        parser.add_argument(
            "--out", type=Path, default=settings.STOPS_ARTEFACT, help="Output artefact."
        )
        parser.add_argument(
            "--places-out",
            type=Path,
            default=settings.PLACES_ARTEFACT,
            help="Place index for resolving free-text user locations.",
        )
        parser.add_argument(
            "--no-geonames",
            action="store_true",
            help="Census tier only. Skips a ~71 MiB download; lowers the match rate.",
        )
        parser.add_argument(
            "--no-geocode-fallback",
            action="store_true",
            help="Do not call Nominatim for stops the offline tiers cannot resolve.",
        )
        parser.add_argument(
            "--force-download", action="store_true", help="Re-fetch cached gazetteers."
        )

    def handle(self, *args, **opts) -> None:
        csv_path: Path = opts["csv"]
        if not csv_path.exists():
            raise CommandError(f"CSV not found: {csv_path}")

        ua = settings.HTTP_USER_AGENT
        cache_dir: Path = settings.CACHE_DIR

        # --- 1. Parse + dedup ------------------------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING("1. Parsing CSV"))
        stops, stats = parse_fuel_csv(csv_path)
        for key in (
            "rows_read",
            "distinct_stops",
            "stops_with_multiple_observations",
            "name_variants_collapsed",
            "rows_skipped_bad_price",
            "rows_skipped_missing_id",
        ):
            self.stdout.write(f"   {key:36s} {stats[key]}")

        by_country: dict[str, int] = {}
        for stop in stops.values():
            by_country[stop.country] = by_country.get(stop.country, 0) + 1
        self.stdout.write(f"   {'stops by country':36s} {by_country}")

        # --- 2. Load gazetteers ---------------------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING("2. Loading offline gazetteers"))
        census_zip = download_if_missing(
            gazetteer.CENSUS_URL,
            cache_dir / "census_gaz_place.zip",
            ua,
            force=opts["force_download"],
        )
        census = gazetteer.load_census_places(census_zip)
        self.stdout.write(f"   census keys                          {len(census)}")

        tiers = [(gazetteer.SOURCE_CENSUS, census)]
        geonames_path = None
        if not opts["no_geonames"]:
            geonames_zip = download_if_missing(
                gazetteer.GEONAMES_URL,
                cache_dir / "geonames_us.zip",
                ua,
                force=opts["force_download"],
            )
            geonames_path = geonames_zip
            populated, landmarks = gazetteer.load_geonames(geonames_zip)
            self.stdout.write(f"   geonames populated keys              {len(populated)}")
            self.stdout.write(f"   geonames landmark keys               {len(landmarks)}")
            tiers.append((gazetteer.SOURCE_GEONAMES, populated))
            tiers.append((gazetteer.SOURCE_GEONAMES_LANDMARK, landmarks))

        index = gazetteer.GazetteerIndex(tiers)

        # --- 3. Resolve ------------------------------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING("3. Resolving coordinates"))
        resolved: dict[str, dict] = {}
        counts = {
            gazetteer.SOURCE_CENSUS: 0,
            gazetteer.SOURCE_GEONAMES: 0,
            gazetteer.SOURCE_GEONAMES_LANDMARK: 0,
            "nominatim": 0,
        }
        ambiguous: list[str] = []
        unresolved: list[tuple[str, str, str]] = []
        bbox_rejected: list[tuple[str, str, str, float, float]] = []

        # Non-US stops are out of scope by design: the brief routes between two US
        # locations, and the CSV's 112 Canadian stops carry prices ~31% higher
        # with no currency column to disambiguate them. They are retained in the
        # artefact's stats but not geocoded (our gazetteers are US-only) and
        # never offered to the optimiser. Reported separately from genuine
        # failures so the two are never confused.
        out_of_scope = [
            (s.opis_id, s.city, s.state, s.country)
            for s in stops.values()
            if s.country != "US"
        ]

        for stop in stops.values():
            if stop.country != "US":
                continue
            hit = index.resolve(stop.city, stop.state)
            if hit is None:
                unresolved.append((stop.opis_id, stop.city, stop.state))
                continue
            # Validate rather than trust the join: a name collision across a
            # state line would otherwise place a stop in the wrong country.
            if not in_us_or_canada(hit.lat, hit.lon):
                bbox_rejected.append(
                    (stop.opis_id, stop.city, stop.state, hit.lat, hit.lon)
                )
                continue
            counts[hit.source] += 1
            if hit.is_ambiguous:
                ambiguous.append(stop.opis_id)
            resolved[stop.opis_id] = {
                "lat": round(hit.lat, 6),
                "lon": round(hit.lon, 6),
                "src": hit.source,
                "amb": round(hit.ambiguity_miles, 2) if hit.is_ambiguous else 0.0,
            }

        # --- 4. Fallback geocoder for the remainder --------------------------
        geocoder_calls = 0
        if unresolved:
            # --no-geocode-fallback means "make no network calls", NOT "ignore the
            # cache". data/geocode_cache.json is committed, so the offline-only
            # path still reproduces the full artefact byte-for-byte.
            offline_only = opts["no_geocode_fallback"]
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    f"4. Fallback geocoder for {len(unresolved)} unresolved stop(s)"
                    + (" (cache only, no network)" if offline_only else "")
                )
            )
            nominatim = CachedNominatim(
                settings.DATA_DIR / "geocode_cache.json", ua, offline_only=offline_only
            )
            still_unresolved: list[tuple[str, str, str]] = []
            for opis_id, city, state in unresolved:
                hit = nominatim.lookup(city, state)
                if hit and in_us(*hit):
                    counts["nominatim"] += 1
                    resolved[opis_id] = {
                        "lat": round(hit[0], 6),
                        "lon": round(hit[1], 6),
                        "src": "nominatim",
                        "amb": 0.0,
                    }
                    self.stdout.write(f"   resolved {city}, {state}")
                else:
                    still_unresolved.append((opis_id, city, state))
            if not offline_only:
                nominatim.flush()
            geocoder_calls = nominatim.calls_made
            unresolved = still_unresolved
            self.stdout.write(f"   network geocode calls made           {geocoder_calls}")
            self.stdout.write(f"   served from committed cache          {nominatim.cache_hits}")

        # --- 5. Report + write ----------------------------------------------
        # Denominator is US stops, not all stops: the Canadian rows are excluded
        # by design and including them would understate the match rate on the
        # population we actually attempt to resolve.
        us_total = sum(1 for s in stops.values() if s.country == "US")
        matched = len(resolved)
        self.stdout.write(self.style.MIGRATE_HEADING("5. Match report"))
        self.stdout.write(f"   {'in-scope (US) stops':36s} {us_total:6d}")
        for source, n in counts.items():
            self.stdout.write(f"   {source:36s} {n:6d}  ({n / us_total:7.3%})")
        self.stdout.write(
            self.style.SUCCESS(
                f"   {'TOTAL RESOLVED (of US)':36s} {matched:6d}  ({matched / us_total:7.3%})"
            )
        )
        self.stdout.write(f"   {'ambiguous (>10mi apart, flagged)':36s} {len(ambiguous):6d}")
        self.stdout.write(f"   {'rejected by bbox validation':36s} {len(bbox_rejected):6d}")

        # Two distinct exclusion reasons, never conflated:
        #   out of scope  = non-US, excluded by design (PLAN.md Q2)
        #   unresolved    = US stop we genuinely failed to locate
        self.stdout.write(
            f"   {'excluded: out of scope (non-US)':36s} {len(out_of_scope):6d}"
        )
        style = self.style.WARNING if unresolved else self.style.SUCCESS
        self.stdout.write(
            style(f"   {'excluded: UNRESOLVED (US)':36s} {len(unresolved):6d}")
        )
        # Never drop a row silently: every exclusion is named in the output and
        # recorded in the artefact so the README count is auditable.
        for opis_id, city, state in unresolved:
            self.stdout.write(
                self.style.WARNING(f"      unresolved {opis_id}: {city}, {state}")
            )
        for opis_id, city, state, lat, lon in bbox_rejected:
            self.stdout.write(
                self.style.ERROR(f"      bbox-rejected {opis_id}: {city}, {state} @ {lat},{lon}")
            )
        by_province: dict[str, int] = {}
        for _, _, state, _ in out_of_scope:
            by_province[state] = by_province.get(state, 0) + 1
        if by_province:
            self.stdout.write(f"      out-of-scope by province: {by_province}")

        us_resolved = matched
        records = [
            {
                "id": stop.opis_id,
                "name": stop.name,
                "address": stop.address,
                "city": stop.city,
                "state": stop.state,
                "country": stop.country,
                "rack_id": stop.rack_id,
                "prices": stop.prices,
                "lat": resolved[stop.opis_id]["lat"],
                "lon": resolved[stop.opis_id]["lon"],
                "geocode_source": resolved[stop.opis_id]["src"],
                "geocode_ambiguity_miles": resolved[stop.opis_id]["amb"],
            }
            for stop in stops.values()
            if stop.opis_id in resolved
        ]
        records.sort(key=lambda r: int(r["id"]) if r["id"].isdigit() else 0)

        # No wall-clock timestamp: it would make every regeneration a spurious
        # diff. Provenance is tracked by hashing the input instead, which is both
        # deterministic and more useful - it identifies the data, not the run.
        artefact = {
            "artefact_version": ARTEFACT_VERSION,
            "source_csv": csv_path.name,
            "source_csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
            "census_gazetteer_year": gazetteer.CENSUS_YEAR,
            "stats": {
                **{k: v for k, v in stats.items()},
                "stops_by_country": by_country,
                "resolved_total": matched,
                "resolved_us": us_resolved,
                "resolved_by_source": counts,
                "ambiguous_count": len(ambiguous),
                "bbox_rejected_count": len(bbox_rejected),
                "us_stops_total": us_total,
                "excluded_unresolved": [
                    {"id": i, "city": c, "state": s} for i, c, s in unresolved
                ],
                "excluded_out_of_scope": [
                    {"id": i, "city": c, "state": s, "country": k}
                    for i, c, s, k in out_of_scope
                ],
                "nominatim_calls": geocoder_calls,
            },
            "stops": records,
        }

        out: Path = opts["out"]
        out.parent.mkdir(parents=True, exist_ok=True)
        # mtime=0 so regenerating identical data produces an identical file and
        # does not show up as a spurious diff.
        with gzip.GzipFile(out, "wb", mtime=0) as fh:
            fh.write(json.dumps(artefact, separators=(",", ":")).encode())
        self.stdout.write(
            self.style.SUCCESS(
                f"\nWrote {out} ({out.stat().st_size / 1024:.1f} KiB) "
                f"with {len(records)} stops ({us_resolved} US)."
            )
        )

        # --- 6. Place index for free-text endpoint resolution ----------------
        self.stdout.write(self.style.MIGRATE_HEADING("6. Place index"))
        places = gazetteer.build_place_records(census_zip, geonames_path)
        places_out: Path = opts["places_out"]
        places_payload = {
            "artefact_version": ARTEFACT_VERSION,
            "census_gazetteer_year": gazetteer.CENSUS_YEAR,
            "min_geonames_population": gazetteer.PLACE_INDEX_MIN_POPULATION,
            # Positional rows rather than dicts: the key names would otherwise be
            # repeated 53,675 times and triple the artefact size.
            "columns": ["name", "state", "lat", "lon"],
            "places": [list(record) for record in places],
        }
        with gzip.GzipFile(places_out, "wb", mtime=0) as fh:
            fh.write(json.dumps(places_payload, separators=(",", ":")).encode())
        self.stdout.write(
            self.style.SUCCESS(
                f"   Wrote {places_out} ({places_out.stat().st_size / 1024:.1f} KiB) "
                f"with {len(places)} places."
            )
        )
