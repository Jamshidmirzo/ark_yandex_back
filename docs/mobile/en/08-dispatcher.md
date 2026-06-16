# 08 — Dispatcher console (Диспетчерская)

How to connect the **dispatcher console** — the *web* surface that watches the whole
fleet live on a map, assigns drivers to orders, and turns server-side auto-dispatch
on/off. Unlike the mobile docs (§02–§07), this one is for the **operator/web client**
and the **integrator** wiring it to the backend.

Everything the console needs is served by **our gateway** (`config.urls` →
`car_orders`), not by the demo backend — see the local-feature box in
[README.md](README.md). The live feed runs over the **fleet WebSocket**; REST is the
snapshot fallback and the action layer (assign / reassign / toggle).

| Env | HTTP base URL | WebSocket base |
|---|---|---|
| Dev (local) | `http://127.0.0.1:8000/api/v1` | `ws://127.0.0.1:8000` |
| Dev (LAN) | `http://<host-IP>:8000/api/v1` | `ws://<host-IP>:8000` |
| Prod | `https://<your-host>/api/v1` | `wss://<your-host>` |

> All HTTP paths below are relative to the HTTP base URL, e.g.
> `GET /car-orders/fleet/live/` = `http://127.0.0.1:8000/api/v1/car-orders/fleet/live/`.
> `geometry` is **always** GeoJSON `[lng, lat]` — flip to `[lat, lng]` for the map.

---

## §1. Architecture at a glance

Three processes cooperate; the console talks only to the **backend** (HTTP + WS):

```
                         ┌─────────────────────────────────────────┐
  Dispatcher console ───▶│  BACKEND  (Daphne ASGI, :8000)            │
   (web, HTTP + WSS)     │   • HTTP /api/v1/car-orders/* (local)     │──proxy──▶ demo
                         │   • WS  /ws/fleet/track/  (live board)    │           backend
                         │   • everything else /api/v1/* → demo      │
                         └───────────────┬─────────────┬────────────┘
                                         │ group_send  │ shared DB
                                   ┌─────▼─────┐  ┌─────▼──────────────┐
                                   │  REDIS    │  │  auto_dispatch     │
                                   │ (channel  │◀─│  WORKER (separate  │
                                   │  layer)   │  │  process, no tab   │
                                   └───────────┘  │  needed)           │
                                                  └────────────────────┘
```

- **Backend** — serves the console's REST + the fleet WebSocket, and reverse-proxies
  login / orders / drivers to the demo backend (`UPSTREAM_API_BASE`).
- **auto_dispatch worker** — assigns the nearest free on-shift driver to due orders
  even when **no dispatcher tab is open**. It `group_send`s assignment/route updates
  through **Redis** so they reach the console's WebSocket.
- **Redis** — the cross-process channel layer. Without it (in-memory layer) the
  worker's pushes never cross the process boundary into the web process.

---

## §2. Run it locally

### Docker (recommended) — one command, four services
```bash
docker compose up --build
```
| Service | What it is |
|---|---|
| `redis` | channel layer (cross-process WS fan-out) |
| `backend` | Daphne ASGI on `:8000` — HTTP + WebSocket, gateway to demo |
| `dispatcher` | `python manage.py auto_dispatch` worker (shares the DB volume + `REDIS_URL`) |
| `frontend` | the web console (`:5173`) |

Key env (from `docker-compose.yml`): `UPSTREAM_API_BASE=https://demo.ark.glob.uz/ru/api/v1`,
`REDIS_URL=redis://redis:6379/0`, `AUTO_DISPATCH_ENABLED=true`,
`CORS_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173`.

### Manual / split processes
```bash
# 1) the ASGI server (HTTP + WS)
daphne -b 0.0.0.0 -p 8000 config.asgi:application
# 2) the auto-dispatch worker (separate terminal, same DB + REDIS_URL)
python manage.py auto_dispatch --poll 15
```

