"""API for the car-orders block.

Workflow (see ТЗ §3):
    draft → pending(submit) → awaiting_driver(admin-approve)
          → in_progress(claim, uses shift car) → completed
          → rejected (dispatcher reject / author cancel, before in_progress)

Permissions mirror ark-backend codenames (``car_order:*``, ``driver:*``,
``garage:*``, ``vehicle_report:*``). Р1 = shift car; Р3 = live location.
"""

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from auth_core.models import AccessGroup, UserAccessGroup
from auth_core.permissions import HasPermission, user_has_permission
from car_orders import dispatch, geometry, scheduling, services
from car_orders.models import (
    Car,
    CarOrder,
    CarOrderTemplate,
    CarType,
    DispatchSettings,
    DriverPosition,
    DriverShift,
    DriverShiftState,
    OrderLiveLocation,
    OrderMeta,
    VehicleReport,
)
from car_orders.permissions import (
    OverlayAuthenticated,
    OverlayDispatcher,
    OverlayDriverOrDispatcher,
    acting_driver_id,
    assignee_driver_id,
)
from car_orders.serializers import (
    CarOrderActivitySerializer,
    CarOrderSerializer,
    CarOrderTemplateSerializer,
    CarOrderWriteSerializer,
    CarSerializer,
    CarTypeSerializer,
    CarTypeWriteSerializer,
    CarWriteSerializer,
    DriverSerializer,
    DriverShiftSerializer,
    LocationSerializer,
    OrderMetaSerializer,
    RouteEstimateSerializer,
    ShiftStartSerializer,
    VehicleReportSerializer,
)
from car_orders.ws import broadcast_location
from config.auth import DemoTokenAuthentication

User = get_user_model()

logger = logging.getLogger(__name__)

DRIVER_GROUP = "Driver"


def _forbidden(message):
    return Response(
        {"error": {"code": "PERMISSION_DENIED", "message": str(message), "details": {}}},
        status=status.HTTP_403_FORBIDDEN,
    )


