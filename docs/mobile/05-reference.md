# 05 — Справочник: статусы, ошибки, эндпоинты

## Статус заявки (`CarOrder.status`, с demo)

| status | Значение | Дальше |
|---|---|---|
| `draft` | Черновик | автор → `submit` |
| `pending` | На согласовании | диспетчер → `admin-approve` / `reject` |
| `awaiting_driver` | Ожидает водителя | водитель → `claim` |
| `in_progress` | В пути | водитель → `complete` |
| `completed` | Завершён | — |
| `rejected` | Отклонён | — |

> У **overlay-принятого** заказа демо-статус остаётся `awaiting_driver`. Реальное состояние бери из
> `meta.trip_state` (см. ниже и «Эффективный статус» в [03](03-scheduling-overlay.md)).

## Этап поездки (`OrderMeta.trip_state`, наш слой)

`assigned → to_client → at_client → in_trip → at_destination → waiting → completed`
Плюс терминальный `cancelled` (после `overlay-release`).
Подписи и кнопки — в [03-scheduling-overlay.md](03-scheduling-overlay.md) §3.6.

## Права (codename)
`car_order:create`, `car_order:approve`, `car_order:reject`, `car_order:list` / `:list_own`,
`driver:accept_order`, `driver:trip_control`, `driver:list`, `garage:list`. Детали — [01](01-auth.md).

## Пагинация
DRF limit/offset: `{ count, next, previous, results: [...] }`. Некоторые эндпоинты
(`drivers/me/cars/`, `car-types/`, `drivers/me/overlay-orders/`) возвращают **просто массив** —
нормализуй: «есть `results` → бери `results`, иначе сам массив».

## Ошибки

**Наши фичи:** `{"error": {"code","message","details"}}`. Коды:

| code | HTTP | Когда |
|---|---|---|
| `VALIDATION` | 400 | неверное тело / `trip_state` |
| `TIME_CONFLICT` | 200/409 | окно пересекается (в `claim-check`/`overlay-claim` — поле `conflict`) |
| `ALREADY_CLAIMED` | 400 | `overlay-claim` чужого активного заказа |
| `INVALID_STATUS` | 400 | смена `trip_state` у завершённого заказа |
| `NOT_FOUND` | 400 | нет meta/окна (напр. `reassign`/`extend` без overlay) |

**demo (DRF):** `{"detail":"..."}` (напр. `"This car is not available."` — машина занята активным
заказом) или `{"field":["..."]}` / `{"non_field_errors":["..."]}`.

Единый разбор в приложении: `error.message` → иначе `detail` → иначе первый `{field:[msg]}` → иначе «Ошибка сети».

## Авторизация overlay-эндпоинтов
В dev они открыты. При `REQUIRE_OVERLAY_AUTH=true` (env) требуют тот же **demo-токен**
(`Authorization: Bearer <access>`): шлюз проверяет его через demo `/auth/me/` и берёт `driver_id`
**из токена** (тело `driver_id` игнорируется → нельзя выдать себя за другого/прочитать чужое).
Без/с неверным токеном — `401`. Исключения: `estimate` (чистая функция) и `live-location` (путь
симулятора) остаются доступны без enforcement-логики; `reassign` — только диспетчеру (`car_order:approve`).

## HTTP-коды
`200` ok · `201` создано · `400` валидация/бизнес-правило · `401` токен протух (→`refresh`) ·
`403` нет прав · `404` не найдено · `409` конфликт времени · `502` шлюз не достучался до demo.

## Единицы и форматы
- Время — ISO-8601 UTC (`2026-06-11T09:00:00Z`), показывай локально.
- Длительность — целое **минут**. Расстояние — метры (`distance_m`).
- `geometry` — `[lng, lat]` (GeoJSON); для карт переворачивай в `[lat, lng]`.

## Полная карта эндпоинтов

| Метод | Путь | Источник | Раздел |
|---|---|---|---|
| POST | `/auth/login/` · `/auth/refresh/` | demo | [01](01-auth.md) |
| GET | `/auth/me/` | demo | [01](01-auth.md) |
| GET·POST | `/car-orders/` (список / создать) | demo | [02](02-car-orders.md) |
| GET | `/car-orders/{id}/` | demo | [02](02-car-orders.md) |
| POST | `/car-orders/{id}/submit/` · `/admin-approve/` · `/reject/` | demo | [02](02-car-orders.md) |
| POST | `/car-orders/{id}/claim/` `{car_id}` · `/complete/` | demo | [02](02-car-orders.md) |
| GET | `/car-orders/drivers/me/cars/` · `/car-orders/car-types/` | demo | [02](02-car-orders.md) |
| POST | `/car-orders/estimate/` | локально | [03](03-scheduling-overlay.md) |
| GET·POST | `/car-orders/{id}/meta/` | локально | [03](03-scheduling-overlay.md) |
| POST | `/car-orders/{id}/claim-check/` `{driver_id}` | локально | [03](03-scheduling-overlay.md) |
| POST | `/car-orders/claim-check-batch/` `{driver_id,order_ids}` · `/meta-batch/` `{order_ids}` | локально | [03](03-scheduling-overlay.md) |
| POST | `/car-orders/{id}/overlay-claim/` `{driver_id,car_id,car_label}` | локально | [03](03-scheduling-overlay.md) |
| POST | `/car-orders/{id}/overlay-release/` | локально | [03](03-scheduling-overlay.md) |
| POST | `/car-orders/{id}/trip-state/` `{trip_state}` | локально | [03](03-scheduling-overlay.md) |
| POST | `/car-orders/{id}/extend/` `{minutes}` · `/reassign/` | локально | [03](03-scheduling-overlay.md) §3.9 |
| GET·POST | `/car-orders/{id}/live-location/` | локально | [04](04-live-tracking.md) |
| POST | `/car-orders/drivers/me/location/` `{driver_id,lat,lng}` | локально | [04](04-live-tracking.md) |
| GET | `/car-orders/drivers/me/overlay-orders/?driver_id=X` | локально | [03](03-scheduling-overlay.md) |
| GET | `/health/` · `/healthcheck/` (проба доступности сервера для мобилки) | локально | [README](README.md) |
| WS | `/ws/car-orders/{id}/location/` | локально | [04](04-live-tracking.md) |

> Мобильная схема: приложение ходит `host/<язык>/api/v1/...` (язык в пути) — шлюз срезает префикс и
> роутит как `/api/v1/...`. Проба URL — `host/healthcheck/` → `200 {"status":"ok"}`. См. [README](README.md).
