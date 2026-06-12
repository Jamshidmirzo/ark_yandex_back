# 04 — Live tracking: driver position (REST + WebSocket)

**Two directions — different transport (important for mobile):**
- **Uplink (phone → server): HTTP.** The driver app **sends** its position with a plain
  `POST /drivers/me/location/` (§4.2). No WebSocket for sending — HTTP survives backgrounding (the OS
  suspends sockets) and is easy to queue offline.
- **Downlink (server → dispatcher/requester): WebSocket.** On each HTTP heartbeat the server **fans
  out** the position in real time to whoever is watching the map (§4.1).

So the phone only **posts** GPS; the live map is rendered by the server over WS. Position/route are
stored in our layer by order id.

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
- `geometry` arrives **once** (in the first message after connect) — it’s the A→B route. Later
  messages don’t include it, so **keep** what you got.

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

## 4.2 REST (fallback)

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
  2. if the driver has a **moving** order (stage `to_client`/`in_trip`), attaches the position to it
     (`OrderLiveLocation`) and fans it out over WebSocket (downlink).
- Response: `{ "updated_orders": [88] }` — which orders it applied to (usually one). If nothing is
  being driven → `{ "updated_orders": [] }` (the per-driver position is still saved).

**Background & offline (mobile guidance):**
- On shift, start background location (native background-location / `watchPosition`), ~5–10 s or by
  significant change. Stop when off shift.
- No network (tunnel, dead zone) → **queue the POSTs** and flush when connectivity returns. This is
  exactly why the uplink is HTTP, not a socket.

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

## 4.3 Simulator (testing without a phone)
`python manage.py auto_simulate` continuously drives every active order — like the mobile app will.
It is **driver-centric and phase-aware**: one driver = one car = one position, which **carries across
orders**:

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