def _bad_request(code, message):
    return Response(
        {"error": {"code": code, "message": str(message), "details": {}}},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _service_error_response(exc):
    """Map a service-layer error (``OrderError`` / ``OverlayError``) onto the standard
    error response, honouring its code / HTTP-status / details (403 → the shared
    PERMISSION_DENIED shape)."""
    if exc.http_status == status.HTTP_403_FORBIDDEN:
        return _forbidden(exc.message)
    return Response(
        {"error": {"code": exc.code, "message": str(exc.message), "details": exc.details}},
        status=exc.http_status,
    )


def _src(request):
    """Where a request came from, for the tracking log: ``📱 <ip>`` (a real phone)
    vs ``🖥 локально`` (our own server / the simulator on 127.0.0.1).

    AUDIT L2: X-Forwarded-For is client-spoofable — this is for the LOG ONLY; never
    repurpose it for any authorization / trust decision."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    ip = xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR", "?")
    return "🖥 локально" if ip in ("127.0.0.1", "::1", "localhost") else f"📱 {ip}"


def _log_tracking(message):
    """Console line for GPS heartbeats / trip-state changes — so you can watch in
    real time what the mobile app sends (and tell it apart from our own/simulator
    traffic). Toggle with settings.LOG_TRACKING."""
    from django.conf import settings

    if getattr(settings, "LOG_TRACKING", False):
        print(message, flush=True)


@csrf_exempt
def admin_approve_overlay(request, pk):
    """Server hook on demo admin-approve: forward the call to demo and, on success,
    flip OUR OrderMeta to ``dispatchable=True`` so the auto-dispatcher picks the
    now-approved order up — regardless of which client approved it. Mounted before
    the gateway catch-all. (The web form already sets the flag; this guarantees it
    for any other approve path.)"""
    from config.gateway import gateway

    resp = gateway(request, f"car-orders/{pk}/admin-approve/")
    if 200 <= resp.status_code < 300:
        # AUDIT H2: demo already committed the approve. If OUR overlay write then fails
        # the two diverge — but the demo response must still reach the client, so we
        # can't re-raise. Log it LOUDLY so the split-brain is alertable, not silent.
        try:
            OrderMeta.objects.update_or_create(order_id=int(pk), defaults={"dispatchable": True})
            # The order just entered the dispatch queue → start the «поиск водителя»
            # clock (idempotent: a re-approve won't reset an already-running search).
            services.overlay.mark_searching(int(pk))
        except Exception:
            logger.exception(
                "car_orders: demo approve succeeded but overlay update FAILED for order %s "
                "— overlay/demo split-brain (order may not auto-dispatch)", pk
            )
    return resp


@csrf_exempt
def reject_overlay(request, pk):
    """Server hook on demo reject: forward to demo and, on success, tear down OUR
    overlay (CANCELLED) so a rejected order leaves the auto-dispatch queue and any
    driver it was on — otherwise an already-approved (``dispatchable=True``) order
    would keep getting auto-assigned after being rejected. Mirror of
    ``admin_approve_overlay``; mounted before the gateway catch-all."""
    from config.gateway import gateway

    resp = gateway(request, f"car-orders/{pk}/reject/")
    if 200 <= resp.status_code < 300:
        # AUDIT H2: demo already rejected. If OUR teardown fails the order stays
        # dispatchable locally and keeps getting auto-assigned — the exact bug this
        # hook prevents. Can't re-raise (the demo response must reach the client), so
        # log LOUDLY so the divergence is caught.
        try:
            services.overlay.release(int(pk))  # terminal: clears claim + dispatchable
        except Exception:
            logger.exception(
                "car_orders: demo reject succeeded but overlay teardown FAILED for order %s "
                "— rejected order may keep auto-dispatching", pk
            )
    return resp


def _inject_effective_status(payload):
    """Decorate a proxied demo car-order payload with our reconciled ``effective_status``.

    The base CarOrder list/detail lives upstream (demo) and is reverse-proxied, so the
    demo ``status`` is ALREADY in the body — we just join each row with its local
    ``OrderMeta`` and compute the single source-of-truth status ONCE on the server (no
    extra upstream call). Every client then shows the same thing instead of re-deriving
    it (which had drifted: a claimed order read «Ожидает водителя» on web while mobile
    showed «В процессе»/the trip stage). Handles the three shapes demo returns: a single
    order dict, a bare list, or a paginated ``{results: [...]}``. One local query.
    """
    from car_orders.services.status import effective_status

    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        rows = payload["results"]
    elif isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and "status" in payload:
        rows = [payload]
    else:
        return payload  # unrecognised shape — leave it untouched
    ids = [r["id"] for r in rows if isinstance(r, dict) and r.get("id") is not None]
    metas = {m.order_id: m for m in OrderMeta.objects.filter(order_id__in=ids)}
    for r in rows:
        if isinstance(r, dict):
            r["effective_status"] = effective_status(r.get("status"), metas.get(r.get("id")))
    return payload


# Short per-process cache of the full demo order set (keyed by bearer token so one
# user's visible orders never leak to another). The "our orders" list pages through
# demo to fetch order bodies; this stops every list load / pagination step from
# re-paging the whole upstream. TTL is small so a demo-side status change shows fast.
_DEMO_ORDERS_CACHE: dict = {}
_DEMO_ORDERS_TTL = 8.0


def _snapshot_descriptive(bodies):
    """Lazily snapshot demo-only DESCRIPTIVE fields (project / note / car-type /
    creator / destination address) from the demo ``bodies`` onto the EXISTING
    OrderMeta rows.

    Why: a driver/customer can't read the demo body of an order managed via our
    overlay (the detail proxy 404s — see ``CarOrderViewSet.get_queryset``), so the
    clients fall back to rebuilding the card from OrderMeta. OrderMeta only stored
    coords/window/driver-snapshot, so the card was missing project/note/car-type/
    creator/address. We fill them here from the bodies a PRIVILEGED client (the
    dispatcher board) already pages — so the next overlay fallback shows full info.

    Only fills BLANK fields on rows we already manage (never creates a row), so it's
    idempotent and a no-op once filled. Best-effort: enrichment only, never on the
    critical path of serving the list, so any failure is logged and swallowed."""
    if not bodies:
        return
    blank = (
        models.Q(project_name="") | models.Q(note="") | models.Q(car_type_name="")
        | models.Q(created_by_name="") | models.Q(dest_address="")
    )
    for m in OrderMeta.objects.filter(order_id__in=list(bodies.keys())).filter(blank):
        body = bodies.get(m.order_id)
        if not isinstance(body, dict):
            continue
        changed = []

        def _fill(field, value, limit=None):
            if value and not getattr(m, field):
                setattr(m, field, value[:limit] if (limit and isinstance(value, str)) else value)
                changed.append(field)

        _fill("project_name", body.get("project_name"), 500)
        _fill("note", body.get("note"))
        ct = body.get("car_type")
        _fill("car_type_name", ct.get("name") if isinstance(ct, dict) else None, 255)
        cb = body.get("created_by")
        _fill("created_by_name", cb.get("name") if isinstance(cb, dict) else None, 255)
        _fill("dest_address", body.get("address"), 500)
        if changed:
            try:
                m.save(update_fields=changed)
            except Exception:
                logger.exception(
                    "car_orders: descriptive snapshot save failed for order %s", m.order_id
                )


def _all_demo_orders(request):
    """``{id: order_body}`` for every demo order the caller can see — paged from the
    upstream list (50/page) and cached briefly per token. Used to render the
    «only our orders» list with full bodies (address/project/client live in the demo
    body, not in our OrderMeta)."""
    import json
    import time
    from urllib.parse import urlsplit

    from config.gateway import gateway

    token = request.META.get("HTTP_AUTHORIZATION", "")
    now = time.time()
    hit = _DEMO_ORDERS_CACHE.get(token)
    if hit and hit[0] > now:
        return hit[1]

    bodies: dict = {}
    orig_qs = request.META.get("QUERY_STRING", "")
    try:
        # Follow demo's OWN `next` link (it paginates by limit/offset, not ?page=), so
        # we walk every page instead of re-fetching the first. The gateway appends
        # request.QUERY_STRING to the upstream URL, so we drive paging through it.
        qs = "page_size=50"
        for _ in range(60):  # safety cap (60 × 50 = 3000 orders)
            request.META["QUERY_STRING"] = qs
            resp = gateway(request, "car-orders/")
            if not (200 <= resp.status_code < 300):
                break
            try:
                data = json.loads(resp.content)
            except (ValueError, TypeError):
                break
            results = data.get("results") if isinstance(data, dict) else data
            if not isinstance(results, list):
                break
            for o in results:
                if isinstance(o, dict) and o.get("id") is not None:
                    bodies[o["id"]] = o
            nxt = data.get("next") if isinstance(data, dict) else None
            if not nxt:
                break
            qs = urlsplit(nxt).query  # demo's real next-page query (limit/offset)
    finally:
        request.META["QUERY_STRING"] = orig_qs

    # Enrich our overlay rows from the bodies we just paged (this caller can see them),
    # so an order the requester later can't read still shows full info via the fallback.
    _snapshot_descriptive(bodies)

    # Drop expired entries so the cache can't grow unbounded across rotating tokens.
    for k in [k for k, (exp, _) in _DEMO_ORDERS_CACHE.items() if exp <= now]:
        _DEMO_ORDERS_CACHE.pop(k, None)
    _DEMO_ORDERS_CACHE[token] = (now + _DEMO_ORDERS_TTL, bodies)
    return bodies


def _fill_demo_statuses(status_map, order_ids, request):
    """Backfill ``status_map`` (order_id → demo status) for ids the LOCAL ``CarOrder``
    mirror couldn't resolve, by pulling the demo body from upstream — the SAME source
    the proxied list uses. Without this the OrderMeta-only feeds (overlay board / fleet
    snapshot) return ``effective_status=null`` for every order that lives only upstream,
    and the clients each reconcile it differently (the cross-client «wrong status» bug).

    Mutates and returns ``status_map``. No-op without a ``request`` (the request-less WS
    refresh keeps its local-only behaviour) or when the upstream body isn't
    visible/reachable — so it degrades to today's behaviour, never crashes."""
    if request is None:
        return status_map
    missing = [oid for oid in order_ids if status_map.get(oid) is None]
    if not missing:
        return status_map
    bodies = _all_demo_orders(request)
    for oid in missing:
        body = bodies.get(oid)
        if isinstance(body, dict) and body.get("status") is not None:
            status_map[oid] = body["status"]
    return status_map


def _driver_snapshot(request, driver_id):
    """``(name, phone)`` for ``driver_id`` from upstream HR (``/employees/{id}/``).

    A driver self-claim captures its OWN name+phone client-side and sends them on the
    claim. A DISPATCHER manual assign can't — the dispatcher's client doesn't hold the
    chosen driver's HR record — so the snapshot came back empty and the order showed no
    driver (name/phone blank on the live-track / detail card). The dispatcher's token DOES
    have employee-read access, so we fetch it here with that token and snapshot it the
    same way, fixing BOTH dispatcher clients (web + mobile) in one place.

    Best-effort: returns ``("", "")`` with no token (tests / open-dev without a bearer),
    no driver, or any upstream failure — i.e. exactly today's behaviour, never a crash.
    A direct GET (NOT ``config.gateway.gateway``, which would forward this request's POST
    method + body to the employees endpoint)."""
    token = request.META.get("HTTP_AUTHORIZATION", "")
    if not token or not driver_id:
        return "", ""
    import requests
    from django.conf import settings

    base = settings.UPSTREAM_API_BASE.rstrip("/")
    try:
        resp = requests.get(
            f"{base}/employees/{driver_id}/",
            headers={"Authorization": token, "Accept": "application/json"},
            timeout=settings.UPSTREAM_TIMEOUT,
        )
        if 200 <= getattr(resp, "status_code", 0) < 300:
            body = resp.json()
            if isinstance(body, dict):
                return body.get("name") or "", body.get("phone") or ""
    except (requests.RequestException, ValueError, TypeError):
        pass
    return "", ""


def _our_orders_list(request):
    """The car-order LIST narrowed to OUR orders only — every order that has a local
    ``OrderMeta`` (i.e. the overlay touched it: coords / claim / dispatch / trip). Plain
    demo-only orders (no OrderMeta) are excluded, so the list matches the dispatcher and
    «forgets demo». Each row is the full demo body + our reconciled ``effective_status``.
    Re-paginated server-side (page / page_size) since we filter the upstream set."""
    from django.http import JsonResponse

    from car_orders.services.status import effective_status

    metas = {m.order_id: m for m in OrderMeta.objects.all()}
    bodies = _all_demo_orders(request)
    rows = []
    for oid, m in metas.items():
        body = bodies.get(oid)
        if not isinstance(body, dict):
            continue  # our order whose demo body the caller can't see / is gone
        b = dict(body)
        b["effective_status"] = effective_status(b.get("status"), m)
        rows.append(b)
    # Newest first (planned pickup, then id) — a sensible default for the board.
    rows.sort(key=lambda b: (b.get("planned_datetime") or "", b.get("id") or 0), reverse=True)

    # Apply the client's filters locally (we fetched the whole set, not a demo page).
    st = request.GET.get("status")
    if st:
        rows = [b for b in rows if st in (b.get("effective_status"), b.get("status"))]
    q = (request.GET.get("search") or "").strip().lower()
    if q:
        rows = [
            b for b in rows
            if q in (b.get("address") or "").lower()
            or q in str(b.get("project_name") or "").lower()
            or q in str(b.get("id") or "")
        ]

    def _int(name, default):
        try:
            return max(1, int(request.GET.get(name, default)))
        except (TypeError, ValueError):
            return default

    total = len(rows)
    page = _int("page", 1)
    page_size = _int("page_size", 20)
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]

    def _url(p):
        if p < 1 or (p - 1) * page_size >= total:
            return None
        return f"{request.path}?page={p}&page_size={page_size}"

    return JsonResponse(
        {"count": total, "next": _url(page + 1), "previous": _url(page - 1), "results": page_rows}
    )


@csrf_exempt
def car_order_proxy(request, pk=None):
    """Reverse-proxy the demo car-order LIST (``/car-orders/``) and DETAIL
    (``/car-orders/<pk>/``).

    - LIST GET → ONLY our orders (those with a local OrderMeta), each as the full demo
      body + reconciled ``effective_status``, re-paginated server-side. Demo-only orders
      are hidden so every surface shows the same «our» set (see ``_our_orders_list``).
    - DETAIL GET → proxy demo + inject ``effective_status``.
    - Non-GET (create / update / delete) and any non-2xx/non-JSON → pass straight through.

    The base CarOrder lives upstream, so this view sits before the gateway catch-all and
    is the single source of truth for order status across web + mobile."""
    import json

    from django.http import HttpResponse

    from config.gateway import gateway

    if pk is None and request.method == "GET":
        return _our_orders_list(request)

    path = "car-orders/" if pk is None else f"car-orders/{pk}/"
    resp = gateway(request, path)
    if request.method != "GET" or not (200 <= resp.status_code < 300):
        return resp
    if "application/json" not in resp.get("Content-Type", ""):
        return resp
    try:
        payload = json.loads(resp.content)
    except (ValueError, TypeError):
        return resp
    _inject_effective_status(payload)
    out = HttpResponse(
        json.dumps(payload),
        status=resp.status_code,
        content_type=resp.get("Content-Type", "application/json"),
    )
    # Preserve the upstream response headers for parity (the gateway already strips
    # Content-Length/-Encoding, so Django recomputes them for our re-serialized body).
    for key, value in resp.items():
        if key.lower() != "content-type":
            out[key] = value
    return out


