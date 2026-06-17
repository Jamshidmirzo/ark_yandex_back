# 09 — Customer order map (detail → map)

How the customer (the order's author) opens the **map** from the **order detail** and watches, in real
time: the order **status**, their **own** location, the driver marker, and the **route to the meeting
point** (pickup). It's the mirror of the driver flow: the driver streams GPS
([07-driver-websocket.md](07-driver-websocket.md)), the customer **listens**. The dispatcher analogue
is [08-dispatcher.md](08-dispatcher.md).

The socket contract is in [04-live-tracking.md](04-live-tracking.md) §4.1 and
[06-websockets.md](06-websockets.md) §2 — this page covers only what's specific to the **customer
screen**: the entry from detail, the own-location marker, the meeting point, and the
**connect/disconnect lifecycle**.

| Env | HTTP base URL | WebSocket base |
|---|---|---|
| Dev (local) | `http://127.0.0.1:8000/api/v1` | `ws://127.0.0.1:8000` |
| Dev (LAN) | `http://<host-IP>:8000/api/v1` | `ws://<host-IP>:8000` |
| Prod | `https://<your-host>/api/v1` | `wss://<your-host>` |

> `geometry` is **always** GeoJSON `[lng, lat]`; flip to `[lat, lng]` for the map.
> The server **owns navigation** — the client just draws what it's told (see [04](04-live-tracking.md)).

---

## §1. Who it's for & what's on screen

The screen is watched by the **customer/author** of a single order. Map layers:

| Layer | Source | When |
|---|---|---|
| Status banner | `trip_state` from the socket (+ order status from REST) | always |
| **Meeting point** marker (pickup 🟢) | `origin_lat/lng` from `meta` ([03 §3.1](03-scheduling-overlay.md)) | always, if coordinates exist |
| Destination marker (🔴, opt.) | `address_lat/lng` from `meta` | optional |
| **Driver** marker | `lat/lng` from the socket | once a driver is assigned and starts streaming |
| **Route** (polyline) | `geometry` from the socket | arrives on connect and on every stage change / re-route |
| **"Me"** marker | phone GPS (geolocation plugin) | always, with location permission |

> The customer is an **observer**: their GPS is **not** sent to the server (the GPS uplink is the
> separate driver socket, [07](07-driver-websocket.md) / [04 §4.2](04-live-tracking.md)). The "me"
> marker is drawn locally only.

---

## §2. Entry: list → detail → map

### Tap an order → a data card (web + mobile)

Tapping an order in the list opens the **detail** — just the order's data. Web and mobile show the same
set of fields (on mobile it's the `CarOrderDetail` bottom sheet); only the presentation differs. The
data comes from `GET /car-orders/{id}/` ([02-car-orders.md](02-car-orders.md) → the `CarOrder` object):

| Card field | From the response | Note |
|---|---|---|
| Project | `project_name` | title |
| Pickup date/time | `planned_datetime` | ISO-8601 UTC → local time |
| Address (destination) | `address` | text address of the destination |
| Note | `note` | may be empty |
| Car type | `car_type.name` | |
| Status | `status` (+ effective, below) | badge |
| Driver | `driver.name` | if assigned |
| Car | `car.model` + `car.plate_number` | if assigned |
| Created by / at | `created_by.name` / `created_at` | |

- **Effective status (important).** An overlay-claimed order keeps the demo status `awaiting_driver` —
  don't show it as is; derive it per [03 "Effective status for the UI"](03-scheduling-overlay.md):
  `meta.overlay_claimed && trip_state ∉ {completed, cancelled}` → show it as "in progress", taking the
  concrete stage from `trip_state`.
- **Stage & live data — from our layer, not the demo detail:** `GET /car-orders/{id}/meta/`
  (`trip_state`, point coordinates) and `GET /car-orders/{id}/live-location/`
  ([03](03-scheduling-overlay.md) / [04](04-live-tracking.md)).
- **`404` on the detail** — the order no longer exists on demo (rejected / pulled). **Don't surface it
  as an error:** quietly drop the order from the list / "My orders" and refresh
  ([02 "Order detail"](02-car-orders.md)).

> The card works both on its own (just data) and as the entry to the map — via the button below. This
> card view **is not implemented on web or mobile yet** (mobile only has the `CarOrderDetail` bottom
> sheet) — it's a contract to build.

### "Show on map" button → the map screen

From the card, the "Show on map" button opens the map screen and passes the **`order_id`**.

**When to show the button.** When there's something to track — the order is in progress (by the
effective status above):
- `meta.overlay_claimed && trip_state ∉ {completed, cancelled}` → show the map;
- otherwise by demo status: the map for `awaiting_driver` and `in_progress`, hidden for
  `draft` / `pending` / `completed` / `rejected`.

Before a driver is assigned (no `driver_id` / no position) the map shows the **meeting point + your
marker**; the driver marker and route appear as soon as the stream starts.

**What's drawn on the map — by order stage** (when the map is open):

| Status / stage | What's on the map |
|---|---|
| `awaiting_driver` (no driver yet) | pickup 🟢 + destination 🔴 + planned route, **no car** |
| `in_progress` | the same + the live driver marker (`lat/lng` from the socket) |
| terminal (`completed` / `rejected` / `cancelled`) | map hidden |

> Don't gate the map on `in_progress` alone: an awaiting order must still show its meeting point and
> route — otherwise the customer can't see where the car will arrive (this was the «order not visible
> on the map» bug).