> Without `REDIS_URL` the channel layer is in-memory: a single web process still works,
> but the **separate** `auto_dispatch` worker's WS pushes won't reach it. For the full
> live experience run Redis (or run the worker with `--once` from the web process for tests).

---

## §3. Authentication

Login is shared with the rest of the app — proxied to demo, so the JWT is demo-signed.

1. `POST /auth/login/` `{username, password}` → `{access, refresh, user}` (see
   [01-auth.md](01-auth.md)).
2. Send `Authorization: Bearer <access>` on every REST request. The gateway validates
   it against demo `GET /auth/me/` (`config/auth.py`, 60 s cache) and maps it to a
   principal with permission codenames.

### Permission model (`REQUIRE_OVERLAY_AUTH`)
The env flag `REQUIRE_OVERLAY_AUTH` (default **`false`**) is the master gate:

| Mode | `OverlayAuthenticated` endpoints | `OverlayDispatcher` endpoints |
|---|---|---|
| **off** (default, open dev) | allow anyone (anonymous OK) | allow anyone |
| **on** (enforced) | require an authenticated demo user | require the **`car_order:approve`** codename (or superuser) |

When enforced, identity comes from the **token, not the request body** — a spoofed
body `driver_id` can never make a non-dispatcher act as one. The dispatcher-only
actions are **`reassign`** and the **auto-dispatch `POST`** toggle.

> **WebSocket auth today:** the downlink sockets (`fleet`, `order`, `notify`) currently
> **accept without a token** — only the driver uplink validates `?token=` / `?driver_id=`.
> Treat the planned form `ws/fleet/track/?token=<access>` as forward-compatible: pass the
> token in the query now and it'll keep working when downlink auth is enforced.

---

## §4. The live board — fleet feed

The board is driven by the **fleet WebSocket**; the REST endpoint is the snapshot
fallback (initial paint, or polling when WS is unavailable).

### 4.1 WebSocket — `ws/fleet/track/`
```
ws://127.0.0.1:8000/ws/fleet/track/
```
- **On connect** — a full snapshot of every active order (including driverless
  **awaiting** ones, so you can assign them):
```json
{ "type": "snapshot", "orders": [ /* FleetOrder, … */ ] }
```
- **Then** — one frame per move / stage change of any order:
```json
{ "type": "update", "order_id": 88, "lat": 41.30061, "lng": 69.20088,
  "geometry": [[69.2009,41.3006],[69.2031,41.3050]], "trip_state": "in_trip" }
```
- An `update` may carry only a position (`lat`/`lng`), only a new `geometry` (a new
  stage or a re-route), or only a `trip_state` — apply whatever fields are present.
- **Reconnect:** on drop, reconnect after ~2 s. The server replays a fresh snapshot on
  every connect, so you never have to catch up.

### 4.2 REST snapshot — `GET /car-orders/fleet/live/`
`OverlayAuthenticated` → `{ "orders": [ FleetOrder, … ] }`. Same shape as the WS
snapshot; use it for the first paint or as a poll fallback.

### 4.3 The `FleetOrder` object
`OrderMetaSerializer` fields + the live position/route injected by `fleet.py`:

```json
{
  "order_id": 88,
  "driver_id": 671,
  "author_id": 412,
  "is_urgent": false,
  "car_type_id": 1,
  "dispatchable": true,
  "car_id": 5,
  "car_label": "Lada (01A777AA)",
  "overlay_claimed": true,
  "origin_lat": 41.30050, "origin_lng": 69.20050,
  "address_lat": 41.31200, "address_lng": 69.24500,
  "has_return": false, "return_lat": null, "return_lng": null,
  "returning": false,
  "estimated_duration": 45, "service_time": 10,
  "planned_datetime": "2026-06-16T10:00:00Z",
  "latest_start": "2026-06-16T10:15:00Z",
  "trip_state": "in_trip",
  "planned_end": "2026-06-16T10:55:00Z",
  "at_risk": false, "is_late": false,
  "lat": 41.30610, "lng": 69.21880,
  "last_seen": "2026-06-16T10:07:42.511Z",
  "geometry": [[69.2188,41.3061],[69.2300,41.3100],[69.2450,41.3120]]
}
```