def _notify_dropped_driver(driver_id, order_id):
    return services.events.notify_dropped_driver(driver_id, order_id)


def _reset_driver_shift(driver):
    return services.shift.reset_driver_shift(driver)


def _active_shift(user):
    return services.shift.active_shift(user)


def _driver_has_active_trip(user):
    return CarOrder.objects.filter(driver=user, status=CarOrder.Status.IN_PROGRESS).exists()


def _can_manage_any_car_order(user):
    return (
        user.is_superuser
        or user_has_permission(user, "car_order:list")
        or user_has_permission(user, "car_order:approve")
    )


def _garage_permissions(action_name):
    mapping = {
        "create": "garage:create",
        "update": "garage:update",
        "partial_update": "garage:update",
        "destroy": "garage:delete",
    }
    codename = mapping.get(action_name, "garage:list")
    return [IsAuthenticated(), HasPermission(codename)()]


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


class FleetLiveView(APIView):
    """Dispatcher dashboard snapshot — every active order with its live position +
    risk flags, for «Диспетчерская». Live updates come over the fleet WebSocket
    (/ws/car-orders/fleet/)."""

    authentication_classes = [DemoTokenAuthentication]
    # Dispatcher-only: this is the whole «Диспетчерская» board (every active order +
    # live position + risk flags). Only dispatcher screens (web FleetLivePage, mobile
    # features/dispatcher) call it, so gating it on car_order:approve closes the leak
    # of the full fleet to any authenticated customer/driver token.
    permission_classes = [OverlayDispatcher]

    def get(self, request):
        from car_orders.fleet import fleet_live_orders

        return Response({"orders": fleet_live_orders(request)})


class LiveLocationView(APIView):
    """Live driver position for an order, served locally (gateway/hybrid setup).
    GET returns the latest position or null; POST upserts {lat, lng}, keyed by order
    id. Mounted at /api/v1/car-orders/<id>/live-location/ BEFORE the gateway
    catch-all.

    AUDIT C3: the POST stays open in dev (auth off) so the local auto-simulator can
    push without a demo JWT, but when ``REQUIRE_OVERLAY_AUTH`` is enforced it requires
    the order's own driver (or a dispatcher) — otherwise any anonymous caller could
    move/forge another order's marker (and defeat the arrival geofence)."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [AllowAny]

    def get(self, request, pk):
        loc = OrderLiveLocation.objects.filter(order_id=pk).first()
        if not loc:
            return Response(None)
        return Response(
            {
                "lat": loc.lat,
                "lng": loc.lng,
                "last_seen": loc.last_seen,
                "geometry": loc.geometry,
            }
        )

    def post(self, request, pk):
        from django.conf import settings

        # AUDIT C3: when enforced, only the assigned driver (or a dispatcher) may write
        # an order's live position. Open in dev so the simulator keeps working.
        if getattr(settings, "REQUIRE_OVERLAY_AUTH", False):
            meta = OrderMeta.objects.filter(order_id=pk).first()
            actor = acting_driver_id(request)
            is_owner = (
                actor is not None
                and meta is not None
                and meta.driver_id is not None
                and str(meta.driver_id) == str(actor)
            )
            if not (is_owner or OverlayDispatcher().has_permission(request, self)):
                return _forbidden(_("You can only update your own order's live location."))
        serializer = LocationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        defaults = {
            "lat": serializer.validated_data["lat"],
            "lng": serializer.validated_data["lng"],
            "last_seen": timezone.now(),
        }
        geometry = request.data.get("geometry")
        if geometry is not None:
            defaults["geometry"] = geometry
        loc, _created = OrderLiveLocation.objects.update_or_create(order_id=pk, defaults=defaults)

        # Push the new position to connected trackers + the fleet dashboard.
        data = {"lat": loc.lat, "lng": loc.lng, "last_seen": loc.last_seen.isoformat()}
        if geometry is not None:  # carry the route on the first push
            data["geometry"] = geometry
        broadcast_location(pk, data)
        _log_tracking(f"🛰 LIVE [{_src(request)}] #{pk} ({loc.lat:.5f},{loc.lng:.5f})")
        return Response({"lat": loc.lat, "lng": loc.lng, "last_seen": loc.last_seen})


class OrderMetaView(APIView):
    """Local feature overlay for an order (coords / window / trip state), keyed by
    the demo order id. GET returns it or null; POST upserts the provided fields.
    AllowAny for now (the frontend sends the driver id). Mounted before the
    gateway catch-all."""

    authentication_classes = [DemoTokenAuthentication]

    def get_permissions(self):
        # Dropping the order from our overlay is a dispatcher/admin action.
        if self.request.method == "DELETE":
            return [OverlayDispatcher()]
        return [OverlayAuthenticated()]

    def get(self, request, pk):
        meta = OrderMeta.objects.filter(order_id=pk).first()
        if not meta:
            return Response(None)
        return Response(OrderMetaSerializer(meta).data)

    # Assignment / dispatch fields are owned by the claim / auto-dispatch / approve
    # flows — never settable via a plain feature-overlay upsert. Otherwise (AUDIT C3/M2)
    # any non-dispatcher could POST {driver_id, dispatchable, …} for ANY order id and
    # self-assign a driver or flip it back into the dispatch queue, bypassing the busy
    # guard in overlay.claim. A dispatcher (or open-dev mode) may still set them.
    _PROTECTED_FIELDS = ("driver_id", "car_id", "car_label", "overlay_claimed", "dispatchable")

    def post(self, request, pk):
        serializer = OrderMetaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        if not OverlayDispatcher().has_permission(request, self):
            for field in self._PROTECTED_FIELDS:
                data.pop(field, None)
        meta, _created = OrderMeta.objects.update_or_create(order_id=pk, defaults=data)
        # Direct-create / dispatcher set dispatchable here → start the «поиск водителя»
        # clock (idempotent; only while still driverless).
        services.overlay.mark_searching(pk)
        return Response(OrderMetaSerializer(meta).data)

    def delete(self, request, pk):
        """Admin: drop the order from OUR overlay (the row + its live location) so it
        disappears from our views. Used by «Удалить» for orders demo won't hard-delete
        (demo allows deleting only drafts). The demo order itself is untouched."""
        OrderLiveLocation.objects.filter(order_id=pk).delete()
        OrderMeta.objects.filter(order_id=pk).delete()
        return Response({"ok": True})


class ClaimCheckView(APIView):
    """Scheduling pre-check before a driver claims an order (overlay/hybrid).

    Body: ``{driver_id}``. Reads the order's saved window from OrderMeta and
    checks it against the driver's other committed orders (+ travel buffer).
    Returns ``{ok, conflict}`` — so a driver CAN take a second order when it fits
    a free gap, instead of a blanket "you already have an order"."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def post(self, request, pk):
        # Check is FOR the candidate driver (dispatcher picks them in the body),
        # not the dispatcher running the check.
        driver_id = assignee_driver_id(request, self)
        meta = OrderMeta.objects.filter(order_id=pk).first()
        # No saved window → nothing to schedule against; allow.
        if not meta or not meta.planned_datetime or not meta.estimated_duration:
            return Response({"ok": True, "conflict": None})
        new_end = scheduling.driving_end(meta.planned_datetime, meta.planned_end, meta.service_time)
        conflict = scheduling.meta_conflict(
            driver_id, meta.planned_datetime, new_end, exclude_order_id=int(pk)
        )
        if conflict is None:
            return Response({"ok": True, "conflict": None})
        return Response(
            {
                "ok": False,
                "conflict": {
                    "order_id": conflict.order_id,
                    "planned_start": conflict.planned_datetime,
                    "planned_end": conflict.planned_end,
                    "address": f"Заказ #{conflict.order_id}",
                },
            }
        )


class MetaBatchView(APIView):
    """Batch read of OrderMeta for a set of order ids, so the list can compute the
    effective (overlay) status per row. Body: ``{order_ids: [...]}``."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def post(self, request):
        order_ids = request.data.get("order_ids") or []
        metas = OrderMeta.objects.filter(order_id__in=order_ids)
        return Response({"results": OrderMetaSerializer(metas, many=True).data})


class ClaimCheckBatchView(APIView):
    """Batch window check: for a list of order ids, which ones fit the driver's
    schedule (so the list can show «можно взять» / «пересекается»).
    Body: ``{driver_id, order_ids: [...]}`` → ``{results: [{order_id, ok, conflict}]}``."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def post(self, request):
        # Check is FOR the candidate driver (body), not the dispatcher running it.
        driver_id = assignee_driver_id(request, self)
        order_ids = request.data.get("order_ids") or []
        metas = {m.order_id: m for m in OrderMeta.objects.filter(order_id__in=order_ids)}
        results = []
        for oid in order_ids:
            meta = metas.get(oid)
            if meta is None or not meta.planned_datetime or not meta.estimated_duration:
                results.append({"order_id": oid, "ok": True, "conflict": None})
                continue
            new_end = scheduling.driving_end(
                meta.planned_datetime, meta.planned_end, meta.service_time
            )
            conflict = scheduling.meta_conflict(
                driver_id, meta.planned_datetime, new_end, exclude_order_id=int(oid)
            )
            results.append(
                {
                    "order_id": oid,
                    "ok": conflict is None,
                    "conflict": None
                    if conflict is None
                    else {
                        "order_id": conflict.order_id,
                        "planned_start": conflict.planned_datetime,
                        "planned_end": conflict.planned_end,
                    },
                }
            )
        return Response({"results": results})


