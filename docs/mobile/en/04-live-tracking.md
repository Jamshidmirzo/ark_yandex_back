# 04 — Live tracking: driver position (REST + WebSocket)

**Two directions:**
- **Uplink (phone → server): send GPS.** Two equal ways — pick one:
  - **WebSocket** — `ws://<host>/ws/driver/track/` (canonical; alias `ws/drivers/me/location/`, see §4.2
    and [07](07-driver-websocket.md)). Open the socket on shift and stream `{lat,lng}` frames.
  - **HTTP** — `POST /drivers/me/location/` (§4.3). One point at a time; easy to queue offline.
  > Both run the exact same server logic (position + order attach + fan-out + route).
- **Downlink (server → dispatcher/requester): WebSocket.** Having received a position, the server
  **fans it out** in real time to whoever is watching the order map (§4.1).

The live map is rendered by the server; position/route are stored in our layer by order id.

## 4.1 WebSocket (recommended)

```
ws://127.0.0.1:8000/ws/car-orders/{order_id}/location/      (dev)
wss://<your-host>/ws/car-orders/{order_id}/location/        (prod)
```

- Connect when the order is **in progress** (demo `in_progress` or our `trip_state` ∉ {completed/cancelled/assigned}).
- **Right after connecting** the server sends the last known position + the route.
- After that, every position update and every stage change is pushed as a separate message.
- WS auth isn’t required for now (dev). For prod we’ll add a token in the query — leave room for it.

### Incoming message format (JSON)

Position message:
```json
{ "lat": 41.32219, "lng": 69.20615, "last_seen": "2026-06-11T09:05:12Z", "geometry": [[69.24,41.31], ...] }
```
- `geometry` — the route of the **current leg**, computed and pushed by the **server** (it owns
  navigation). It arrives in the first message after connect **and again on every stage change**
  (`trip-state`): approach (`assigned`/`to_client`) → `driver position → pickup point`; trip
  (`at_client`/`in_trip`) → `pickup → destination`; return → `destination → return point`. On parked
  stages (`waiting`/terminal `at_destination`) there is no route. Between messages **keep** the last
  one you got and draw it as a line; on a new `geometry` — **replace** it.

Stage-change message:
```json
{ "trip_state": "in_trip", "returning": true }
```
- Sent when `trip-state/` is called. Update the client’s status banner.
- `returning` — the **return-leg** flag of a round trip (section 03 §3.6.1). Arrives with the stage
  change; `returning:true` after `at_destination` means the driver headed back (return point). Keep it,
  like `geometry`.
- `trip_state: "completed"` — order finished; `trip_state: "cancelled"` — order dropped
  (`overlay-release`). On either, close the tracking panel and the WS.

### UI recommendations
- Move the driver marker **smoothly** (interpolate between points over ~1.5 s); only recentre the
  map when the driver nears the edge — otherwise you hammer map tiles.
- “Connection lost” — if `last_seen` is older than ~30 s.
- Reconnect on drop (2 s backoff).

### Flutter — example
```dart
final ch = WebSocketChannel.connect(
  Uri.parse('ws://127.0.0.1:8000/ws/car-orders/$orderId/location/'));
List<List<double>>? geometry;
double? lat, lng; String? tripState;
ch.stream.listen((raw) {
  final m = jsonDecode(raw);
  if (m['lat'] != null) { lat = (m['lat'] as num).toDouble(); lng = (m['lng'] as num).toDouble(); }
  if (m['geometry'] != null) geometry = (m['geometry'] as List).map<List<double>>(
      (p) => [(p[0] as num).toDouble(), (p[1] as num).toDouble()]).toList();
  if (m['trip_state'] != null) tripState = m['trip_state'];
  if (m['returning'] != null) returning = m['returning'] as bool; // return leg
  setState(() {});
});
```

## 4.2 Uplink over WebSocket (sending GPS)

`ws://<host>:8000/ws/driver/track/` (canonical; `ws/drivers/me/location/` is a working alias) — a
**separate** socket for SENDING the position (not to be confused with the order socket in §4.1, which
is receive-only).

- Identify in the query: `?token=<demo JWT>` (validated like the REST token) **or** `?driver_id=670`
  (dev fallback). You may also send it as the first JSON message: `{"token":"…"}` / `{"driver_id":670}`.
- After connect the server sends `{ "ok": true, "driver_id": 670 }`.
- Then stream the position as frames: `{ "lat": 41.331, "lng": 69.255 }` (every 5–10 s). Each frame is
  answered with `{ "updated_orders": [88] }`.
- Does exactly what the HTTP heartbeat does: writes `DriverPosition`, attaches to the driver's active
  order, fans out over WS to watchers, and (re)computes the "to client" approach route.
