# Orders by role: a driver sees their own, an admin sees all

Role-scoped access to the active overlay-orders list. One endpoint serves both
cases:

- **driver** → only their own active orders;
- **admin / dispatcher** → the whole active board (optionally filtered by driver).

> 🇷🇺 Russian master: [../car-orders-orders-rbac.md](../car-orders-orders-rbac.md).
> Spans the backend (`ark_yandex`) and the web client (`ark_yandex_front`). The
> mobile "My orders" screen (driver-only) is in
> [mobile/en/03-scheduling-overlay.md §3.8](../mobile/en/03-scheduling-overlay.md).

---

## 1. Endpoint

`GET /api/v1/car-orders/drivers/me/overlay-orders/`

Served locally by the gateway (not proxied to `demo`). Returns an array of
`OrderMeta`, **excluding** terminal states (`completed` / `cancelled`), sorted by
`planned_datetime`, then `order_id`.

### Parameters

| Param | Used by | Description |
|---|---|---|
| `driver_id` | driver (dev) / admin | In non-enforced-auth mode a driver scopes the result to themselves by passing their id. An admin can narrow the board to a single driver. Under enforced auth it is **ignored for a driver** (identity comes from the token — IDOR protection). |

### Selection rule

```
qs = OrderMeta, excluding completed/cancelled
if the caller is a dispatcher (OverlayDispatcher):
    if driver_id given → filter by it
    else               → the whole board
else (driver):
    driver_id = acting_driver_id(request, ?driver_id)   # token under enforced auth
    if empty → []
    else → filter driver_id == own
```

Implemented in `MyOverlayOrdersView.get` —
[car_orders/views.py](../../car_orders/views.py).

### Who is a "dispatcher"

`OverlayDispatcher` (see [car_orders/permissions.py](../../car_orders/permissions.py)):
superuser **or** a permission that satisfies `car_order:approve` via the ARK
hierarchy (`administrator` ⊇ everything, `X_all` ⊇ `X`). So the whole board goes to
holders of `car_order:approve`, `car_order:approve_all`, `administrator`, and
superusers.

> ⚠️ **Hierarchy contract.** `OverlayDispatcher` expands the hierarchy via
> `expand_permission_codename("car_order:approve")` over the in-memory `DemoUser`
> permission set — **exactly** like `useMyPermissions.hasPermission` on the
> frontend. This is deliberate: if the backend checks the codename literally while
> the frontend expands the hierarchy, an `administrator` without a literal
> `car_order:approve` would get the admin UI but driver-scoped data (their own,
> usually empty, list). Any new server gate the web UI mirrors must expand the same
> way.

### Behavior by mode and role

| Mode | Who | Frontend request | Result |
|---|---|---|---|
| enforced | driver | `?driver_id=<own>` (or none) | own orders (identity from token; a foreign `driver_id` is ignored) |
| enforced | dispatcher | no `driver_id` | whole board |
| enforced | dispatcher | `?driver_id=99` | driver 99's orders |
| enforced | superuser | no `driver_id` | whole board |
| dev (auth off) | driver | `?driver_id=<own>` | own (in dev everyone reads as dispatcher, but `driver_id` narrows to them) |
| dev (auth off) | admin tool | no `driver_id` | whole board |

### Example `200` response

```json
[
  {
    "order_id": 831,
    "driver_id": 99,
    "trip_state": "in_trip",
    "planned_datetime": "2026-06-17T14:00:00Z",
    "planned_end": "2026-06-17T15:00:00Z",
    "car_label": "Cobalt (01A777AA)",
    "at_risk": false,
    "is_late": false
  }
]
```

Full `OrderMeta` schema — in
[mobile/en/03-scheduling-overlay.md](../mobile/en/03-scheduling-overlay.md).

---

## 2. Web client (`ark_yandex_front`)

The **"My orders"** page is now role-aware and serves both cases.

| File | What it does |
|---|---|
| `src/api/endpoints/carOrders.ts` | `myOverlayOrders(driverId?)` — `driverId` is optional; **without** it the backend returns the whole board (admin) |
| `src/pages/car-orders/DriverSchedulePage.tsx` | `isDispatcher = hasPermission("car_order:approve")`. Dispatcher → whole board, title "Все заказы", neutral stage labels, "Водитель #id" tag. Driver → own, "Мои заказы", first-person labels |
| `src/router.tsx` | `/orders/schedule` guard broadened to `["driver:accept_order", "car_order:approve"]`; `car_order:approve` added to `CAR_ANY` (so a dispatcher can open order detail `/orders/car/:id`) |
| `src/layouts/DashboardLayout.tsx` | menu item shown to driver and dispatcher; label "Все заказы" for `car_order:approve`, else "Мои заказы" |

The frontend role check (`isDispatcher = hasPermission("car_order:approve")`)
matches the backend (`OverlayDispatcher`) thanks to the shared permission hierarchy.

> A dispatcher gets a flat "All orders" list **in addition to** the live
> "Dispatcher" map (`FleetLivePage`) — they are different views (list vs map).

---

## 3. Tests

`car_orders/tests/test_auth_bridge.py` (enforced auth):

- `test_enforced_my_orders_ignores_query_driver_id` — a driver cannot enumerate others via `?driver_id=`;
- `test_admin_overlay_orders_sees_the_whole_board` — a dispatcher (`car_order:approve`) gets all active orders;
- `test_admin_overlay_orders_can_filter_to_one_driver` — `?driver_id=` filter;
- `test_admin_overlay_orders_honours_permission_hierarchy` — `administrator` and `car_order:approve_all` also get the board (hierarchy).

Run:

```bash
.venv/bin/pytest car_orders/tests/test_auth_bridge.py car_orders/tests/test_overlay.py -q
```

---

## 4. Notes and edge cases

- **Terminal orders** (`completed` / `cancelled`) never appear on the board — neither for a driver nor an admin. History would need a separate flag (`?include_terminal=1`) — not implemented yet.
- **Dual role** (both `driver:accept_order` and `car_order:approve`): the user counts as a dispatcher and sees the **whole** board (label "Все заказы").
- **`OverlayDispatcher` is a shared gate**: the hierarchy expansion also applies to `reassign`, the auto-dispatch toggle, and meta deletion. This is an intentional widening (literal `car_order:approve` and superuser still pass) — it only adds rights for `administrator` / `*_all` holders.