class OverlayClaimView(APIView):
    """Claim an order in OUR layer (not demo), so a driver can take a second
    order with the SAME car sequentially — which the demo backend forbids
    (one car / one driver per active order). Runs the window conflict check
    first; on success records driver + car on the OrderMeta. demo stays the
    source of login/base data."""

    authentication_classes = [DemoTokenAuthentication]
    # Driver self-claims (token) or dispatcher assigns (body) — never a customer-tier
    # token. Matches the native ``claim`` gate (driver:accept_order).
    permission_classes = [OverlayDriverOrDispatcher]

    def post(self, request, pk):
        # The assignee — a dispatcher assigns to the CHOSEN driver (body), a driver
        # self-claims their own (token). NOT the acting user, or a dispatcher's
        # assignment would be claimed for the dispatcher.
        driver_id = assignee_driver_id(request, self)
        driver_name = request.data.get("driver_name", "")
        driver_phone = request.data.get("driver_phone", "")
        # Dispatcher manual assign sends no driver snapshot (it doesn't hold the chosen
        # driver's HR record), so fill it server-side with the dispatcher's token — else
        # the assigned order shows a blank driver. A self-claim already passes both, so
        # this only fires for the (otherwise blank) dispatcher path.
        if not driver_name and not driver_phone:
            driver_name, driver_phone = _driver_snapshot(request, driver_id)
        try:
            meta = services.overlay.claim(
                pk, driver_id, request.data.get("car_id"), request.data.get("car_label", ""),
                driver_name=driver_name,
                driver_phone=driver_phone,
            )
        except services.overlay.OverlayError as exc:
            return _service_error_response(exc)
        return Response({"ok": True, "conflict": None, "meta": OrderMetaSerializer(meta).data})


class TripStateView(APIView):
    """Advance the richer trip state (overlay): to_client / at_client / in_trip /
    at_destination / waiting / completed. Authoritative: only the assigned driver
    (or a dispatcher) may advance it, transitions must follow the flow, and arrival
    stages are geofenced. Thin HTTP adapter over ``services.trip_state``."""

    authentication_classes = [DemoTokenAuthentication]
    # Advancing the trip is a driver/dispatcher mutation (the service still enforces
    # actor==assigned-driver|dispatcher); the class gate keeps a customer-tier token
    # out entirely, consistent with the other overlay mutations (§A).
    permission_classes = [OverlayDriverOrDispatcher]

    def post(self, request, pk):
        try:
            meta = services.trip_state.advance(
                int(pk),
                request.data.get("trip_state"),
                actor_driver_id=acting_driver_id(request),
                is_dispatcher=OverlayDispatcher().has_permission(request, self),
            )
        except services.trip_state.TripStateError as exc:
            if exc.http_status == status.HTTP_403_FORBIDDEN:
                return _forbidden(exc.message)
            return _bad_request(exc.code, exc.message)
        _log_tracking(
            f"🚦 STATUS [{_src(request)}] #{pk} driver={meta.driver_id} → {meta.trip_state}"
        )
        return Response(OrderMetaSerializer(meta).data)


class OverlayReleaseView(APIView):
    """Tear down the overlay for an order — call it on demo reject / cancel /
    release / reassign / done. Clears the claim so the order stops blocking the
    driver's schedule and the auto-simulator, and pushes a ``cancelled`` state
    over the WebSocket. Idempotent."""

    authentication_classes = [DemoTokenAuthentication]
    # Tearing down a claim is a driver (own order) or dispatcher action — not a
    # customer-tier token. Matches the native ``release`` gate (driver:accept_order).
    permission_classes = [OverlayDriverOrDispatcher]

    def post(self, request, pk):
        try:
            meta = services.overlay.release(
                pk,
                requeue=bool(request.data.get("requeue")),
                actor_driver_id=acting_driver_id(request),
                is_dispatcher=OverlayDispatcher().has_permission(request, self),
            )
        except services.overlay.OverlayError as exc:
            return _service_error_response(exc)
        if meta is None:
            return Response({"ok": True})
        return Response({"ok": True, "meta": OrderMetaSerializer(meta).data})


class NoShowView(APIView):
    """«Клиент не вышел» — the driver (or a dispatcher) cancels an order whose client
    never came out at the pickup. Only valid while ``trip_state == at_client`` (the
    pickup-wait stage; usually past the wait threshold, but the threshold is a UI
    nudge — the server allows it whenever waiting). Tears down the overlay, mirrors
    the native cancel and audits the no-show. Body: none → ``{ok, waited_s, meta}``."""

    authentication_classes = [DemoTokenAuthentication]
    # Same gate as overlay-release: the assigned driver (own order) or a dispatcher.
    permission_classes = [OverlayDriverOrDispatcher]

    def post(self, request, pk):
        # request.user is a demo-bridged DemoUser (not a persisted row); the audit FK
        # needs a real User, so resolve one by id when it exists locally, else None.
        actor = User.objects.filter(pk=getattr(request.user, "pk", None)).first()
        try:
            meta, waited_s = services.overlay.cancel_no_show(
                pk,
                actor=actor,
                actor_driver_id=acting_driver_id(request),
                is_dispatcher=OverlayDispatcher().has_permission(request, self),
            )
        except services.overlay.OverlayError as exc:
            return _service_error_response(exc)
        return Response(
            {"ok": True, "waited_s": waited_s, "meta": OrderMetaSerializer(meta).data}
        )


class ExtendView(APIView):
    """Add minutes to an order's planned duration in OUR overlay (demo doesn't
    store the window). Pushes ``planned_end`` out and re-checks the driver's next
    window. Body: ``{minutes}`` → ``{ok, meta, conflict}``. The extension is always
    applied; ``conflict`` is a warning the new end overlaps the driver's next order.
    Allowed for the driver or a dispatcher (the frontend gates the button)."""

    authentication_classes = [DemoTokenAuthentication]
    # Per the docstring this is "driver or dispatcher" only — now enforced, not just
    # gated by the frontend button.
    permission_classes = [OverlayDriverOrDispatcher]

    def post(self, request, pk):
        try:
            minutes = int(request.data.get("minutes", 0))
        except (TypeError, ValueError):
            minutes = 0
        try:
            meta, conflict = services.overlay.extend(
                pk,
                minutes,
                actor_driver_id=acting_driver_id(request),
                is_dispatcher=OverlayDispatcher().has_permission(request, self),
            )
        except services.overlay.OverlayError as exc:
            return _service_error_response(exc)
        return Response({"ok": True, "meta": OrderMetaSerializer(meta).data, "conflict": conflict})


