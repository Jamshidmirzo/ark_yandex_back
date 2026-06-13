"""URL configuration for the config project.

All /api/v1/* traffic is reverse-proxied to the real backend
(UPSTREAM_API_BASE) by config.gateway — see INTEGRATION.md.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path

from car_orders.views import (
    ClaimCheckBatchView,
    ClaimCheckView,
    DriverLocationView,
    DriverPositionsView,
    LocationBatchView,
    DriverShiftView,
    DriverShiftsView,
    EstimateView,
    ExtendView,
    FleetLiveView,
    LiveLocationView,
    MetaBatchView,
    MyOverlayOrdersView,
    OrderMetaView,
    OverlayClaimView,
    OverlayReleaseView,
    ReassignView,
    TripStateView,
)
from config.gateway import gateway
from core.views import health

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    path("healthcheck/", health, name="healthcheck"),
    path("api/v1/car-orders/estimate/", EstimateView.as_view(), name="car-order-estimate"),
    path("api/v1/car-orders/fleet/live/", FleetLiveView.as_view(), name="car-order-fleet-live"),
    path(
        "api/v1/car-orders/drivers/me/overlay-orders/",
        MyOverlayOrdersView.as_view(),
        name="car-order-my-overlay-orders",
    ),
    path(
        "api/v1/car-orders/drivers/me/location/",
        DriverLocationView.as_view(),
        name="car-order-driver-location",
    ),
    # The mobile app's batched GPS uploads (offline queue). Top-level path — NOT
    # under /car-orders/ — to match what the app actually posts. Before gateway.
    path("api/v1/location/batch/", LocationBatchView.as_view(), name="location-batch"),
    path(
        "api/v1/car-orders/drivers/positions/",
        DriverPositionsView.as_view(),
        name="car-order-driver-positions",
    ),
    path(
        "api/v1/car-orders/drivers/me/shift/",
        DriverShiftView.as_view(),
        name="car-order-driver-shift",
    ),
    path(
        "api/v1/car-orders/drivers/shifts/",
        DriverShiftsView.as_view(),
        name="car-order-driver-shifts",
    ),
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
        "api/v1/car-orders/claim-check-batch/",
        ClaimCheckBatchView.as_view(),
        name="car-order-claim-check-batch",
    ),
    path(
        "api/v1/car-orders/meta-batch/",
        MetaBatchView.as_view(),
        name="car-order-meta-batch",
    ),
    path(
        "api/v1/car-orders/<int:pk>/overlay-claim/",
        OverlayClaimView.as_view(),
        name="car-order-overlay-claim",
    ),
    path(
        "api/v1/car-orders/<int:pk>/overlay-release/",
        OverlayReleaseView.as_view(),
        name="car-order-overlay-release",
    ),
    path(
        "api/v1/car-orders/<int:pk>/trip-state/",
        TripStateView.as_view(),
        name="car-order-trip-state",
    ),
    path("api/v1/car-orders/<int:pk>/extend/", ExtendView.as_view(), name="car-order-extend"),
    path(
        "api/v1/car-orders/<int:pk>/reassign/",
        ReassignView.as_view(),
        name="car-order-reassign",
    ),
    # Transparent gateway → real DEV backend (demo.ark.glob.uz). Keep last.
    re_path(r"^api/v1/(?P<path>.*)$", gateway, name="gateway"),
]

if settings.DEBUG:
    urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
