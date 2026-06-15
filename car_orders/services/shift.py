"""Driver-shift helpers: look up a driver's active shift and reset it to ONLINE.

Small, dependency-light service shared by the order-lifecycle service and the
views (shift control, feed visibility).
"""

from car_orders.models import DriverShift


def active_shift(user):
    """The driver's currently-open shift (with its car eager-loaded), or None."""
    return (
        DriverShift.objects.filter(driver=user, ended_at__isnull=True)
        .select_related("car", "car__type")
        .first()
    )


def reset_driver_shift(driver):
    """Put a driver's active shift back to ONLINE (e.g. after their trip is
    cancelled / reassigned out from under them)."""
    if driver is None:
        return
    shift = DriverShift.objects.filter(driver=driver, ended_at__isnull=True).first()
    if shift and shift.status != DriverShift.Status.ONLINE:
        shift.status = DriverShift.Status.ONLINE
        shift.save(update_fields=["status", "updated_at"])
