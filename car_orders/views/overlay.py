"""Overlay layer: claim / release / trip-state / extend / no-show / reassign on the
demo order via our local OrderMeta, plus the role-scoped «my orders» / «my active
order» reads. ``_overlay_rows`` is imported by tests, so it's re-exported."""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from car_orders import scheduling, services
from car_orders.models import OrderLiveLocation, OrderMeta
from car_orders.permissions import (
    OverlayAuthenticated,
    OverlayDispatcher,
    OverlayDriverOrDispatcher,
    acting_driver_id,
    assignee_driver_id,
)
from car_orders.serializers import OrderMetaSerializer
from config.auth import DemoTokenAuthentication

from .base import User, _bad_request, _forbidden, _log_tracking, _service_error_response, _src
from .proxy import _all_demo_orders, _driver_snapshot, _fill_demo_statuses

__all__ = (
    "OrderMetaView",
    "ClaimCheckView",
    "MetaBatchView",
    "ClaimCheckBatchView",
    "OverlayClaimView",
    "TripStateView",
    "OverlayReleaseView",
    "NoShowView",
    "ExtendView",
    "ReassignView",
    "MyOverlayOrdersView",
    "MyActiveOrderView",
    "_overlay_rows",
)


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
        # ?include_terminal=1 → the driver's full HISTORY (completed/cancelled too),
        # most-recent-first. Default keeps the active-only board (mobile «Мои заказы»,
        # the dispatcher board) untouched. The web «Заявки на машину» history view
        # opts in: «1 водитель = 1 активный заказ», so without this a driver only ever
        # sees their single live order and never the orders they already finished.
        include_terminal = request.query_params.get("include_terminal") in ("1", "true", "True")
        qs = OrderMeta.objects.all() if include_terminal else OrderMeta.objects.not_terminal()
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
        meta = (
            OrderMeta.objects.active_for_driver(driver_id)
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
