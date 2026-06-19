# `car_orders` — QA / Code Audit (findings report)

**Scope:** the whole `car_orders` backend (native `CarOrder` lifecycle, overlay
`OrderMeta` trip-state, dispatch, scheduling, geofence, live-tracking, shifts,
permissions) in the hybrid *gateway* setup — `config/urls.py` mounts some local
views and proxies the rest to an upstream demo backend (`config/gateway.py`);
`car_orders` keeps an overlay layer (`OrderMeta`, `DriverShiftState`,
`DriverPosition`, `OrderLiveLocation`) keyed by **demo** ids.

**Method:** read-only review across 7 dimensions (concurrency · authz/IDOR · data
integrity · resilience · correctness · input validation · test-suite quality),
grounded in source with `file:line` refs. **No product code was changed** — this is
a findings report; fixes happen only after you pick them. The companion automated
suite (`car_orders/tests/test_*_extra.py`, `test_models.py`, `test_scheduling.py`,
`test_permissions.py`, `test_geometry.py`, `test_serializers.py`,
`test_views_errors.py`, `test_views_api.py`, `test_native_shift.py`,
`test_workflow_extra.py`, `test_route_extra.py`, `test_commands.py`) pins current
behaviour; **303 tests pass**.

✔ = I spot-verified the claim against source during this audit.

## Executive summary

The single most important sentence: **the headline business rule — «1 водитель =
1 активный заказ» — is not concurrency-safe, and on the default database the row
locks meant to protect it are silently disabled.** Layered on top of an
auth-off-by-default posture where unauthenticated callers can write order
positions, the highest-severity findings cluster around *concurrency* and
*authorization*, not the (well-covered) business logic.

| Severity | Count | IDs |
|----------|-------|-----|
| Critical | 3 | C1, C2, C3 |
| High | 5 | H1, H2, H3, H4, H5 |
| Medium | 6 | M1–M6 |
| Low | 3 | L1–L3 |

## Environment assumptions (these frame every finding) ✔