**Data for the map's first frame:** status / driver / car — from the detail above; the point
coordinates and `trip_state` — from `meta` ([03 §3.1](03-scheduling-overlay.md)); then the socket
(replay-on-connect sends position + route + stage, see §6).

---

## §3. Order status — two layers

1. **Workflow status** (`CarOrderStatus`, from REST): `draft · pending · awaiting_driver · in_progress ·
   completed · rejected`. In the app it's the `CarOrderStatus` enum (`titleDisplay`, colours
   `getBgColor` / `getTextColor`) — `lib/features/car_orders/domain/enum/car_order_status.dart`. Status
   reference — [05-reference.md](05-reference.md).
2. **Live stage** (`trip_state`, from the socket/`meta`): `assigned → to_client → at_client → in_trip →
   at_destination → waiting → completed / cancelled`. **The map banner is driven by `trip_state`** — it
   changes in real time.

The customer-facing labels are already defined — the "Client sees" column in
[03 §3.6](03-scheduling-overlay.md):

| `trip_state` | Customer banner | What's on the map |
|---|---|---|
| `assigned` | Driver assigned | the driver may not be streaming a position yet |
| `to_client` | En route to pickup | **route: driver → meeting point** |
| `at_client` | At pickup | driver at the meeting point |
| `in_trip` | En route to destination | route: pickup → destination |
| `at_destination` | Arrived | no route (parked) |
| `waiting` | On hold | no route |
| `completed` | Completed | close the panel, disconnect (§6) |
| `cancelled` | Cancelled | close the panel, disconnect (§6) |

> Besides the map, the customer can receive **toasts** on status changes: the server sends them to the
> order's author (`author_id`) too, on the `ws/notify/{user_id}/` socket — with ready-made text
> ("Driver is on the way to pickup", etc.). These are background notifications, see
> [06 §4](06-websockets.md); the map itself only needs `trip_state` from the order socket.

---

## §4. The customer's own location

So you can tell where you are relative to the meeting point:

1. Request **location permission** (`while in use`) when the map opens.
2. Subscribe to the phone's position stream (watch / significant-change) and draw the **"me"** marker.
3. Frame the camera so **both you and the meeting point** fit (and the driver, once assigned); don't
   recompute the camera on every frame, or you'll hammer the tiles.

This is purely client-side: nothing is **sent anywhere**. If permission is denied, the map still works
without the "me" marker (show the meeting point and the driver).

---

## §5. Route to the meeting point

The "meeting point with the driver" = the **pickup** point (`origin_*` in `meta`, 🟢 in
[03 §3.1](03-scheduling-overlay.md)).

The main route is computed by the **server** and pushed in the socket's `geometry` field. Which leg it
is depends on the stage ([04 §4.1](04-live-tracking.md)):
- `assigned` / `to_client` → **driver → meeting point** (this is the "route to the meeting point");
- `at_client` / `in_trip` → meeting point → destination;
- `waiting` / `at_destination` → no route (parked).

