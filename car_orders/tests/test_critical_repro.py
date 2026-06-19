"""Исполняемые демонстраторы Critical-находок из AUDIT.ru.md.

Эти тесты ФИКСИРУЮТ ТЕКУЩЕЕ (небезопасное) поведение, поэтому СЕЙЧАС они ЗЕЛЁНЫЕ —
это и есть доказательство наличия бага. После фикса нужный assert надо «перевернуть»
(помечено комментарием `# ПОСЛЕ ФИКСА:`), и тогда тест станет регрессионным.

Запуск:  .venv/bin/pytest car_orders/tests/test_critical_repro.py -v

C1 (гонка «1 водитель = 1 активный заказ») воспроизводится только на Postgres с
параллельными клиентами — см. scripts/repro_c1_double_book.py, здесь только
последовательная проверка штатного guard'а (что ДОЛЖНО держаться и под нагрузкой).
"""

import pytest
from django.db import connection
from django.test import override_settings
from rest_framework.test import APIClient

from car_orders.models import OrderLiveLocation, OrderMeta
from car_orders.services import overlay

TS = OrderMeta.TripState


# ---- C3: неаутентифицированная запись позиции любого заказа -----------------

@override_settings(REQUIRE_OVERLAY_AUTH=True)  # при ВКЛЮЧЁННОЙ оверлей-аутентификации
@pytest.mark.django_db
def test_c3_live_location_anon_write_rejected_when_enforced():
    """ИСПРАВЛЕНО (C3): когда auth включена, аноним больше НЕ может писать позицию
    чужого заказа — endpoint требует владельца/диспетчера. В dev (auth off) остаётся
    открытым для симулятора (см. test_auth_bridge::test_live_location_open_for_simulator_in_dev)."""
    client = APIClient()  # БЕЗ токена
    r = client.post(
        "/api/v1/car-orders/424242/live-location/",
        {"lat": 41.0, "lng": 69.0, "geometry": [[69.0, 41.0], [69.1, 41.1]]},
        format="json",
    )
    assert r.status_code in (401, 403)  # было: 200 (баг C3)
    assert not OrderLiveLocation.objects.filter(order_id=424242).exists()  # ничего не записано


@pytest.mark.django_db
def test_c3_meta_mass_assign_open_in_dev_posture():
    """Это уже H5/«auth off по умолчанию» (НЕ enforced-режим): в dev OverlayDispatcher
    открыт, поэтому /meta/ принимает служебные поля — как и весь dev-контур.
    Закрытие enforced-режима (C3/M2-фикс) проверяется в
    test_auth_bridge::test_meta_post_strips_assignment_fields_for_non_dispatcher."""
    client = APIClient()  # БЕЗ токена, auth выключена по умолчанию (H5)
    r = client.post(
        "/api/v1/car-orders/525252/meta/",
        {"driver_id": 999, "dispatchable": True, "overlay_claimed": True},
        format="json",
    )
    assert r.status_code == 200
    m = OrderMeta.objects.get(order_id=525252)
    # В dev служебные поля проходят (H5). При REQUIRE_OVERLAY_AUTH=True — отсекаются.
    assert m.driver_id == 999 and m.dispatchable is True and m.overlay_claimed is True


# ---- C2: блокировки строк — no-op на дефолтной БД ---------------------------

@pytest.mark.django_db
def test_c2_select_for_update_is_noop_on_sqlite():
    """На SQLite (дефолт) `SELECT ... FOR UPDATE` молча игнорируется, поэтому
    row-lock в overlay.claim / dispatch.claim не защищает ничего."""
    if connection.vendor == "sqlite":
        # ПОСЛЕ перехода на Postgres станет True — тогда хотя бы однострочная защита есть.
        assert connection.features.has_select_for_update is False
    else:
        assert connection.features.has_select_for_update is True


# ---- C1: штатный (последовательный) guard — что ДОЛЖНО держаться под нагрузкой

@override_settings(CAR_ORDER_OSRM_URL="")
@pytest.mark.django_db
def test_c1_sequential_guard_holds():
    """Последовательно guard работает: второй claim того же водителя падает DRIVER_BUSY.
    Баг C1 в том, что ПАРАЛЛЕЛЬНО этот инвариант НЕ держится (busy-check вне блокировки)
    — это воспроизводится только на Postgres, см. scripts/repro_c1_double_book.py."""
    OrderMeta.objects.create(order_id=600)
    OrderMeta.objects.create(order_id=601)
    overlay.claim(600, driver_id=5)
    with pytest.raises(overlay.OverlayError) as exc:
        overlay.claim(601, driver_id=5)
    assert exc.value.code == "DRIVER_BUSY"
    # Под нагрузкой (две параллельные транзакции) ОБА claim проходят — вот это и есть C1.
