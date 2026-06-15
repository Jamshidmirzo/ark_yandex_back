"""Service layer for the car-orders block.

Split by concern (mirrors ark-backend's ``services/`` package layout):

- :mod:`~car_orders.services.audit`         — the ``CarOrderActivity`` trail (``record_*``).
- :mod:`~car_orders.services.notifications` — notification / live-location fan-out hooks.
- :mod:`~car_orders.services.routing`       — OSRM / haversine route + duration estimates.
- :mod:`~car_orders.services.trip_state`    — the overlay trip-state machine.

The public names are re-exported here so existing ``from car_orders import
services`` call sites keep working unchanged (``services.estimate_route``,
``services.record_created``, …).
"""

from car_orders.services import events, orders, overlay, shift, trip_state
from car_orders.services.audit import (
    record_accepted,
    record_approved,
    record_cancelled,
    record_completed,
    record_created,
    record_extended,
    record_reassigned,
    record_rejected,
    record_released,
    record_sent,
)
from car_orders.services.notifications import notify, publish_driver_location
from car_orders.services.routing import estimate_duration, estimate_payload, estimate_route

__all__ = [
    "events",
    "orders",
    "overlay",
    "shift",
    "trip_state",
    "record_accepted",
    "record_approved",
    "record_cancelled",
    "record_completed",
    "record_created",
    "record_extended",
    "record_reassigned",
    "record_rejected",
    "record_released",
    "record_sent",
    "notify",
    "publish_driver_location",
    "estimate_duration",
    "estimate_payload",
    "estimate_route",
]