class ReassignView(APIView):
    """Dispatcher takes an order off its driver and returns it to the queue
    (overlay). Frees our claim — the order stops blocking the driver's schedule
    and the simulator — and pushes a ``cancelled`` trip-state over the WebSocket,
    so another driver can pick it up. A plain demo claim is owned by demo and
    can't be reassigned from here (only overlay-claimed orders). Idempotent."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayDispatcher]

    def post(self, request, pk):
        try:
            meta = services.overlay.reassign(pk)
        except services.overlay.OverlayError as exc:
            return _service_error_response(exc)
        return Response({"ok": True, "meta": OrderMetaSerializer(meta).data})


def _apply_driver_location(driver_id, lat, lng, src="", heading=None):
    """Store the driver's position and attach it to their ACTIVE (non-terminal)
    order — NOT only the moving stages. With «1 водитель = 1 активный заказ» that's
    exactly their current order, so live phone GPS drives the map in every stage
    (assigned / en route / parked). Shared by the single + batch location endpoints.
    Returns the order ids whose live position was updated.

    ``heading`` (deg) is the device-reported travel direction, when the app sends it."""
    now = timezone.now()
    # Never attach to driver_id=None — a None filter matches EVERY driverless order
    # and smears one phone's GPS across all of them. The caller must identify the
    # driver (token or body driver_id).
    if driver_id is None:
        _log_tracking(f"📍 GPS [{src}] БЕЗ driver_id — пропущено (телефон не опознан)")
        return []
    # Travel direction for the OSRM start-snap (stops the route flipping to the
    # oncoming carriageway). Prefer the bearing DERIVED from the previous fix → this
    # one — that's the driver's true motion and is unambiguous — and fall back to the
    # device heading only when we can't (no prior fix / move too small to be real).
    prev_pos = DriverPosition.objects.filter(driver_id=driver_id).first()
    travel_bearing = None
    if prev_pos is not None and prev_pos.lat is not None:
        if geometry.haversine_km(prev_pos.lat, prev_pos.lng, lat, lng) * 1000 >= geometry.MIN_MOVE_M:
            travel_bearing = geometry.bearing_deg(prev_pos.lat, prev_pos.lng, lat, lng)
    if travel_bearing is None and heading is not None:
        travel_bearing = heading
    DriverPosition.objects.update_or_create(
        driver_id=driver_id,
        defaults={"lat": lat, "lng": lng, "heading": heading, "last_seen": now},
    )
    terminal = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)
    metas = list(OrderMeta.objects.filter(driver_id=driver_id).exclude(trip_state__in=terminal))
    # Stages where the driver is DRIVING — we keep the route fresh on these.
    moving = (
        OrderMeta.TripState.ASSIGNED,
        OrderMeta.TripState.TO_CLIENT,
        OrderMeta.TripState.IN_TRIP,
    )
    from car_orders import dispatch

    updated = []
    for meta in metas:
        prev = OrderLiveLocation.objects.filter(order_id=meta.order_id).first()
        moved_m = (
            geometry.haversine_km(prev.lat, prev.lng, lat, lng) * 1000
            if (prev and prev.lat is not None)
            else float("inf")
        )
        updated.append(meta.order_id)
        # Parked / GPS jitter: hasn't really moved since the last SHOWN point → keep
        # the marker and line exactly where they are (don't redraw — that was the
        # in-place flicker), just keep the fix fresh. Compare vs the last shown point
        # (not the last frame) so a slow crawl still accumulates and eventually updates.
        if prev is not None and moved_m < geometry.MIN_MOVE_M:
            OrderLiveLocation.objects.filter(order_id=meta.order_id).update(last_seen=now)
            # Heartbeat: the marker/line stay put, but watchers (customer detail +
            # dispatcher fleet) gate «Связь потеряна» on last_seen. A parked driver
            # who keeps streaming the same fix would otherwise look offline after 30s,
            # so push a last_seen-only frame — the client merge keeps prev lat/lng.
            broadcast_location(meta.order_id, {"last_seen": now.isoformat()})
            continue
        loc, _ = OrderLiveLocation.objects.update_or_create(
            order_id=meta.order_id, defaults={"lat": lat, "lng": lng, "last_seen": now}
        )
        # Snap the DISPLAYED marker onto the route so the dot rides the line instead of
        # floating 80–100 m beside it on biased GPS — within a heading-gated corridor
        # (SNAP_CORRIDOR_M); a real detour falls back to raw and the deviation re-route
        # below takes over. Display-only: the stored `loc`/`DriverPosition` and the
        # `lat/lng` used for the deviation check stay RAW, so re-routing is unchanged.
        show_lat, show_lng = lat, lng
        if meta.trip_state in moving and loc.geometry:
            show_lat, show_lng = geometry.snap_to_route(lat, lng, loc.geometry, travel_bearing)
        broadcast_location(
            meta.order_id, {"lat": show_lat, "lng": show_lng, "last_seen": now.isoformat()}
        )
        # RE-ROUTE on deviation: recompute the polyline from the LIVE position when
        # there's no route yet OR the driver has strayed >30 m off the current one
        # (turned the «wrong» way) — so it redraws along the road they actually took.
        # 30 m (was 80 m): in dense blocks 80 m is a street over, so the line could run
        # along a parallel/oncoming street before a re-route kicked in.
        if meta.trip_state in moving:
            deviated = (
                True
                if not loc.geometry
                else geometry.min_dist_km_to_polyline(lat, lng, loc.geometry) > 0.03
            )
            if deviated:
                dispatch.push_order_route(meta, driver_pos=(lat, lng), bearing=travel_bearing)
            elif loc.geometry:
                # On-route & actually moved: trim the canonical line to what's ahead
                # and pin its start to the car. Smooth follow without OSRM per frame.
                broadcast_location(
                    meta.order_id,
                    {"geometry": geometry.trim_geometry(loc.geometry, lat, lng)},
                )
    _log_tracking(
        f"📍 GPS [{src}] driver={driver_id} ({lat:.5f},{lng:.5f}) → "
        + (
            ", ".join(f"#{m.order_id} [{m.trip_state}]" for m in metas)
            if metas
            else "нет активного заказа"
        )
    )
    return updated


class DriverLocationView(APIView):
    """The driver app posts its GPS ONCE here; the server attaches it to the
    driver's ACTIVE order and fans it out over WebSocket — so the mobile app
    doesn't need to know which order id to send to (it just streams its position).
    Body: ``{driver_id, lat, lng}`` → ``{updated_orders: [...]}``."""

    authentication_classes = [DemoTokenAuthentication]
    # Posting GPS attaches it to the driver's active order — a driver (or dispatcher)
    # action, mirroring the native ``my_location`` gate (driver:accept_order).
    permission_classes = [OverlayDriverOrDispatcher]

    def post(self, request):
        driver_id = acting_driver_id(request, request.data.get("driver_id"))
        serializer = LocationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = _apply_driver_location(
            driver_id,
            serializer.validated_data["lat"],
            serializer.validated_data["lng"],
            _src(request),
            heading=serializer.validated_data.get("heading"),
        )
        return Response({"updated_orders": updated})


class DriverShiftView(APIView):
    """Local OVERLAY «driver on shift» (Р1) — demo has no set-shift endpoint, so we
    keep it locally by demo driver id. GET current shift / PATCH go on shift (pick a
    car) / DELETE end. Mounted at /drivers/me/shift/ BEFORE the gateway catch-all."""

    authentication_classes = [DemoTokenAuthentication]

    def get_permissions(self):
        # Reading your own shift is harmless (any authenticated user); going ON shift
        # or ending it mutates the overlay, so require an actual driver (or dispatcher),
        # matching the native ``my_shift`` gate (driver:accept_order).
        if self.request.method in ("PATCH", "DELETE"):
            return [OverlayDriverOrDispatcher()]
        return [OverlayAuthenticated()]

    def get(self, request):
        driver_id = acting_driver_id(request, request.query_params.get("driver_id"))
        s = DriverShiftState.objects.filter(driver_id=driver_id).first() if driver_id else None
        return Response(s.as_shift() if s else None)

    def patch(self, request):
        """Go on shift OR swap the shift car. Swapping (an existing shift, a
        different car) is the «drove to the garage, changed the car, back on the
        line» flow — but it is BLOCKED while the driver still has ANY active
        (non-terminal) order: let them finish (or hand off) their work first, then
        change cars. This avoids splitting a half-done schedule across two
        vehicles. Re-selecting the SAME car isn't a change, so it's never blocked."""
        driver_id = acting_driver_id(request, request.data.get("driver_id"))
        car_id = request.data.get("car_id")
        if driver_id is None or car_id is None:
            return _bad_request("VALIDATION", _("driver and car_id are required."))

        def _int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        # Car type is REQUIRED — the dispatcher/auto-dispatcher matches orders by
        # car type, so an on-shift driver without a type is silently un-dispatchable.
        new_car_type = _int(request.data.get("car_type_id"))
        if new_car_type is None:
            return _bad_request(
                "VALIDATION",
                _("car_type_id is required to go on shift (orders are matched by car type)."),
            )
        new_car_id = _int(car_id)
        existing = DriverShiftState.objects.filter(driver_id=driver_id).first()
        changing = existing is not None and existing.car_id != new_car_id

        if changing:
            terminal = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)
            active = (
                OrderMeta.objects.filter(driver_id=driver_id)
                .exclude(trip_state__in=terminal)
                .count()
            )
            if active:
                return _bad_request(
                    "HAS_ACTIVE_ORDERS",
                    _("Finish your %(n)s active order(s) before changing cars.")
                    % {"n": active},
                )

        s, _created = DriverShiftState.objects.update_or_create(
            driver_id=driver_id,
            defaults={
                "car_id": new_car_id,
                "car_model": request.data.get("car_model", ""),
                "car_plate": request.data.get("car_plate", ""),
                "car_type_id": new_car_type,
                "car_type_name": request.data.get("car_type_name", ""),
                "status": "online",
            },
        )
        return Response(s.as_shift())

    def delete(self, request):
        driver_id = acting_driver_id(
            request, request.data.get("driver_id") or request.query_params.get("driver_id")
        )
        if driver_id is None:
            return Response(None)
        # Don't strand an in-flight order: refuse to end the shift while the driver
        # still has an active (non-terminal) order — finish or hand it off first.
        terminal = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)
        active = (
            OrderMeta.objects.filter(driver_id=driver_id).exclude(trip_state__in=terminal).count()
        )
        if active:
            return _bad_request(
                "HAS_ACTIVE_ORDERS",
                _("Finish your %(n)s active order(s) before ending the shift.") % {"n": active},
            )
        DriverShiftState.objects.filter(driver_id=driver_id).delete()
        return Response(None)


class DriverShiftsView(APIView):
    """All active overlay shifts → `{ "671": {car_id, car_type_id, car_model, …} }`.
    The dispatcher merges this into the driver roster so an on-shift driver becomes a
    candidate with the right car type."""

    authentication_classes = [DemoTokenAuthentication]
    # Dispatcher-only: the full on-shift roster (every driver + their car) feeds the
    # dispatcher candidate list. Only dispatcher screens read it, so gate it on
    # car_order:approve rather than exposing every driver's shift to any token.
    permission_classes = [OverlayDispatcher]

    def get(self, request):
        return Response(
            {
                str(s.driver_id): {
                    "car_id": s.car_id,
                    "car_model": s.car_model,
                    "car_plate": s.car_plate,
                    "car_type_id": s.car_type_id,
                    "car_type_name": s.car_type_name,
                    "status": s.status,
                }
                for s in DriverShiftState.objects.all()
            }
        )


class AutoDispatchView(APIView):
    """Runtime on/off switch for the server-side auto-dispatch worker, so the
    dispatcher can flip auto-assignment from the «Диспетчерская» page.

      GET  → current state (anyone authenticated may read)
      POST → {"enabled": bool}  set the switch (dispatcher-only)

    `enabled` is the dispatcher toggle; `effective` also factors in the env-var
    master kill-switch and is what the worker actually obeys."""

    authentication_classes = [DemoTokenAuthentication]

    def get_permissions(self):
        # Reading the state is fine for any dispatcher tab; flipping it is gated.
        if self.request.method == "POST":
            return [OverlayDispatcher()]
        return [OverlayAuthenticated()]

    def _state(self):
        from django.conf import settings

        cfg = DispatchSettings.load()
        return Response(
            {
                "enabled": cfg.auto_enabled,
                "env_enabled": bool(getattr(settings, "AUTO_DISPATCH_ENABLED", True)),
                "effective": dispatch.auto_enabled(),
                "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
                "updated_by": cfg.updated_by,
            }
        )

    def get(self, request):
        return self._state()

    def post(self, request):
        enabled = request.data.get("enabled")
        if not isinstance(enabled, bool):
            return Response(
                {"detail": "`enabled` (bool) is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        cfg = DispatchSettings.load()
        cfg.auto_enabled = enabled
        cfg.updated_by = acting_driver_id(request)
        cfg.save()
        return self._state()


class DriverPositionsView(APIView):
    """Latest position per driver → `{ "671": {lat, lng, last_seen}, ... }`. Powers
    the dispatcher's «nearest free driver» suggestion. Optional `?max_age=600`
    (seconds) drops stale fixes."""

    authentication_classes = [DemoTokenAuthentication]
    # Dispatcher-only: the latest position of EVERY driver powers the dispatcher's
    # «nearest free driver» suggestion (only dispatcher screens call it). Gating on
    # car_order:approve stops any authenticated token from enumerating all drivers'
    # whereabouts.
    permission_classes = [OverlayDispatcher]

    def get(self, request):
        qs = DriverPosition.objects.all()
        try:
            max_age = int(request.query_params.get("max_age", 0))
        except (TypeError, ValueError):
            max_age = 0
        if max_age > 0:
            cutoff = timezone.now() - timedelta(seconds=max_age)
            qs = qs.filter(last_seen__gte=cutoff)
        return Response(
            {
                str(p.driver_id): {
                    "lat": p.lat,
                    "lng": p.lng,
                    "last_seen": p.last_seen.isoformat(),
                }
                for p in qs
            }
        )


def _overlay_rows(metas, request=None):
    """Serialize overlay ``metas`` and decorate each row with the reconciled
    ``effective_status`` (+ the raw demo ``status``) — the SAME single source of truth
    the order list/detail and mobile use. The metas are evaluated once and the backing
    statuses fetched in one batched query (no N+1); ``ListSerializer`` preserves order,
    so we can zip the model instances with their serialized rows.

    Orders usually live only upstream (no local ``CarOrder`` mirror), so when a
    ``request`` is supplied we backfill their demo status from the upstream bodies —
    otherwise the board would read ``effective_status=null`` and each client would
    reconcile it on its own (drifting). See ``_fill_demo_statuses``."""
    from car_orders.services.status import effective_status, status_map_for

    metas = list(metas)
    order_ids = [m.order_id for m in metas]
    status_map = status_map_for(order_ids)
    _fill_demo_statuses(status_map, order_ids, request)
    rows = OrderMetaSerializer(metas, many=True).data
    out = []
    for m, data in zip(metas, rows):
        raw = status_map.get(m.order_id)
        eff = effective_status(raw, m)
        # `status` falls back to the reconciled `effective_status` when the raw demo
        # status can't be read (an overlay-only order whose upstream body is unreadable —
        # e.g. a finished ride in a driver's history). Clients that gate a status badge on
        # `status != null` (the mobile cards) then still render it, NOT a blank.
        out.append({**data, "status": raw or eff, "effective_status": eff})
    return out


class MyOverlayOrdersView(APIView):
    """Active orders from our overlay (both demo-claimed and overlay-claimed carry
    driver_id on OrderMeta). Role-scoped, so it powers BOTH the driver's «Мои
    заказы» page and an admin/dispatcher board:

      • driver          → only their OWN active orders
      • admin/dispatcher → the WHOLE active board (optionally narrowed to one
                           driver via ``?driver_id=X``)

    When auth is enforced the role + driver identity come from the token, so a
    plain driver's spoofed ``?driver_id=`` can't enumerate another driver's orders
    (IDOR — see test_auth_bridge). In open dev mode everyone reads as a dispatcher,
    so the mobile app scopes to itself by passing ``?driver_id=`` while an admin
    tool omits it to get everything."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def get(self, request):
        terminal = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)
        # ?include_terminal=1 → the driver's full HISTORY (completed/cancelled too),
        # most-recent-first. Default keeps the active-only board (mobile «Мои заказы»,
        # the dispatcher board) untouched. The web «Заявки на машину» history view
        # opts in: «1 водитель = 1 активный заказ», so without this a driver only ever
        # sees their single live order and never the orders they already finished.
        include_terminal = request.query_params.get("include_terminal") in ("1", "true", "True")
        qs = OrderMeta.objects.all() if include_terminal else OrderMeta.objects.exclude(
            trip_state__in=terminal
        )
        order_by = ("-planned_datetime", "-order_id") if include_terminal else (
            "planned_datetime",
            "order_id",
        )
        requested = request.query_params.get("driver_id")

        if OverlayDispatcher().has_permission(request, self):
            # Admin / dispatcher: the whole active board, optionally filtered to one
            # driver via ?driver_id=.
            if requested:
                qs = qs.filter(driver_id=requested)
            return Response(_overlay_rows(qs.order_by(*order_by), request=request))

        # Driver: only their own. Identity is the token's user when enforced, so a
        # spoofed ?driver_id= can't enumerate another driver's orders.
        driver_id = acting_driver_id(request, requested)
        if not driver_id:
            return Response([])
        qs = qs.filter(driver_id=driver_id).order_by(*order_by)
        return Response(_overlay_rows(qs, request=request))


