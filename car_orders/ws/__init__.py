"""WebSocket package for the car-orders block.

- groups.py   — channel groups + server→client push helpers (broadcast_location,
                notify_order_status, notify_user). Imported by HTTP views / dispatch.
- tracking.py — downlink consumers: per-order map stream, fleet feed, notifications,
                catch-all.
- driver.py   — uplink: the driver phone's bidirectional GPS socket.

Re-exports the helpers so existing ``from car_orders.ws import ...`` keeps working,
and assembles the routed ``websocket_urlpatterns`` (config/asgi.py mounts these).
"""

from django.urls import re_path

from car_orders.ws.driver import DriverLocationConsumer
from car_orders.ws.groups import (
    FLEET_GROUP,
    broadcast_location,
    group_name,
    notify_order_status,
    notify_user,
    user_group,
)
from car_orders.ws.tracking import (
    FallbackConsumer,
    FleetConsumer,
    LiveLocationConsumer,
    NotificationConsumer,
)

# Optional leading «/<lang>/» (the mobile uses /ru/... for HTTP and may reuse the
# habit for WS) — mirror the HTTP MobileLanguagePrefixMiddleware so both schemes route.
_L = r"^(?:[a-z]{2}/)?"
websocket_urlpatterns = [
    # Downlink (watch a map).
    re_path(_L + r"ws/car-orders/fleet/$", FleetConsumer.as_asgi()),
    re_path(_L + r"ws/notifications/(?P<user_id>\d+)/$", NotificationConsumer.as_asgi()),
    re_path(_L + r"ws/car-orders/(?P<order_id>\d+)/location/$", LiveLocationConsumer.as_asgi()),
    # Uplink (driver phone streams its GPS, gets back marker + polyline).
    re_path(_L + r"ws/drivers/me/location/$", DriverLocationConsumer.as_asgi()),
    # Catch-all LAST: unknown WS paths close quietly instead of raising a traceback.
    re_path(r".*", FallbackConsumer.as_asgi()),
]

__all__ = [
    "websocket_urlpatterns",
    "broadcast_location",
    "notify_order_status",
    "notify_user",
    "group_name",
    "user_group",
    "FLEET_GROUP",
]
