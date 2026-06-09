"""URL configuration for the config project.

All /api/v1/* traffic is reverse-proxied to the real backend
(UPSTREAM_API_BASE) by config.gateway — see INTEGRATION.md.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path

from car_orders.views import (
    ClaimCheckView,
    EstimateView,
    LiveLocationView,
    OrderMetaView,
    TripStateView,
)
from config.gateway import gateway
from core.views import health

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    # New feature endpoints served LOCALLY (added here, data from demo elsewhere).
    # Must come BEFORE the gateway catch-all. login/drivers/garage/car-orders
    # stay proxied to the real backend.
    path("api/v1/car-orders/estimate/", EstimateView.as_view(), name="car-order-estimate"),
    path(
        "api/v1/car-orders/<int:pk>/live-location/",
        LiveLocationView.as_view(),
        name="car-order-live-location",
    ),
    path("api/v1/car-orders/<int:pk>/meta/", OrderMetaView.as_view(), name="car-order-meta"),
    path(
        "api/v1/car-orders/<int:pk>/claim-check/",
        ClaimCheckView.as_view(),
        name="car-order-claim-check",
    ),
    path(
        "api/v1/car-orders/<int:pk>/trip-state/",
        TripStateView.as_view(),
        name="car-order-trip-state",
    ),
    # Transparent gateway → real DEV backend (demo.ark.glob.uz). Keep last.
    re_path(r"^api/v1/(?P<path>.*)$", gateway, name="gateway"),
]

if settings.DEBUG:
    urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
