"""Shared pytest fixtures for the car-orders test suite."""

import pytest

from car_orders.services import routing


@pytest.fixture(autouse=True)
def _isolate_route_cache():
    """The OSRM route memo is a process-global dict; clear it around every test so a
    result cached by one test can never leak into another (e.g. an ``osrm`` route
    bleeding into a test that pins ``CAR_ORDER_OSRM_URL=""``)."""
    routing.clear_route_cache()
    yield
    routing.clear_route_cache()
