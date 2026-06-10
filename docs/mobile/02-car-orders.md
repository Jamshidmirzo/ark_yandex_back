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

Правила demo (могут вернуть ошибку):
- **Одна машина — один активный заказ.** Если у машины уже есть `in_progress`-заказ → `400` `"This car is not available."`
- **Один водитель — один активный заказ.** Если у водителя уже есть активный → ошибка.

Хочешь взять **второй заказ той же машиной последовательно** (по непересекающимся окнам) — НЕ через
demo-`claim`, а через наш `overlay-claim` (см. [03-scheduling-overlay.md](03-scheduling-overlay.md)).

## Машины водителя

`GET /car-orders/drivers/me/cars/` → машины, назначенные текущему водителю:
```json
[ { "id": 5, "model": "Cobalt", "plate_number": "01A777AA", "is_available": true } ]
```
Используй для модалки выбора машины при `claim`. `is_available: false` = машина сейчас занята
активным заказом (но её можно взять для последовательного заказа через `overlay-claim`).
