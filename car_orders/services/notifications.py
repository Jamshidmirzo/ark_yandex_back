"""Integration hooks that, on merge, fan out notifications and live-location WS
events. Kept as no-ops here so this block runs standalone; wire them to
ark-backend's ``apps.notifications`` and the WS bus on merge — see INTEGRATION.md.
"""


def notify(user, title, body="", route_type="car_order", extra=None):
    """No-op notification hook.

    On integration, replace with ark-backend's
    ``apps.notifications.send_notification(user, title, body, route_type, extra)``.
    """
    return None


def publish_driver_location(shift):
    """No-op live-location hook.

    On integration, fan out a ``driver_status`` WS event to the order author's
    bus group so the tracking map updates in real time (see INTEGRATION.md).
    """
    return None
