# 10 — Fleet map (all orders on one map) — mobile

How a **mobile** client shows **every active order on one map**, exactly like the web
dispatcher board ([08-dispatcher.md](08-dispatcher.md)) — markers for each order and
the route each one should drive, live. This is the mobile counterpart of the web
«Диспетчерская»: same backend feed, same data, just rendered in the app.

It reuses **one** feed — the **fleet** socket (live) with a REST snapshot fallback.
You do **not** open a socket per order; one connection streams the whole fleet.

> If you need to watch **one** order (the customer flow), use the per-order socket
> instead — [09-customer-map.md](09-customer-map.md). This page is for the
> **all-orders** view.

| Env | HTTP base URL | WebSocket base |
|---|---|---|
| Dev (local) | `http://127.0.0.1:8000/api/v1` | `ws://127.0.0.1:8000` |
| Dev (LAN) | `http://<host-IP>:8000/api/v1` | `ws://<host-IP>:8000` |
| Prod | `https://<your-host>/api/v1` | `wss://<your-host>` |

> `geometry` is **always** GeoJSON `[lng, lat]` — flip to `[lat, lng]` for the map.
> The server **owns the route**: a driverless order still carries its planned
> pickup → destination route, so you always see where it should go (see §4).

---

## §1. The feed — one socket for the whole fleet

```
ws://<host>:8000/ws/fleet/track/
```

- **On connect** — a full snapshot of every active order (including ones still
  **awaiting** a driver, so you can see and place them):
```json
{ "type": "snapshot", "orders": [ FleetOrder, … ] }
```
- **Then** — one frame per move / stage change of any order, tagged with `order_id`:
```json
{ "type": "update", "order_id": 134, "lat": 41.3061, "lng": 69.2188,
  "geometry": [[69.2188,41.3061],[69.2300,41.3100]], "trip_state": "in_trip" }
```
- An `update` may carry only a position (`lat`/`lng`), only a new `geometry` (new
  stage or re-route), or only a `trip_state` — **apply whatever fields are present**,
  matched to the order by `order_id`.

**REST snapshot fallback** (first paint, or if you don't use the socket):
```
GET /car-orders/fleet/live/   →  { "orders": [ FleetOrder, … ] }
```
Same shape as the socket snapshot.

> **Why one socket, not many:** the fleet feed already fans every order's updates
> into a single stream. Opening `ws/order/{id}/track/` per order is for the
> single-order screen, not the map of all of them.

---

## §2. The `FleetOrder` object

The fields you need to render one order:

```json
{
  "order_id": 134,
  "driver_id": null,            // null = awaiting a driver
  "trip_state": "assigned",
  "lat": null, "lng": null,     // live driver position; null until a driver streams
  "last_seen": null,
  "geometry": [[69.2797,41.3102],[69.2650,41.3180],[69.2407,41.3338]],  // [lng,lat]
  "origin_lat": 41.3111, "origin_lng": 69.2797,    // pickup 🟢
  "address_lat": 41.3338, "address_lng": 69.2407,  // destination 🔴
  "car_label": "",
  "is_urgent": false, "at_risk": false, "is_late": false,
  "dispatchable": true,
  "planned_datetime": "2026-06-16T18:00:00Z"
}
```

| Field | Use on the map |
|---|---|
| `lat` / `lng` | the **car marker**. If `null` (awaiting / not departed) → place the marker at `origin` instead |
| `geometry` | the **route polyline** — draw it (flip `[lng,lat]→[lat,lng]`) |
| `origin_lat/lng` | pickup marker 🟢 (the meeting point) |
| `address_lat/lng` | destination marker 🔴 |
| `driver_id` | `null` → colour it as **awaiting** |
| `trip_state` | the order's stage (banner / colour) |
| `is_urgent` / `at_risk` / `is_late` | attention colours (see [08 §5](08-dispatcher.md)) |

---

## §3. Rendering — what to draw per order

For every order in the snapshot (and on each update, re-render the matching one):

1. **Car marker** at `[lat, lng]`; if those are `null`, at `[origin_lat, origin_lng]`.
2. **Route polyline** from `geometry` (flipped to `[lat, lng]`). Replace it whenever a
   **new** `geometry` arrives; if an update has no `geometry`, keep the old line and
   just move the marker.
3. **Pickup 🟢** at `origin_*`, **destination 🔴** at `address_*` (if present).
4. **Colour** by state: urgent → red, awaiting (no driver) → yellow, otherwise by
   trip stage. A moving car whose `last_seen` is older than ~30 s → grey ("no signal").

Camera: fit all visible markers; don't recompute on every frame (you'll hammer tiles).

---

## §4. Driverless orders DO show a route

A just-created, not-yet-assigned order (e.g. **134**) has **no driver and no live
position**, but the server now puts its **planned pickup → destination route** in
the snapshot `geometry` — so you can see where it should go before anyone is
assigned. Render it the same way; the car marker sits at the pickup until a driver
starts streaming, then it follows the live position.

> Backend: `fleet_live_orders()` computes this planned route for any order without a
> live position ([car_orders/fleet.py](../../../car_orders/fleet.py)). Needs the
> order's `origin_*` **and** `address_*` coords — if either is missing there's no
> route to draw (only the markers).

---

## §5. Authentication

