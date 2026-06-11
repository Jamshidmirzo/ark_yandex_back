# Car-orders block — integration & API guide

This Django project (`ark_yandex`) is a **self-contained block** that implements
the «Заявки на машину» feature (car orders + garage + drivers) plus two approved
product decisions:

- **Р1 — «машина на смене»**: a driver picks ONE car on going on shift; the
  awaiting-driver feed is filtered to that car's type; `claim` uses the shift car.
- **Р3 — live tracking**: the driver app pushes GPS; the order author watches the
  driver on a map while the trip is `in_progress`.

It deliberately mirrors **ark-backend** conventions so it can later be folded into
the main CRM. Source of truth for behaviour: `ark-system-requirements/modules/orders/`.

---

## 1. Run it standalone

```bash
cd ark_yandex
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python manage.py migrate            # also seeds permissions + access groups
python manage.py createsuperuser
python manage.py runserver          # http://127.0.0.1:8000
pytest                              # 5 workflow/permission tests
```

Base URL: `http://127.0.0.1:8000/api/v1/` (unprefixed — no `ru/uz` language
prefix in this block; ark-backend wraps these in `i18n_patterns`, see §5).

---

## 2. API surface

### Auth (`/api/v1/auth/`)
| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `login/` | `{username, password}` | `{access, refresh, user}` |
| POST | `refresh/` | `{refresh}` | `{access[, refresh]}` |
| GET | `me/` | — | `{id, name, username, is_superuser, permissions[]}` |
| GET | `me/permissions/` | — | `{permissions: [...]}` |

Auth header: `Authorization: Bearer <access>`.

### Car orders (`/api/v1/car-orders/`)
| Method | Path | Who | Effect |
|---|---|---|---|
| GET | `/` | any (scoped) | list (search/status filter/ordering, paginated) |
| POST | `/` | `car_order:create` | create draft |
| GET | `/{id}/` | visible-to-user | detail (+ `driver_location` while in progress) |
| PATCH | `/{id}/` | author, draft only | edit draft |
| DELETE | `/{id}/` | author/admin, draft only | delete draft |
| POST | `/{id}/submit/` | author | draft → pending |
| POST | `/{id}/admin-approve/` | `car_order:approve` | pending → awaiting_driver |
| POST | `/{id}/reject/` | author or `car_order:reject` | → rejected (before in_progress) |
| POST | `/{id}/claim/` | `driver:accept_order` | awaiting_driver → in_progress (**uses shift car, Р1**) |
| POST | `/{id}/complete/` | assigned driver (`driver:trip_control`) | in_progress → completed |
| GET | `/{id}/activity/` | visible-to-user | audit trail |
| GET | `/me/active-order/` | driver | the driver's current trip or `null` |

### Drivers + shift + location (`/api/v1/car-orders/drivers/`)
| Method | Path | Who | Effect |
|---|---|---|---|
| GET | `/` | `driver:list` | registry of users in the `Driver` group |
| GET | `/me/cars/` | driver | the driver's assigned cars (for the shift modal) |
| GET·PATCH·DELETE | `/me/shift/` | `driver:accept_order` | read / start-switch (`{car_id}`) / end shift (**Р1**) |
| POST | `/me/location/` | `driver:accept_order` | GPS heartbeat `{lat, lng}` (**Р3**) |
| POST | `/make-driver/` | `driver:assign_to_user` | add user to `Driver` group `{user_id}` |
| POST | `/remove-driver/` | `driver:assign_to_user` | remove from `Driver` group `{user_id}` |

### Garage (`/api/v1/car-orders/cars/`, `/car-types/`) and reports (`/vehicle-reports/`)
Standard CRUD gated by `garage:*` / `vehicle_report:*` (see §3).

Errors use a consistent envelope: `{"error": {"code, message, details}}`.

---

## 3. Permissions & access groups

Codenames match ark-backend exactly (seeded in
`auth_core/migrations/0002_seed_permissions.py`):

`car_order:create | list_own | list | approve | reject | dispatch` ·
`driver:accept_order | trip_control | list | assign_to_user` ·
`garage:list | retrieve | create | update | delete` ·
`vehicle_report:create | list_own | list | retrieve` · `administrator`.

Seeded groups: **Car Requester**, **Car Admin**, **Driver**, **Garage Manager**,
**Administrator**. Hierarchy: `administrator` ⊇ everything, `X` ⊇ `X_own`,
`X_all` ⊇ `X` (`auth_core.permissions.expand_permission_codename`).

