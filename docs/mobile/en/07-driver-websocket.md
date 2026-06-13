# 07 — Driver WebSocket (the driver app's real-time socket)

> The ONE socket the driver app needs. The phone streams its GPS and, on every frame, gets back
> where to put its marker and which route polyline to draw. For all sockets see
> [06-websockets.md](06-websockets.md); for trip stages see [03-scheduling-overlay.md](03-scheduling-overlay.md) §3.6.

```
ws://<host>:8000/ws/driver/track/
```
Bidirectional. Dev base: `ws://<host-IP>:8000`; prod: `wss://<domain>`.

---

## 1. Connect & identity
```
ws://<host>:8000/ws/driver/track/?token=<demo JWT>      ← recommended
ws://<host>:8000/ws/driver/track/?driver_id=670         ← dev fallback
```
- Identify the driver with **`?token=`** (the same demo JWT used for HTTP — validated server-side) or
  **`?driver_id=`** (dev only). Identity may also be sent in the FIRST message: `{"token":"…"}` or
  `{"driver_id":670}`.
- On connect the server replies once:
  ```json
  { "ok": true, "driver_id": 670 }
  ```
  If `driver_id` is `null` — the token/driver_id wasn't accepted; fix auth before streaming.
- A leading `/<lang>/` is tolerated (`/ru/ws/driver/track/`), but prefer no prefix.

## 2. Send — your GPS (every ~5–10 s)
On each location fix, send one frame:
```json
{ "lat": 41.30050, "lng": 69.20050 }
```
That's it — no `order_id` needed; the server figures out which order is active.

## 3. Receive — marker + polyline (one reply per frame)
```json
{
  "order_id": 88,
  "trip_state": "to_client",
  "lat": 41.30050,
  "lng": 69.20050,
  "geometry": [[69.20,41.30], [69.21,41.305], ...]
}
```
| Field | Meaning | What to do |
|---|---|---|
| `lat`, `lng` | where the marker is now | **move the marker** to this point (animate along the line) |
| `geometry` | the current leg's polyline, `[lng,lat]` pairs — **only present when it CHANGED** | **redraw** the route; if absent, keep the previous polyline |
| `order_id` | the driver's current active order (`null` = none, just on shift) | bind the UI to this order |
| `trip_state` | the order's stage | update the status banner |

When does `geometry` change (and thus arrive)?
- the order is **assigned** / you tapped **«on the way»** → the approach route **your position → pickup**;
- a **stage change** (pickup→destination, the return leg);
- a **route deviation** — you turned the wrong way (strayed **>80 m** off the line) → the server recomputes
  the route from your current point along the road you actually took (like a navigator). Drive straight
  along the line → no recompute.

So: **always** move the marker to `lat/lng`; **replace** the route only when a new `geometry` arrives.

## 4. Reconnect & offline
- On disconnect, reconnect after ~2 s. No catch-up needed — the next reply carries the current state.
- No network → **buffer** your fixes and resend them when the socket is back (don't drop GPS).

## 5. Flutter example
```dart
final ws = WebSocketChannel.connect(
  Uri.parse('ws://$host:8000/ws/driver/track/?token=$jwt'));

ws.stream.listen((raw) {
  final m = jsonDecode(raw) as Map;
  if (m['ok'] == true) { /* identified as driver m['driver_id'] */ }
  if (m['lat'] != null) moveMarker(m['lat'] * 1.0, m['lng'] * 1.0);   // marker along the line
  if (m['geometry'] != null) {                                         // new polyline → redraw
    final pts = (m['geometry'] as List)
        .map((p) => LatLng((p[1] as num).toDouble(), (p[0] as num).toDouble())) // [lng,lat] → LatLng
        .toList();
    drawRoute(pts);
  }
  currentOrderId = m['order_id'];
  currentStage   = m['trip_state'];
}, onDone: reconnectAfter2s, onError: (_) => reconnectAfter2s());

// on every GPS fix:
ws.sink.add(jsonEncode({'lat': pos.latitude, 'lng': pos.longitude}));
```

## 6. What this socket does NOT do
- **It does not advance trip stages.** Tap «on the way / arrived / start / complete» → still a separate
  `POST /car-orders/{id}/trip-state/` (see [03 §3.6](03-scheduling-overlay.md)). After you POST a stage,
  this socket starts replying with the new leg's `geometry`.
- **It is not the customer's view.** The customer/dispatcher watches your car on
  `ws/order/{order_id}/track/` ([06 §2](06-websockets.md)); the server links you to them by order.
- **It is not the only way to send GPS.** The HTTP `POST /drivers/me/location/` does the same server
  work ([04 §4.2](04-live-tracking.md)) — use the socket OR the HTTP, not both.

## 7. Quick checklist
1. On shift (`PATCH /drivers/me/shift/`) → open `ws/driver/track/?token=`.
2. Stream `{lat,lng}` every 5–10 s; move the marker to the reply's `lat/lng`.
3. On a `geometry` field — redraw the route polyline.
4. Drive the stages with `POST /trip-state/`; the socket follows with the right leg.
5. Reconnect on drop; buffer GPS offline.
