"""API for the car-orders block.

Workflow (see ТЗ §3):
    draft → pending(submit) → awaiting_driver(admin-approve)
          → in_progress(claim, uses shift car) → completed
          → rejected (dispatcher reject / author cancel, before in_progress)

Permissions mirror ark-backend codenames (``car_order:*``, ``driver:*``,
``garage:*``, ``vehicle_report:*``). Р1 = shift car; Р3 = live location.

Split one module per concern (see the django-code-style guide). This package
re-exports every public view + the handful of helpers that tests, the WS consumer
and the fleet snapshot import, so ``from car_orders.views import X`` is unchanged.
"""

from .base import *  # noqa: F401, F403
from .proxy import *  # noqa: F401, F403
from .overlay import *  # noqa: F401, F403
from .tracking import *  # noqa: F401, F403
from .shifts import *  # noqa: F401, F403
from .misc import *  # noqa: F401, F403
from .orders import *  # noqa: F401, F403
from .garage import *  # noqa: F401, F403
