"""Load data/stops.json.gz into the database. Idempotent.

This is the only data step a reviewer needs to run. The expensive work
(gazetteer joins, coordinate resolution) happened offline in build_fuel_index and
its output is committed, so seeding is a local file read with no network access.

    python manage.py seed_stops
"""
from __future__ import annotations

import gzip
import json
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuelroute.models import PriceObservation, TruckStop
from services.geo import in_us_or_canada

BATCH_SIZE = 1000


class Command(BaseCommand):
    help = "Load the committed stop artefact into the database (idempotent)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--artefact", type=Path, default=settings.STOPS_ARTEFACT)
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete existing stops first instead of upserting.",
        )

    def handle(self, *args, **opts) -> None:
        path: Path = opts["artefact"]
        if not path.exists():
            raise CommandError(
                f"Artefact not found: {path}\n"
                "It is committed to the repo; if you removed it, regenerate with:\n"
                "  python manage.py build_fuel_index"
            )

        with gzip.open(path, "rb") as fh:
            artefact = json.load(fh)

        version = artefact.get("artefact_version")
        if version != 1:
            raise CommandError(f"Unsupported artefact_version {version!r}; expected 1.")

        records = artefact["stops"]
        self.stdout.write(f"Artefact: {path.name}  ({len(records)} stops)")
        self.stdout.write(f"  source CSV sha256: {artefact.get('source_csv_sha256', '?')[:16]}...")

        # Validate before writing: a bad coordinate reaching the optimiser yields
        # a confidently wrong route, so the seed refuses rather than loads it.
        bad = [r for r in records if not in_us_or_canada(r["lat"], r["lon"])]
        if bad:
            raise CommandError(
                f"{len(bad)} stop(s) outside the US/Canada bounding box, refusing to seed. "
                f"First: {bad[0]['id']} {bad[0]['city']}, {bad[0]['state']} "
                f"@ {bad[0]['lat']},{bad[0]['lon']}"
            )

        with transaction.atomic():
            if opts["flush"]:
                deleted, _ = TruckStop.objects.all().delete()
                self.stdout.write(f"  flushed {deleted} row(s)")

            stops = [
                TruckStop(
                    opis_id=r["id"],
                    name=r["name"],
                    address=r["address"],
                    city=r["city"],
                    state=r["state"],
                    country=r["country"],
                    rack_id=r["rack_id"],
                    latitude=r["lat"],
                    longitude=r["lon"],
                    geocode_source=r["geocode_source"],
                    geocode_ambiguity_miles=r["geocode_ambiguity_miles"],
                )
                for r in records
            ]
            # Upsert on the natural key, so re-running updates in place rather
            # than failing on the unique constraint or duplicating rows.
            TruckStop.objects.bulk_create(
                stops,
                batch_size=BATCH_SIZE,
                update_conflicts=True,
                update_fields=[
                    "name",
                    "address",
                    "city",
                    "state",
                    "country",
                    "rack_id",
                    "latitude",
                    "longitude",
                    "geocode_source",
                    "geocode_ambiguity_miles",
                ],
                unique_fields=["opis_id"],
            )

            pk_by_opis = dict(TruckStop.objects.values_list("opis_id", "pk"))

            # Observations are replaced wholesale rather than diffed. They carry no
            # natural key of their own (no date, and 104 stops repeat an identical
            # price), so there is nothing to match an incoming row against. Delete
            # + insert inside the transaction is the honest idempotent operation.
            PriceObservation.objects.all().delete()
            observations = [
                PriceObservation(
                    stop_id=pk_by_opis[r["id"]],
                    price_usd_per_gallon=Decimal(str(price)),
                )
                for r in records
                for price in r["prices"]
            ]
            PriceObservation.objects.bulk_create(observations, batch_size=BATCH_SIZE)

        stop_count = TruckStop.objects.count()
        obs_count = PriceObservation.objects.count()
        us_count = TruckStop.objects.in_country(TruckStop.Country.US).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {stop_count} stops ({us_count} US) "
                f"and {obs_count} price observations."
            )
        )
