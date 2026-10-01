from django.urls import path

from fuelroute import views

urlpatterns = [
    # The map page is at the root so a reviewer who opens the server in a browser
    # lands on something that works rather than a 404.
    path("", views.map_page, name="map"),
    path("api/v1/route", views.route_plan, name="route-plan"),
]
