"""Test-only URLconf: mounts the car_orders router (the *standalone* wiring) so the
workflow tests exercise the native CRUD endpoints locally.

In production ``config/urls.py`` runs as a gateway — the ``CarOrderViewSet`` router
is NOT mounted and ``/api/v1/car-orders/*`` falls through to the upstream demo
backend (see [[project-architecture]] gateway-vs-standalone gotcha). Tests point
``ROOT_URLCONF`` here (via ``pytest.mark.urls``) so they hit the local views
instead of proxying to a server that doesn't know the test fixtures.
"""

from django.urls import include, path

urlpatterns = [
    path("api/v1/car-orders/", include("car_orders.urls")),
]
