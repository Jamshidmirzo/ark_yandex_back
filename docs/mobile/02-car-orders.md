# 02 — Заявки на машину (базовый процесс)

Эти эндпоинты проксируются на `demo`-бэкенд. Базовый рабочий процесс заявки:

```
draft ──submit──▶ pending ──admin-approve──▶ awaiting_driver ──claim(car_id)──▶ in_progress ──complete──▶ completed
   └────────────────────── reject(reason) ──────────────────────▶ rejected
```

> Этапы поездки (выехал/на месте/ожидание) и последовательные заказы одной машиной —
> в [03-scheduling-overlay.md](03-scheduling-overlay.md). Live-позиция — в [04-live-tracking.md](04-live-tracking.md).

## Список заявок

`GET /car-orders/`

Query-параметры:
| Параметр | Пример | Описание |
|---|---|---|
| `status` | `awaiting_driver` | фильтр по статусу |
| `search` | `Темур` | поиск по адресу/проекту/заметке |
| `ordering` | `-created_at` | сортировка |
| `limit` / `offset` | `50` / `0` | пагинация |

Ответ `200` (пагинация):
```json
{
  "count": 12,
  "next": "http://.../car-orders/?limit=50&offset=50",
  "previous": null,
  "results": [ { /* CarOrder, см. ниже */ } ]
}
```

## Деталь заявки

`GET /car-orders/{id}/`

Ответ `200` — объект **CarOrder**:
```json
{
  "id": 88,
  "project_name": "Turandot Residences",
  "planned_datetime": "2026-06-11T09:00:00Z",
  "address": "Амира Тимура проспект, Ташкент",
  "note": "Забрать оборудование",
  "car_type": { "id": 4, "name": "Легковая" },
  "driver": { "id": 671, "name": "Иван Водитель" },
  "car": { "id": 5, "model": "Cobalt", "plate_number": "01A777AA" },
  "status": "awaiting_driver",
  "started_at": null,
  "finished_at": null,
  "created_by": { "id": 10, "name": "Диспетчер" },
  "created_at": "2026-06-10T08:00:00Z",
  "updated_at": "2026-06-10T08:00:00Z",
  "available_vehicles": [
    { "id": 5, "model": "Cobalt", "plate_number": "01A777AA" }
  ]
}
```

- `available_vehicles` приходит **только** на статусе `awaiting_driver` — это свободные машины,
  из которых водитель выбирает при приёме.
- Полный список статусов — в [05-reference.md](05-reference.md).

> **Деталь проксируется на `demo`.** `order_id` для этого GET бери из списка (`GET /car-orders/`)
> или из «Мои заказы» (`GET /car-orders/drivers/me/overlay-orders/` — [03 §3.8](03-scheduling-overlay.md));
> это id **реального demo-заказа**.
>
> Если деталь вернула **`404 NOT_FOUND`** (`No CarOrder matches the given query`) — заказа на `demo`
> уже нет (отклонён / снят / удалён). **Не показывай это как ошибку**: тихо убери заказ из «Мои заказы»
> /активного экрана и обнови список. Иначе водитель «зависнет» на пропавшем заказе (именно так ловится
> 404, когда заказ отклонили или он был только в нашем оверлее). Live-данные (координаты/этап) бери из
> наших локальных эндпоинтов (`/live-location/`, `/meta/` — [03](03-scheduling-overlay.md),
> [04](04-live-tracking.md)), а не из demo-детали.

## Создание заявки

`POST /car-orders/` (право `car_order:create`)

Обязательные поля: `project_name`, `planned_datetime` (ISO-8601 UTC), `address`, `car_type_id`.

> **`address`** — текстовый адрес **назначения** (куда нужна машина). Координаты точек на карте
> сюда НЕ кладутся — они идут в `meta` (`address_lat/lng` = назначение, `origin_lat/lng` = подача,
> см. [03](03-scheduling-overlay.md)).
```json
{
  "project_name": "Turandot Residences",
  "planned_datetime": "2026-06-11T09:00:00Z",
  "address": "Амира Тимура проспект, Ташкент",
  "note": "Забрать оборудование",
  "car_type_id": 4
}
```
Ответ `201` — созданная заявка в статусе `draft`.