> `car_order:dispatch` is **new** here (the live dispatch/tracking screen). If
> ark-backend keeps it, add it there too; otherwise gate the map with `car_order:list`.

---

## 4. Folding this into ark-backend (it already has `apps.car_orders`)

ark-backend already ships a `car_orders` app, so integration is a **delta**, not a copy:

1. **Auth/permissions** — drop this block's `auth_core`; use ark-backend's
   `apps.auth_core` (identical model shape: `Permission` / `AccessGroup` /
   `UserAccessGroup`, `user_has_permission`, `HasPermission`). All FKs here already
   point at `settings.AUTH_USER_MODEL`, so they bind to `users.User` unchanged.
   Add the `car_order:dispatch` codename + group edits via a data migration there.
2. **Port Р1 (`DriverShift`)** into ark-backend's `apps.car_orders`: add the model
   + migration, change `claim` to use the shift car (drop the per-order `car_id`
   choice / `available_vehicles`), and filter the awaiting feed by the shift car
   type (see `get_queryset` and `claim` here).
3. **Port Р3 (location)**: add `lat/lng/last_seen` on the shift, the
   `me/location` heartbeat, and `driver_location` on the order serializer.
   Replace `services.publish_driver_location` with a real WS publish to the bus
   group `bus_user_<author_id>` (event `driver_status`, payload incl. `order_id`)
   — see ark-backend `apps/notifications/utils.py::_send_via_websocket` and the
   draft contract on the `a2a` bus (`DRAFT: driver auth + live status`).
4. **Notifications**: replace `services.notify` no-op with
   `apps.notifications.send_notification(user, title, body, route_type, extra)`.
5. **Pictures/reports**: here `CarType.picture_url` / `Car.picture_url` are plain
   URLs and `VehicleReport` has no photos; ark-backend uses `storage.StoredFile`
   FKs — swap to that on merge.
6. **i18n**: wrap the routes in `i18n_patterns` (ark-backend serves
   `/{lang}/api/v1/...`); this block serves them unprefixed.

Differences vs ark-backend's current model are otherwise cosmetic — statuses
already use `rejected`, fields line up.

---

## 5. Frontend connection (for `ark_yandex_front`)

- Set the API base to `http://127.0.0.1:8000/api/v1/`.
- Login → store `access`/`refresh`; send `Authorization: Bearer <access>`; refresh
  on 401 via `auth/refresh/`.
- Drive UI visibility off `GET auth/me/` `permissions[]` (e.g. show «Согласовать»
  only with `car_order:approve`).
- Tracking (Р3): MVP can **poll** `GET car-orders/{id}/` and read `driver_location`;
  upgrade to the `driver_status` WS event once §4.3 lands on the backend.