- Lang-prefix is fine: `ws://host/ru/ws/driver/track/` also routes.

```dart
final up = WebSocketChannel.connect(
  Uri.parse('ws://$host:8000/ws/driver/track/?token=$jwt'));
// on every geolocation fix:
up.sink.add(jsonEncode({'lat': pos.latitude, 'lng': pos.longitude}));
```

> No connection → buffer fixes and flush when the socket recovers (reconnect with backoff). The HTTP
> alternative below (§4.3 REST) has identical server logic; pick ONE, don't duplicate.

---

## 4.3 REST (fallback)

### Get position
`GET /car-orders/{id}/live-location/` → `null` or:
```json
{ "lat": 41.351, "lng": 69.290, "last_seen": "2026-06-11T09:43:00Z", "geometry": [[69.24,41.31], ...] }
```
Poll every 3 s if you don’t use the WebSocket.

### Send position (driver app) — the MAIN way
`POST /car-orders/drivers/me/location/` (with `Authorization: Bearer <token>`)
```json
{ "lat": 41.331, "lng": 69.255 }
```
- The **driver** app posts its GPS periodically (~every 5–10 s) — **one endpoint, no need to know the
  order id**. The driver comes from the token (`driver_id` in the body is only a dev fallback).
- On each heartbeat the server does **two things**:
  1. stores a **per-driver position** (`DriverPosition`) — used by the dispatcher to find the
     **nearest free** driver. So send GPS **even while the driver is just on shift with no active
     order** — otherwise they won't appear in «Рекомендуем».
  2. attaches the position to the driver's **active** order (any non-terminal stage, not only
     `to_client`/`in_trip`) — `OrderLiveLocation` — and fans it out over WebSocket (downlink). With
     the "one active order per driver" rule this is exactly their current order, so the map moves on
     any stage.
- Response: `{ "updated_orders": [88] }` — which orders it applied to (usually one). If nothing is
  being driven → `{ "updated_orders": [] }` (the per-driver position is still saved).

**Background & offline (mobile guidance):**
- On shift, start background location (native background-location / `watchPosition`), ~5–10 s or by
  significant change. Stop when off shift.
- No network (tunnel, dead zone) → **queue the POSTs** and flush when connectivity returns. For this
  case the HTTP uplink is handy (easy to queue); the socket uplink (§4.2) is an equal alternative.

> **Reading fleet positions (for the dispatcher client, not the driver):**
> `GET /car-orders/drivers/positions/?max_age=180` → `{ "671": {lat,lng,last_seen}, ... }` — the
> latest position per driver (for nearest-driver matching). `max_age` (seconds) drops stale fixes.

### Send position to a specific order (alternative)
`POST /car-orders/{id}/live-location/`
```json
{ "lat": 41.331, "lng": 69.255 }
```
- Same thing but explicitly by order id (used by the simulator). Also fans out over WebSocket.
- You may attach the route once: `"geometry": [[lng,lat], ...]` (optional).

> `geometry` in responses/route is `[lng, lat]`. Flip to `[lat, lng]` for rendering.

## Where the route (geometry) comes from
If the order has `meta` with A→B coordinates, the route is computed automatically (see
[03](03-scheduling-overlay.md) → `estimate`). With no coordinates, tracking shows only the driver
dot without a line. So **save the coordinates to `meta`** when creating the order.

## 4.4 Simulator (testing without a phone)
> **Off by default (updated 2026-06):** real phones stream GPS to `/drivers/me/location/`, and the
> simulator would conflict with them. To run it for testing **without a phone** — set
> `AUTO_SIMULATE_ENABLED=1` or pass the `--force` flag. Do not run it against real drivers.

`python manage.py auto_simulate --force` continuously drives every active order — like the mobile app
will. It is **driver-centric and phase-aware**: one driver = one car = one position, which **carries
across orders**:

- stage `to_client` — drives from the **driver's current position** to the **pickup** (origin). This
  is also the "empty" leg **between orders**: after finishing order 1 at its destination the driver
  heads to order 2's pickup, and you **see** it on the map;
- stage `in_trip` — drives from the **pickup** (origin) to the **destination** (address).

Stopped stages (`assigned`/`at_client`/`waiting`/`at_destination`) and terminal ones
(`completed`/`cancelled`) stay put. The position survives a simulator restart (seeded from the
driver's last stored live-location).

> Transparent to the mobile app: the phone just streams **real GPS** (section 4 above) and the driver
> advances stages with buttons (`trip-state/`). The simulator only mimics that same stream so wiring
> the real app brings no surprises.
