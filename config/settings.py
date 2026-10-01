"""Django settings. Single source of truth for the vehicle/optimiser constants,
so the API's `assumptions` object and the optimiser can never disagree.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# .env is optional: every setting below has a working default so a clean clone
# runs with no configuration at all.
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# --- Core -------------------------------------------------------------------
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "dev-only-insecure-key-override-via-env-in-production"
)
DEBUG = _env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "rest_framework",
    "fuelroute",
]
# No auth/sessions/admin: this is a read-only public API with one endpoint and a
# demo map page. Carrying the auth stack would add migrations and middleware that
# nothing here uses.

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.gzip.GZipMiddleware",  # route geometry compresses ~8x
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }
]

# --- Database ---------------------------------------------------------------
# One environment variable switches engines, so a reviewer who will not run
# Docker still gets a working database with zero setup.
if os.environ.get("DB_ENGINE", "sqlite").strip().lower() == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "fuelroute"),
            "USER": os.environ.get("POSTGRES_USER", "fuelroute"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Static -----------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- Cache ------------------------------------------------------------------
# Local-memory cache: process-local, zero setup, and sufficient because what we
# cache (route geometry per rounded origin/destination) is derived data that is
# cheap to recompute on a cold process. Redis would be the production choice and
# needs only a change here.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuelroute-routes",
        "TIMEOUT": int(os.environ.get("ROUTE_CACHE_TTL_SECONDS", "86400")),
        "OPTIONS": {"MAX_ENTRIES": 512},
    }
}

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "UNAUTHENTICATED_USER": None,
    "EXCEPTION_HANDLER": "fuelroute.errors.problem_detail_handler",
}

# --- Domain constants -------------------------------------------------------
# 500-mile range at 10 mpg == a 50-gallon tank. Both numbers come from the brief;
# RANGE is derived rather than configured so the two can never drift apart.
TANK_CAPACITY_GALLONS = _env_float("TANK_CAPACITY_GALLONS", 50.0)
MILES_PER_GALLON = _env_float("MILES_PER_GALLON", 10.0)
VEHICLE_RANGE_MILES = TANK_CAPACITY_GALLONS * MILES_PER_GALLON

# Default 0: the reported total then covers all fuel the trip consumes, which is
# what "total money spent on fuel" asks for. See README for the semantics.
START_TANK_GALLONS = _env_float("START_TANK_GALLONS", 0.0)

# Default 10 rather than 5. Measured city-centroid geocoding error is median
# 1.59 mi / p90 4.54 mi, so a 5-mile corridor would filter at a finer resolution
# than the coordinates actually have. 10 exceeds p90. Overridable per request.
MAX_DETOUR_MILES = _env_float("MAX_DETOUR_MILES", 10.0)
MAX_DETOUR_MILES_LIMIT = 50.0

PRICE_BASIS = os.environ.get("PRICE_BASIS", "median")

# --- External routing -------------------------------------------------------
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
OSRM_FALLBACK_BASE_URL = os.environ.get(
    "OSRM_FALLBACK_BASE_URL", "https://routing.openstreetmap.de/routed-car"
).rstrip("/")
OSRM_TIMEOUT_SECONDS = _env_float("OSRM_TIMEOUT_SECONDS", 20.0)
HTTP_USER_AGENT = os.environ.get(
    "HTTP_USER_AGENT",
    "fuel-route-api/1.0 (backend assessment; contact: see repo README)",
)

DATA_DIR = BASE_DIR / "data"
CACHE_DIR = BASE_DIR / ".cache"
STOPS_ARTEFACT = DATA_DIR / "stops.json.gz"
FUEL_CSV = DATA_DIR / "fuel-prices-for-be-assessment.csv"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
