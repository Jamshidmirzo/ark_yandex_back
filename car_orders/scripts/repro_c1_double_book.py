"""Воспроизведение Critical-находки C1 — гонка «1 водитель = 1 активный заказ».

overlay.claim / dispatch.claim блокируют (select_for_update) ТОЛЬКО строку самого
заказа, а проверку «водитель уже занят» делают на ОТДЕЛЬНОМ незаблокированном
запросе. Две параллельные транзакции, назначающие ДВА РАЗНЫХ заказа ОДНОМУ
водителю, обе видят «не занят» и обе коммитятся → у водителя два активных заказа.

⚠️  Требуется Postgres (на SQLite select_for_update — no-op, см. C2, и потоки
    блокируют файл БД). Запуск:

    POSTGRES_HOST=localhost POSTGRES_DB=ark_yandex POSTGRES_USER=ark POSTGRES_PASSWORD=ark \
        .venv/bin/python car_orders/scripts/repro_c1_double_book.py

Скрипт создаёт две временные OrderMeta (order_id 990001/990002), гоняет гонку и
за собой убирает. На исправленном коде (DB-constraint / общая блокировка)
скрипт НЕ сможет назначить оба заказа — выведет «C1 НЕ воспроизводится».
"""

import os
import sys
import threading

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("CAR_ORDER_OSRM_URL", "")  # офлайн-маршрут, без сети
django.setup()

from django.db import connection  # noqa: E402

from car_orders.models import OrderMeta  # noqa: E402
from car_orders.services import overlay  # noqa: E402

DRIVER = 990999
OIDS = (990001, 990002)


def _cleanup():
    OrderMeta.objects.filter(order_id__in=OIDS).delete()


def _attempt():
    _cleanup()
    for oid in OIDS:
        OrderMeta.objects.create(order_id=oid)  # свободный заказ, без водителя

    barrier = threading.Barrier(len(OIDS))
    results = {}

    def worker(oid):
        barrier.wait()  # стартуем максимально одновременно
        try:
            overlay.claim(oid, driver_id=DRIVER)
            results[oid] = "OK"
        except overlay.OverlayError as exc:
            results[oid] = exc.code
        finally:
            connection.close()  # у каждого потока своё соединение — закрываем

    threads = [threading.Thread(target=worker, args=(oid,)) for oid in OIDS]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    active = OrderMeta.objects.filter(
        driver_id=DRIVER
    ).exclude(trip_state__in=(OrderMeta.TripState.COMPLETED, OrderMeta.TripState.CANCELLED)).count()
    return results, active


def main():
    if connection.vendor != "postgresql":
        print(f"⚠️  Текущая БД — {connection.vendor}. Гонка C1 видна только на Postgres.")
        print("    Задайте POSTGRES_HOST/POSTGRES_DB/… (см. .env.example) и повторите.")
        return 2

    print(f"Гоняю гонку (водитель {DRIVER}, заказы {OIDS}). До 30 попыток…")
    try:
        for i in range(1, 31):
            results, active = _attempt()
            if active >= 2:
                print(f"\n💥 C1 ВОСПРОИЗВЕДЕНО на попытке {i}: оба заказа назначены одному "
                      f"водителю (active={active}, results={results}).")
                print("   Инвариант «1 водитель = 1 активный заказ» нарушен под нагрузкой.")
                return 1
        print("\n✅ C1 НЕ воспроизводится за 30 попыток — вероятно, фикс на месте "
              "(DB-constraint / общая блокировка водителя держат инвариант).")
        return 0
    finally:
        _cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
