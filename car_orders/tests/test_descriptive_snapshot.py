"""`_snapshot_descriptive` lazily fills demo-only descriptive fields (project /
note / car-type / creator / address) onto an EXISTING OrderMeta from the demo
bodies a privileged client paged — so an order whose demo body the requester
can't read still shows full info via the client overlay fallback."""

import pytest

from car_orders.models import OrderMeta
from car_orders.views import _snapshot_descriptive


@pytest.mark.django_db
def test_snapshot_fills_blank_fields_on_existing_meta():
    meta = OrderMeta.objects.create(order_id=186)
    body = {
        "id": 186,
        "project_name": "Стройка №7",
        "note": "Забрать прораба",
        "car_type": {"id": 3, "name": "Микроавтобус"},
        "created_by": {"id": 42, "name": "Иванов И."},
        "address": "ул. Навои, 12",
    }

    _snapshot_descriptive({186: body})

    meta.refresh_from_db()
    assert meta.project_name == "Стройка №7"
    assert meta.note == "Забрать прораба"
    assert meta.car_type_name == "Микроавтобус"
    assert meta.created_by_name == "Иванов И."
    assert meta.dest_address == "ул. Навои, 12"


@pytest.mark.django_db
def test_snapshot_never_creates_a_row_for_demo_only_orders():
    # No OrderMeta for 999 → it's a plain demo order we don't manage; must stay out
    # of our overlay (else it would pollute the «our orders» list).
    _snapshot_descriptive({999: {"id": 999, "project_name": "X", "address": "Y"}})
    assert not OrderMeta.objects.filter(order_id=999).exists()


@pytest.mark.django_db
def test_snapshot_does_not_overwrite_existing_values():
    meta = OrderMeta.objects.create(
        order_id=5, project_name="Original", dest_address="Original addr"
    )
    _snapshot_descriptive(
        {5: {"id": 5, "project_name": "New", "address": "New addr", "note": "added"}}
    )
    meta.refresh_from_db()
    # Pre-filled fields are left intact; only the blank `note` is added.
    assert meta.project_name == "Original"
    assert meta.dest_address == "Original addr"
    assert meta.note == "added"


@pytest.mark.django_db
def test_snapshot_tolerates_missing_and_malformed_body_fields():
    meta = OrderMeta.objects.create(order_id=7)
    # car_type/created_by absent or not dicts, no address — must not crash, leaves blank.
    _snapshot_descriptive({7: {"id": 7, "car_type": None, "created_by": "oops"}})
    meta.refresh_from_db()
    assert meta.car_type_name == ""
    assert meta.created_by_name == ""
    assert meta.project_name == ""
