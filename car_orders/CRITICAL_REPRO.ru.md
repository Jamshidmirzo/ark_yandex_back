# Critical-находки — как воспроизвести и как проверить после фикса

Спутник [AUDIT.ru.md](AUDIT.ru.md). По каждой из трёх Critical-находок: **кейс**
(где всплывает), **как воспроизвести сейчас** (увидеть баг), **что увидите**, и
**что должно стать после фикса** (как зелёным подтвердить, что починено).

Готовые артефакты:
- `car_orders/tests/test_critical_repro.py` — исполняемые демонстраторы (C2, C3 + штатный guard C1).
  Сейчас они **зелёные** — фиксируют текущее (небезопасное) поведение. После фикса
  «переверните» помеченные `# ПОСЛЕ ФИКСА:` assert'ы — и они станут регрессионными.
- `car_orders/scripts/repro_c1_double_book.py` — воспроизведение гонки C1 (нужен Postgres).

Запуск демонстраторов:
```
.venv/bin/pytest car_orders/tests/test_critical_repro.py -v
```
Прогон сейчас (реальный вывод):
```
test_c3_live_location_writable_without_any_token PASSED
test_c3_meta_mass_assign_when_auth_off          PASSED
test_c2_select_for_update_is_noop_on_sqlite     PASSED
test_c1_sequential_guard_holds                  PASSED
4 passed
```

> Везде ниже `$BASE` — адрес вашего запущенного бэкенда (см. заметку про dev-runtime,
> по умолчанию шлюз на `http://localhost:8000`). Оверлейные эндпойнты отвечают локально
> (до gateway catch-all), поэтому upstream-демо для этих кейсов не нужен.

---

## C3 — аноним пишет позицию/мету любого заказа (write-IDOR)

