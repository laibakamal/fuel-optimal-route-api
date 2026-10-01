"""WSGI entry point, and the right place to warm process-level caches.

`runserver` and gunicorn both load this module via settings.WSGI_APPLICATION, and
it runs *after* the app registry is fully populated - unlike AppConfig.ready(),
where touching the database triggers Django's "Accessing the database during app
initialization is discouraged" warning.

Warming here means no request ever pays the ~400 ms index build. The brief asks
twice for results to be as quick as possible, and a lazily-built index just makes
one unlucky caller pay for everybody else.
"""
from __future__ import annotations

import logging
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()

logger = logging.getLogger(__name__)


def _warm_indexes() -> None:
    from fuelroute.indexes import get_place_index, get_stop_index

    try:
        place_index = get_place_index()
        stop_index = get_stop_index()
    except Exception as exc:  # noqa: BLE001 - never block startup
        # An unseeded database must not stop the process booting, because
        # `manage.py seed_stops` is how a reviewer fixes it.
        logger.warning(
            "index warmup skipped (%s: %s). The first request will build them. "
            "If the database is empty, run: python manage.py seed_stops",
            type(exc).__name__,
            exc,
        )
        return
    logger.info(
        "warm: %d stops in %d grid cells, %d places",
        len(stop_index),
        stop_index.cell_count,
        len(place_index),
    )


_warm_indexes()
