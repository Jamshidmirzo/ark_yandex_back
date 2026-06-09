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