| Field group | Fields | Meaning |
|---|---|---|
| Identity | `order_id`, `driver_id`, `author_id`, `car_id`, `car_label` | who/what; `driver_id: null` = **awaiting**, assign it |
| Routing pts | `origin_lat/lng`, `address_lat/lng`, `has_return`, `return_lat/lng`, `returning` | pickup → destination (→ return); `returning` = on the way back |
| Schedule | `planned_datetime`, `latest_start`, `planned_end`, `estimated_duration`, `service_time` | window + computed end |
| State | `trip_state`, `dispatchable`, `overlay_claimed`, `is_urgent` | current stage + flags |
| Live (from `fleet.py`) | `lat`, `lng`, `last_seen`, `geometry` | marker + leg polyline (trimmed, pinned to the car) |
| Risk (computed) | `at_risk`, `is_late` | see §5 |

`trip_state` values: `assigned → to_client → at_client → in_trip → at_destination →
waiting → completed`, plus `cancelled` (terminal). Full table in
[05-reference.md](05-reference.md).

---

## §5. Risk flags

Three booleans drive the board's "attention" triage. They are computed, not stored:

| Flag | True when | Use |
|---|---|---|
| `is_urgent` | the order is marked urgent | sort to top; auto-dispatch treats it as due *now* |
| `at_risk` | the projected start blows past `latest_start` — the assigned driver **won't make it** | suggest a reassign |
| `is_late` | a driver accepted (`driver_id` set, `trip_state == assigned`) but the planned pickup time has already passed (hasn't departed) | nudge the driver |

> A freshly created, not-yet-claimed order defaults to `trip_state=assigned` but has no
> `driver_id`, so it does **not** read as `is_late`.

---

## §6. Building driver suggestions

To offer "nearest free driver" candidates for an awaiting order, merge three reads:

| Method · path | Returns |
|---|---|
| `GET /car-orders/drivers/positions/?max_age=600` | `{ "671": {lat, lng, last_seen}, … }` — latest GPS per driver (drops fixes older than `max_age` s) |
| `GET /car-orders/drivers/shifts/` | `{ "671": {car_id, car_model, car_plate, car_type_id, car_type_name, status}, … }` — who's on shift with which car |
| `POST /car-orders/estimate/` | route + duration A→B (below) |

Rank locally the same way the server does (§9): keep drivers whose `car_type_id`
matches the order, who are free (no active order), nearest by distance.

**Estimate** — `POST /car-orders/estimate/`
```json
// request
{ "origin_lat": 41.3005, "origin_lng": 69.2005,
  "dest_lat": 41.3120, "dest_lng": 69.2450, "service_minutes": 10 }
// response
{ "distance_m": 4120, "drive_minutes": 12, "service_minutes": 10,
  "duration_minutes": 22, "geometry": [[69.2005,41.3005], …], "source": "osrm" }
```

---

## §7. Assigning & managing orders

All paths under `/api/v1/car-orders/`:

| Verb · path | Perm | Body → Response |
|---|---|---|
| `POST <id>/overlay-claim/` | OverlayAuthenticated | `{driver_id, car_id?, car_label?}` → `{ok, conflict, meta}` |
| `POST <id>/reassign/` | **OverlayDispatcher** | `{}` → `{ok, meta}` — take it off the driver, back to the queue (overlay-claimed only) |
| `POST <id>/trip-state/` | OverlayAuthenticated* | `{trip_state}` → `meta` — advance the stage |
| `POST <id>/extend/` | OverlayAuthenticated | `{minutes}` → `{ok, meta, conflict}` — grow the window |
| `POST <id>/claim-check/` | OverlayAuthenticated | `{driver_id}` → `{ok, conflict}` — does it fit the driver's schedule? |
| `POST claim-check-batch/` | OverlayAuthenticated | `{driver_id, order_ids}` → `{results: [{order_id, ok, conflict}]}` |
| `POST <id>/overlay-release/` | OverlayAuthenticated | `{requeue?}` → `{ok, meta?}` — drop the claim (cancel, or requeue) |

\* `trip-state` is gated `OverlayAuthenticated`, but the dispatcher (token with
`car_order:approve`) is allowed to advance any order — a plain driver may only advance
their own.

**Assign a chosen driver** — `POST /car-orders/88/overlay-claim/`
```json
// request — driver_id in the body = the assignee the dispatcher picked
{ "driver_id": 671, "car_id": 5, "car_label": "Lada (01A777AA)" }
// response
{ "ok": true, "conflict": null, "meta": { /* FleetOrder-style meta */ } }
```
On success the server records the driver + car, pushes the approach route, and fans
out the update over the fleet/order/notify sockets.

**Conflict / error shape.** Gateway-feature errors use:
```json
{ "error": { "code": "DRIVER_BUSY", "message": "…", "details": { … } } }
```
- `overlay-claim` enforces **one active order per driver** → `400 DRIVER_BUSY`, and
  rejects an already-taken order → `400 ALREADY_CLAIMED`.
- `extend`/`claim-check` return a non-null `conflict` `{order_id, planned_start,
  planned_end, address}` — for `extend` it's a **warning** (the extension still
  applies); for `claim-check` `ok:false` means it doesn't fit.

