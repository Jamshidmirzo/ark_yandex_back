# 03 — Overlay: scheduling, claiming, trip stages

These endpoints are served **locally by the gateway** (demo doesn’t have them). All work by the
**demo order id**. Error format — `{"error": {code, message, details}}`.

> Duration is always an integer of **minutes**. Time is ISO-8601 UTC.
> `geometry` is an array of `[lng, lat]` (GeoJSON); flip to `[lat, lng]` for maps.

## Why the overlay (short)
demo stores the base order but can’t hold: the A→B route, duration/windows, **trip stages**, and
**sequential same-car orders**. We keep all of that in `OrderMeta` keyed by the order id.

---

## 3.1 The `OrderMeta` object

```json
{
  "order_id": 88,
  "driver_id": 671,
  "car_id": 5,
  "car_label": "Cobalt (01A777AA)",
  "overlay_claimed": true,
  "origin_lat": 41.311, "origin_lng": 69.240,
  "address_lat": 41.351, "address_lng": 69.290,
  "has_return": false,
  "return_lat": null, "return_lng": null,
  "returning": false,
  "estimated_duration": 43,
  "service_time": 30,
  "planned_datetime": "2026-06-11T09:00:00Z",
  "latest_start": null,
  "trip_state": "to_client",
  "planned_end": "2026-06-11T09:43:00Z"
}
```
- `origin_*` — the **pickup** point coordinates (from 🟢); `address_*` — the **destination** point
  coordinates (to 🔴). The `address` prefix means destination. The pickup point has no separate text
  address — only coordinates. The destination's text address is stored by demo in `address` (section 02).
- `has_return` / `return_lat` / `return_lng` / `returning` — **round trip** in one order (§3.6.1):
  `has_return=true` → after delivery the driver waits, then drives back to `return_*` (if `null` —
  back to the pickup). `returning=true` — the driver is already **on the return leg** (set
  automatically). There's no return time — the shoot ends unpredictably, so the driver starts the
  return manually.
- `driver_id` — set on ANY accept (used for the window check).
- `overlay_claimed` — `true` **only** if the order was claimed via our layer (`overlay-claim`),
  not demo. Use it to tell “managed by us” from a normal demo claim.
- `trip_state` — trip stage (see §3.6). Terminal: `completed`, `cancelled`.

### Read / write
- `GET /car-orders/{id}/meta/` → object or `null`.
- `POST /car-orders/{id}/meta/` — upsert, send only the fields you need.

**When to write meta:** right **after creating the order** (`POST /car-orders/` returned an `id`),
save the picked coordinates and the duration — otherwise the route/tracking can’t be built:
```json
{ "origin_lat":41.311, "origin_lng":69.240, "address_lat":41.351, "address_lng":69.290,
  "estimated_duration":43, "service_time":30, "planned_datetime":"2026-06-11T09:00:00Z" }
```

---

## 3.2 Route & duration estimate — `estimate`

`POST /car-orders/estimate/` — **no auth**.
```json
{ "origin_lat":41.311, "origin_lng":69.240, "dest_lat":41.351, "dest_lng":69.290, "service_minutes":30 }
```
Response:
```json
{ "distance_m":8508, "drive_minutes":13, "service_minutes":30, "duration_minutes":43,
  "geometry":[[69.240,41.311], ...], "source":"osrm" }
```
`source`: `osrm` (real route) or `haversine` (straight-line fallback).

---

## 3.3 Window check before claiming — `claim-check`

