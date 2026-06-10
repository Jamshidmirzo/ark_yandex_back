# 05 — Reference: statuses, errors, endpoints

## Order status (`CarOrder.status`, from demo)

| status | Meaning | Next |
|---|---|---|
| `draft` | Draft | author → `submit` |
| `pending` | Awaiting approval | dispatcher → `admin-approve` / `reject` |
| `awaiting_driver` | Awaiting a driver | driver → `claim` |
| `in_progress` | In progress | driver → `complete` |
| `completed` | Completed | — |
| `rejected` | Rejected | — |

> An **overlay-claimed** order keeps a demo status of `awaiting_driver`. Take the real state from
> `meta.trip_state` (see below and “Effective status” in [03](03-scheduling-overlay.md)).

## Trip stage (`OrderMeta.trip_state`, our layer)

`assigned → to_client → at_client → in_trip → at_destination → waiting → completed`
plus the terminal `cancelled` (after `overlay-release`).
Labels and buttons — [03-scheduling-overlay.md](03-scheduling-overlay.md) §3.6.

## Permissions (codename)
`car_order:create`, `car_order:approve`, `car_order:reject`, `car_order:list` / `:list_own`,
`driver:accept_order`, `driver:trip_control`, `driver:list`, `garage:list`. Details — [01](01-auth.md).

## Pagination
DRF limit/offset: `{ count, next, previous, results: [...] }`. Some endpoints
(`drivers/me/cars/`, `car-types/`, `drivers/me/overlay-orders/`) return **a plain array** —
normalise: “if there’s `results` use it, else the array itself”.

## Errors

**Our features:** `{"error": {"code","message","details"}}`. Codes:

| code | HTTP | When |
|---|---|---|
| `VALIDATION` | 400 | bad body / `trip_state` |
| `TIME_CONFLICT` | 200/409 | window overlap (in `claim-check`/`overlay-claim` — the `conflict` field) |
| `ALREADY_CLAIMED` | 400 | `overlay-claim` of someone else’s active order |
| `INVALID_STATUS` | 400 | changing `trip_state` of a completed order |

**demo (DRF):** `{"detail":"..."}` (e.g. `"This car is not available."` — car busy on an active
order) or `{"field":["..."]}` / `{"non_field_errors":["..."]}`.

One error parser in the app: `error.message` → else `detail` → else first `{field:[msg]}` → else “Network error”.

## HTTP codes
`200` ok · `201` created · `400` validation/business rule · `401` token expired (→`refresh`) ·
`403` forbidden · `404` not found · `409` time conflict · `502` gateway couldn’t reach demo.

## Units & formats
- Time — ISO-8601 UTC (`2026-06-11T09:00:00Z`), display in local zone.
- Duration — integer **minutes**. Distance — meters (`distance_m`).
- `geometry` — `[lng, lat]` (GeoJSON); flip to `[lat, lng]` for maps.

## Full endpoint map

| Method | Path | Source | Section |
|---|---|---|---|
| POST | `/auth/login/` · `/auth/refresh/` | demo | [01](01-auth.md) |
| GET | `/auth/me/` | demo | [01](01-auth.md) |
| GET·POST | `/car-orders/` (list / create) | demo | [02](02-car-orders.md) |
| GET | `/car-orders/{id}/` | demo | [02](02-car-orders.md) |
| POST | `/car-orders/{id}/submit/` · `/admin-approve/` · `/reject/` | demo | [02](02-car-orders.md) |
| POST | `/car-orders/{id}/claim/` `{car_id}` · `/complete/` | demo | [02](02-car-orders.md) |
| GET | `/car-orders/drivers/me/cars/` · `/car-orders/car-types/` | demo | [02](02-car-orders.md) |
| POST | `/car-orders/estimate/` | local | [03](03-scheduling-overlay.md) |
| GET·POST | `/car-orders/{id}/meta/` | local | [03](03-scheduling-overlay.md) |
| POST | `/car-orders/{id}/claim-check/` `{driver_id}` | local | [03](03-scheduling-overlay.md) |
| POST | `/car-orders/{id}/overlay-claim/` `{driver_id,car_id,car_label}` | local | [03](03-scheduling-overlay.md) |
| POST | `/car-orders/{id}/overlay-release/` | local | [03](03-scheduling-overlay.md) |
| POST | `/car-orders/{id}/trip-state/` `{trip_state}` | local | [03](03-scheduling-overlay.md) |
| GET·POST | `/car-orders/{id}/live-location/` | local | [04](04-live-tracking.md) |
| POST | `/car-orders/drivers/me/location/` `{driver_id,lat,lng}` | local | [04](04-live-tracking.md) |
| GET | `/car-orders/drivers/me/overlay-orders/?driver_id=X` | local | [03](03-scheduling-overlay.md) |
| WS | `/ws/car-orders/{id}/location/` | local | [04](04-live-tracking.md) |
