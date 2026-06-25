"""URL configuration for the config project.

All /api/v1/* traffic is reverse-proxied to the real backend
(UPSTREAM_API_BASE) by config.gateway — see INTEGRATION.md.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path

from car_orders.views import (
    AutoDispatchView,
    CarOrderTemplateDetailView,
    CarOrderTemplatesView,
    ClaimCheckBatchView,
    ClaimCheckView,
    admin_approve_overlay,
    car_order_proxy,
    reject_overlay,
    DriverLocationView,
    DriverPositionsView,
    DriverShiftView,
    DriverShiftsView,
    EstimateView,
    ExtendView,
    FleetLiveView,
    GeocodeView,
    LiveLocationView,
    MetaBatchView,
    MyActiveOrderView,
    MyOverlayOrdersView,
    NoShowView,
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
    # Server-side address lookup proxy (keeps the browser off OSM's rate-limited
    # public server). Before the catch-all so demo never sees it.
    path("api/v1/car-orders/geocode/", GeocodeView.as_view(), name="car-order-geocode"),
    # Reusable order «заготовки» (local form-prefill overlay). Before the catch-all
    # so demo never sees them — the order itself is still created upstream.
    path(
        "api/v1/car-orders/templates/",
        CarOrderTemplatesView.as_view(),
        name="car-order-templates",
    ),
    path(
        "api/v1/car-orders/templates/<int:pk>/",
        CarOrderTemplateDetailView.as_view(),
        name="car-order-template-detail",
    ),
    path("api/v1/car-orders/fleet/live/", FleetLiveView.as_view(), name="car-order-fleet-live"),
    path(
        "api/v1/car-orders/drivers/me/overlay-orders/",
        MyOverlayOrdersView.as_view(),
        name="car-order-my-overlay-orders",
    ),
    # The caller's single active order, reconciled with our overlay. Before the
    # catch-all so it isn't proxied to demo (which has no such route → 404).
    path(
        "api/v1/car-orders/me/active-order/",
        MyActiveOrderView.as_view(),
        name="car-order-my-active-order",
    ),
    path(
        "api/v1/car-orders/drivers/me/location/",
        DriverLocationView.as_view(),
        name="car-order-driver-location",
    ),
    path(
        "api/v1/car-orders/drivers/positions/",
        DriverPositionsView.as_view(),
        name="car-order-driver-positions",
    ),
    path(
        "api/v1/car-orders/auto-dispatch/",
        AutoDispatchView.as_view(),
        name="car-order-auto-dispatch",
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
    # «Клиент не вышел» — cancel an at_client order whose client never showed.
    # Before the gateway catch-all so demo never sees it.
    path(
        "api/v1/car-orders/<int:pk>/no-show/",
        NoShowView.as_view(),
        name="car-order-no-show",
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
    # Hook on demo admin-approve: proxy to demo + flip OrderMeta.dispatchable so
    # the auto-dispatcher sees the now-approved order. Before the gateway catch-all.
    path(
        "api/v1/car-orders/<int:pk>/admin-approve/",
        admin_approve_overlay,
        name="car-order-admin-approve",
    ),
    # Hook on demo reject: proxy to demo + tear down OUR overlay (CANCELLED) so a
    # rejected order leaves the auto-dispatch queue. Before the gateway catch-all.
    path(
        "api/v1/car-orders/<int:pk>/reject/",
        reject_overlay,
        name="car-order-reject",
    ),
    # Proxy the demo car-order LIST + DETAIL but inject our reconciled effective_status
    # (single source of truth). Must sit AFTER the specific /car-orders/<pk>/<action>/
    # routes above (longer paths win) and BEFORE the catch-all. Non-GET passes through.
    path("api/v1/car-orders/", car_order_proxy, name="car-order-list-proxy"),
    path("api/v1/car-orders/<int:pk>/", car_order_proxy, name="car-order-detail-proxy"),
    # Transparent gateway → real DEV backend (demo.ark.glob.uz). Keep last.
    re_path(r"^api/v1/(?P<path>.*)$", gateway, name="gateway"),
]

if settings.DEBUG:
    urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