class MyActiveOrderView(APIView):
    """The caller's single ACTIVE car order — ``GET /car-orders/me/active-order/``.

    The base ``CarOrder`` lives upstream (demo), but the DRIVER ASSIGNMENT lives in
    OUR overlay — so demo reports a claimed order as ``awaiting_driver / driver=null``
    even after a driver took it. We resolve the caller's active ``OrderMeta`` (driver
    from the token; a spoofed body ``driver_id`` is ignored when auth is enforced),
    fetch the base order body from demo, and overlay our ``driver`` + ``trip_state``
    onto it. Returns the reconciled order, or ``null`` when the caller has none.

    Mounted before the gateway catch-all: the base ``CarOrderViewSet`` router is NOT
    served locally, so without this the path proxies to demo and 404s (which is why
    the mobile/web "my active order" came back empty)."""

    authentication_classes = [DemoTokenAuthentication]
    permission_classes = [OverlayAuthenticated]

    def get(self, request):
        driver_id = acting_driver_id(request, request.query_params.get("driver_id"))
        if not driver_id:
            return Response(None)
        terminal = (OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)
        meta = (
            OrderMeta.objects.exclude(trip_state__in=terminal)
            .filter(driver_id=driver_id)
            .order_by("planned_datetime", "order_id")
            .first()
        )
        if not meta:
            return Response(None)

        import json

        from car_orders.services.status import effective_status
        from config.gateway import gateway

        overlay = OrderMetaSerializer(meta).data
        # Pull the base order body from demo and reconcile our assignment onto it, so
        # the order doesn't read as the stale demo «Ожидает водителя / без водителя».
        order = None
        resp = gateway(request, f"car-orders/{meta.order_id}/")
        if 200 <= resp.status_code < 300:
            try:
                body = json.loads(resp.content)
            except (ValueError, TypeError):
                body = None
            if isinstance(body, dict):
                order = body
        if order is None:
            # A MANUAL assignment keeps the demo order at driver=null, so the assignee
            # often can't read it via the single-order GET (403/404) → fall back to the
            # demo LIST bodies (the SAME source the order list already renders from,
            # cached per token), which the driver CAN see while it's awaiting.
            order = _all_demo_orders(request).get(meta.order_id)

        if isinstance(order, dict):
            order = dict(order)
            order["trip_state"] = meta.trip_state
            order["driver_id"] = meta.driver_id
            order["effective_status"] = effective_status(order.get("status"), meta)
            order["overlay"] = overlay
            return Response(order)

        # No demo body anywhere — return an object that still MATCHES the client's
        # CarOrderModel shape (id, status, coords, planned_datetime) instead of the raw
        # overlay, whose `order_id`/missing `id`/`status` mismatched the model and left
        # the card blank («модели не сходятся»). Address/project text isn't in the
        # overlay, so those stay empty — but the card renders id + status + route + time.
        from car_orders.models import CarOrder

        active = (
            CarOrder.Status.IN_PROGRESS
            if meta.overlay_claimed
            else CarOrder.Status.AWAITING_DRIVER
        )
        return Response({
            **overlay,
            "id": meta.order_id,
            "status": active,
            "effective_status": active,
            "trip_state": meta.trip_state,
            "driver_id": meta.driver_id,
            "overlay": overlay,
        })


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