`POST /car-orders/{id}/claim-check/` `{ "driver_id": 671 }`
```json
{ "ok": true,  "conflict": null }
{ "ok": false, "conflict": { "order_id":90, "planned_start":"...", "planned_end":"...", "address":"Order #90" } }
```
Checks the order’s **driving** window against the driver’s other active orders (+ a travel buffer).
Completed/cancelled/**parked** (`at_destination`/`waiting`) windows don’t count.

> **Important (changed):** `ok:false` is a **warning, not a block**. Claiming is still allowed
> (see §3.4) — show the conflict as a warning but let them accept. Filling gaps during a long shoot is
> the product's point, so a hard block is wrong. The only hard block is an order already taken by a
> **different** driver.
>
> **On-site time is FREE.** The window is computed over **driving** time only
> (`planned_end − service_time`), so a long on-site wait doesn’t “occupy” the driver or stop a gap order.

**Batch (for the list screen)** — so you don't call them one by one:
- `POST /car-orders/claim-check-batch/` `{ "driver_id":671, "order_ids":[88,90] }` →
  `{ "results":[ { "order_id":88, "ok":true, "conflict":null }, ... ] }` — does each fit the window.
- `POST /car-orders/meta-batch/` `{ "order_ids":[88,90] }` → `{ "results":[ OrderMeta, ... ] }` —
  the overlay for all at once (effective status per list row).

---

## 3.4 Claiming — two paths

| Case | How to accept | Result |
|---|---|---|
| Car is **free** | demo `claim`: `POST /car-orders/{id}/claim/` `{car_id}` (section 02) → then `POST /meta/ {driver_id}` | demo `in_progress` |
| Car is **busy** (your own, you’re driving it on another order) | **`overlay-claim`** (below) | claimed in our layer, `overlay_claimed=true`, `trip_state=assigned` |

demo forbids “one car — one active order”, so a 2nd order on the same car is only possible via
`overlay-claim`.

### `overlay-claim`
`POST /car-orders/{id}/overlay-claim/`
```json
{ "driver_id":671, "car_id":5, "car_label":"Cobalt (01A777AA)" }
```
- `{ "ok": true, "conflict": null, "meta": {...} }` — accepted, no overlap.
- `{ "ok": true, "conflict": {...}, "meta": {...} }` — **accepted, but there's a time overlap**
  (a warning — the order is taken anyway; show “mind the timing” and proceed).
- `400 ALREADY_CLAIMED` — the order is already taken by a **different** driver (and still active) —
  the **only** hard block.
- Re-calling as the same driver does **not** reset the current stage (won’t rewind the trip).

---

## 3.5 Drop / return to queue — `overlay-release`

`POST /car-orders/{id}/overlay-release/` (no body)
```json
{ "ok": true, "meta": { "overlay_claimed": false, "driver_id": null, "trip_state": "cancelled", ... } }
```
Clears our claim: the order stops occupying the schedule and stops being driven by the simulator.

**Call it on teardown actions:** on demo `reject`, on “cancel”, on “return to queue”, and you may
call it after completion. Idempotent (if there’s no meta it just returns `{ "ok": true }`).

---

## 3.6 Trip stages — `trip-state`

`POST /car-orders/{id}/trip-state/` `{ "trip_state": "to_client" }` → updated `meta`.
The change is **pushed in real time** over WebSocket (section 04).

Labels are **perspective-aware**: the driver sees a first-person action, the client/observer a
neutral status. Each phase has its own tag colour (neighbouring phases are distinct, the pause stands
out). UI strings are Russian; the **Driver/Client sees** columns add an English gloss in parentheses,
the **button** column shows the literal Russian button label the driver taps.

| trip_state | Driver sees | Client sees | Tag color | Driver button → next |
|---|---|---|---|---|
| `assigned` | Принят (accepted) | Назначен водитель (driver assigned) | default | “Выехал к клиенту” → `to_client` |
| `to_client` | Еду к клиенту (en route to pickup) | В пути к подаче (en route to pickup) | geekblue | “Я на месте” → `at_client` |
| `at_client` | Жду клиента (waiting for client) | На подаче (at pickup) | cyan | “Начать поездку” → `in_trip` |
| `in_trip` | Везу клиента (carrying) | В пути к месту (en route to dest.) | blue | “Прибыли на место” → `at_destination` |
| `at_destination` | На месте (arrived) | Прибыл на место (arrived) | lime | normal: “**Завершить**” (complete); round trip: “**Выехать обратно**” (drive back) → `in_trip` (see §3.6.1) |
| `waiting` | На паузе (on hold) | Пауза — ожидание (on hold) | orange | (optional manual pause) “Продолжить” → `in_trip` |
| `completed` | Завершил (completed) | Завершён (completed) | green | — |
| `cancelled` | Отменён (cancelled) | Отменён (cancelled) | red | — (set by `overlay-release`) |

> A normal order (no return) **completes** at `at_destination` — no `waiting`/“continue”. `waiting` is
> only a manual pause now.

- `400 INVALID_STATUS` — you can’t change the stage of an already **completed** order.
- Geofence (**hard gate**): the “I’m here” and “Arrived” buttons appear **only** when the driver is
  within the zone (**~100 m**) of the pickup/destination **and** with a fresh GPS fix — so arrival
  can't be marked from afar. While far, the distance to the point is shown instead. 100 m because the
  pin is often dropped inside a building the car can't enter — it's "at the entrance", not "on the pin".
- Planned time (**soft, not a block**): the driver may start before `planned_datetime`. Starting much
  earlier (more than **30 min** before the pickup) shows an “you're leaving early” notice; and once at
  the pickup **before** the planned time, it shows “wait ≈ N min” (until `planned_datetime`). Claiming
  and proceeding are still allowed — it's only a notice.
- One trip at a time (`400 ACTIVE_TRIP_EXISTS`): you can't start **driving** a 2nd order while already
  driving one (`to_client`/`in_trip`). But a **parked** driver (`waiting` / `at_destination` — e.g. on
  hold during a long shoot) **can take a gap order** — that's the whole point.

> **Long shoot → keep the driver busy.** While the driver is parked (`at_destination`/`waiting`) they
> can take a gap order: the window check treats on-site time as free (§3.3) and claiming isn't blocked
> (§3.4). “Drop off and pick back up” is **one round-trip order** (§3.6.1), not two separate ones.

---

## 3.6.1 Round trip (one order)

`has_return=true` → after delivery the driver **waits** during the shoot, then drives **back** (to
`return_*`, defaulting to the pickup). **No return time** — the shoot ends unpredictably, so the driver
starts the return manually.

Stage flow:
```
… → in_trip → at_destination ──“Drive back”──▶ in_trip (returning=true) ──“Arrived”──▶ at_destination ──“Complete”──▶ completed
```
- At `at_destination` with `has_return && !returning` the button is “**Выехать обратно**” (drive back),
  NOT “Complete”. Send `POST /trip-state/ {"trip_state":"in_trip"}` — the backend sets `returning=true`
  itself and **pushes it over WS** (section 04), so the app flips instantly.
- On the return leg (`returning=true`) `in_trip` runs `destination → return point`; the “Arrived”
  geofence is measured from the **return point**.
- At `at_destination` already with `returning=true` (back home) the button is “**Завершить**” (§3.7).
- `returning` resets to `false` on re-claim / `overlay-release`.

---

## 3.7 Completing an order

- Order accepted via **demo** (`overlay_claimed=false`): `POST /car-orders/{id}/complete/` (demo) **and**
  `POST /trip-state/ {completed}` — so the overlay doesn’t drift.
- Order accepted via **our layer** (`overlay_claimed=true`): just `POST /trip-state/ {completed}`
  (demo doesn’t know about it).
> demo allows `complete` **only by the assigned driver** — an admin/dispatcher can’t complete a demo order.

---

## 3.8 “My orders” — the driver’s active orders

`GET /car-orders/drivers/me/overlay-orders/?driver_id=671` → an array of the driver’s `OrderMeta`,
excluding `completed`/`cancelled`:
```json
[ { "order_id":88, "trip_state":"to_client", "car_label":"Cobalt (01A777AA)",
    "planned_datetime":"...", "planned_end":"...", "overlay_claimed":true }, ... ]
```
Includes **both demo- and overlay-claimed** orders (both have `driver_id`). Use it for the
“My orders” screen — show the id, stage (`trip_state`), time window, car, link to the detail.

---

## 3.9 Extend / Reassign an order

`POST /car-orders/{id}/extend/` `{ "minutes": 30 }` → `{ "ok": true, "meta": {...}, "conflict": null }`
Adds minutes to `estimated_duration` (pushes `planned_end` out). The extension is **always** applied;
`conflict` (when not `null`) only warns that the new end now overlaps the driver's next order. Allowed
for the driver or a dispatcher. `400 VALIDATION` — `minutes` not positive, or the order has no saved
window.

`POST /car-orders/{id}/reassign/` (no body) → `{ "ok": true, "meta": {...} }`
A dispatcher takes the order off its driver and returns it to the queue (same as `overlay-release`, but
it's the **dispatcher's** action): `overlay_claimed=false`, `driver_id=null`, `trip_state=cancelled`,
pushes `cancelled` over WS — the order is available to another driver again. Works for overlay-claimed
orders (a demo claim is owned by demo and can't be reassigned from here). `400 NOT_FOUND` — no meta.

---

## Effective status for the UI (important)
An overlay-claimed order keeps a demo status of `awaiting_driver`. Don’t show that — derive it:
- if `meta.overlay_claimed && trip_state ∉ {completed, cancelled}` → show it **as “in progress”**,
  and take the concrete stage from `trip_state`;
- otherwise — show the demo order status.