---

## §8. Auto-dispatch

Server-side auto-assignment runs in the **`auto_dispatch` worker** so orders get a
driver even with no dispatcher tab open.

### Run / tune (env)
```bash
python manage.py auto_dispatch [--poll 15] [--once]
```
| Env var | Default | Effect |
|---|---|---|
| `AUTO_DISPATCH_ENABLED` | `true` | **ops kill-switch** — if false the worker never assigns |
| `AUTO_DISPATCH_LEAD_MIN` | `45` | assign a scheduled order this many minutes before pickup |
| `AUTO_DISPATCH_STALE_SEC` | `180` | assign an ASAP (no-time) order after it's waited this long |
| `AUTO_DISPATCH_POS_MAX_AGE` | `180` | ignore driver GPS fixes older than this when ranking |

### The runtime toggle (from the console)
Auto-dispatch is live **only when both** are on: the env kill-switch **AND** the
in-app DB toggle (`DispatchSettings`, default off).

`/car-orders/auto-dispatch/`:
```json
// GET  (OverlayAuthenticated) — read state
{ "enabled": true, "env_enabled": true, "effective": true,
  "updated_at": "2026-06-16T09:00:00Z", "updated_by": 412 }
// POST (OverlayDispatcher) — flip the in-app switch
{ "enabled": false }   // → same state object back
```
- `enabled` — the dispatcher's in-app switch (`DispatchSettings.auto_enabled`).
- `env_enabled` — the env kill-switch.
- `effective` — `enabled && env_enabled` — **what the worker actually obeys**. Show
  this in the UI, not `enabled` alone.

### Selection rule
Each pass (`dispatch.run_once`) assigns an **ideal** candidate only — on shift,
matching `car_type_id`, free (load < 1) — nearest by haversine. An order is **due**
when it's urgent (now), scheduled within the lead window, or ASAP after the stale wait.
"1 driver = 1 active order" is enforced on claim.

---

## §9. Live map / route geometry

The server **owns navigation** — the console just draws what it's told:

- `geometry` is GeoJSON `[lng, lat]`; **flip to `[lat, lng]`** for most map SDKs.
- A fresh route is computed (OSRM) **on assignment and on every trip-state change**;
  the leg shown is "what the driver should do next" (approach → trip → return).
