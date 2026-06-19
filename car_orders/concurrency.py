"""Concurrency helpers for the claim paths.

The «1 водитель = 1 активный (movable) заказ» guard in ``services.overlay.claim`` /
``dispatch.claim`` reads the «driver busy?» state on a query that the surrounding
``select_for_update`` does NOT cover (it locks the order row, not the driver's other
rows). Two concurrent claims for the same driver therefore both pass the check and
both commit (AUDIT finding C1). A per-driver advisory lock serialises those claims
so the second one sees the first's committed state and is correctly rejected —
without a schema-level «one row per driver» constraint, which would forbid the
legitimate gap-filling case (a parked order + a gap order coexisting).
"""

from django.db import connection

# Arbitrary namespace so our advisory-lock keys don't collide with anyone else's
# (pg_advisory_xact_lock takes two int4 keys: a namespace + the driver id).
_DRIVER_LOCK_NS = 0x4341524F  # "CARO"


def lock_driver(driver_id) -> None:
    """Take a transaction-scoped advisory lock keyed by ``driver_id`` so concurrent
    claims for the same driver serialise. MUST be called inside ``transaction.atomic``
    (the lock releases on commit/rollback). No-op when the driver is unknown, the id
    isn't an int, or the backend has no advisory locks (e.g. SQLite — which already
    serialises writers, so the C1 race can't occur there)."""
    if driver_id is None or connection.vendor != "postgresql":
        return
    try:
        # Mask into the signed int4 range the two-key advisory-lock signature expects —
        # driver_id is an untrusted body field, and a value > 2^31-1 would otherwise
        # raise "integer out of range" and 500 the claim. A rare key collision only
        # over-serialises two drivers (harmless), never weakens correctness.
        key = int(driver_id) & 0x7FFFFFFF
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_DRIVER_LOCK_NS, key])
    except Exception:
        # The lock is a serialisation optimisation, not a correctness gate: if it can't
        # be taken (bad value, driver missing, etc.) the claim still runs its busy-check.
        # Never let lock acquisition turn a normal claim into a 500.
        return
