"""Unit tests for the realtime fan-out (``car_orders.services.events``).

Pin the two ark-backend invariants this layer adds: broadcasts are deferred to
``transaction.on_commit`` (a watcher never sees uncommitted state), and a broken
channel layer never propagates into the request that triggered the broadcast.
"""

import pytest

from car_orders.services import events


class _BoomLayer:
    """A channel layer whose ``group_send`` always fails (stands in for Redis down)."""

    async def group_send(self, *args, **kwargs):
        raise RuntimeError("redis down")


def test_group_send_swallows_layer_errors(monkeypatch):
    monkeypatch.setattr("channels.layers.get_channel_layer", lambda: _BoomLayer())
    events._group_send("any_group", {"type": "x", "data": {}})  # must NOT raise


def test_group_send_noop_without_layer(monkeypatch):
    monkeypatch.setattr("channels.layers.get_channel_layer", lambda: None)
    events._group_send("g", {"type": "x"})  # must NOT raise


def test_notify_user_ignores_none():
    events.notify_user(None, {"x": 1})  # no recipient → no-op, no raise


@pytest.mark.django_db
def test_broadcast_location_defers_to_on_commit(django_capture_on_commit_callbacks, monkeypatch):
    sent = []
    monkeypatch.setattr(events, "_group_send", lambda group, msg: sent.append((group, msg)))

    with django_capture_on_commit_callbacks(execute=True):
        events.broadcast_location(900, {"trip_state": "in_trip"})
        assert sent == []  # deferred — nothing sent before the transaction commits

    # After commit: both the per-order group and the fleet group got the frame.
    groups = {group for group, _ in sent}
    assert events.group_name(900) in groups
    assert events.FLEET_GROUP in groups
    # The fleet frame is tagged with the order_id so the dashboard can route it.
    fleet_msg = next(msg for group, msg in sent if group == events.FLEET_GROUP)
    assert fleet_msg["data"]["order_id"] == 900


@pytest.mark.django_db
def test_notify_order_status_targets_driver_and_author(
    django_capture_on_commit_callbacks, monkeypatch
):
    sent = []
    monkeypatch.setattr(events, "_group_send", lambda group, msg: sent.append((group, msg)))

    class _Meta:
        order_id = 900
        driver_id = 5
        author_id = 7

    with django_capture_on_commit_callbacks(execute=True):
        events.notify_order_status(_Meta(), "in_trip")

    groups = {group for group, _ in sent}
    assert events.user_group(5) in groups  # the driver
    assert events.user_group(7) in groups  # the order's author
