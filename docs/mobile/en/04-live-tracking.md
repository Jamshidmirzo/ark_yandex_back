# 04 — Live tracking: driver position (REST + WebSocket)

The driver position and route are stored in our layer by order id. Two ways to receive them:
**WebSocket** (recommended — real-time push) and **REST** (fallback / one-shot).

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

### Send position (driver app) — RECOMMENDED
`POST /car-orders/drivers/me/location/`
```json
{ "driver_id": 671, "lat": 41.331, "lng": 69.255 }
```
- The **driver** app posts its GPS here every ~10 s — **one endpoint, no need to know the order id**.
- The server finds the driver's **current active order** (stage `to_client`/`in_trip`) and attaches the
  position to it, then fans it out over WebSocket.
- Response: `{ "updated_orders": [88] }` — which orders it applied to (usually one). If the driver
  isn't driving anything right now, you get `{ "updated_orders": [] }`.

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