- CORS is **not** configured in this block yet — add `django-cors-headers`
  (mirror ark-backend's `corsheaders` setup) before wiring a browser client.

---

## 6. Scheduling & live-tracking extension

> **Note.** §6 describes the original **standalone** scheduler (own `scheduled`/
> `start` flow, hard `TIME_CONFLICT`). In the running demo this is superseded by
> the **hybrid overlay** in §7: orders live in the demo backend and the schedule/
> trip-state are tracked locally in `OrderMeta`. Read §7 for the current behaviour.

This block now models a **planned** dispatch flow (a driver's day is a set of
non-overlapping time windows) on top of the original workflow, plus an
auto-estimate and a movement simulator. See `car_orders/scheduling.py`.

### Status flow (updated)

```
draft → pending → awaiting_driver → scheduled → in_progress → completed
                                   ↘ (claim reserves the window)
any pre-terminal → cancelled        scheduled/in_progress → awaiting_driver (release/reassign)
```

`scheduled` = the driver claimed the order and its window is reserved, but the
trip hasn't started. A driver may hold **several** `scheduled` orders in
non-overlapping windows; only **one** may be `in_progress` at a time.

### New / changed order fields

`estimated_duration`, `service_time`, `latest_start`, `planned_end` (derived),
`origin_lat/lng`, `address_lat/lng`, and computed `is_delayed` / `needs_reassign`.

### New endpoints (`/api/v1/car-orders/`)

| Method | Path | Effect |
|---|---|---|
| POST | `/estimate/` | `{origin_lat,origin_lng,dest_lat,dest_lng,service_minutes?}` → `{distance_m, drive_minutes, service_minutes, duration_minutes, geometry:[[lng,lat]…], source}` |
| POST | `/{id}/claim/` | reserve window → `scheduled`; **409 `TIME_CONFLICT`** (details: conflicting `order_id`, window) when it overlaps another of the driver's windows (+ `CAR_ORDER_TRAVEL_BUFFER`) |
| POST | `/{id}/start/` | `scheduled → in_progress` (blocks if another trip is active) |
| POST | `/{id}/cancel/` | author / `car_order:reject` → `cancelled`, frees the window |
| POST | `/{id}/release/` | assigned driver hands back → `awaiting_driver` |
| POST | `/{id}/reassign/` | `car_order:approve` takes it off the driver → `awaiting_driver` |
| POST | `/{id}/extend/` | `{minutes}` → grows duration, returns order + `schedule_conflict` (or `null`) |
| GET | `/drivers/me/schedule/` | the driver's `scheduled` + `in_progress` timeline |

### Settings

`CAR_ORDER_TRAVEL_BUFFER` (default 30 min), `CAR_ORDER_DEFAULT_SERVICE`
(default 30 min), `CAR_ORDER_OSRM_URL` (default OSRM public demo; swap for a
self-hosted OSRM or the Yandex Router API in prod). The auto-estimate falls back
to a straight-line haversine estimate when the router is unreachable.

### Local serving

`config/urls.py` serves `auth/` and `car-orders/` **locally** (before the
gateway catch-all) so the block runs standalone again; every other `/api/v1/*`
path still falls through to the upstream gateway.

### Simulator (test live tracking without a phone)

```bash
python manage.py simulate_driver --order <id> --steps 60 --interval 2
```

Interpolates the order's A→B route and writes positions to the driver's active
shift, so the dispatcher map (polling `driver_location`) shows the car moving.

---

## 7. Hybrid overlay layer (current live architecture)

In the running demo, `ark_yandex` is a **gateway + overlay**, not a standalone
backend: login, drivers, garage and the base car-orders CRUD are **proxied** to
the demo backend (`UPSTREAM_API_BASE`, e.g. `demo.ark.glob.uz/ru/api/v1`), while
the new features are served **locally** from an `OrderMeta` overlay keyed by the
demo order id. Local routes are mounted **before** the gateway catch-all
(`config/urls.py`); everything else falls through upstream. demo stays the source
of truth for auth/data — **never break that proxy**.

Mobile clients may call the language-prefixed scheme `/<lang>/api/v1/...` and
`/healthcheck/` — `MobileLanguagePrefixMiddleware` strips the prefix. CORS in
`DEBUG` allows any `localhost`/`127.0.0.1` port + the LAN.

### 7.1 Trip-state machine (overlay)

`OrderMeta.trip_state`: `assigned → to_client → at_client → in_trip →
at_destination → completed` (+ `waiting` optional pause, `cancelled`). The UI
renders **perspective-aware** wording (driver vs client/requester).

| Method | Path (`/api/v1/car-orders/…`) | Effect |
|---|---|---|
| POST | `/{id}/trip-state/` | `{trip_state}` → advance the stage; pushed over WS + toasts **driver & requester** |
| GET·POST | `/{id}/meta/` | read / upsert the overlay (coords, window, return, …) |
| POST | `/{id}/overlay-claim/` | claim in OUR layer — sequential **same car** (which demo forbids); returns `{ok, conflict, meta}` |
| POST | `/{id}/overlay-release/` | tear the overlay down (reject / cancel / release / reassign) |
| POST | `/{id}/claim-check/` · `/claim-check-batch/` | `{driver_id}` → `{ok, conflict}` schedule pre-check (advisory) |
| POST | `/{id}/reassign/` | dispatcher takes the order off its driver |
| POST | `/{id}/extend/` | `{minutes}` → grow the planned window, returns `{ok, meta, conflict}` |
| POST | `/estimate/` | route A→B → `{distance_m, drive_minutes, duration_minutes, geometry:[[lng,lat]…]}` |
| GET | `/fleet/live/` | dispatcher snapshot: every active order + live pos + route |
| GET·POST | `/{id}/live-location/` | read / write the order's live position |
| POST | `/drivers/me/location/` | GPS heartbeat (drives the live map) |

**WebSockets:** `/ws/car-orders/{id}/location/` (per-order pos + `trip_state` +
`returning`), `/ws/car-orders/fleet/` (dispatcher), `/ws/notifications/{user_id}/`
(per-user toasts to **driver AND requester**). InMemory channel layer in dev;
Redis via `REDIS_URL` in prod (daphne/Channels).

### 7.2 Arrival geofence (100 m)

The «Я на месте» / «Прибыли на место» buttons unlock only within **100 m** of the
relevant point **and** with a fresh GPS fix — arrival can't be marked from afar.
The pin may sit inside a building the car can't enter, so 100 m means "at the
entrance", not "on the pin". Frontend gate; the backend does not reject by distance.

### 7.3 Round trip («туда-обратно») as ONE order

A round trip is a single order, not a separate sub-order:
`OrderMeta.has_return` + `return_lat/lng` (drop-back point; defaults to the pickup)
+ `returning` (the active return leg) — migration `0014`. **No return time** (the
shoot end is unknown, so the driver starts the return manually). Flow:
`… → at_destination` shows **«Выехать обратно»** (not «Завершить») → that begins
the return leg `destination → return point` → on arrival → **«Завершить»**.
`returning` is inferred in `TripStateView` (broadcast over WS so the app flips
instantly) and reset on (re)claim / release.

### 7.4 Gap-filling — a schedule overlap is a WARNING, not a block

Keeping a driver busy during a long shoot is the product's whole point, so:

- **On-site time is FREE.** `scheduling.meta_conflict` uses
  `driving_end = planned_end − service_time`; an order only "occupies" the driver
  while DRIVING, so a long on-site wait doesn't reserve the window against others.
- **Overlap never hard-blocks a claim.** `overlay-claim` proceeds and returns the
  overlap as `conflict` for the UI to show as a warning. The **only** hard block
  left is an order already taken by a **different** driver. A late return is
  surfaced via `at_risk` («Под угрозой»), not by forbidding the gap order.
- **One drive at a time still holds:** `TripStateView` blocks starting a 2nd
  MOVING stage (`to_client`/`in_trip`) while another order is actively moving.

### 7.5 Dispatcher («Диспетчерская», `/orders/fleet`)

A live board of every active order, gated by `car_order:list | approve`: a Yandex
map with a coloured marker per car + its A→B route (🟢 pickup, 🔴 destination) and
a **traffic** layer, plus rich cards — откуда→куда, project, подача / ориент.
окончание, время на месте, «Туда-обратно» / «Везёт обратно», risk and GPS.
Urgent / at-risk / late orders beep + toast.

**Auto-dispatch (nearest free driver).** Each AWAITING order shows a «Рекомендуем»
top-3 of the nearest eligible drivers (on shift + right car type + free, or parked
on a shoot) ranked by distance, with one-click «Назначить» (overlay-claim on their
behalf). A «Авто-распределение» toggle assigns automatically — urgent right away,
scheduled within ~45 min of pickup, plain ASAP after ~3 min unclaimed; always
reversible. The ranking runs in the dispatcher (it has the demo roster + token);
positions come from `/drivers/positions/` — the per-driver GPS store fed by every
`drivers/me/location/` heartbeat (incl. free drivers), with a fallback to a parked
driver's order position. No backend daemon / demo service token is required.

### 7.6 Misc

- «Принять» is hidden for a user with **no assigned car** (a dispatcher/admin
  can't claim anyway).
- `is_urgent` orders sort first, flag red and beep in the dispatcher.
- Empty pickup time = «сейчас / ASAP» (the form defaults it on create).

### 7.7 Simulator (phase-aware) + reminders

```bash
python manage.py runserver 0.0.0.0:8000 --noreload   # web + WS (daphne)
python manage.py auto_simulate --interval 1.5         # drive every active order
python manage.py remind_departures --loop             # «пора выезжать» nudges
```

`auto_simulate` drives the live position **per phase**: `to_client` =
driver→pickup, `in_trip` = pickup→destination, and the **return leg**
destination→return point while `returning`. Do **not** pass `--loop` (it re-drives
a finished leg from the start). The older `simulate_driver` (§6) targets the
standalone scheduler.

To test the **nearest-driver suggestion / auto-dispatch** (§7.5) without a phone,
fake the idle-driver GPS — the per-driver heartbeat that a free driver would send:

```bash
python manage.py seed_driver_positions --drivers 671,13 --loop   # scatter + drift
```

It writes `DriverPosition` rows (and keeps `last_seen` fresh), so free drivers get
a position the dispatcher can rank by distance. A driver still only appears as a
candidate if demo lists them **on shift with a car of the order's type** (see the
«Водители» page) — this command only supplies the position.