- **DB defaults to SQLite** ([config/settings.py:117-122](../../config/settings.py#L117-L122)); no `DATABASE_URL` in `pyproject.toml`. On SQLite, Django's `select_for_update()` is a **silent no-op** (the backend sets `has_select_for_update = False`). This holds for 100% of the test suite too.
- **`REQUIRE_OVERLAY_AUTH` defaults to `False`** ([config/settings.py:197](../../config/settings.py#L197)) → overlay endpoints run fully open unless the env var is set.
- **No `ATOMIC_REQUESTS`** — transactions exist only where `atomic()` is written explicitly.
- Channel layer defaults to in-memory (per-process, lost on restart); Redis only if `REDIS_URL` set.

## Trust boundary (who supplies what)

```
 browser / driver app ──▶ /api/v1/car-orders/*  ──┬─▶ local overlay views (OrderMeta, trip-state, claim, live-loc)
                          (DemoTokenAuthentication │   identity from token IF REQUIRE_OVERLAY_AUTH else from body/query
                           or AllowAny)            └─▶ gateway ──▶ upstream demo backend (login + base order data)
```
The body-supplied `driver_id` and the path `order_id` are the untrusted inputs;
identity is derived from the demo token **only when `REQUIRE_OVERLAY_AUTH=True`**.

---

## Findings

### C1 — Critical — `claim` locks the order row but checks "driver busy" on an unlocked query ✔
`car_orders/services/overlay.py:37-62` and the worker twin `car_orders/dispatch.py:95-110`.
Both `select_for_update()` the **target order row** (`order_id=…`), then validate the
one-active-order rule with a **separate, unlocked** query
(`OrderMeta.objects.filter(driver_id=…).exclude(terminal)`). Two concurrent claims
assigning *two different* orders to the *same* driver each lock their own row, both
read "not busy", both commit → the driver ends with two active orders. No DB
constraint backs the rule.
**Exploit:** double-tap / two devices / worker-vs-dispatcher race → driver double-booked.
**Fix sketch:** enforce at the DB layer (a partial unique constraint on `driver_id WHERE
trip_state NOT IN terminal`, handling the IntegrityError), or lock the driver's whole
active set (e.g. `select_for_update` over the driver rows) inside one transaction.
*Not reproducible on SQLite — needs Postgres + threaded clients.*

### C2 — Critical (contingent) — row locks are a no-op on the default DB ✔
`config/settings.py:117-122`. With SQLite, every `select_for_update()` in
`overlay.claim` / `dispatch.claim` is silently ignored, so even the *single-row*
protection is absent. Combined with C1, claim concurrency is effectively unguarded.
**Action:** confirm production sets `DATABASE_URL=postgres://…`; if any environment
runs SQLite, escalate to Critical. State the prod DB explicitly in the runbook.

### C3 — Critical — unauthenticated write of any order's live location / meta (IDOR write) ✔
`car_orders/views.py:219-261` (`LiveLocationView`, `authentication_classes=[]`,
`AllowAny`) and `OrderMetaView.post` `car_orders/views.py:284-290`. `LiveLocationView`
stays open **even when `REQUIRE_OVERLAY_AUTH=True`** (and `test_auth_bridge.py:130-138`
blesses this as intended). Any anonymous caller can `POST {lat,lng,geometry}` for **any**
order id — moving another order's marker, injecting arbitrary `geometry` JSON that is
broadcast to all watchers and persisted. `OrderMetaView.post` similarly upserts overlay
fields for any id, gated only by `OverlayAuthenticated` (open by default).
**Exploit:** hijack/spoof live tracking; falsify a driver position to satisfy the geofence (see H4).
**Fix sketch:** require auth + ownership (driver owns the order, or dispatcher) on the
write paths; if the simulator needs an open path, scope it to a shared secret / non-prod.

---

### H1 — High — `trip_state.advance()` has no lock and no transaction ✔
`car_orders/services/trip_state.py:165-182`: reads `meta`, runs `validate()` (which does
the "one moving trip" check-then-set at `:135-148`), then `update_or_create`. No
`select_for_update`, no `atomic()`. The forward state machine and the one-moving-trip
guard are pure read-then-write races — double-tap or two devices can both advance, or a
driver can enter two moving trips.

### H2 — High — non-atomic proxy+local dual write → demo/overlay split-brain ✔
`car_orders/views.py:114-141` (`admin_approve_overlay` / `reject_overlay`). The gateway
call to demo and the local overlay mutation are sequential and non-atomic with no
compensation. If `services.overlay.release()` (`:140`) or the `OrderMeta.update_or_create`
(`:125`) raises after demo already returned 2xx, demo and overlay diverge permanently —
e.g. demo rejected but overlay still `dispatchable=True` → keeps auto-assigning, exactly
the bug the hook exists to prevent. **Untested:** `test_critical_fixes.py` mocks the
gateway to a static 200, so the partial-failure branch never runs.

### H3 — High — `DriverShift` create race raises an uncaught `IntegrityError` (500) ✔
`car_orders/views.py:1172-1189`. The partial unique constraints
`one_active_shift_per_driver` / `one_active_shift_per_car`
(`car_orders/models.py:326-337`) are pre-checked with `.filter(...).exists()` then
`DriverShift.objects.create(...)` inside `atomic()`, with **no `IntegrityError` handler**.
Concurrent shift starts (or a car double-book that slips past the pre-check) raise an
uncaught `IntegrityError` → 500 instead of a clean `CAR_BUSY`. The overlay twin
`DriverShiftState` (`models.py:383-398`) has **no** such constraint at all — its only
guard is `update_or_create` on `driver_id`.

### H4 — High — geofence is defeatable via the AllowAny position write ✔
`car_orders/services/trip_state.py:65-89` reads the "fresh GPS fix" from `DriverPosition`,
which is written by the **AllowAny** `LiveLocationView` / `_apply_driver_location`. Via C3,
an unauthenticated caller can POST a position on the target coords and pass the geofence.
*(Secondary, Medium:* the deviation check `geometry.min_dist_km_to_polyline`
`car_orders/geometry.py:77-82` measures distance to polyline **vertices only**, not
segments — on a sparse/downsampled line a driver mid-segment can read as off-route,
causing spurious re-routes.)*

### H5 — High — `REQUIRE_OVERLAY_AUTH=False` is the production default ✔
`config/settings.py:197`, `car_orders/permissions.py:15-29`. With the default, every
overlay endpoint trusts body/query `driver_id` (`acting_driver_id` fallback,
`permissions.py:56-62`) and `MyOverlayOrdersView` (`views.py:792-832`) treats everyone as a
dispatcher → full active-board enumeration + cross-driver reads/writes. Nothing warns or
refuses if it ships off. **Fix sketch:** default to `True`, or hard-fail startup when
`DEBUG=False` and the flag is off.

---

### M1 — Medium — `auto_enabled()` swallows every exception as "off" ✔
`car_orders/dispatch.py:34-37` — `except Exception: return False`. A DB outage / missing
migration silently disables auto-dispatch with no signal; a real failure masquerades as a
benign config state. Narrow the catch (e.g. only the does-not-exist case) and log.

### M2 — Medium→High — `OrderMeta` mass-assignment on the POST upsert ✔
`car_orders/serializers.py:315-357` + `OrderMetaView.post` (`views.py:284-290`). The POST
passes `serializer.validated_data` straight into `update_or_create`; writable fields include
`driver_id`, `dispatchable`, `car_id`, `overlay_claimed`, `author_id`, `is_urgent`,
`parent_order_id`, coords and window. A caller can self-assign a driver, flip
`dispatchable`, or overwrite a window — bypassing `overlay.claim`'s busy guard. Open by
default (escalates with H5). Restrict the writable set or route writes through the service.

### M3 — Medium — extend minutes: lower-bound only ✔
`ExtendView` coerces a bad `minutes` to `0` (`views.py:470-474`), then `overlay.extend`
raises `VALIDATION` on `<=0` (`overlay.py:131`) — fine — but there is **no upper bound**, so
a huge value pushes `planned_end` arbitrarily far. Add a sane cap.

### M4 — Medium — orphan overlay rows (bare integer keys, no FK) ✔
`OrderLiveLocation.order_id` / `DriverPosition.driver_id` / `OrderMeta.order_id` are
`PositiveIntegerField` with no FK (`models.py:349,370,390,…`). Nothing GCs rows when the
demo order/driver disappears; `OrderMetaView.delete` is the only cleanup and is manual +
dispatcher-gated. Stale rows can re-enter the dispatch queue or leak a stale marker. Add a
periodic reaper or reconcile against upstream.

### M5 — Medium — OSRM first-assignment persists a straight line ✔
`car_orders/dispatch.py:230-238`. The "don't overwrite a good route" guard only fires when
an existing geometry is present (`:232`); on the *first* assignment during an OSRM outage it
stores the 2-point haversine line and won't self-heal until the next state change /
deviation. Cosmetic-but-persistent (cuts across roads). *(Pinned by
`test_route_extra.py::test_push_route_draws_fallback_on_first_assignment`.)*

### M6 — Medium — `run_once` `first_seen` resets on worker restart ✔
`car_orders/dispatch.py:273-294` + `auto_dispatch.py:35`. `first_seen` is an in-process dict;
a worker restart resets every ASAP order's "waited long enough" clock, indefinitely delaying
stale-order dispatch across restarts. Persist `first_seen` (or derive it from a timestamp on
the order).

---

### L1 — Low — `downsample` endpoint handling ✔
`car_orders/geometry.py:40-49`: verify no off-by-one drops the true last vertex on
exact-multiple lengths (the explicit last-point append guards most cases). *(Edges pinned by
`test_geometry.py`.)*

### L2 — Low — `X-Forwarded-For` is spoofable (logging only) ✔
`car_orders/views.py:96-101` reads `HTTP_X_FORWARDED_FOR` for the tracking log, not authz.
Document it as untrusted so it's never repurposed for trust.

### L3 — Low — gateway forwards all non-skip headers verbatim ✔
`config/gateway.py:91-95` forwards inbound headers (minus host/length/connection/
accept-encoding) to upstream, including `Authorization` and arbitrary client headers; retries
replay only on `ConnectionError` (safe — before any response). Cookie isolation is handled
elsewhere. Low because upstream is the trust authority; note it so no header is later trusted.

---

## Cross-cutting themes
1. **Row-lock scope vs cross-row invariants** (C1, H1, H3) — locks cover one row but rules
   span rows; the DB, not application code, should own "1 driver = 1 active order".
2. **Non-atomic proxy + local dual writes** (H2) — every gateway hook that also mutates the
   overlay can split-brain.
3. **AllowAny + body-trusted identity** (C3, H4, H5, M2) — the open-by-default posture turns
   several "works in dev" behaviours into write-IDOR in prod.
4. **Exception-swallowing that hides failures as benign states** (M1, and the broad
   `except Exception` in `push_order_route`).

## Test-suite assessment
- **Concurrency is structurally untestable here:** SQLite + `django_db` means C1/C2/H1/H3
  races can't be reproduced; the green `test_overlay_one_active.py` etc. assert only the
  *sequential* guard — false confidence on the headline invariant. Add a Postgres CI lane
  with threaded clients for these.
- **Gateway-proxied paths are never exercised:** `test_critical_fixes.py` mocks the gateway,
  so H2's partial-failure branch and real 502/timeout handling are untested.
- **AllowAny IDOR is encoded as intended** (`test_auth_bridge.py:130-138`) rather than flagged.
- The new suite added in this pass closes the *logic* gaps (lifecycle error branches,
  geofence edges, scheduling maths, permission fail-closed, model constraints, serializer
  validation, native shift Р1, command wrappers) — 156 new tests, all green — but cannot
  close the *concurrency* and *auth-posture* gaps above.

## Prioritised remediation roadmap
- **P0:** C1 (+ C2 confirm prod DB), C3, H1, H2 — the double-book + write-IDOR + split-brain core.
- **P1:** H3 (IntegrityError→CAR_BUSY), H4, H5 (auth default), M2 (mass-assignment).
- **P2:** M1, M3–M6, L1–L3.
Each P0/P1 fix should ship with a Postgres-lane regression test (the SQLite suite can't prove it).

## Appendix — endpoint × auth × identity-source (overlay)
| Endpoint | Auth class | Identity source | Notes |
|----------|-----------|-----------------|-------|
| `live-location` GET/POST | AllowAny | n/a | **C3** open write |
| `meta` GET/POST | OverlayAuthenticated | body | **M2** mass-assign; open by default |
| `meta` DELETE | OverlayDispatcher | token | gated ✔ |
| `overlay-claim` | OverlayAuthenticated | `assignee_driver_id` (token or body) | **C1** lock gap |
| `trip-state` | OverlayAuthenticated | `acting_driver_id` | **H1** no lock |
| `reassign` | OverlayDispatcher | — | gated ✔ |
| `auto-dispatch` POST | OverlayDispatcher | — | gated ✔ |
| `drivers/me/overlay-orders` | OverlayAuthenticated | token (enforced) / body (open) | **H5** board enum when open |
