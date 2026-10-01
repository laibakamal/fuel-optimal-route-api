from django.apps import AppConfig


class FuelrouteConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fuelroute"

    # Index warmup deliberately does NOT live here. Building the stop index reads
    # the database, and Django rightly warns that querying during app
    # initialisation is unsafe - at that point the app registry is still being
    # populated. Warmup belongs to process startup, which for both `runserver`
    # and gunicorn means loading the WSGI application: see config/wsgi.py.