class CarOrderViewSet(viewsets.ModelViewSet):
    """CRUD + workflow actions for car orders."""

    search_fields = ["address", "note", "project_name"]
    ordering_fields = ["created_at", "planned_datetime", "status"]
    filterset_fields = ["status"]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        qs = CarOrder.objects.select_related(
            "car_type", "car", "car__type", "driver", "created_by", "rejected_by"
        ).prefetch_related("car__drivers")
        user = self.request.user
        if _can_manage_any_car_order(user):
            return qs
        visibility = models.Q(created_by=user) | models.Q(driver=user)
        if user_has_permission(user, "driver:accept_order"):
            shift = _active_shift(user)
            if shift:
                visibility |= models.Q(
                    status=CarOrder.Status.AWAITING_DRIVER, car_type=shift.car.type_id
                )
        return qs.filter(visibility).distinct()

    def get_serializer_class(self):
        if self.action in ("create", "partial_update"):
            return CarOrderWriteSerializer
        if self.action == "activity":
            return CarOrderActivitySerializer
        return CarOrderSerializer

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        # Batch the overlay metas for a LIST so CarOrderSerializer.effective_status
        # resolves without an N+1 (one query for the filtered set, not per row).
        if self.action == "list":
            ids = list(
                self.filter_queryset(self.get_queryset()).values_list("pk", flat=True)
            )
            ctx["metas_by_order_id"] = {
                m.order_id: m for m in OrderMeta.objects.filter(order_id__in=ids)
            }
        return ctx

    # Per-action permissions. Centralised here because overriding
    # get_permissions bypasses any permission_classes set on @action.
    _action_permissions = {
        "create": ["car_order:create"],
        "estimate": ["car_order:create"],
        "admin_approve": ["car_order:approve"],
        "reassign": ["car_order:approve"],
        "claim": ["driver:accept_order"],
        "release": ["driver:accept_order"],
        "start": ["driver:trip_control"],
        "complete": ["driver:trip_control"],
    }

    def get_permissions(self):
        perms = [IsAuthenticated()]
        for codename in self._action_permissions.get(self.action, []):
            perms.append(HasPermission(codename)())
        return perms

    def _read(self, order):
        return CarOrderSerializer(order, context=self.get_serializer_context()).data

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = serializer.save(created_by=request.user, status=CarOrder.Status.DRAFT)
        services.record_created(order, request.user)
        # Direct create: a requester with car_order:create_direct (or a dispatcher
        # with car_order:approve) skips the draft→pending→approve dance — their own
        # brand-new order lands straight in the driver queue. This only fast-tracks
        # the order being created here; it grants no authority over anyone else's
        # orders (no claim/approve/reject), so a customer never sees driver actions.
        if user_has_permission(request.user, "car_order:create_direct") or user_has_permission(
            request.user, "car_order:approve"
        ):
            order.status = CarOrder.Status.AWAITING_DRIVER
            order.save(update_fields=["status", "updated_at"])
            services.record_sent(order, request.user)
            services.record_approved(order, request.user)
        return Response(self._read(order), status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        order = self.get_object()
        if order.status != CarOrder.Status.DRAFT:
            return _bad_request("INVALID_STATUS", _("Only draft orders can be edited."))
        if order.created_by_id != request.user.id:
            return _forbidden(_("Only the creator can edit a draft car order."))
        serializer = self.get_serializer(order, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        order = serializer.save()
        return Response(self._read(order))

    def destroy(self, request, *args, **kwargs):
        order = self.get_object()
        if order.status != CarOrder.Status.DRAFT:
            return _bad_request("INVALID_STATUS", _("Only draft orders can be deleted."))
        is_admin = request.user.is_superuser or user_has_permission(request.user, "administrator")
        if order.created_by_id != request.user.id and not is_admin:
            return _forbidden(_("Only the creator or an administrator can delete this draft."))
        order.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], url_path="submit")
    def submit(self, request, pk=None):
        order = self.get_object()
        if order.created_by_id != request.user.id:
            return _forbidden(_("Only the creator can submit this order."))
        if order.status != CarOrder.Status.DRAFT:
            return _bad_request("INVALID_STATUS", _("Only a draft can be submitted."))
        order.status = CarOrder.Status.PENDING
        order.save(update_fields=["status", "updated_at"])
        services.record_sent(order, request.user)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="admin-approve")
    def admin_approve(self, request, pk=None):
        order = self.get_object()
        if order.status != CarOrder.Status.PENDING:
            return _bad_request("INVALID_STATUS", _("Only a pending order can be approved."))
        order.status = CarOrder.Status.AWAITING_DRIVER
        order.save(update_fields=["status", "updated_at"])
        services.record_approved(order, request.user)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        order = self.get_object()
        if order.status not in (CarOrder.Status.PENDING, CarOrder.Status.AWAITING_DRIVER):
            return _bad_request(
                "INVALID_STATUS", _("This order can no longer be rejected or cancelled.")
            )
        is_author = order.created_by_id == request.user.id
        can_reject = user_has_permission(request.user, "car_order:reject")
        if not (is_author or can_reject):
            return _forbidden(_("You cannot reject this order."))
        order.status = CarOrder.Status.REJECTED
        order.rejected_at = timezone.now()
        order.rejected_by = request.user
        order.reject_reason = request.data.get("reason", "")
        order.save(
            update_fields=["status", "rejected_at", "rejected_by", "reject_reason", "updated_at"]
        )
        services.record_rejected(order, request.user, reason=order.reject_reason)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="claim")
    def claim(self, request, pk=None):
        """Driver reserves an awaiting order into their schedule (Р1: shift car).
        The order moves to ``scheduled``; its window must not overlap another of
        the driver's (plus the travel buffer), else ``TIME_CONFLICT``."""
        try:
            order = services.orders.claim(pk, request.user)
        except services.orders.OrderError as exc:
            return _service_error_response(exc)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="start")
    def start(self, request, pk=None):
        """Driver begins a scheduled trip → ``in_progress`` (only one at a time)."""
        try:
            order = services.orders.start(pk, request.user)
        except services.orders.OrderError as exc:
            return _service_error_response(exc)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="complete")
    def complete(self, request, pk=None):
        """Assigned driver finishes the in-progress trip → ``completed``."""
        try:
            order = services.orders.complete(pk, request.user)
        except services.orders.OrderError as exc:
            return _service_error_response(exc)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        """Dispatcher (or author) cancels an order; frees the driver's window."""
        try:
            order = services.orders.cancel(pk, request.user, reason=request.data.get("reason", ""))
        except services.orders.OrderError as exc:
            return _service_error_response(exc)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="release")
    def release(self, request, pk=None):
        """Assigned driver hands an order back; it returns to ``awaiting_driver``."""
        try:
            order = services.orders.release(pk, request.user, reason=request.data.get("reason", ""))
        except services.orders.OrderError as exc:
            return _service_error_response(exc)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="reassign")
    def reassign(self, request, pk=None):
        """Dispatcher takes an order off its driver → ``awaiting_driver`` so a new
        car can pick it up (e.g. when the driver can't make the latest start)."""
        try:
            order = services.orders.reassign(pk, request.user)
        except services.orders.OrderError as exc:
            return _service_error_response(exc)
        return Response(self._read(order))

    @action(detail=True, methods=["post"], url_path="extend")
    def extend(self, request, pk=None):
        """Add minutes to an active/scheduled order's duration and re-check the
        driver's next window. Allowed for the driver or a dispatcher."""
        try:
            minutes = int(request.data.get("minutes", 0))
        except (TypeError, ValueError):
            minutes = 0
        try:
            order, conflict = services.orders.extend(pk, request.user, minutes)
        except services.orders.OrderError as exc:
            return _service_error_response(exc)
        data = self._read(order)
        data["schedule_conflict"] = conflict
        return Response(data)

    @action(detail=False, methods=["post"], url_path="estimate")
    def estimate(self, request):
        """Auto-estimate route + duration for the create-order card.

        Body: ``{origin_lat, origin_lng, dest_lat, dest_lng, service_minutes?}``.
        """
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

    @action(detail=True, methods=["get"], url_path="activity")
    def activity(self, request, pk=None):
        order = self.get_object()
        qs = order.activities.select_related("actor").all()
        return Response(CarOrderActivitySerializer(qs, many=True).data)

    @action(detail=False, methods=["get"], url_path="me/active-order")
    def my_active_order(self, request):
        order = (
            self.get_queryset()
            .filter(driver=request.user, status=CarOrder.Status.IN_PROGRESS)
            .first()
        )
        return Response(self._read(order) if order else None)


