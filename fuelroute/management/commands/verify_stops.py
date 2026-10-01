"""Validate seeded stop data. Read-only; exits non-zero on failure.

Phase 2 of the build asked for coordinates to be validated rather than trusted.
This is that check, as a command so it can run in CI and be re-run by a reviewer.

    python manage.py verify_stops
"""
from __future__ import annotations

import sys
from collections import Counter

from django.core.management.base import BaseCommand
from django.db.models import Count, Max, Min

from fuelroute.models import PriceObservation, TruckStop
from services.geo import CANADA_BBOX, US_BBOX, in_us, in_us_or_canada


class Command(BaseCommand):
    help = "Validate seeded truck-stop coordinates and prices."

    def handle(self, *args, **opts) -> None:
        failures: list[str] = []
        warnings: list[str] = []

        total = TruckStop.objects.count()
        if total == 0:
            self.stderr.write(self.style.ERROR("No stops seeded. Run: manage.py seed_stops"))
            sys.exit(1)

        self.stdout.write(self.style.MIGRATE_HEADING("Coordinate validation"))
        self.stdout.write(f"   stops                                {total}")

        # Hard failure: anything outside US+Canada means the gazetteer join put a
        # stop on the wrong continent.
        outside = [
            s for s in TruckStop.objects.all() if not in_us_or_canada(s.latitude, s.longitude)
        ]
        if outside:
            failures.append(f"{len(outside)} stop(s) outside the US/Canada bounding box")
            for s in outside[:10]:
                self.stdout.write(
                    self.style.ERROR(f"      {s.opis_id} {s.city}, {s.state} @ {s.latitude},{s.longitude}")
                )
        self.stdout.write(f"   outside US+CA bbox                   {len(outside)}")

        # A US-labelled stop whose coordinate is not in the US bbox is a flag, not
        # necessarily an error: the bbox is rectangular and clips a little.
        mislabelled = [
            s
            for s in TruckStop.objects.in_country(TruckStop.Country.US)
            if not in_us(s.latitude, s.longitude)
        ]
        if mislabelled:
            warnings.append(f"{len(mislabelled)} US-labelled stop(s) outside the US bbox")
            for s in mislabelled[:10]:
                self.stdout.write(
                    self.style.WARNING(f"      {s.opis_id} {s.city}, {s.state} @ {s.latitude},{s.longitude}")
                )
        self.stdout.write(f"   US-labelled outside US bbox          {len(mislabelled)}")

        agg = TruckStop.objects.aggregate(
            min_lat=Min("latitude"), max_lat=Max("latitude"),
            min_lon=Min("longitude"), max_lon=Max("longitude"),
        )
        self.stdout.write(
            f"   observed extent                      "
            f"lat {agg['min_lat']:.4f}..{agg['max_lat']:.4f}  "
            f"lon {agg['min_lon']:.4f}..{agg['max_lon']:.4f}"
        )
        self.stdout.write(f"   US bbox                              {US_BBOX}")
        self.stdout.write(f"   Canada bbox                          {CANADA_BBOX}")

        # Null-island and zero-coordinate detection: a failed join often yields 0,0.
        null_island = TruckStop.objects.filter(latitude=0.0, longitude=0.0).count()
        if null_island:
            failures.append(f"{null_island} stop(s) at 0,0 (null island)")
        self.stdout.write(f"   at 0,0 (null island)                 {null_island}")

        self.stdout.write(self.style.MIGRATE_HEADING("Geocode provenance"))
        for source, n in Counter(
            TruckStop.objects.values_list("geocode_source", flat=True)
        ).most_common():
            self.stdout.write(f"   {source:36s} {n:6d}  ({n / total:7.3%})")
        ambiguous = TruckStop.objects.filter(geocode_ambiguity_miles__gt=0).count()
        self.stdout.write(f"   {'flagged ambiguous':36s} {ambiguous:6d}")

        self.stdout.write(self.style.MIGRATE_HEADING("Price validation"))
        obs = PriceObservation.objects.count()
        self.stdout.write(f"   observations                         {obs}")
        nonpositive = PriceObservation.objects.filter(price_usd_per_gallon__lte=0).count()
        if nonpositive:
            failures.append(f"{nonpositive} non-positive price(s)")
        self.stdout.write(f"   non-positive prices                  {nonpositive}")

        orphans = TruckStop.objects.annotate(n=Count("price_observations")).filter(n=0)
        orphan_count = orphans.count()
        if orphan_count:
            failures.append(f"{orphan_count} stop(s) with no price observation")
        self.stdout.write(f"   stops with no price                  {orphan_count}")

        # Implausible prices are a warning, not a failure: $6.40 is high but real
        # for California diesel, so we surface the range rather than reject it.
        extreme = PriceObservation.objects.filter(price_usd_per_gallon__gt=10).count()
        if extreme:
            warnings.append(f"{extreme} price(s) above $10/gal")
        price_agg = PriceObservation.objects.aggregate(
            lo=Min("price_usd_per_gallon"), hi=Max("price_usd_per_gallon")
        )
        self.stdout.write(f"   price range                          ${price_agg['lo']} .. ${price_agg['hi']}")

        self.stdout.write("")
        for w in warnings:
            self.stdout.write(self.style.WARNING(f"WARN  {w}"))
        if failures:
            for f in failures:
                self.stdout.write(self.style.ERROR(f"FAIL  {f}"))
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS("All coordinate and price validations passed."))
