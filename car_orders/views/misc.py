"""Standalone helper endpoints mounted before the gateway catch-all: the public
route estimate + geocode proxies, and the (team-shared) order-template CRUD."""

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from car_orders import services
from car_orders.models import CarOrderTemplate
from car_orders.permissions import OverlayAuthenticated, acting_driver_id
from car_orders.serializers import CarOrderTemplateSerializer, RouteEstimateSerializer
from config.auth import DemoTokenAuthentication

from .base import _bad_request

__all__ = (
    "EstimateView",
    "GeocodeView",
    "CarOrderTemplatesView",
    "CarOrderTemplateDetailView",
)


class EstimateView(APIView):
    """Standalone route/duration estimate, served locally in the gateway setup.
    Public (AllowAny): it's a pure function of two coordinates with no upstream
    auth needed, the mobile create-order card calls it with the no-auth client,
    and the docs advertise it as auth-free — so it must stay open even when
    REQUIRE_OVERLAY_AUTH is enabled (unlike the privileged overlay views).
    Mounted at /api/v1/car-orders/estimate/ BEFORE the gateway catch-all."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RouteEstimateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        return Response(
            services.estimate_payload(
                data["origin_lat"],
                data["origin_lng"],
                data["dest_lat"],
                data["dest_lng"],
                service_minutes=data.get("service_minutes"),
            )
        )


class GeocodeView(APIView):
    """Server-side geocoding proxy (search + reverse) for the order form's address
    lookup. Public (AllowAny) like :class:`EstimateView` — it's a stateless
    address↔coords helper with no upstream auth. Keeps clients off
    ``nominatim.openstreetmap.org`` directly (OSM blocks browser-origin bursts with
    HTTP 429, which silently empties the form's «Откуда/Куда» suggestions); we add a
    proper User-Agent, a 1 req/s throttle and a day-long cache. Mounted at
    /api/v1/car-orders/geocode/ BEFORE the gateway catch-all.

    - ``GET ?q=<text>``       → ``{"results": [{lat, lng, label}, …]}``
    - ``GET ?lat=<>&lng=<>``  → ``{"label": "<address>"}``
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request):
        from car_orders.services import geocode

        lat = request.query_params.get("lat")
        lng = request.query_params.get("lng")
        if lat is not None and lng is not None:
            try:
                label = geocode.reverse(float(lat), float(lng))
            except (TypeError, ValueError):
                return Response({"detail": "bad lat/lng"}, status=status.HTTP_400_BAD_REQUEST)
            return Response({"label": label})
        return Response({"results": geocode.search(request.query_params.get("q", ""))})


class CarOrderTemplatesView(APIView):
    """Reusable order «заготовки» (e.g. съёмки «Севимли → Сквер»). A LOCAL
    form-prefill overlay — the order itself is still created in demo. Templates are
    SHARED across the team, so GET returns the whole list. Mounted before the
    gateway catch-all.

      • GET  → all templates (ordered by name)
      • POST → create one (records the caller as ``created_by_id`` when known)
    """

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def get(self, request):
        templates = CarOrderTemplate.objects.all()
        return Response(CarOrderTemplateSerializer(templates, many=True).data)

    def post(self, request):
        serializer = CarOrderTemplateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        template = serializer.save(created_by_id=acting_driver_id(request))
        return Response(
            CarOrderTemplateSerializer(template).data, status=status.HTTP_201_CREATED
        )


class CarOrderTemplateDetailView(APIView):
    """Edit / delete a single order template. Any authenticated user may update or
    remove a shared template (it's an internal team tool)."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def patch(self, request, pk):
        template = CarOrderTemplate.objects.filter(pk=pk).first()
        if not template:
            return _bad_request("NOT_FOUND", "Шаблон не найден.")
        serializer = CarOrderTemplateSerializer(template, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(CarOrderTemplateSerializer(template).data)

    def delete(self, request, pk):
        deleted, _ = CarOrderTemplate.objects.filter(pk=pk).delete()
        return Response({"ok": bool(deleted)})