class CarTypeViewSet(viewsets.ModelViewSet):
    queryset = CarType.objects.all()
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    search_fields = ["name"]

    def get_serializer_class(self):
        if self.action in ("create", "partial_update"):
            return CarTypeWriteSerializer
        return CarTypeSerializer

    def get_permissions(self):
        return _garage_permissions(self.action)


class CarViewSet(viewsets.ModelViewSet):
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    search_fields = ["model", "plate_number"]
    filterset_fields = ["type", "status"]

    def get_queryset(self):
        return Car.objects.select_related("type").prefetch_related("drivers")

    def get_serializer_class(self):
        if self.action in ("create", "partial_update"):
            return CarWriteSerializer
        return CarSerializer

    def get_permissions(self):
        return _garage_permissions(self.action)


class DriverViewSet(viewsets.GenericViewSet):
    """Reader over users in the ``Driver`` group + the driver's own shift/location."""

    serializer_class = DriverSerializer
    search_fields = ["name", "username"]

    def get_queryset(self):
        return (
            User.objects.filter(access_group_memberships__group__name=DRIVER_GROUP)
            .distinct()
            .prefetch_related("driven_cars")
        )

    def list(self, request, *args, **kwargs):
        if not (request.user.is_superuser or user_has_permission(request.user, "driver:list")):
            return _forbidden(_("Requires permission: driver:list"))
        qs = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(DriverSerializer(page, many=True).data)
        return Response(DriverSerializer(qs, many=True).data)

    @action(detail=False, methods=["get"], url_path="me/cars")
    def my_cars(self, request):
        cars = request.user.driven_cars.select_related("type").all()
        return Response(CarSerializer(cars, many=True).data)

    @action(detail=False, methods=["get"], url_path="me/schedule")
    def my_schedule(self, request):
        """The driver's committed timeline: scheduled + in-progress orders,
        ordered by planned start, each annotated with delay / reassign flags."""
        if not user_has_permission(request.user, "driver:accept_order"):
            return _forbidden(_("Requires permission: driver:accept_order"))
        orders = (
            CarOrder.objects.filter(
                driver=request.user,
                status__in=[CarOrder.Status.SCHEDULED, CarOrder.Status.IN_PROGRESS],
            )
            .select_related("car_type", "car", "car__type", "driver", "created_by")
            .order_by("planned_datetime", "created_at")
        )
        return Response(
            CarOrderSerializer(orders, many=True, context=self.get_serializer_context()).data
        )

    @action(detail=False, methods=["get", "patch", "delete"], url_path="me/shift")
    def my_shift(self, request):
        if not user_has_permission(request.user, "driver:accept_order"):
            return _forbidden(_("Requires permission: driver:accept_order"))
        shift = _active_shift(request.user)

        if request.method == "GET":
            return Response(DriverShiftSerializer(shift).data if shift else None)

        if request.method == "DELETE":
            if not shift:
                return Response(None)
            if _driver_has_active_trip(request.user):
                return _bad_request(
                    "DRIVER_BUSY", _("Finish your active trip before ending the shift.")
                )
            shift.ended_at = timezone.now()
            shift.status = DriverShift.Status.OFFLINE
            shift.save(update_fields=["ended_at", "status", "updated_at"])
            return Response(DriverShiftSerializer(shift).data)

        # PATCH -> start / switch the shift car (Р1)
        serializer = ShiftStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        car = serializer.validated_data["car"]
        if not car.drivers.filter(pk=request.user.pk).exists():
            return _forbidden(_("This car is not assigned to you."))
        if car.status != Car.Status.ACTIVE:
            return _bad_request("CAR_UNAVAILABLE", _("This car is not active."))
        if (
            DriverShift.objects.filter(car=car, ended_at__isnull=True)
            .exclude(driver=request.user)
            .exists()
        ):
            return _bad_request("CAR_BUSY", _("This car is already on another driver's shift."))
        try:
            with transaction.atomic():
                if shift:
                    if _driver_has_active_trip(request.user):
                        return _bad_request(
                            "DRIVER_BUSY", _("Finish your active trip before switching cars.")
                        )
                    shift.ended_at = timezone.now()
                    shift.status = DriverShift.Status.OFFLINE
                    shift.save(update_fields=["ended_at", "status", "updated_at"])
                shift = DriverShift.objects.create(
                    driver=request.user, car=car, status=DriverShift.Status.ONLINE
                )
        except IntegrityError:
            # AUDIT H3: the .exists() pre-check above is not atomic — a concurrent
            # shift can grab the car (one_active_shift_per_car) or the driver
            # (one_active_shift_per_driver) between check and create. The DB
            # constraint then fires; map it to a clean 400 instead of a 500.
            return _bad_request("CAR_BUSY", _("This car is already on another driver's shift."))
        return Response(DriverShiftSerializer(shift).data)

    @action(detail=False, methods=["post"], url_path="me/location")
    def my_location(self, request):
        if not user_has_permission(request.user, "driver:accept_order"):
            return _forbidden(_("Requires permission: driver:accept_order"))
        shift = _active_shift(request.user)
        if not shift:
            return _bad_request("NO_SHIFT", _("No active shift."))
        serializer = LocationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        shift.lat = serializer.validated_data["lat"]
        shift.lng = serializer.validated_data["lng"]
        shift.last_seen = timezone.now()
        shift.save(update_fields=["lat", "lng", "last_seen", "updated_at"])
        services.publish_driver_location(shift)
        return Response({"lat": shift.lat, "lng": shift.lng, "last_seen": shift.last_seen})

    @action(
        detail=False,
        methods=["post"],
        url_path="make-driver",
        permission_classes=[IsAuthenticated, HasPermission("driver:assign_to_user")],
    )
    def make_driver(self, request):
        target = User.objects.filter(pk=request.data.get("user_id")).first()
        if not target:
            return _bad_request("NOT_FOUND", _("User not found."))
        group, _created = AccessGroup.objects.get_or_create(name=DRIVER_GROUP)
        UserAccessGroup.objects.get_or_create(
            user=target, group=group, defaults={"assigned_by": request.user}
        )
        return Response({"status": "ok", "user_id": target.id})

    @action(
        detail=False,
        methods=["post"],
        url_path="remove-driver",
        permission_classes=[IsAuthenticated, HasPermission("driver:assign_to_user")],
    )
    def remove_driver(self, request):
        user_id = request.data.get("user_id")
        group = AccessGroup.objects.filter(name=DRIVER_GROUP).first()
        if group:
            UserAccessGroup.objects.filter(user_id=user_id, group=group).delete()
        return Response({"status": "ok", "user_id": user_id})


class VehicleReportViewSet(viewsets.ModelViewSet):
    serializer_class = VehicleReportSerializer
    http_method_names = ["get", "post", "head", "options"]
    filterset_fields = ["vehicle", "date"]

    def get_queryset(self):
        user = self.request.user
        qs = VehicleReport.objects.select_related("submitted_by", "vehicle").all()
        if user.is_superuser or user_has_permission(user, "vehicle_report:list"):
            return qs
        return qs.filter(submitted_by=user)

    def get_permissions(self):
        if self.action == "create":
            return [IsAuthenticated(), HasPermission("vehicle_report:create")()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        serializer.save(submitted_by=self.request.user)
