# `car_orders` — Manual QA Test Plan

Senior-QA walkthrough for the whole `car_orders` feature (native `CarOrder`
lifecycle + overlay `OrderMeta` trip-state + dispatch + scheduling + geofence +
live-tracking + shifts + permissions).

Use it for release sign-off, exploratory passes, and the cases the automated suite
**cannot** reach on the default DB (concurrency — see §8).

## How to use
- Each case: **Pre** (setup) → **Steps** → **Expect** → **Sev** (Blocker/High/Med/Low) → Pass/Fail.
- Roles: **Requester** (`car_order:create`), **Dispatcher** (`car_order:approve`/`reject`/`list`),
  **Driver** (`driver:accept_order`/`driver:trip_control`).
- Backend run + base URLs: see the dev-runtime memory note. Default `REQUIRE_OVERLAY_AUTH=False`
  (open dev). For auth cases, run with `REQUIRE_OVERLAY_AUTH=True`.
- A green automated test exists for most rows below — the ID in `[brackets]` points at it;
  manual focus is the UI/real-device/timing/integration behaviour the test can't assert.

---

## 1. Order lifecycle (native CarOrder)

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|1.1| Requester logged in | Create → Submit → (Dispatcher) Approve → (Driver on shift) Claim → Start → Complete | Status walks draft→pending→awaiting_driver→scheduled→in_progress→completed; each transition visible to all roles | Blocker `[test_full_workflow]` |
|1.2| Draft order | Requester edits the draft (address/time) | Allowed only while DRAFT and only by the creator; dispatcher edit → 403/404 | High `[test_draft_edit_only_by_creator]` |
|1.3| Pending order | Requester (author) Reject with a reason | Status=rejected; reason + rejected_by recorded; leaves the queue | High `[test_reject_by_author]` |
|1.4| Awaiting order | Dispatcher Reject | Status=rejected; overlay torn down so it stops auto-dispatching | High `[test_reject_by_dispatcher_on_awaiting]` |
|1.5| Scheduled order | Dispatcher Cancel with reason | Status=cancelled; driver's window freed; shift back to ONLINE | High `[test_cancel_frees_window]` |
|1.6| Draft order | Requester Delete | 204; row gone. Non-draft delete → 400 | Med `[test_destroy_*]` |
|1.7| Any role mismatch | Requester tries Approve; non-creator tries Submit; wrong driver tries Complete | All 403 | High `[test_requester_cannot_approve / submit_forbidden / only_assigned_driver_completes]` |
|1.8| Completed/rejected/cancelled order | Try Cancel / Reject / Extend | 400 INVALID_STATUS (terminal) | Med `[test_cancel_rejects_terminal / reject_rejects_wrong_status]` |
|1.9| Order with activity | Open the audit trail | created/sent/approved/accepted/completed/… entries with actor + timestamp | Low `[test_activity_lists_the_audit_trail]` |

## 2. Dispatcher

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|2.1| Scheduled order on a driver | Dispatcher Reassign | Back to awaiting_driver; driver dropped + notified; re-enters queue | High `[test_reassign_by_dispatcher]` |
|2.2| Auto-dispatch page | Read the toggle, flip ON, flip OFF | `effective` = env-switch AND DB-toggle; only a dispatcher may POST (driver → 403) | High `[test_auto_dispatch_* / auto_dispatch_post_forbidden]` |
|2.3| Env `AUTO_DISPATCH_ENABLED=false` | Toggle ON in UI | `effective=false` (env kill-switch wins); worker assigns nothing | High `[test_auto_enabled_respects_env_kill_switch]` |
|2.4| Several active orders/drivers | Open the fleet/live board | Every active order with its live marker; terminal orders absent | Med |
|2.5| Worker running, toggle flipped OFF mid-pass | Observe in-flight pass | The pass already started still assigns; the NEXT pass does nothing | Med |

## 3. Driver — shift (Р1)

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|3.1| Driver with ≥1 assigned car | Go on shift (pick car) | Online shift created; feed filtered to that car's type | Blocker `[test_full_workflow]` |
|3.2| On shift | Pick a car not assigned to you / inactive / on another driver's shift | 403 / CAR_UNAVAILABLE / CAR_BUSY respectively | High `[test_shift_rejects_*]` |
|3.3| On shift, no active trip | Switch car | Old shift ends, new ONLINE shift on the new car | Med `[test_switch_car_ends_old_and_starts_new]` |
|3.4| On shift, trip in progress | Switch car / End shift | 400 DRIVER_BUSY (finish the trip first) | High `[test_switch_blocked_mid_trip / end_shift_blocked_mid_trip]` |
|3.5| On shift, free | End shift | ended_at set, status OFFLINE; car freed for others | Med `[test_end_shift_when_free_sets_offline]` |

## 4. Driver — claim / trip-state

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|4.1| Awaiting order, driver NOT on shift | Claim | 400 NO_SHIFT | High `[test_claim_requires_active_shift]` |
|4.2| Shift car type ≠ order type | Claim | 400 TYPE_MISMATCH | High `[test_claim_rejects_type_mismatch]` |
|4.3| Driver holds an order overlapping the new window | Claim | 409 TIME_CONFLICT with the conflicting order's details | High `[test_overlapping_window_rejected]` |
|4.4| Two non-overlapping windows | Claim both | Both scheduled (one driver, several orders) | High `[test_non_overlapping_windows_allowed]` |
|4.5| Scheduled order | Trip-state taps to_client → at_client → in_trip → at_destination → completed | Each tap advances one legal step; skips rejected (INVALID_TRANSITION) | High `[test_can_transition_* / validate_*]` |
|4.6| Driver already moving on order A | Try to start moving on order B (to_client/in_trip) | 400 ACTIVE_TRIP_EXISTS; a *parked* (waiting/at_destination) order does NOT block | High `[test_blocks_second_moving_trip*]` |
|4.7| Round-trip order (has_return) | At destination, try Complete before the return leg | Blocked; must go at_destination→in_trip (returning) → back → Complete | Med `[test_validate_blocks_complete_before_return_leg]` |