**Кейс.** `LiveLocationView` объявлен `authentication_classes=[]` + `AllowAny`
([views.py:219-261](views.py#L219-L261)) — он открыт **даже когда
`REQUIRE_OVERLAY_AUTH=True`**. `OrderMetaView.post` принимает `validated_data`
напрямую в `update_or_create` ([views.py:284-290](views.py#L284-L290)),
а в `OrderMetaSerializer` записываемы `driver_id`, `dispatchable`, `overlay_claimed`
и т.д. ([serializers.py:315-357](serializers.py#L315-L357)). Всплывает в
любом сценарии, где злоумышленник знает (или перебирает) `order_id`.

**Как воспроизвести сейчас — вариант А (curl, без токена):**
```
# Подвинуть маркер ЧУЖОГО заказа + впрыснуть произвольную geometry, без авторизации:
curl -s -X POST "$BASE/api/v1/car-orders/424242/live-location/" \
  -H "Content-Type: application/json" \
  -d '{"lat":41.0,"lng":69.0,"geometry":[[69.0,41.0],[69.1,41.1]]}'
# Затем прочитать обратно — позиция сохранилась и разослана наблюдателям:
curl -s "$BASE/api/v1/car-orders/424242/live-location/"
```
**Вариант Б (pytest, детерминированно):**
```
.venv/bin/pytest car_orders/tests/test_critical_repro.py::test_c3_live_location_writable_without_any_token -v
.venv/bin/pytest car_orders/tests/test_critical_repro.py::test_c3_meta_mass_assign_when_auth_off -v
```

**Что увидите сейчас (баг):** обе команды → **HTTP 200**; `OrderLiveLocation`
создаётся/перезаписывается, `geometry` уходит в WebSocket всем, кто смотрит трек.
Через `/meta/` аноним так же проставляет себе `driver_id`/`dispatchable` в обход
`overlay.claim`.

**Что должно быть после фикса:** запись позиции/меты требует аутентифицированного
владельца (водитель своего заказа) или диспетчера. Тот же `curl` без токена → **401/403**,
строка `OrderLiveLocation` не появляется. В тесте «переверните» assert:
`assert r.status_code in (401, 403)` и `assert not OrderLiveLocation.objects.filter(order_id=424242).exists()`.

---

## C2 — блокировки строк не работают на дефолтной БД

**Кейс.** Дефолт — SQLite ([settings.py:117-122](../config/settings.py#L117-L122)).
На SQLite `SELECT … FOR UPDATE` молча игнорируется, значит `select_for_update()` в
`overlay.claim` / `dispatch.claim` не даёт **никакой** защиты (и так же — во всём
наборе тестов). Это усиливает C1.

**Как воспроизвести сейчас:**
```
.venv/bin/python -c "import django,os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup(); from django.db import connection; print('vendor=',connection.vendor,' has_select_for_update=',connection.features.has_select_for_update)"
```
или тестом:
```
.venv/bin/pytest car_orders/tests/test_critical_repro.py::test_c2_select_for_update_is_noop_on_sqlite -v
```

**Что увидите сейчас (баг):** `vendor= sqlite  has_select_for_update= False` —
блокировки строк отключены.

**Что должно быть после фикса:** прод/стейдж работает на Postgres
(`DATABASE_URL=postgres://…`); та же команда → `vendor= postgresql
has_select_for_update= True`. Это **необходимое** условие, чтобы фикс C1 вообще имел
силу. Зафиксируйте СУБД прода в runbook.

---

## C1 — двойное бронирование водителя (гонка)

**Кейс.** `overlay.claim` блокирует строку самого заказа
([overlay.py:37-62](services/overlay.py#L37-L62)), а «водитель занят»
проверяет ОТДЕЛЬНЫМ незаблокированным запросом (`filter(driver_id=…)`). Двойник в
воркере — `dispatch.claim` ([dispatch.py:95-110](dispatch.py#L95-L110)).
Всплывает при **параллельных** claim'ах ДВУХ РАЗНЫХ заказов на ОДНОГО водителя:
двойной тап, два устройства, или гонка «воркер авто-диспетча vs ручной claim
диспетчера». Последовательно guard работает — проблема только под нагрузкой.

**Как воспроизвести сейчас (нужен Postgres + параллелизм):**
```
DATABASE_URL=postgres://user:pass@localhost:5432/arkdb \
  .venv/bin/python car_orders/scripts/repro_c1_double_book.py
```
Эквивалент «руками»: два почти одновременных запроса на разные заказы одного водителя:
```
curl -s -X POST "$BASE/api/v1/car-orders/990001/overlay-claim/" -H "Content-Type: application/json" -d '{"driver_id":990999}' &
curl -s -X POST "$BASE/api/v1/car-orders/990002/overlay-claim/" -H "Content-Type: application/json" -d '{"driver_id":990999}' &
wait
# затем проверить, сколько активных заказов у водителя 990999 (должно быть ≤ 1)
```

**Что увидите сейчас (баг):** скрипт печатает `💥 C1 ВОСПРОИЗВЕДЕНО … оба заказа
назначены одному водителю` — у водителя 990999 **два** активных заказа, инвариант
«1 водитель = 1 активный заказ» нарушен.

**Что должно быть после фикса:** инвариант держит **БД**, а не код приложения —
например частичный `UniqueConstraint` на `driver_id WHERE trip_state NOT IN
(terminal)` (с обработкой `IntegrityError` → `DRIVER_BUSY`), либо общая блокировка
всех строк водителя в одной транзакции. Тогда скрипт печатает `✅ C1 НЕ
воспроизводится` (ровно один claim проходит, второй → `DRIVER_BUSY`). Фикс C1
обязателен на Postgres (см. C2) и должен поставляться с регрессионным тестом на
Postgres-линии CI.

---

## Чек-лист «до → после»

| # | Воспроизведение сейчас (баг) | После фикса (ожидание) |
|---|---|---|
| C3 | `curl` без токена на `/live-location/` → 200, строка создана | без токена → 401/403, строки нет |
| C3 | `/meta/` без токена ставит `driver_id`/`dispatchable` | запись служебных полей запрещена |
| C2 | `has_select_for_update = False` (SQLite) | `True` (Postgres) |
| C1 | скрипт: «💥 C1 ВОСПРОИЗВЕДЕНО», 2 активных заказа | «✅ C1 НЕ воспроизводится», ≤ 1 |

После ваших правок я переверну помеченные assert'ы в `test_critical_repro.py`
(200→401/403 и т.д.) — и зелёный прогон станет доказательством, что Critical закрыты.
