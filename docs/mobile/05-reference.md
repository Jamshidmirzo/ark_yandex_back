# 05 — Справочник: статусы, ошибки, пагинация

## Статусы заявки (`CarOrder.status`, приходит с demo)

| status | Значение | Кто двигает дальше |
|---|---|---|
| `draft` | Черновик | автор → `submit` |
| `pending` | На согласовании | диспетчер → `admin-approve` / `reject` |
| `awaiting_driver` | Ожидает водителя | водитель → `claim` |
| `in_progress` | В пути | водитель → `complete` |
| `completed` | Завершён | — |
| `rejected` | Отклонён | — |

> Наш слой добавляет к этому **`trip_state`** (этапы поездки) и может вести заказ, принятый через
> `overlay-claim`, как «в пути», даже если демо-статус ещё `awaiting_driver`. Для отображения
> «эффективного» статуса используйте `meta.trip_state`.

## Этапы поездки (`OrderMeta.trip_state`, наш слой)

`assigned` → `to_client` → `at_client` → `in_trip` → `at_destination` → `waiting` → (`completed`)

Подписи для заказчика и кнопки водителя — в [03-scheduling-overlay.md](03-scheduling-overlay.md) §3.5.

## Права (codename) — кратко

`car_order:create`, `car_order:approve`, `car_order:reject`, `car_order:list` / `car_order:list_own`,
`driver:accept_order`, `driver:trip_control`, `driver:list`, `garage:list`. Подробно — [01-auth.md](01-auth.md).

## Пагинация (списки)

DRF limit/offset:
```json
{ "count": 120, "next": "…?limit=50&offset=50", "previous": null, "results": [ ... ] }
```
- `limit` (по умолч. 50), `offset`.
- Некоторые эндпоинты (например, `drivers/me/cars/`, `car-types/`) могут вернуть **просто массив**.
  Нормализуй: «если пришёл объект с `results` — бери `results`, иначе сам массив».

## Формат ошибок

### Наши шлюзовые фичи
```json
{ "error": { "code": "TIME_CONFLICT", "message": "…", "details": { "order_id": 90 } } }
```
Коды: `VALIDATION`, `TIME_CONFLICT` (см. §детали), и т.п.

### demo (DRF)
```json
{ "detail": "This car is not available." }
```
или валидация полей:
```json
{ "planned_datetime": ["This field is required."] }
```
или общий:
```json
{ "non_field_errors": ["…"] }
```

Рекомендация для мобилки — единая функция разбора ошибки:
1. Есть `error.message` → показать его.
2. Иначе есть `detail` → показать его.
3. Иначе первый ключ-массив (`{field: [msg]}`) → `field: msg`.
4. Иначе — «Ошибка сети».

## HTTP-коды

| Код | Значение |
|---|---|
| `200` | OK |
| `201` | Создано (create) |
| `400` | Ошибка валидации / бизнес-правила (demo) |
| `401` | Токен протух → `refresh` |
| `403` | Недостаточно прав |
| `404` | Не найдено |
| `409` | Конфликт по времени (некоторые наши эндпоинты) |
| `502` | Шлюз не достучался до demo (upstream недоступен) |

## Единицы и форматы

- **Время** — ISO-8601 UTC, напр. `2026-06-11T09:00:00Z`. Показывай в локальной зоне.
- **Длительность** — целое число **минут**.
- **Координаты в geometry** — `[lng, lat]` (GeoJSON). Для карт переворачивай в `[lat, lng]`.
- **Расстояние** — метры (`distance_m`).

## Полная карта эндпоинтов (шпаргалка)

| Метод | Путь | Источник | Раздел |
|---|---|---|---|
| POST | `/auth/login/` | demo | [01](01-auth.md) |
| POST | `/auth/refresh/` | demo | [01](01-auth.md) |
| GET | `/auth/me/` | demo | [01](01-auth.md) |
| GET | `/car-orders/` | demo | [02](02-car-orders.md) |
| POST | `/car-orders/` | demo | [02](02-car-orders.md) |
| GET | `/car-orders/{id}/` | demo | [02](02-car-orders.md) |
| POST | `/car-orders/{id}/submit/` | demo | [02](02-car-orders.md) |
| POST | `/car-orders/{id}/admin-approve/` | demo | [02](02-car-orders.md) |
| POST | `/car-orders/{id}/reject/` | demo | [02](02-car-orders.md) |
| POST | `/car-orders/{id}/claim/` | demo | [02](02-car-orders.md) |
| POST | `/car-orders/{id}/complete/` | demo | [02](02-car-orders.md) |
| GET | `/car-orders/drivers/me/cars/` | demo | [02](02-car-orders.md) |
| GET | `/car-orders/car-types/` | demo | [02](02-car-orders.md) |
| POST | `/car-orders/estimate/` | локально | [03](03-scheduling-overlay.md) |
| GET·POST | `/car-orders/{id}/meta/` | локально | [03](03-scheduling-overlay.md) |
| POST | `/car-orders/{id}/claim-check/` | локально | [03](03-scheduling-overlay.md) |
| POST | `/car-orders/{id}/overlay-claim/` | локально | [03](03-scheduling-overlay.md) |
| POST | `/car-orders/{id}/trip-state/` | локально | [03](03-scheduling-overlay.md) |
| GET·POST | `/car-orders/{id}/live-location/` | локально | [04](04-live-tracking.md) |
| WS | `/ws/car-orders/{id}/location/` | локально | [04](04-live-tracking.md) |
