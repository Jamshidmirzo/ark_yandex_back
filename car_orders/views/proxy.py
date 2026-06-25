"""Reverse-proxy of the demo car-order list/detail + the helpers that reconcile and
snapshot demo data onto our overlay. These sit before the gateway catch-all and are
the single source of truth for order status across web + mobile.

``_inject_effective_status`` / ``_snapshot_descriptive`` / ``_fill_demo_statuses`` /
``_DEMO_ORDERS_CACHE`` are imported by tests, the fleet snapshot and sibling view
modules, so they're re-exported from the package namespace (listed in ``__all__``)."""

from django.db import models
from django.views.decorators.csrf import csrf_exempt

from car_orders import services
from car_orders.models import OrderMeta

from .base import logger

__all__ = (
    "admin_approve_overlay",
    "reject_overlay",
    "car_order_proxy",
    "_inject_effective_status",
    "_snapshot_descriptive",
    "_fill_demo_statuses",
    "_DEMO_ORDERS_CACHE",
)


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