- **Re-route on deviation:** if the driver strays > **80 m** off the line, the server
  recomputes from the live position and pushes a new `geometry` — **replace** the line
  when one arrives; otherwise keep the old line and just move the marker.
- The line is **trimmed and pinned to the car** (the passed part is dropped) and
  **downsampled** to stay under the **1 MB WebSocket frame** limit
  (`MAX_GEOM_POINTS=500`, `MAX_STREAM_POINTS=160`; legs > `MAX_LEG_KM=300` are skipped).

For the per-order watch socket and the driver uplink, see
[04-live-tracking.md](04-live-tracking.md) and [06-websockets.md](06-websockets.md).

---

## §10. Production checklist

- **Redis channel layer** — set `REDIS_URL`. Multi-worker prod (and the separate
  `auto_dispatch` process) needs it, or cross-process WS fan-out is lost.
- **ASGI server** — run under Daphne/Uvicorn (`config.asgi:application`), not the WSGI
  dev server, so WebSockets are served.
- **Reverse proxy** — WebSockets are **not** proxied by the demo gateway. The
  proxy/ingress must pass the `Upgrade` header on `/ws/` to the ASGI process.
- **Kill-switches** — keep `AUTO_DISPATCH_ENABLED` as the ops-level off switch; the
  console's toggle (`DispatchSettings`) is the day-to-day one. `effective` reflects both.
- **Auth** — flip `REQUIRE_OVERLAY_AUTH=true` once login is verified end-to-end; send
  the token in the WS query (`?token=`) ahead of downlink-auth enforcement.
- `/<lang>/` prefixes on WS paths are tolerated (`…/ru/ws/fleet/track/`), but prefer
  no prefix.

---

## §11. Reference — endpoints & sockets

### HTTP (relative to the HTTP base URL)
| Method · path | Perm | Purpose |
|---|---|---|
| `GET /car-orders/fleet/live/` | OverlayAuthenticated | fleet snapshot (board paint / poll fallback) |
| `GET /car-orders/drivers/positions/?max_age=` | OverlayAuthenticated | latest GPS per driver |
| `GET /car-orders/drivers/shifts/` | OverlayAuthenticated | on-shift drivers + their car |
| `POST /car-orders/estimate/` | OverlayAuthenticated | route + duration A→B |
| `POST /car-orders/<id>/overlay-claim/` | OverlayAuthenticated | assign a chosen driver |
| `POST /car-orders/<id>/reassign/` | **OverlayDispatcher** | take order off driver → queue |
| `POST /car-orders/<id>/trip-state/` | OverlayAuthenticated | advance the stage |
| `POST /car-orders/<id>/extend/` | OverlayAuthenticated | grow the window |
| `POST /car-orders/<id>/claim-check/` | OverlayAuthenticated | schedule pre-check |
| `POST /car-orders/claim-check-batch/` | OverlayAuthenticated | batch schedule pre-check |
| `POST /car-orders/<id>/overlay-release/` | OverlayAuthenticated | drop the claim (cancel/requeue) |
| `GET /car-orders/auto-dispatch/` | OverlayAuthenticated | read auto-dispatch state |
| `POST /car-orders/auto-dispatch/` | **OverlayDispatcher** | flip the in-app switch |

### WebSocket (relative to the WS base)
| Path | Direction | Purpose |
|---|---|---|
| `ws/fleet/track/` | downlink | board: `snapshot` then `update` frames (this doc) |
| `ws/order/{id}/track/` | downlink | watch one order's position/route/stage ([04](04-live-tracking.md)) |
| `ws/notify/{user_id}/` | downlink | per-user status toasts ([06](06-websockets.md)) |

> Deprecated aliases still route: `ws/car-orders/fleet/` → `ws/fleet/track/`,
> `ws/car-orders/{id}/location/` → `ws/order/{id}/track/`. Use the new names.