> Координаты точек A→B и длительность demo **не хранит** — их надо отдельно сохранять в наш
> оверлей через `POST /car-orders/{id}/meta/` (см. [03](03-scheduling-overlay.md)), иначе маршрут и
> трекинг работать не будут.

## Типы машин (для выпадающего списка при создании)

`GET /car-orders/car-types/` → `[{ "id": 4, "name": "Легковая", ... }]` (массив или пагинация — нормализуй оба).

## Рабочие действия (workflow)

| Метод | Путь | Право | Эффект |
|---|---|---|---|
| POST | `/car-orders/{id}/submit/` | автор | `draft → pending` |
| POST | `/car-orders/{id}/admin-approve/` | `car_order:approve` | `pending → awaiting_driver` |
| POST | `/car-orders/{id}/reject/` | автор / `car_order:reject` | `→ rejected`, тело `{ "reason": "..." }` |
| POST | `/car-orders/{id}/claim/` | `driver:accept_order` | `awaiting_driver → in_progress`, тело `{ "car_id": 5 }` |
| POST | `/car-orders/{id}/complete/` | `driver:trip_control` | `in_progress → completed` |

Тело только у `reject` и `claim`, остальные — пустой `POST`. Все возвращают обновлённый CarOrder.

### Приём заказа (claim) — важно
`POST /car-orders/{id}/claim/` `{ "car_id": 5 }`

Правила (могут вернуть ошибку):
- **Один водитель — один активный заказ.** Если у водителя уже есть активный заказ → ошибка
  (demo: `claim`; наш слой: `overlay-claim` → `400 DRIVER_BUSY`). Второй заказ можно взять только
  после завершения текущего. Это правило едино для обоих путей (см. [03 §3.4](03-scheduling-overlay.md)).
- **Одна машина — один активный заказ** (demo): если у машины уже есть `in_progress` → `400`
  `"This car is not available."`

> Чаще всего вручную принимать не нужно — заказ назначает **сервер** (auto-dispatch) и он приходит
> в «Мои заказы» уже назначенным (см. [03 §3.8](03-scheduling-overlay.md)).

## Машины водителя

`GET /car-orders/drivers/me/cars/` → машины, назначенные текущему водителю:
```json
[ { "id": 5, "model": "Cobalt", "plate_number": "01A777AA", "is_available": true } ]
```
Используй для модалки выбора машины при `claim`. `is_available: false` = машина сейчас занята
активным заказом.

## Смена водителя (обязательно для авто-распределения)

Чтобы получать заказы, водитель должен **выйти на смену** — иначе авто-диспетчер его не видит
(нет кандидата по типу машины).

| Метод | Путь | Эффект |
|---|---|---|
| `GET` | `/car-orders/drivers/me/shift/` | текущая смена или `null` |
| `PATCH` | `/car-orders/drivers/me/shift/` | выйти на смену / сменить машину |
| `DELETE` | `/car-orders/drivers/me/shift/` | завершить смену |

**Выйти на смену** — `PATCH`:
```json
{ "driver_id": 670, "car_id": 5, "car_model": "Cobalt", "car_plate": "01A777AA",
  "car_type_id": 4, "car_type_name": "Легковая" }
```
- **`car_type_id` обязателен** (`400 VALIDATION` без него) — по нему диспетчер подбирает заказы.
- Идентификация — токеном; `driver_id` в теле — dev-фолбэк.
- **Сменить машину** = тот же `PATCH` с другим `car_id`. Заблокировано (`400 HAS_ACTIVE_ORDERS`),
  пока есть активные заказы — сначала заверши их.

**Завершить смену** — `DELETE` (с `?driver_id=` в dev). Заблокировано (`400 HAS_ACTIVE_ORDERS`),
если есть активный заказ — нельзя бросить заказ.

После выхода на смену + стрима GPS заказы приходят авто-назначенными в `/drivers/me/overlay-orders/`.
