"""Data model for the «Заявки на машину» (car orders) block.

Split into one file per domain concern (see the django-code-style guide). This
package re-exports every public name, so ``from car_orders.models import Car``
keeps working exactly as it did when this was a single module.

Mirrors ark-backend's ``apps.car_orders`` models (statuses already use
``rejected``) and adds two product decisions from the approved ТЗ:

* **Р1 — «машина на смене»**: a driver picks ONE car when going on shift
  (:class:`DriverShift`). The awaiting-driver feed is filtered to that car's
  type, and ``claim`` uses the shift car (no per-order car choice).
* **Р3 — live tracking**: the active shift carries the driver's last known
  ``lat``/``lng``/``last_seen``; the order author watches it on a map while
  the trip is ``in_progress``.

See INTEGRATION.md for how this maps back onto ark-backend.
"""

from .cars import *  # noqa: F401, F403
from .orders import *  # noqa: F401, F403
from .shifts import *  # noqa: F401, F403
from .tracking import *  # noqa: F401, F403
from .overlay import *  # noqa: F401, F403
from .reports import *  # noqa: F401, F403
from .templates import *  # noqa: F401, F403
from .settings import *  # noqa: F401, F403
