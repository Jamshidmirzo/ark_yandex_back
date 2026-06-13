# 06 — WebSockets (full reference)

All WebSockets are served by **our server (Channels/ASGI)** — not demo, not via the
HTTP gateway. Same host as HTTP, but the `ws://` / `wss://` scheme:

| Env | WS base |
|---|---|
| Dev (LAN) | `ws://<host-IP>:8000` |
| Prod | `wss://<domain>` |

> **Lang prefix is tolerated.** If the app prepends `…/ru/ws/...` out of habit it still routes like
> `…/ws/...` (we mirror the HTTP normalisation). Prefer sending without a prefix.
>
> **`geometry` is always `[lng, lat]`** (GeoJSON). Flip to `[lat, lng]` for the map.
>
> **Reconnect:** on drop, reconnect after ~2 s. On every connect the server replays the current state
> (position/route, or a snapshot), so you never have to catch up after a reconnect.

Four sockets — the mobile cares about §1 and §2:

| Socket | Direction | Who | Purpose |
|---|---|---|---|
| `ws/driver/track/` | **uplink + reply** | driver | streams its GPS, gets the marker + polyline back |
| `ws/order/{id}/track/` | **downlink** | customer / dispatcher | one order's live position + route + stage |
| `ws/fleet/track/` | downlink | dispatcher | snapshot + updates for every active order |
| `ws/notify/{user_id}/` | downlink | any user | toasts on status changes of their orders |

---

## §1. Driver socket — `ws/driver/track/` (sending GPS)

**Bidirectional, the main socket for the driver app.** The phone streams GPS; on every frame the
server replies with where to place the marker and which polyline to draw.

### Connect
```
ws://<host>:8000/ws/driver/track/?token=<demo JWT>
ws://<host>:8000/ws/driver/track/?driver_id=670        # dev fallback
```
- Identity: `?token=` (validated like the REST token) **or** `?driver_id=` (dev). It may also arrive
  in the first message: `{"token":"…"}` / `{"driver_id":670}`.
- Right after connect the server sends: `{ "ok": true, "driver_id": 670 }`.

### What you send (every ~5–10 s, on each location fix)
```json
{ "lat": 41.30050, "lng": 69.20050 }
```

### What you get back on every frame
```json
{
  "order_id": 88,
  "trip_state": "to_client",
  "lat": 41.30050, "lng": 69.20050,
  "geometry": [[69.20,41.30], [69.21,41.305], ...]
}
```
- `lat`/`lng` — **where the marker is now** (move it along the line).
- `geometry` — the **current leg's polyline**; sent **only when it changed** (a new stage, or the
  approach route was recomputed after moving >200 m). When it arrives — **redraw** the line; when it
  doesn't — keep the previous one and just move the marker.
- `order_id`/`trip_state` — the driver's current active order and its stage. `order_id:null` — the
  driver has no active order (just on shift).
- No network → buffer fixes and resend on reconnect.

### Flutter
```dart
final ws = WebSocketChannel.connect(
  Uri.parse('ws://$host:8000/ws/driver/track/?token=$jwt'));
ws.stream.listen((raw) {
  final m = jsonDecode(raw);
  if (m['lat'] != null) moveMarker(m['lat'], m['lng']);        // marker along the line
  if (m['geometry'] != null) drawRoute(m['geometry']);          // redraw the route
});
// on each fix:
ws.sink.add(jsonEncode({'lat': pos.latitude, 'lng': pos.longitude}));
```

> Socket-less alternative — HTTP `POST /drivers/me/location/` (see [04 §4.2](04-live-tracking.md)).
> Same server logic; pick ONE.

---

## §2. Order socket — `ws/order/{order_id}/track/` (watch a car)

**Receive-only.** The customer / dispatcher subscribes to ONE order and watches the car move.
Get `order_id` from `GET /drivers/me/overlay-orders/` (or the order detail).

### Connect
```
ws://<host>:8000/ws/order/88/track/
```
On connect — the last known position + route (if any).

### Incoming messages
Position (move the marker):
```json
{ "lat": 41.30050, "lng": 69.20050, "last_seen": "2026-06-13T09:05:12Z", "geometry": [[69.20,41.30], ...] }
```
- `geometry` arrives on connect and **on every stage change / approach recompute** — on a new one, **replace** the line.

Stage change (update the banner):
```json
{ "trip_state": "in_trip", "returning": true }
```
- `trip_state: "completed"` / `"cancelled"` — close the tracking panel, disconnect.

> This is the «output»: the driver feeds §1, the customer listens on §2 — the server links them by order.

---

## §3. Fleet socket — `ws/fleet/track/`

**Receive-only, for the web dispatcher** (the mobile usually doesn't need it).
```
ws://<host>:8000/ws/fleet/track/
```
- On connect: `{ "type": "snapshot", "orders": [ FleetOrder, ... ] }` — every active order + awaiting ones.
- Then: `{ "type": "update", "order_id": 88, "lat":…, "lng":…, "geometry":…, "trip_state":… }` on
  every move / stage change of any order.

---

## §4. Notification socket — `ws/notify/{user_id}/`

**Receive-only.** The driver and the order's author subscribe to their `user_id` and get a toast on
status changes of their orders.
```
ws://<host>:8000/ws/notify/671/
```
Incoming:
```json
{ "order_id": 88, "trip_state": "to_client", "message": "Driver is on the way to pickup" }
```

---

## Prod notes (important)
- **WebSockets are NOT proxied** by the demo gateway. They terminate on our Channels process — in prod
  the reverse-proxy/ingress must allow the `Upgrade` header on `/ws/` and route it to Channels
  (Daphne/Uvicorn), separately from HTTP.
- Multi-worker prod needs a **Redis** channel layer (`REDIS_URL`), otherwise cross-worker fan-out is lost.
- WS auth in prod — a token in the query (`?token=`); the driver socket already does this, the other
  downlink sockets are planned to be closed with the same token.

---

## Old names (deprecated, still routed)
Paths were renamed; the old ones are kept as **aliases**, new clients use the names above.

| New | Old (alias) |
|---|---|
| `ws/driver/track/` | `ws/drivers/me/location/` |
| `ws/order/{id}/track/` | `ws/car-orders/{id}/location/` |
| `ws/fleet/track/` | `ws/car-orders/fleet/` |
| `ws/notify/{id}/` | `ws/notifications/{id}/` |