```dart
final url = buildArkWebSocketUrl(
  baseApiUrl: endpoints.host,          // host WITHOUT /api/v1  ← important (see §7)
  accessToken: storage.accessToken,    // adds ?token=…
  path: '/ws/fleet/track/',
);
```

> The downlink sockets (`fleet`, `order`, `notify`) currently **accept without a
> token** — only the driver uplink validates it ([08 §3](08-dispatcher.md)). Sending
> `?token=…` is harmless and forward-compatible for when downlink auth is enforced —
> prefer sending it. REST `GET /car-orders/fleet/live/` takes the usual
> `Authorization: Bearer <token>`.

---

## §6. Connect / reconnect / disconnect

- **Connect** when the map screen opens; render the `snapshot` frame immediately so
  the map never flashes blank.
- **Reconnect** on drop with a ~2 s backoff (reuse `WebSocketService`). On every
  connect the server replays a fresh snapshot — nothing to catch up on.
- **Disconnect** in the screen/provider `dispose()` (normal close code). The server
  removes you from the fleet group itself.

---

## §7. Troubleshooting — "can't connect / nothing shows"

This is the usual cause when the web works but mobile doesn't:

| Symptom | Cause | Fix |
|---|---|---|
| Socket connects then **closes instantly** | wrong WS path → the server's catch-all closes unknown paths quietly | path must be exactly `/ws/fleet/track/` (trailing slash). A `/ru/` prefix is tolerated, but no `/api/v1`. |
| Socket **never connects** | the WS URL was built from the **HTTP API base** (it contains `/api/v1` and maybe `/ru/`) | WebSockets are **NOT** under `/api/v1` and are **NOT** proxied by the gateway. Build from the **host only** (`http://<host>:8000`) → `ws://<host>:8000/ws/fleet/track/`. Use `buildArkWebSocketUrl(baseApiUrl: endpoints.host, …)`, not the api base. |
| `ws://` fails in prod | prod is TLS | use `wss://<domain>`; the reverse proxy must pass the `Upgrade` header on `/ws/` to the ASGI server. |
| Connects, but **no live updates** (snapshot OK) | multi-process prod without a shared channel layer | set `REDIS_URL` so the auto-dispatch worker's pushes reach the web process ([08 §10](08-dispatcher.md)). |
| Connects, snapshot empty / **a new order is missing** | the server didn't reload, or the order isn't dispatchable yet | the dev server runs `runserver --noreload` → restart it after backend changes; confirm `GET /car-orders/fleet/live/` lists the order over plain HTTP first. |
| Order shows a marker but **no route line** | the order has no destination coords | a route needs both `origin_*` and `address_*` on the order's `meta` (§4). |

**Quick check** (does the backend even serve it?):
```bash
curl -s http://<host>:8000/api/v1/car-orders/fleet/live/ | jq '.orders[] | {order_id, driver_id, geometry: (.geometry|length)}'
```
If an order shows here with a non-zero `geometry` length but not on mobile, the issue
is the **WS URL / rendering** in the app, not the backend.

---

## §8. Flutter implementation (notes)

The fleet map screen doesn't exist in the app yet — build it like the customer map
([09 §7](09-customer-map.md)), but rendering the **list** of orders instead of one:

- **Socket URL & lifecycle** — `buildArkWebSocketUrl` + `WebSocketService`
  (`lib/features/chats/data/websocket_service.dart`): connect / reconnect+backoff /
  dispose. Fleet frames are plain JSON `{type, orders|order_id, …}` — add a small
  dedicated client or generalise the message model (it currently parses Odoo
  `BaseSocketMessageModel`).
- **Provider** — `fleetLiveMapProvider`: hold `Map<int, FleetOrder>` keyed by
  `order_id`; on `snapshot` replace the map, on `update` merge the changed fields into
  the matching order, then re-render. `ref.onDispose(() => disconnect())`.
- **Map SDK** — Yandex MapKit (in keeping with `ark_yandex`): one `Placemark` per car +
  pickup/destination dots, one `Polyline` per order from `geometry`.
- **Endpoints to add** to `lib/core/hosts/endpoints.dart`: `fleet/live/` (REST
  snapshot) and the `ws/fleet/track/` path (none of the tracking endpoints exist there
  yet).

---

## §9. Reference & checklist

| Method · path | Purpose |
|---|---|
| `ws://host:8000/ws/fleet/track/` | live feed: `snapshot` then `update` frames (all orders) |
| `GET /car-orders/fleet/live/` | REST snapshot (first paint / fallback) |
| `GET /car-orders/{id}/meta/` | one order's coords + stage, if you open a detail ([03 §3.1](03-scheduling-overlay.md)) |

- [ ] Build the WS URL from the **host only** (no `/api/v1`) → `ws://host:8000/ws/fleet/track/`.
- [ ] On open — connect; render the `snapshot`; keep orders in a map keyed by `order_id`.
- [ ] On `update` — merge fields into the matching order (position / geometry / stage).
- [ ] Draw: car marker (or pickup if no live pos), route polyline (`[lng,lat]→[lat,lng]`), pickup/destination dots.
- [ ] Driverless orders show the planned pickup→destination route (§4) — render it too.
- [ ] Colours by urgent / awaiting / stage; stale `last_seen` (>~30 s) → grey.
- [ ] Reconnect ~2 s on drop; `disconnect` on screen close.
- [ ] If nothing shows — run the `curl` check in §7 to split backend vs app.
