from django.urls import include, path
from rest_framework.routers import DefaultRouter

from car_orders.views import (
    CarOrderViewSet,
    CarTypeViewSet,
    CarViewSet,
    DriverViewSet,
    VehicleReportViewSet,
)

router = DefaultRouter()
router.register(r"car-types", CarTypeViewSet, basename="car-type")
router.register(r"cars", CarViewSet, basename="car")
router.register(r"drivers", DriverViewSet, basename="driver")
router.register(r"vehicle-reports", VehicleReportViewSet, basename="vehicle-report")
router.register(r"", CarOrderViewSet, basename="car-order")

urlpatterns = [
    path("", include(router.urls)),
]
