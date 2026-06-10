# Mobile API — Overview & Connection

Per-section docs for integrating a mobile client (Flutter / Kotlin / Swift) with the
“car orders” feature.

## Sections
1. [Connection & auth](01-auth.md) — base URL, login, refresh, tokens.
2. [Car orders](02-car-orders.md) — list, detail, create, workflow (submit/approve/claim/complete).
3. [Scheduling & overlay](03-scheduling-overlay.md) — route estimate, meta, claim-check, sequential claim, trip-state machine.
4. [Live tracking (REST + WebSocket)](04-live-tracking.md) — real-time driver position & route.
5. [Reference](05-reference.md) — statuses, trip_state, error format, pagination, endpoint map.

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
                                   live-location, overlay-orders («My orders»), WebSocket
```

- **Login and base data** (accounts, orders, drivers, cars) come from `demo`.
- **New features** (route estimate, duration/windows, sequential same-car orders, trip stages,
  live tracking) are served **locally by this gateway**.
- The app does **not** need to know what is proxied vs local — it always hits one base URL.

## Base URL

| Env | HTTP base URL | WebSocket base |
|---|---|---|
| Dev (local) | `http://127.0.0.1:8000/api/v1` | `ws://127.0.0.1:8000` |
| Prod | `https://<your-host>/api/v1` | `wss://<your-host>` |

> All paths in these docs are relative to the HTTP base URL, e.g.
> `POST /car-orders/{id}/claim/` = `http://127.0.0.1:8000/api/v1/car-orders/12/claim/`.

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
   - car is free → `POST /car-orders/{id}/claim/` `{car_id}` (demo) + `POST /meta/ {driver_id}`;
   - your own busy car (a 2nd order on the same car) → `POST /car-orders/{id}/overlay-claim/`
     `{driver_id, car_id, car_label}`. Before accepting, check the window: `POST /claim-check/ {driver_id}`.
5. **Stages** (each pushed over WS): `POST /trip-state/` `{trip_state}`:
   `to_client → at_client → in_trip → at_destination → waiting`.
6. Connect to `ws://.../ws/car-orders/{id}/location/` → live driver position + route.
7. **Complete:** demo order → `POST /complete/` + `POST /trip-state/ {completed}`; overlay order → just
   `POST /trip-state/ {completed}`.
8. **Drop / return to queue** (on reject/cancel): `POST /car-orders/{id}/overlay-release/`.

**“My orders” screen:** `GET /car-orders/drivers/me/overlay-orders/?driver_id=<id>` → all of the
driver’s active orders with their stage.

Full path list — [05-reference.md](05-reference.md). Overlay details — [03](03-scheduling-overlay.md).
