# Mobile API — Overview & Connection

Per-section docs for integrating a mobile client (Flutter / Kotlin / Swift) with the
“car orders” feature.

## Sections
1. [Connection & auth](01-auth.md) — base URL, login, refresh, tokens.
2. [Car orders](02-car-orders.md) — list, detail, create, workflow (submit/approve/claim/complete).
3. [Scheduling & overlay](03-scheduling-overlay.md) — **trip stages** (how to start and run them), claiming an order, auto-computed route/duration, “one active order per driver”, auto-dispatch.
4. [Live tracking (REST + WebSocket)](04-live-tracking.md) — real-time driver position & route on the map.
5. [Reference](05-reference.md) — statuses, trip_state, error format, pagination.
6. [WebSockets](06-websockets.md) — **all sockets**: the driver GPS socket, the order socket, fleet, notifications.
7. [Driver WebSocket](07-driver-websocket.md) — **just for the driver app**: connect, send GPS, receive marker + polyline, Flutter example.

---

## Architecture (read this first)

The mobile app talks to a **single gateway**. The gateway decides what to serve locally and
what to proxy to the big `demo` backend:

```
  Mobile app ──HTTPS/WSS──▶  GATEWAY (this service)
                               ├─ auth/*, car-orders (list/detail/create/
                               │   submit/approve/reject/claim/complete),
                               │   drivers/*, garage/*   ──proxy──▶  demo backend
                               └─ FEATURES (local): estimate, meta, claim-check,
                                   overlay-claim, overlay-release, trip-state,
                                   extend, reassign, live-location,
                                   overlay-orders («My orders»), WebSocket
```

- **Login and base data** (accounts, orders, drivers, cars) come from `demo`.
- **New features** (route estimate, duration/windows, trip stages, live tracking) are served
  **locally by this gateway**.
- The app does **not** need to know what is proxied vs local — it always hits one base URL.

## Base URL

| Env | HTTP base URL | WebSocket base |
|---|---|---|
| Dev (local) | `http://127.0.0.1:8000/api/v1` | `ws://127.0.0.1:8000` |
| Dev (LAN) | `http://<host-IP>:8000/api/v1` | `ws://<host-IP>:8000` |
| Prod | `https://<your-host>/api/v1` | `wss://<your-host>` |

> All paths in these docs are relative to the HTTP base URL, e.g.
> `POST /car-orders/{id}/claim/` = `http://127.0.0.1:8000/api/v1/car-orders/12/claim/`.

### Entering the host in the app (Base URL screen)
- The app takes the **host without `/api/v1`** (e.g. `http://192.168.68.59:8000`), then builds paths
  itself as `host/<lang>/api/v1/...` (language in the path — demo's native scheme).
- On save it validates the URL with **`GET host/healthcheck/`** (no auth), expecting
  **`200 {"status":"ok"}`**. The gateway answers this the same way demo does.
- The gateway accepts **both** schemes: the web `/api/v1/...` (no language) and the mobile
  `/<lang>/api/v1/...` — the language prefix is stripped on the way in, and `/ru/` is still added
  upstream to demo. Same for the probe: `host/healthcheck/` and `host/ru/healthcheck/` both return `200`.

## Auth (short)

1. `POST /auth/login/` → get `access` and `refresh`.
2. Add header `Authorization: Bearer <access>` to every request.
3. On `401` → refresh via `POST /auth/refresh/` and retry once.

Details in [01-auth.md](01-auth.md).

## Responses & errors (short)

- Lists use DRF pagination: `{count, next, previous, results: [...]}` (see [05-reference.md](05-reference.md)).
- Gateway-feature errors: `{"error": {"code", "message", "details"}}`.
- demo errors: DRF format — `{"detail": "..."}` or `{"field": ["..."]}`.

## Minimal “driver” scenario

1. `POST /auth/login/` → token (+ `user.id` = your `driver_id`).
2. `GET /car-orders/?status=awaiting_driver` → available orders.
3. `GET /car-orders/{id}/` → detail.
4. **Accept:**
   - the server **auto-dispatches** orders to the nearest free on-shift driver — the order arrives
     already assigned in **“My orders”** (overlay-orders), so manual claim is rarely needed;
   - if you do claim manually → `POST /car-orders/{id}/claim/` `{car_id}` (demo) + `POST /meta/ {driver_id}`,
     or `POST /car-orders/{id}/overlay-claim/` `{driver_id, car_id, car_label}`. Note: **one active order
     per driver** — if you already have an active order, overlay-claim returns `400 DRIVER_BUSY`. Finish
     the current order before taking the next one.
5. **Stages** (each pushed over WS): `POST /trip-state/` `{trip_state}`:
   `to_client → at_client → in_trip → at_destination → waiting`.
6. Connect to `ws://.../ws/car-orders/{id}/location/` → live driver position + route.
7. **Complete:** demo order → `POST /complete/` + `POST /trip-state/ {completed}`; overlay order → just
   `POST /trip-state/ {completed}`.
8. **Drop / return to queue** (on reject/cancel): `POST /car-orders/{id}/overlay-release/`.

**“My orders” screen:** `GET /car-orders/drivers/me/overlay-orders/?driver_id=<id>` → all of the
driver’s active orders with their stage.

Full path list — [05-reference.md](05-reference.md). Overlay details — [03](03-scheduling-overlay.md).
