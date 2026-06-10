# 02 — Car Orders (base workflow)

These endpoints are proxied to the `demo` backend. The base order workflow:

```
draft ──submit──▶ pending ──admin-approve──▶ awaiting_driver ──claim(car_id)──▶ in_progress ──complete──▶ completed
   └────────────────────── reject(reason) ──────────────────────▶ rejected
```

> Trip stages (en route / arrived / waiting) and sequential same-car orders are in
> [03-scheduling-overlay.md](03-scheduling-overlay.md). Live position is in [04-live-tracking.md](04-live-tracking.md).

## List orders

`GET /car-orders/`

Query params:
| Param | Example | Meaning |
|---|---|---|
| `status` | `awaiting_driver` | filter by status |
| `search` | `Temur` | search address/project/note |
| `ordering` | `-created_at` | sort |
| `limit` / `offset` | `50` / `0` | pagination |

Response `200` (paginated):
```json
{
  "count": 12,
  "next": "http://.../car-orders/?limit=50&offset=50",
  "previous": null,
  "results": [ { /* CarOrder, see below */ } ]
}
```

## Order detail

`GET /car-orders/{id}/`

Response `200` — a **CarOrder**:
```json
{
  "id": 88,
  "project_name": "Turandot Residences",
  "planned_datetime": "2026-06-11T09:00:00Z",
  "address": "Amir Temur ave, Tashkent",
  "note": "Pick up equipment",
  "comment": "",
  "car_type": { "id": 4, "name": "Sedan" },
  "driver": { "id": 671, "name": "Ivan Driver" },
  "car": { "id": 5, "model": "Cobalt", "plate_number": "01A777AA" },
  "status": "awaiting_driver",
  "started_at": null,
  "finished_at": null,
  "created_by": { "id": 10, "name": "Dispatcher" },
  "created_at": "2026-06-10T08:00:00Z",
  "updated_at": "2026-06-10T08:00:00Z",
  "available_vehicles": [
    { "id": 5, "model": "Cobalt", "plate_number": "01A777AA" }
  ]
}
```

- `available_vehicles` is returned **only** in the `awaiting_driver` state — the free cars a driver
  may pick when claiming.
- Full status list — [05-reference.md](05-reference.md).

## Create order

`POST /car-orders/` (permission `car_order:create`)

Required: `project_name`, `planned_datetime` (ISO-8601 UTC), `address`, `car_type_id`.
```json
{
  "project_name": "Turandot Residences",
  "planned_datetime": "2026-06-11T09:00:00Z",
  "address": "Amir Temur ave, Tashkent",
  "note": "Pick up equipment",
  "comment": "",
  "car_type_id": 4
}
```
Response `201` — the created order in `draft`.

> demo does **not** store the A→B coordinates or duration — save those separately to our overlay
> via `POST /car-orders/{id}/meta/` (see [03](03-scheduling-overlay.md)), otherwise the route and
> tracking won’t work.

## Car types (for the create dropdown)

`GET /car-orders/car-types/` → `[{ "id": 4, "name": "Sedan", ... }]` (array or paginated — normalise both).

## Workflow actions

| Method | Path | Permission | Effect |
|---|---|---|---|
| POST | `/car-orders/{id}/submit/` | author | `draft → pending` |
| POST | `/car-orders/{id}/admin-approve/` | `car_order:approve` | `pending → awaiting_driver` |
| POST | `/car-orders/{id}/reject/` | author / `car_order:reject` | `→ rejected`, body `{ "reason": "..." }` |
| POST | `/car-orders/{id}/claim/` | `driver:accept_order` | `awaiting_driver → in_progress`, body `{ "car_id": 5 }` |
| POST | `/car-orders/{id}/complete/` | `driver:trip_control` | `in_progress → completed` |

Only `reject` and `claim` take a body; the rest are empty `POST`s. All return the updated CarOrder.

### Claim — important
`POST /car-orders/{id}/claim/` `{ "car_id": 5 }`

demo rules (may return an error):
- **One car — one active order.** If the car already has an `in_progress` order → `400` `"This car is not available."`
- **One driver — one active order.** If the driver already has an active one → error.

To take a **2nd order with the same car sequentially** (non-overlapping windows), don’t use demo
`claim` — use our `overlay-claim` (see [03-scheduling-overlay.md](03-scheduling-overlay.md)).

## Driver’s cars

`GET /car-orders/drivers/me/cars/` → cars assigned to the current driver:
```json
[ { "id": 5, "model": "Cobalt", "plate_number": "01A777AA", "is_available": true } ]
```
Use it for the claim car-picker. `is_available: false` = the car is currently busy on an active
order (but it can still be taken for a sequential order via `overlay-claim`).
