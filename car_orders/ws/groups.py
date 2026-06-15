"""Backward-compatible re-export of the WS fan-out helpers.

The fan-out now lives in :mod:`car_orders.services.events` (ark-backend keeps event
fan-out in the service layer, deferred to ``transaction.on_commit`` and routed
through a safe ``_group_send``). This shim keeps existing
``from car_orders.ws import broadcast_location`` / ``...ws.groups import ...``
imports working unchanged.
"""

from car_orders.services.events import (
    FLEET_GROUP,
    broadcast_location,
    group_name,
    notify_order_status,
    notify_user,
    user_group,
)

__all__ = [
    "FLEET_GROUP",
    "broadcast_location",
    "group_name",
    "notify_order_status",
    "notify_user",
    "user_group",
]
