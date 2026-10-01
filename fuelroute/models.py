"""Domain schema.

Two tables, because the source data has two distinct entities and conflating them
would lose information:

  TruckStop         one physical truck stop. Identity + resolved location.
  PriceObservation  one observed retail diesel price at a stop.

The split is driven by measurement, not taste. In the supplied CSV, 678 of 6,738
distinct OPIS Truckstop IDs appear on more than one row, and 597 of those differ
only in Retail Price - they are repeated price readings of the same physical
stop. Flattening them into a single price column on TruckStop would throw away
7,531 - 6,626 = 905 observations and hide the fact that the quoted price is an
aggregate.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from services.geo import US_BBOX


class TruckStopQuerySet(models.QuerySet):
    def in_country(self, country: str) -> TruckStopQuerySet:
        return self.filter(country=country)

    def in_bbox(
        self, min_lat: float, min_lon: float, max_lat: float, max_lon: float
    ) -> TruckStopQuerySet:
        """Stops inside a lat/lon rectangle.

        This is the query the (latitude, longitude) index exists for. It is used
        by `verify_stops` and is the DB-backed equivalent of the in-memory
        corridor prefilter, kept so the index is not speculative.
        """
        return self.filter(
            latitude__gte=min_lat,
            latitude__lte=max_lat,
            longitude__gte=min_lon,
            longitude__lte=max_lon,
        )

    def for_optimizer(self) -> TruckStopQuerySet:
        """Everything the optimiser needs, in one query.

        prefetch_related on the reverse FK rather than select_related: this is a
        one-to-many, so a join would multiply the stop rows by their observation
        count. Two queries beat 6,626 (the N+1 this avoids).
        """
        return (
            self.in_country(TruckStop.Country.US)
            .prefetch_related("price_observations")
            .order_by("opis_id")
        )


class TruckStop(models.Model):
    class Country(models.TextChoices):
        US = "US", "United States"
        CA = "CA", "Canada"

    class GeocodeSource(models.TextChoices):
        CENSUS = "census_gazetteer", "US Census Gazetteer"
        GEONAMES = "geonames", "GeoNames populated place"
        GEONAMES_LANDMARK = "geonames_landmark", "GeoNames landmark proxy"
        NOMINATIM = "nominatim", "Nominatim"

    # Natural key from the source system. CharField, not an integer, even though
    # every value in the supplied file happens to be numeric: this is an opaque
    # external identifier that we never do arithmetic on, and treating it as text
    # survives leading zeros and a format change upstream. unique=True gives us
    # the index that seed_stops' upsert needs.
    opis_id = models.CharField(max_length=16, unique=True, verbose_name="OPIS Truckstop ID")

    name = models.CharField(max_length=128)
    # Highway-exit descriptors like "I-44, EXIT 283 & US-69", not street
    # addresses: 96.4% of rows carry a highway/exit token and 0.1% look like a
    # street address. Kept verbatim for display and future exit-level refinement.
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2)
    country = models.CharField(max_length=2, choices=Country.choices, default=Country.US)
    rack_id = models.CharField(max_length=16, blank=True)

    # FloatField, not DecimalField: coordinates are measurements with real
    # uncertainty (our own city-centroid error is ~1.6 mi median), so fixed-point
    # precision would imply an accuracy the data does not have. Prices get
    # Decimal because money must be exact; coordinates do not.
    latitude = models.FloatField(
        validators=[MinValueValidator(-90.0), MaxValueValidator(90.0)]
    )
    longitude = models.FloatField(
        validators=[MinValueValidator(-180.0), MaxValueValidator(180.0)]
    )

    # Provenance of the coordinate, so answer quality is auditable per stop
    # rather than being a single global claim.
    geocode_source = models.CharField(max_length=32, choices=GeocodeSource.choices)
    #: Max separation between same-name candidate places, in miles. 0 when the
    #: (city, state) pair was unambiguous. Non-zero means we picked one of
    #: several same-named towns and the stop may be in the wrong one.
    geocode_ambiguity_miles = models.FloatField(default=0.0)

    objects = TruckStopQuerySet.as_manager()

    class Meta:
        constraints = [
            # Enforced in the database, not just in Python: a bad coordinate that
            # reaches the optimiser produces a confidently wrong route, so this is
            # worth a hard constraint.
            models.CheckConstraint(
                condition=models.Q(latitude__gte=-90.0, latitude__lte=90.0),
                name="truckstop_latitude_in_range",
            ),
            models.CheckConstraint(
                condition=models.Q(longitude__gte=-180.0, longitude__lte=180.0),
                name="truckstop_longitude_in_range",
            ),
        ]
        indexes = [
            # Serves TruckStopQuerySet.in_bbox(). Latitude leads because it is the
            # more selective of the two for a corridor-shaped region on
            # continental routes, and a composite b-tree can only range-scan on
            # its leading column.
            models.Index(fields=["latitude", "longitude"], name="truckstop_lat_lon_idx"),
        ]
        # Deliberately NOT indexed: `country` and `state`.
        # country is 98.3% 'US' across 6,738 rows, so a query planner will
        # correctly prefer a sequential scan and the index would be dead weight
        # that still has to be maintained on write. state has 48 distinct values
        # over the same 6,738 rows; at this cardinality a full scan is sub-
        # millisecond. Indexes are added here when a measured query needs one.
        ordering = ["opis_id"]
        verbose_name = "truck stop"

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state})"

    @property
    def is_in_us_bbox(self) -> bool:
        min_lat, min_lon, max_lat, max_lon = US_BBOX
        return (
            min_lat <= self.latitude <= max_lat and min_lon <= self.longitude <= max_lon
        )


class PriceObservation(models.Model):
    """One observed retail diesel price, in USD per gallon.

    There is deliberately no `observed_at` field. The source CSV has no date
    column, so the observations cannot be ordered or dated, and inventing a
    timestamp would let callers believe we know which price is current. We do
    not. The API reports the aggregation basis in its `assumptions` object
    instead of implying recency.
    """

    stop = models.ForeignKey(
        TruckStop,
        on_delete=models.CASCADE,
        related_name="price_observations",
        # Django indexes an FK by default; that index serves the
        # prefetch_related in TruckStopQuerySet.for_optimizer().
    )
    # Decimal, not float: this is money and it is compared and summed. The source
    # carries up to 8 decimal places (e.g. 3.00733333), so the column stores the
    # observation exactly as supplied rather than rounding on the way in.
    price_usd_per_gallon = models.DecimalField(max_digits=12, decimal_places=8)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(price_usd_per_gallon__gt=Decimal("0")),
                name="priceobservation_price_positive",
            ),
        ]
        # No unique_together on (stop, price): 104 stops legitimately record the
        # same price twice, so a uniqueness constraint would silently discard
        # real observations. Verified against the supplied file before deciding.
        indexes = []
        verbose_name = "price observation"

    def __str__(self) -> str:
        return f"{self.stop.opis_id} @ ${self.price_usd_per_gallon}"
