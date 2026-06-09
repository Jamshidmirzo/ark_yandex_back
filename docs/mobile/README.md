# API для мобильного приложения — обзор и подключение

Документация по разделам для интеграции мобильного клиента (Flutter / Kotlin / Swift)
с блоком «Заявки на машину».

## Разделы
1. [Подключение и авторизация](01-auth.md) — base URL, login, refresh, токены.
2. [Заявки на машину](02-car-orders.md) — список, деталь, создание, рабочий процесс (submit/approve/claim/complete).
3. [Расписание и оверлей](03-scheduling-overlay.md) — авто-расчёт маршрута, длительность, проверка окон, последовательные заказы, статусы поездки.
4. [Live-трекинг (REST + WebSocket)](04-live-tracking.md) — позиция водителя в реальном времени, маршрут на карте.
5. [Справочник](05-reference.md) — статусы, trip_state, формат ошибок, пагинация.

---

## Архитектура (важно понять перед интеграцией)

Мобилка обращается к **одному шлюзу (gateway)** — он сам решает, что обслужить локально, а что
проксировать на «большой» бэкенд `demo`:

```
  Mobile app ──HTTPS/WSS──▶  GATEWAY (этот сервис)
                               ├─ auth/*, car-orders (список/деталь/создание/
                               │   submit/approve/reject/claim/complete),
                               │   drivers/*, garage/*   ──proxy──▶  demo backend
                               └─ ФИЧИ (локально): estimate, meta, claim-check,
                                   overlay-claim, trip-state, live-location, WebSocket
```

- **Логин и базовые данные** (аккаунты, заявки, водители, машины) приходят с `demo`.
- **Новые фичи** (расчёт маршрута, длительность/окна, последовательные заказы одной машиной,
  этапы поездки, live-трекинг) обслуживаются **этим шлюзом локально**.
- Мобилке **не нужно** знать, что проксируется, а что нет — она всегда ходит на один base URL.

## Base URL

| Среда | HTTP base URL | WebSocket base |
|---|---|---|
| Dev (локально) | `http://127.0.0.1:8000/api/v1` | `ws://127.0.0.1:8000` |
| Прод | `https://<ваш-домен>/api/v1` | `wss://<ваш-домен>` |

> Все пути в документации указаны относительно HTTP base URL, например
> `POST /car-orders/{id}/claim/` = `http://127.0.0.1:8000/api/v1/car-orders/12/claim/`.

## Авторизация (коротко)

1. `POST /auth/login/` → получаешь `access` и `refresh`.
2. В каждый запрос добавляй заголовок: `Authorization: Bearer <access>`.
3. При `401` — обнови токен через `POST /auth/refresh/` и повтори запрос.

Подробно — в [01-auth.md](01-auth.md).

## Формат ответов и ошибок (коротко)

- Списки — пагинация DRF: `{count, next, previous, results: [...]}` (см. [05-reference.md](05-reference.md)).
- Ошибки шлюзовых фич: `{"error": {"code", "message", "details"}}`.
- Ошибки demo: DRF-формат — `{"detail": "..."}` или `{"field": ["..."]}`.

## Минимальный сценарий «водитель»

1. `POST /auth/login/` → токен.
2. `GET /car-orders/?status=awaiting_driver` → список доступных заявок.
3. `GET /car-orders/{id}/` → деталь (+ список свободных машин `available_vehicles`).
4. `POST /car-orders/{id}/claim/` `{car_id}` → принять заказ.
5. `POST /car-orders/{id}/trip-state/` `{trip_state: "to_client"}` → этап «выехал».
6. Подключиться к `ws://.../ws/car-orders/{id}/location/` → видеть себя/смотреть позицию на карте.
7. `POST /car-orders/{id}/complete/` → завершить.