Drawing rules (same as the dispatcher's, [08 §9](08-dispatcher.md)):
- `geometry` arrives on connect and **on every stage change / re-route** (the driver strayed >80 m off
  the line) — a new one → **replace** the line; none → keep the previous one and just move the marker.
- The line is already trimmed to the car's current position and downsampled to fit the frame — draw it
  as is (flipped to `[lat, lng]`).

**Your own route to the meeting point (optional).** To show how **you** get to the pickup, build the
`me → meeting point` leg via `POST /car-orders/estimate/` ([03 §3.2](03-scheduling-overlay.md)):

```json
// request: origin = my position, dest = meeting point
{ "origin_lat": 41.305, "origin_lng": 69.201, "dest_lat": 41.311, "dest_lng": 69.240, "service_minutes": 0 }
// response
{ "distance_m": 1180, "drive_minutes": 4, "duration_minutes": 4,
  "geometry": [[69.201,41.305], ...], "source": "osrm" }
```

Draw this `geometry` in your own colour, separate from the driver's route. `estimate` needs no auth.

---

## §6. WebSocket: connect & disconnect

The heart of the screen. The customer listens to the **single-order socket** — `read-only`, sends
nothing.

### URL

```
ws://<host>:8000/ws/order/<order_id>/track/
```

New name; the old alias `ws/car-orders/<id>/location/` still routes (use the new one). A `/<lang>/`
prefix is tolerated, but prefer none.

**Building the URL in the app** — reuse the existing `buildArkWebSocketUrl` helper
(`lib/features/chats/data/websocket_service.dart`):

```dart
final url = buildArkWebSocketUrl(
  baseApiUrl: endpoints.host,          // host without /api/v1
  accessToken: storage.accessToken,    // adds ?token=… (see below)
  path: '/ws/order/$orderId/track/',
);
```

> **About the token.** The downlink sockets (`order`, `fleet`, `notify`) currently **accept without a
> token** — only the driver uplink validates it ([08 §3](08-dispatcher.md)). The order socket joins by
> the `order_id` in the path and doesn't read the query, so an extra `?token=…` does **not** bother it
> and stays forward-compatible for when downlink auth is enforced. You may send the token already.

### Connect (when the screen opens)

Connect the moment the map opens (screen init / provider build). Right after `accept` the server does a
**replay**: the first frame is the last known state, so the map never flashes blank:

```json
{ "lat": 41.30050, "lng": 69.20050, "last_seen": "2026-06-16T09:05:12Z",
  "geometry": [[69.20,41.30], ...], "trip_state": "to_client", "returning": false }
```

If the order has neither a position nor `meta` yet, there's no replay frame — draw the meeting point
from `meta` and wait for live frames.

### Messages (what arrives next)

Same shape as [04 §4.1](04-live-tracking.md) / [06 §2](06-websockets.md) — apply whatever fields are
present in the frame:
- **position:** `{ "lat", "lng", "last_seen", "geometry"? }` → move the driver marker; a new `geometry`
  → replace the polyline;
- **stage change:** `{ "trip_state", "returning" }` → update the banner (§3);
- `trip_state: "completed"` / `"cancelled"` → show the final status and **disconnect** (below).

### Disconnect (when the screen closes)

Close the socket when the screen goes away — in the screen/provider `dispose()` — with the normal close
code `WebSocketStatus.normalClosure`. The server removes you from the order's group itself
([tracking.py](../../../car_orders/ws/tracking.py) `disconnect → group_discard`). Also disconnect on a
terminal `trip_state` (`completed`/`cancelled`) after showing the final state.

### Reconnect & liveness

- Drop → reconnect with a ~2 s backoff (the ready-made logic lives in `WebSocketService`); on every
  connect the server replays again, so there's nothing to catch up on.
- `last_seen` older than ~30 s → show "Connection lost" (driver offline / in a tunnel), leave the marker
  in place.
- On prod tokens: on `401/403` refresh the token and reconnect (already built into `WebSocketService`
  via `TokenRefreshedEvent`).

---

## §7. Flutter implementation (notes)

The map screen doesn't exist in the app **yet** — it has to be built. What to reuse rather than rewrite:

- **The socket URL & lifecycle** — `buildArkWebSocketUrl` + the `WebSocketService` class
  (connect / reconnect+backoff / ping / dispose, token refresh) from
  `lib/features/chats/data/websocket_service.dart`. Caveat: order frames are **plain** `{lat,lng,…}`,
  whereas `WebSocketService` parses incoming data into the Odoo `BaseSocketMessageModel`. So either
  generalise the message model, or add a small dedicated order-track client following the same lifecycle.
- **The provider** — wrap it in a Riverpod `orderLiveMapProvider(orderId)`: `connect()` on subscribe,
  `ref.onDispose(() => disconnect())` (mirror `webSocketServiceProvider`). Take the detail and status
  from the existing `carOrderDetailProvider`
  (`presentation/providers/car_order_detail_provider/`).
- **New dependencies** — a map SDK (**Yandex MapKit**, in keeping with `ark_yandex`) for tiles, markers
  and the polyline + a geolocation plugin for the "me" marker.

The screen state holds: `tripState`, the driver position (`lat/lng`), `geometry` (flipped to
`[lat, lng]`), `returning`, the meeting point/destination coordinates from `meta`, and your own position
(`myLat/myLng`).

---

## §8. Example (skeleton)

```dart
// screen opens → connect
final url = buildArkWebSocketUrl(
  baseApiUrl: endpoints.host,
  accessToken: storage.accessToken,
  path: '/ws/order/$orderId/track/',
);
final ch = WebSocketChannel.connect(Uri.parse(url));

List<List<double>>? route; // [lat, lng] for the map
double? carLat, carLng; String tripState = 'assigned'; bool returning = false;

ch.stream.listen((raw) {
  final m = jsonDecode(raw) as Map<String, dynamic>;
  if (m['lat'] != null) { carLat = (m['lat'] as num).toDouble(); carLng = (m['lng'] as num).toDouble(); }
  if (m['geometry'] != null) {
    route = (m['geometry'] as List)
        .map<List<double>>((p) => [(p[1] as num).toDouble(), (p[0] as num).toDouble()]) // flip [lng,lat]→[lat,lng]
        .toList();
  }
  if (m['trip_state'] != null) tripState = m['trip_state'] as String;
  if (m['returning'] != null) returning = m['returning'] as bool;
  if (tripState == 'completed' || tripState == 'cancelled') {
    ch.sink.close(WebSocketStatus.normalClosure); // terminal → disconnect
  }
  setState(() {});
});

// the "me" marker — from the geolocation plugin (NOT sent to the server)
geo.getPositionStream().listen((p) => setState(() { myLat = p.latitude; myLng = p.longitude; }));

// screen closes → disconnect
@override
void dispose() {
  ch.sink.close(WebSocketStatus.normalClosure);
  super.dispose();
}
```

> In prod, prefer sending the token already (`buildArkWebSocketUrl` adds `?token=`), even though the
> order socket doesn't require it yet (§6).

---

## §9. Reference & checklist

**Screen endpoints:**

| Method · path | Purpose |
|---|---|
| `GET /car-orders/{id}/` | status, driver, car, address ([02](02-car-orders.md)) |
| `GET /car-orders/{id}/meta/` | point coordinates + `trip_state` ([03 §3.1](03-scheduling-overlay.md)) |
| `POST /car-orders/estimate/` | your own `me → meeting point` route (opt., [03 §3.2](03-scheduling-overlay.md)) |
| `GET /car-orders/{id}/live-location/` | REST position fallback if not using the socket ([04 §4.2](04-live-tracking.md)) |

**Sockets:**

| Path | Direction | Purpose |
|---|---|---|
| `ws/order/{id}/track/` | downlink | one order's position + route + stage (this screen; [06 §2](06-websockets.md)) |
| `ws/notify/{user_id}/` | downlink | background status-change toasts (opt., [06 §4](06-websockets.md)) |

> The alias `ws/car-orders/{id}/location/` still routes — use the new name.

**Integration checklist:**

- [ ] "Show on map" button on the detail (shown by effective status, §2), passes `order_id`.
- [ ] On open — `connect` to `ws/order/{id}/track/`, first frame (replay) → render without a blank map.
- [ ] `geometry` flipped `[lng,lat] → [lat,lng]`; a new `geometry` replaces the line, otherwise move the marker.
- [ ] The banner follows `trip_state` ("Client sees" column, §3).
- [ ] "Me" marker from geolocation; nothing sent to the server.
- [ ] Meeting point = `origin_*` from `meta`; (opt.) your own route to it via `estimate`.
- [ ] `completed`/`cancelled` → final state and `disconnect`.
- [ ] On screen close — `disconnect` (`normalClosure`); reconnect ~2 s; `last_seen` > ~30 s → "Connection lost".
