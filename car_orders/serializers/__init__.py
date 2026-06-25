"""DRF serializers for the car-orders block.

Split one file per resource (see the django-code-style guide); each submodule
declares ``__all__`` and this package re-exports everything via wildcard, so
``from car_orders.serializers import CarOrderSerializer`` is unchanged.

Read/Detail and Write serializers that share a field block now inherit a common
base and extend ``Meta.fields`` via unpacking (rule [3]) instead of re-listing it.
"""

from .fields import *  # noqa: F401, F403
from .cars import *  # noqa: F401, F403
from .shifts import *  # noqa: F401, F403
from .orders import *  # noqa: F401, F403
from .overlay import *  # noqa: F401, F403
from .templates import *  # noqa: F401, F403
from .reports import *  # noqa: F401, F403