## 5. Geofence (arrival confirmation) — needs a real device/sim GPS

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|5.1| Driver >100 m from pickup, fresh GPS | Tap "Arrived" (at_client) | 400 TOO_FAR (shows the distance) | High `[test_geofence_rejects_just_outside]` |
|5.2| Driver ≤100 m, fresh GPS | Tap "Arrived" | Allowed | High `[test_geofence_passes_just_inside]` |
|5.3| Driver on the point, GPS fix >120 s old | Tap "Arrived" | 400 NO_FRESH_GPS | High `[test_geofence_rejects_a_stale_but_present_fix]` |
|5.4| Same, at the DESTINATION (incl. return leg) | Tap "Arrived at destination" | Geofenced against the destination / return point | High `[test_geofence_at_destination_*]` |
|5.5| `CAR_ORDER_ARRIVAL_GEOFENCE_M=0` | Tap "Arrived" from anywhere | Geofence skipped (disabled) | Low `[test_geofence_disabled_when_radius_zero]` |

## 6. Live tracking (real-device + map) — exploratory

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|6.1| Trip in progress, customer watching | Driver moves | Marker + route update ~1 Hz on the customer & fleet maps | High |
|6.2| Driver parked (no movement) | Idle 1–2 min | "Connection" stays alive (parked heartbeat); no marker jitter | High |
|6.3| Driver GPS jitter <12 m | Stand still | Marker doesn't twitch (dead-zone) | Med |
|6.4| Driver deviates >80 m off route | Take a wrong turn | Route re-computes from the new position | Med |
|6.5| Driverless approved order | Open the tracker | A→B planned route drawn (pins + line) before any driver | Med |
|6.6| Reassign / reject mid-trip | Watch the customer map | Tracking ends cleanly ("cancelled"); the new driver's order starts fresh | High |
|6.7| OSRM down | Assign + move | Falls back to a straight line; a good road route already drawn is NOT overwritten | Med `[test_push_route_keeps_good_route*]` |

## 7. Scheduling & extend

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|7.1| Order A 10:00–15:00 claimed | Claim B starting 15:20 (inside 30-min buffer) vs 16:00 | 15:20 → 409; 16:00 → OK | High `[test_travel_buffer_enforced]` |
|7.2| Driver parked at a long shoot (at_destination/waiting) | Claim a gap order inside that window | Allowed — parked time is free | High `[test_meta_conflict_parked_*]` |
|7.3| Active order, driver or dispatcher | Extend ("продлить") by N minutes | Always applies; warns if the new end overlaps the next order | High `[test_extend_flags_conflict_with_next]` |
|7.4| Order created with no duration | Extend | Works (starts from 0), no 400 | Med `[test_extend_with_no_prior_duration]` |
|7.5| Driver on an overrunning trip | Check the next order | Flagged at_risk / needs_reassign once projected start passes latest_start | Med `[test_projected_start_pushes_past_an_overrunning_trip]` |

## 8. Resilience & concurrency (manual / staging only)

> The automated suite runs on SQLite where `select_for_update()` is a **no-op**, so the
> race cases below are **not** covered by green tests — verify them on a Postgres staging
> env with two parallel clients. See `AUDIT.md` findings C1/C2/H1/H3.

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|8.1| One awaiting order | Two clients Claim it simultaneously | Exactly one wins; the other gets a clean error (not a double-assign) | Blocker |
|8.2| One free driver, two due orders | Auto-dispatch + a manual claim race | Driver ends with exactly ONE active order | Blocker |
|8.3| One car, two drivers | Both start a shift on it at once | One succeeds; the other gets CAR_BUSY, not a 500 | High |
|8.4| Redis/channel layer down | Drive a trip | REST calls still succeed; live frames are missed but nothing 500s | High |
|8.5| Upstream demo 502/timeout | Approve/Reject via the gateway hooks | No demo/overlay split-brain (both move together, or neither) | High |
|8.6| Worker restart with pending ASAP orders | Restart `auto_dispatch` | ASAP "waited long enough" clock should not reset indefinitely (known gap — M6) | Med |

## 9. AuthZ / security (run with `REQUIRE_OVERLAY_AUTH=True`)

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|9.1| Plain driver token | Call reassign / meta DELETE / auto-dispatch POST | 403 (dispatcher-only) | High `[test_reassign_forbidden / meta_delete_forbidden / auto_dispatch_post_forbidden]` |
|9.2| Driver A token, body driver_id=B | Overlay-claim | Identity from the token (A), body ignored | High `[test_enforced_identity_comes_from_token_not_body]` |
|9.3| Driver token | GET my-overlay-orders?driver_id=other | Only own orders (IDOR-safe); dispatcher may filter to any | High `[test_enforced_my_orders_ignores_query_driver_id]` |
|9.4| **No** token | POST live-location / meta for an arbitrary order id | ⚠️ Currently ACCEPTED (AllowAny) — see AUDIT C3. Verify whether this is acceptable for the deployment | Blocker |
