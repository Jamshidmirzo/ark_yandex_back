# ARK Car-Orders — полная документация системы (архитектура, API, карты, интеграция)

> Один файл, который можно отдать другому разработчику целиком. Описывает **весь**
> контур «Заявки на машину»: бэкенд `ark_yandex`, веб-клиент, мобильное приложение,
> карты и геосервисы (какие, как работают, какие ключи нужны), потоки данных,
> протоколы (HTTP/WebSocket), права и пошаговый запуск.

---

## Оглавление

1. [Что это и зачем](#1-что-это-и-зачем)
2. [Состав системы (репозитории)](#2-состав-системы-репозитории)
3. [Технологический стек](#3-технологический-стек)
4. [Высокоуровневая архитектура](#4-высокоуровневая-архитектура)
5. [Два режима: gateway+overlay и standalone](#5-два-режима-gatewayoverlay-и-standalone)
6. [Поток запроса: как config/urls.py разводит](#6-поток-запроса-как-configurlspy-разводит)
7. [Gateway (реверс-прокси)](#7-gateway-реверс-прокси)
8. [Middleware](#8-middleware)
9. [Аутентификация и права](#9-аутентификация-и-права)
10. [Модель данных](#10-модель-данных)
11. [Единый «эффективный статус»](#11-единый-эффективный-статус)
12. [HTTP API (эндпоинты)](#12-http-api-эндпоинты)
13. [WebSockets](#13-websockets)
14. [Машина этапов поездки (trip-state)](#14-машина-этапов-поездки-trip-state)
15. [Авто-распределение и фоновые воркеры](#15-авто-распределение-и-фоновые-воркеры)
16. [🗺 Карты и геосервисы — ПОЛНОСТЬЮ](#16--карты-и-геосервисы--полностью)
17. [Клиентские приложения (web + mobile)](#17-клиентские-приложения-web--mobile)
18. [Внешние сервисы](#18-внешние-сервисы)
19. [Конфигурация (env-переменные)](#19-конфигурация-env-переменные)
20. [Локальный запуск](#20-локальный-запуск)
21. [Деплой](#21-деплой)
22. [Gotchas (что важно знать при интеграции)](#22-gotchas-что-важно-знать-при-интеграции)
23. [Карта файлов](#23-карта-файлов)

---

## 1. Что это и зачем

Система реализует блок **«Заявки на машину»** (car orders): сотрудник создаёт
заявку на машину (откуда → куда, тип машины, время), её согласуют, назначают
водителя (вручную или авто), и заказчик/диспетчер **видят машину на карте в
реальном времени** на всех этапах поездки.

`ark_yandex` — это центральный бэкенд этого блока. Он работает как **gateway +
overlay** над «настоящим» бэкендом ARK (`demo.ark.glob.uz`):

- **gateway** — прозрачно проксирует логин, пользователей, гараж и базовый CRUD
  заявок на upstream (там источник истины);
- **overlay** — сам обслуживает «живые» фичи (этапы поездки, live-карта,
  расписание, авто-распределение), храня их в локальной БД, привязанной к id
  заказа из demo.

Исторически это самостоятельный Django-блок, который позже вольётся в основной
CRM `ark-backend`, поэтому он повторяет его соглашения (JWT, `HasPermission`,
конверт ошибок, сервисный слой).

---

## 2. Состав системы (репозитории)

| Репозиторий | Что это | Технологии |
|---|---|---|
| **`ark_yandex`** | ЭТОТ бэкенд — gateway + overlay блока car-orders | Django 5.2, DRF, Channels |
| **`ark_yandex_front`** | Веб-клиент (диспетчер, форма заказа, карты) | React + Vite + TypeScript + antd + react-query + zustand |
| **`new_order/car_order`** | Мобильное приложение (водитель + заказчик) | Flutter |
| **`ark-backend`** | «Настоящий» бэкенд ARK = upstream/источник истины (`demo.ark.glob.uz`) | Django |

Клиенты (web + mobile) ходят **только** на `ark_yandex` (:8000), а он уже сам
решает — ответить локально или сходить на `ark-backend`.

---

## 3. Технологический стек

### Бэкенд (`ark_yandex`)

| Слой | Технология |
|---|---|
| Язык / рантайм | Python 3.13 |
| Web-фреймворк | Django 5.2 |
| API | Django REST Framework (DRF) |
| Аутентификация | `djangorestframework-simplejwt` (JWT, `Bearer`, HS256) |
| WebSockets / ASGI | Django **Channels** 4.3 + **daphne** 4.2 |
| Channel layer | InMemory (dev) → **Redis** (`channels_redis` + `redis 5.0.x`, RESP2) в проде |
| Фильтры | `django-filter`, DRF Search/Ordering |
| CORS | `django-cors-headers` |
| Конфигурация | `django-environ` (`.env` + переменные окружения) |
| БД | SQLite (dev) → **PostgreSQL** (`psycopg`) в проде |
| HTTP-клиент к upstream | `requests` (пул keep-alive) |
| Маршруты (routing) | внешний **OSRM** |
| Геокодинг | внешний **Nominatim** (OSM) |
| Прод-сервер | `gunicorn` (WSGI) / `daphne` (ASGI для WS) |

### Веб-клиент (`ark_yandex_front`)

React 19 + Vite + TypeScript + Ant Design + `@tanstack/react-query` + `zustand`.
Карты — **Yandex Maps JS API 2.1**, плюс `leaflet` (legacy bootstrap).

### Мобильное приложение (`new_order/car_order`)

Flutter. Карты — **Yandex MapKit (native SDK)** `yandex_maps_mapkit`,
геолокация — `geolocator`.

---

## 4. Высокоуровневая архитектура

```
                ┌──────────────────────────────────────────────────────────┐
   Клиенты      │  Web (React/Vite, ark_yandex_front, :5173)               │
   ──────────►  │  Mobile (Flutter, new_order/car_order)                   │
                │  — оба ходят ТОЛЬКО на ark_yandex (:8000)                │
                └───────────────┬──────────────────────────────────────────┘
                                │ HTTP /api/v1/*  +  WebSocket /ws/*
                                ▼
        ┌───────────────────────────────────────────────────────────────┐
        │  ark_yandex  (Django + DRF + Channels)    ← ЭТОТ проект        │
        │                                                               │
        │  config/urls.py — разводящий маршрутов:                       │
        │   1) локальные фича-вьюхи  car_orders.views.*                 │
        │      (overlay: этапы, live-карта, расписание, авто-диспетч.)  │
        │   2) car_order_proxy — список/деталь demo + наш статус        │
        │   3) gateway (catch-all) — прозрачный реверс-прокси           │
        │                                                               │
        │  Локальное overlay-хранилище: OrderMeta, OrderLiveLocation,   │
        │  DriverPosition, DriverShiftState … (SQLite / Postgres)       │
        └───────┬───────────────────┬───────────────────┬───────────────┘
                │ server-to-server  │ routing           │ geocoding
                ▼                   ▼                   ▼
   ┌────────────────────┐  ┌──────────────────┐  ┌──────────────────┐
   │ ark-backend        │  │ OSRM             │  │ Nominatim (OSM)  │
   │ (demo.ark.glob.uz) │  │ маршруты A→B     │  │ адрес ↔ коорд.   │
   │ UPSTREAM_API_BASE  │  └──────────────────┘  └──────────────────┘
   │ auth, users,       │
   │ гараж, заявки      │
   └────────────────────┘

   Карты рисуются НА КЛИЕНТЕ:
     · Web    → Yandex Maps JS API 2.1 (тайлы/пробки/метки в браузере)
     · Mobile → Yandex MapKit native SDK (тайлы/маршрут/поиск на устройстве)
```

Ключевая идея: **браузер/телефон общается только с `ark_yandex`** (поэтому наш
CORS и наши фичи всегда применяются). Тайлы карты клиенты тянут напрямую от
Yandex; бэкенд карты не рендерит — он отдаёт только координаты и геометрию
маршрута (`[[lng, lat], …]`).

---

## 5. Два режима: gateway+overlay и standalone

Один и тот же код, разница — отвечает ли upstream:

1. **Gateway + overlay (рабочий demo-режим).** `auth/`, гараж, базовый CRUD
   заявок проксируются на `UPSTREAM_API_BASE` (demo — источник истины), фичи
   живут локально в `OrderMeta` (ключ — id заказа из demo). **Никогда не ломать
   этот прокси** — на нём держатся логин и данные.

2. **Standalone (тесты / автономная разработка).** Локальные приложения
   `auth_core` + `car_orders` могут обслуживать всё сами, без upstream. Так
   гоняются юнит-тесты сервисного слоя (`car_orders/tests/`).

Переключение — наличие живого `UPSTREAM_API_BASE` и порядок маршрутов в
`config/urls.py` (локальные пути стоят ПЕРЕД catch-all gateway).

---

## 6. Поток запроса: как config/urls.py разводит

Маршруты резолвятся **сверху вниз**, порядок критичен:

1. **Конкретные фича-роуты** (`/car-orders/estimate/`, `/geocode/`,
   `/templates/`, `/fleet/live/`, `/me/active-order/`, `/drivers/...`,
   `/{id}/trip-state/`, `/{id}/meta/`, `/{id}/overlay-claim/`, `/{id}/no-show/`,
   `/{id}/extend/`, `/{id}/reassign/`…) → **локальные** вьюхи `car_orders.views`.
   Их нет на demo (там было бы 404), поэтому стоят до catch-all.

2. **Хуки на demo-действия** — `/{id}/admin-approve/`, `/{id}/reject/`:
   вьюха **сначала проксирует** на demo, **затем** правит overlay
   (`OrderMeta.dispatchable` / сносит overlay), чтобы авто-диспетчер увидел
   согласованный/отклонённый заказ.

3. **`car_order_proxy`** (`/car-orders/`, `/car-orders/{id}/`) — проксирует
   demo-список и деталь, **но на GET вписывает наш `effective_status`** (единый
   источник истины по статусу). Не-GET проходит насквозь.

4. **`gateway` (catch-all `^api/v1/(?P<path>.*)$`)** — всё остальное
   (`auth/*`, гараж, профиль, уведомления…) **прозрачно проксируется** на
   `UPSTREAM_API_BASE`. Стоит последним.

```
запрос /api/v1/...
   ├─ совпал с фича-роутом?           → локальная вьюха (overlay)
   ├─ admin-approve / reject?         → проксировать на demo + поправить overlay
   ├─ /car-orders/ или /{id}/ (GET)?  → car_order_proxy (demo + наш effective_status)
   └─ иначе                           → gateway → UPSTREAM_API_BASE (demo)
```

---

## 7. Gateway (реверс-прокси) — `config/gateway.py`

- **Один пул соединений на процесс** (`requests.Session` + `HTTPAdapter`,
  keep-alive): рукопожатие DNS+TCP+TLS к upstream платится один раз.
- **Cookies заблокированы** (`_BlockAllCookies`): сессия общая на всех вызывающих,
  хранить `Set-Cookie` нельзя (утечёт). Авторизация пробрасывается явно через
  `Authorization: Bearer`.
- **Проброс заголовков**: входящие заголовки клиента форвардятся как есть (кроме
  `host`, `content-length`, `connection`, `accept-encoding`). Из ответа upstream
  вырезаются hop-by-hop и CORS-заголовки (CORS ставит наш `corsheaders`).
- **Ретрай только на сброс соединения** (`ConnectionError`, до 2 раз с бэкоффом):
  устаревший keep-alive сокет роняет первый запрос, но он ещё не дошёл до upstream
  → переслать безопасно. Реальный ответ / read-timeout не ретраятся.
- **Ошибка upstream → 502** в конверте:
  `{"error": {"code": "UPSTREAM_UNREACHABLE", "message", "details": {"upstream"}}}`.
- **Таймауты** `UPSTREAM_TIMEOUT = (connect, read)`: connect короткий (быстро
  падаем на мёртвом), read щедрый (медленный-но-живой эндпоинт не режем).

---

## 8. Middleware — `config/middleware.py`

1. **`MobileLanguagePrefixMiddleware`** — снимает ведущий `/<lang>/`, чтобы
   мобильная схема demo `/<lang>/api/v1/...` (и `/<lang>/healthcheck/`)
   резолвилась так же, как веб-схема `/api/v1/...`. Стрипается только перед
   реальными путями. Веб-клиент шлёт без префикса — для него no-op. Должен идти
   **до резолва URL**.

2. **`RequestLogMiddleware`** — логирует **каждый** `/api/v1/*` и `/health`
   запрос с источником (`📱 <ip>` телефон / `🖥 локально`) и русской подписью
   действия (таблица `_ACTIONS`). Управляется `LOG_TRACKING` (по умолчанию on).

---

## 9. Аутентификация и права

### 9.1 JWT

- DRF по умолчанию: `JWTAuthentication` + `IsAuthenticated`.
- Токен: `Authorization: Bearer <access>`. Access ≈ 24 ч (`JWT_ACCESS_HOURS`),
  refresh 7 дней, ротация включена, HS256 — форма как у `ark-backend.auth_core`.
- **В gateway-режиме логин не наш**: `auth/login/`, `auth/refresh/`, `auth/me/`
  проксируются на demo (источник истины по пользователям).

### 9.2 Права (standalone) — `car_orders/permissions.py`, `auth_core`

Кодовые имена совпадают с `ark-backend` (сидятся миграцией
`auth_core/migrations/0002_seed_permissions.py`):

```
car_order: create | list_own | list | approve | reject | dispatch
driver:    accept_order | trip_control | list | assign_to_user
garage:    list | retrieve | create | update | delete
vehicle_report: create | list_own | list | retrieve
administrator
```

Иерархия (`auth_core.permissions.expand_permission_codename`):
`administrator ⊇ всё`, `X ⊇ X_own`, `X_all ⊇ X`. Группы: **Car Requester,
Car Admin, Driver, Garage Manager, Administrator**. Гейт во вьюхах —
`HasPermission("codename")`. Та же иерархия повторена в вебе
(`utils/permissionHierarchy.ts`).

### 9.3 Overlay-аутентификация (`REQUIRE_OVERLAY_AUTH`)

- **OFF (dev, по умолчанию)** — overlay-эндпоинты доверяют `driver_id` из тела и
  показывают всю доску. Удобно для разработки.
- **ON (прод)** — `config.auth.DemoTokenAuthentication` валидирует demo-bearer
  через demo `/auth/me/` и берёт водителя оттуда.

> ⚠️ Settings громко предупреждают (`RuntimeWarning`), если в проде
> (`DEBUG=False`) остался `REQUIRE_OVERLAY_AUTH=False` (IDOR, finding H5) или
> SQLite (`select_for_update()` — no-op, claim-гонки не защищены, C2).

---

## 10. Модель данных

Модели — `car_orders/models.py`. Две группы.

### 10.1 «Родные» модели (источник истины в standalone; в gateway — на demo)

| Модель | Назначение |
|---|---|
| `CarType` | Тип машины |
| `Car` | Машина (модель, гос-номер, тип, водители m2m, статус); менеджер аннотирует `is_available` |
| `CarOrder` | Заявка. Статусы `draft → pending → awaiting_driver → scheduled → in_progress → completed` + `rejected`/`cancelled`. Координаты A→B, расписание (`planned_datetime`, `estimated_duration`, `service_time`, `latest_start`, derived `planned_end`) |
| `CarOrderActivity` | Аудит переходов состояния |
| `DriverShift` | Смена водителя на ОДНОЙ машине (Р1) + последняя GPS (Р3). Partial-unique: одна активная смена на водителя и на машину |
| `VehicleReport` | Суточный отчёт о состоянии машины |

### 10.2 Overlay-модели (всегда локальные — чего нет в demo)

Ключуются по **id сущности из demo** (`order_id`/`driver_id`), не FK — сама
сущность живёт на upstream.

| Модель | Назначение |
|---|---|
| **`OrderMeta`** | Главный overlay заказа: координаты A→B, плановое окно, `trip_state`, `overlay_claimed`, `dispatchable`, `is_urgent`, `car_type_id`, снапшот водителя (`driver_name/phone/car_label`), `author_id`, «туда-обратно» (`has_return`, `return_lat/lng`, `returning`), таймеры (`search_started_at`, `arrived_at`), `excluded_driver_ids`, снапшоты адресов/полей (`origin_address`, `dest_address`, `project_name`…) |
| `OrderLiveLocation` | Живая позиция по заказу + геометрия маршрута (для карты) |
| `DriverPosition` | Последняя GPS **на водителя** (heartbeat шлётся даже когда свободен — чтобы найти ближайшего); опц. `heading` |
| `DriverShiftState` | Overlay «водитель на смене» (у demo нет set-shift): какая машина + её тип |
| `CarOrderTemplate` | Переиспользуемая «заготовка» заказа (префилл формы) |
| `DispatchSettings` | Синглтон (`pk=1`): live on/off авто-распределения |

---

## 11. Единый «эффективный статус» — `car_orders/services/status.py`

Главная тонкость данных: у заказа **два хранилища состояния** — demo
`CarOrder.status` и наш `OrderMeta` (`trip_state`/`overlay_claimed`/
`dispatchable`). Overlay-claimed заказ **намеренно** держит demo-статус
`awaiting_driver`, поэтому реальный статус нужно **примирять**:

`effective_status(demo_status, meta)` — единственный источник истины:

- overlay-claimed + `trip_state=completed` → `completed`;
- overlay-claimed + активный этап → `in_progress` (если demo не терминальный);
- direct-created (`dispatchable`, ещё `draft/pending` на demo) → `awaiting_driver`;
- иначе авторитетен demo-статус.

`status_map_for(order_ids)` достаёт `id → demo status` одним запросом (без N+1)
для overlay-only фидов. **Клиенты обязаны повторять ту же логику** (web
`utils/orderStatus.ts`), иначе активная поездка покажет «Ожидает водителя».
Поэтому `car_order_proxy` вписывает `effective_status` прямо в проксируемый ответ.

---

## 12. HTTP API (эндпоинты)

База: `http://<host>:8000/api/v1/`. Мобилка может слать `/<lang>/api/v1/...` —
префикс снимается middleware. Конверт ошибок везде:
`{"error": {"code", "message", "details"}}`.

### 12.1 Auth (проксируется на demo в gateway-режиме)

| Метод | Путь | Тело | Ответ |
|---|---|---|---|
| POST | `auth/login/` | `{username, password}` | `{access, refresh, user}` |
| POST | `auth/refresh/` | `{refresh}` | `{access[, refresh]}` |
| GET | `auth/me/` | — | `{id, name, username, is_superuser, permissions[]}` |
| GET | `auth/me/permissions/` | — | `{permissions: [...]}` |

### 12.2 Заявки — локальные overlay/экшены (`/api/v1/car-orders/...`)

| Метод | Путь | Эффект |
|---|---|---|
| POST | `estimate/` | маршрут A→B → `{distance_m, drive_minutes, service_minutes, duration_minutes, geometry:[[lng,lat]…], source}` |
| GET | `geocode/` | серверный прокси адресного поиска/обратного геокодинга (`?q=` или `?lat=&lng=`) |
| GET·POST·… | `templates/`, `templates/{pk}/` | CRUD «заготовок» заказов |
| GET | `fleet/live/` | снимок диспетчерской: каждый активный заказ + live-позиция + маршрут |
| GET | `me/active-order/` | текущая поездка вызывающего (или `null`) |
| GET | `drivers/me/overlay-orders/` | «мои заказы» водителя в нашем слое |
| POST | `drivers/me/location/` | GPS-heartbeat `{lat, lng[, heading]}` (питает live-карту и подбор ближайшего) |
| GET | `drivers/positions/` | позиции всех водителей (для ранжирования по дистанции) |
| GET·PATCH·DELETE | `drivers/me/shift/` | читать / выйти на смену (`{car_id}`) / завершить смену (Р1) |
| GET | `drivers/shifts/` | список активных смен |
| GET·POST | `auto-dispatch/` | прочитать / переключить live-флаг авто-распределения |
| GET·POST | `{id}/live-location/` | читать / писать live-позицию заказа |
| GET·POST | `{id}/meta/` | читать / upsert overlay (координаты, окно, return…) |
| POST | `meta-batch/` | overlay для списка заказов пачкой |
| POST | `{id}/trip-state/` | `{trip_state}` → сменить этап (пуш по WS + тосты водителю И заказчику) |
| POST | `{id}/overlay-claim/` | взять/назначить в нашем слое → `{ok, conflict, meta}` |
| POST | `{id}/overlay-release/` | вернуть заказ в очередь / снести overlay |
| POST | `{id}/claim-check/`, `claim-check-batch/` | `{driver_id}` → `{ok, conflict}` предпроверка окна |
| POST | `{id}/no-show/` | «клиент не вышел» — отменить заказ `at_client` |
| POST | `{id}/reassign/` | диспетчер снимает заказ с водителя |
| POST | `{id}/extend/` | `{minutes}` → продлить окно → `{ok, meta, conflict}` |

### 12.3 Заявки — хуки и прокси

| Метод | Путь | Эффект |
|---|---|---|
| POST | `{id}/admin-approve/` | проксирует на demo + ставит `OrderMeta.dispatchable=True` |
| POST | `{id}/reject/` | проксирует на demo + сносит overlay |
| GET | `car-orders/`, `car-orders/{id}/` | проксирует demo список/деталь + вписывает `effective_status` |
| (любой) | прочее `/api/v1/*` | прозрачный gateway на demo |

---

## 13. WebSockets

Mount: `config/asgi.py` → `car_orders.ws.websocket_urlpatterns`. Пути допускают
необязательный `/<lang>/` и `api/v1/` префикс и опциональный завершающий слэш.

| Путь | Назначение | Направление |
|---|---|---|
| `ws/driver/track/` | водитель стримит GPS, получает маркер + полилинию ноги | uplink (bidirectional) |
| `ws/order/{order_id}/track/` | смотреть live-позицию/маршрут/этап одного заказа | downlink |
| `ws/fleet/track/` | фид всей фабрики для диспетчера | downlink |
| `ws/notify/{user_id}/` | тосты-уведомления пользователю (водителю И заказчику) | downlink |

**Старые алиасы** (`ws/drivers/me/location/`, `ws/car-orders/{id}/location/`,
`ws/car-orders/fleet/`, `ws/notifications/{user_id}/`) сохранены. Неизвестный
WS-путь ловит `FallbackConsumer` и тихо закрывает (без reconnect-петли).

Серверные хелперы (`car_orders/ws/groups.py`): `broadcast_location`,
`notify_order_status`, `notify_user`. Рассылки обёрнуты в `transaction.on_commit`
(события не уходят раньше коммита), `group_send` безопасен к падению Redis.

**Что именно течёт по WS:** живая позиция водителя (`lat/lng`), полилиния
текущей ноги маршрута, смена `trip_state`, флаг `returning`, тосты-уведомления.
`DriverLocationConsumer` на каждый фикс пересчитывает маршрут (через OSRM) и
проецирует точку на дорогу (snap-to-route).

> Channel layer: InMemory в dev (теряет группы на рестарте, не переживает
> мультипроцесс). В проде **обязателен Redis** (`REDIS_URL`). Версия `redis-py`
> запинена на `5.0.x` (RESP2) — на `8.x` (RESP3) `channels_redis` 4.x ломается и
> рвёт все WS (connect→disconnect-петля).

---

## 14. Машина этапов поездки (trip-state) — `car_orders/services/trip_state.py`

```
assigned → to_client → at_client → in_trip → at_destination → completed
                                                   ↘ (туда-обратно)
            + waiting (пауза)        + cancelled (отмена/возврат)
```

- **Геозона прибытия (100 м).** Кнопки «Я на месте»/«Прибыли» разблокируются
  только в радиусе `CAR_ORDER_ARRIVAL_GEOFENCE_M` от точки и при свежем GPS
  (`CAR_ORDER_GPS_FRESH_S`). Гейт на клиенте; бэк по дистанции не режет.
- **Туда-обратно — ОДИН заказ.** `has_return` + `return_lat/lng` + `returning`.
  Времени возврата нет; возврат стартует водитель вручную: `at_destination` →
  «Выехать обратно» → `destination → return point` → «Завершить».
- **Одна активная «едущая» стадия за раз.** `TripStateView` блокирует старт
  второй движущейся стадии (`to_client`/`in_trip`), пока другой заказ едет.
  Пересечение расписаний — **предупреждение**, не блок (простой на съёмке
  «бесплатен»: `scheduling.meta_conflict`).

Машина централизована (`advance()`/`validate()`/`can_transition()`), ошибки —
`TripStateError(code, msg, http_status)`. `TripStateView` — тонкий адаптер.

---

## 15. Авто-распределение и фоновые воркеры

### 15.1 Авто-диспетчер — `manage.py auto_dispatch`

Назначает ожидающие заказы ближайшему свободному водителю **server-side** (без
открытой вкладки диспетчера). Активен, только если включён И env
`AUTO_DISPATCH_ENABLED`, И DB-флаг `DispatchSettings.auto_enabled`
(`dispatch.auto_enabled`). Рассматривает только `dispatchable` + без водителя
заказы. Логика — `car_orders/dispatch.py`, `car_orders/fleet.py`.

Параметры (env): `AUTO_DISPATCH_LEAD_MIN` (45), `AUTO_DISPATCH_STALE_SEC` (180),
`AUTO_DISPATCH_POS_MAX_AGE` (180), `CAR_ORDER_ABANDON_SEC` (реап брошенного пина).

### 15.2 Management-команды (`car_orders/management/commands/`)

| Команда | Назначение |
|---|---|
| `auto_dispatch` | серверное авто-распределение |
| `auto_simulate` | гнать live-позицию каждого активного заказа по фазам (тест без телефона; **без `--loop`**) |
| `remind_departures` | нуджи «пора выезжать» |
| `seed_driver_positions` | разбросать/дрейфить GPS свободных водителей |
| `order_watchdog` | сторож зависших заказов |
| `reap_overlay_orphans` | чистка осиротевших overlay-записей |
| `simulate_driver` / `simulate_location` | старый standalone-симулятор |

---

## 16. 🗺 Карты и геосервисы — ПОЛНОСТЬЮ

Это самая частая зона вопросов при интеграции, поэтому подробно.

### 16.1 Кто что рисует (обзор)

| Где | Движок карты | Что делает | Ключ |
|---|---|---|---|
| **Веб-клиент** | **Yandex Maps JS API 2.1** | тайлы, метки, маршрут-полилиния, слой пробок | JS-ключ `VITE_YANDEX_MAPS_KEY` (клиентский, в браузере) |
| **Веб (legacy)** | **Leaflet** (`leafletSetup.ts`) | бутстрап маркеров OSM-тайлов; текущие карты-компоненты используют Yandex | — |
| **Мобилка** | **Yandex MapKit (native SDK)** | тайлы, маршрут (DrivingRouter), reverse-geocode (SearchManager) | MapKit API key (нативный, в проекте Android/iOS) |
| **Бэкенд** | — (карты не рендерит) | только считает маршрут и геокодит | OSRM/Nominatim URL'ы |

**Важно:** тайлы и сам рендер карты — всегда **на клиенте** (браузер тянет от
Yandex, телефон — через MapKit SDK). Бэкенд возвращает только **координаты** и
**геометрию маршрута** в формате GeoJSON `[[lng, lat], …]`. Клиенты переворачивают
в `[lat, lng]` (`toLatLng()` есть и в `yandex.ts`, и в `leafletSetup.ts`).

### 16.2 Веб: Yandex Maps JS API 2.1

- **Загрузка** (`src/components/map/ymapsLoader.ts`): один раз подгружает скрипт
  `https://api-maps.yandex.ru/2.1/?apikey=<KEY>&lang=ru_RU`, резолвит глобальный
  `ymaps`. React-обёртки нет (надёжнее под React 19).
- **Ключ** (`src/components/map/yandex.ts`): `VITE_YANDEX_MAPS_KEY` из
  `.env.local`, есть дефолтный hardcoded-ключ для dev. JS-ключ **публичный** (по
  дизайну виден в браузере) — для прода завести свой в Кабинете разработчика
  Яндекса и ограничить по HTTP-referer.
- **Компоненты карты** (`src/components/map/`):
  - `LiveTrackingMap.tsx` — отслеживание одной поездки (метка водителя
    `islands#blueAutoIcon`, A→B-полилиния, метки 🟢/🔴).
  - `FleetMap.tsx` — диспетчерская: все машины + их маршруты.
  - `PointPickerMap.tsx` — выбор точки на карте в форме заказа (клик → координаты).
- **Слой пробок** (`addTraffic()` в `yandex.ts`): `TrafficControl` с
  `traffic#actual`; если ключ без слоя пробок — тихо отключается.
- **Регион/центр** (`yandex.ts`): по умолчанию **Ташкент** `[41.311081,
  69.240562]`. Override через env:
  - `VITE_FLEET_CENTER="lat,lng"` — центр карты;
  - `VITE_FLEET_RADIUS_DEG` (≈5° ≈ 550 км) — «в регионе» для валидации геокодера;
  - `VITE_FLEET_FIT_RADIUS_DEG` (≈1.5°) — радиус кадрирования всей фабрики.

### 16.3 Веб: геокодинг (поиск адреса + обратный) — `src/components/map/geocode.ts`

Трёхуровневая стратегия, чтобы поиск **всегда работал и оставался бесплатным**:

1. **Yandex JS-геокодер** (`ymaps.geocode`) — первичный, регион-смещённый
   (`boundedBy`). НО: бандловый JS-ключ обычно **не включает** геокодер →
   `ymaps.geocode` кидает ошибку; тогда на сессию помечаем `yandexGeocoderBroken`
   и идём дальше.
2. **Бэкенд-прокси** `GET /api/v1/car-orders/geocode/` → **Nominatim (OSM)**
   (см. 16.5). Браузеру **нельзя** ходить в OSM напрямую (бан 429).
3. Результаты фильтруются по региону (`inRegion`) — матч в другом
   городе/континенте отбрасывается (лучше ничего, чем точка в Америке).

**Yandex Suggest (подсказки по мере ввода, `ymaps.suggest`)** — отдельный
**платный** продукт (нужен `suggest_apikey`). По умолчанию **выключен**
(`VITE_YANDEX_SUGGEST_ENABLED` не задан) — подсказки идут бесплатно через
geocode-fallback. Включать осознанно.

### 16.4 Мобилка: Yandex MapKit (native SDK)

- Зависимость `yandex_maps_mapkit: ^4.38.1` (full SDK). Нужен **MapKit API key**,
  прописанный в нативной части Android/iOS (это **не** тот JS-ключ, что в вебе —
  отдельный ключ MapKit Mobile SDK).
- **Маршрут** (`car_order_request/data/route_service.dart`): native
  `DrivingRouter` (`DirectionsFactory…createDrivingRouter`) рисует дорожную
  геометрию + ETA (traffic-aware). Только Android/iOS; на web/desktop/в тестах
  возвращает `null`, и клиент падает на прямую линию или на бэкендовый OSRM
  `estimate` (для пред-claim превью).
- **Обратный геокодинг** (`car_order_request/data/reverse_geocoder.dart`): native
  `SearchManager` (`SearchType.Geo`, `zoom: 18`) — координата → адрес для «выбрать
  на карте».
- **GPS** — пакет `geolocator`; позиция шлётся на бэк heartbeat'ом
  (`POST /drivers/me/location/`) и/или по WS `ws/driver/track/`.

### 16.5 Бэкенд: маршруты (OSRM) — `car_orders/services/routing.py`

- `estimate_route(...)` зовёт **OSRM** (`CAR_ORDER_OSRM_URL`,
  `/route/v1/driving/{lng,lat;lng,lat}`), возвращает
  `{distance_m, duration_s, geometry:[[lng,lat]…], source:"osrm"}`.
- **Фолбэк** — прямая линия (haversine, средняя скорость 30 км/ч),
  `source:"haversine"`. Фолбэк-геометрия **не отдаётся клиенту** в `/estimate/`
  (иначе нарисует линию сквозь дома) — отдаём пустую геометрию, клиенты рисуют
  «только пины». Фолбэк **никогда не кешируется**.
- **Кеш**: успешные хиты OSRM мемоизируются в процессе на
  `CAR_ORDER_ROUTE_CACHE_TTL` (180 с; 0 = off). Ключ включает базовый URL OSRM и
  стартовый `bearing` (чтобы живая нога с моторного фикса не переиспользовала
  маршрут другого направления).
- **Ретраи**: 3 попытки с бэкоффом (публичный OSRM-demo лимитирует/роняет).
- **Snap to road / встречка**: для живой ноги (старт с движущейся позиции
  водителя) передаётся `bearings` (`CAR_ORDER_OSRM_BEARING_RANGE`, 90°), чтобы
  OSRM привязал старт к нужной (не встречной) полосе.
- По умолчанию это публичный demo-сервер OSRM. **В проде** — поднять свой OSRM
  (снять лимиты) или переключить routing на **Yandex Router API**.

### 16.6 Бэкенд: геокодинг (Nominatim) — `car_orders/services/geocode.py`

- `GET /api/v1/car-orders/geocode/?q=<текст>` → поиск →
  `{results:[{lat,lng,label}]}`; `?lat=&lng=` → обратный → `{label}`.
- Ходит в **Nominatim** (`CAR_ORDER_NOMINATIM_URL`, по умолчанию публичный OSM):
  правильный `User-Agent` (`CAR_ORDER_GEOCODER_USER_AGENT`), throttle **1 req/s**,
  кеш на сутки (`CAR_ORDER_GEOCODE_CACHE_TTL`), регион-смещение (`bounded=1` +
  пост-фильтр `inRegion` вокруг центра фабрики).
- Зачем прокси: браузеру нельзя бить OSM напрямую (бан 429). В проде — поднять
  свой Nominatim (снять лимит).

### 16.7 Навигация: deep-links в внешние навигаторы — `src/utils/navLinks.ts`

Кнопка «Навигация» у водителя открывает внешнее приложение по route A→B:
**Яндекс Навигатор**, **Яндекс Карты**, **2GIS**, **Google Maps**, **Apple Maps**
(iOS). Тонкость порядка координат: Yandex/Google/Apple — `lat,lng`, а **2GIS —
`lng,lat`** (перевёрнуто). Сначала пробуется app-scheme (`yandexnavi://`,
`dgis://`…), затем веб-fallback (`yandex.ru/maps`, `2gis.ru`, `google.com/maps`).

### 16.8 Сводка: какой ключ где нужен

| Ключ | Где задаётся | Для чего | Прод-замечание |
|---|---|---|---|
| `VITE_YANDEX_MAPS_KEY` | веб `.env.local` | Yandex Maps JS API (тайлы/метки/пробки) | завести свой, ограничить по referer |
| Yandex Suggest key (`suggest_apikey`) | веб (опц.) | платные подсказки адреса | по умолчанию off, включать осознанно |
| MapKit Mobile SDK key | нативный Android/iOS проект мобилки | Yandex MapKit (карта/маршрут/поиск на телефоне) | отдельный от JS-ключа |
| `CAR_ORDER_OSRM_URL` | бэкенд env | маршруты A→B | свой OSRM или Yandex Router API |
| `CAR_ORDER_NOMINATIM_URL` + `CAR_ORDER_GEOCODER_USER_AGENT` | бэкенд env | геокодинг-прокси | свой Nominatim |

---

## 17. Клиентские приложения (web + mobile)

### 17.1 Веб (`ark_yandex_front`)

- **Стек**: React 19 + Vite + TypeScript + Ant Design + react-query + zustand.
- **API base**: `VITE_API_BASE_URL` (по умолчанию `http://localhost:8000/api/v1`).
- **Auth**: login → хранит `access`/`refresh` (zustand `authStore`,
  `ark-yandex-auth`), шлёт `Authorization: Bearer`, на 401 — `auth/refresh/`.
- **Видимость UI** — по `auth/me/` `permissions[]` (+ иерархия
  `utils/permissionHierarchy.ts`, зеркало бэкенда).
- **Драйвер GPS-uplink** (`hooks/useGpsUplink.ts`): пока водитель на смене, 1 Гц
  шлёт позицию по WS — это делает live-трекинг рабочим и из браузера.
- **Страницы**: форма заказа (`pages/car-orders/CarOrderFormPage.tsx`),
  диспетчерская (FleetLivePage / «Диспетчерская»), отслеживание поездки.
- **Статус заказа** считается через `utils/orderStatus.ts` (`effectiveStatus`) —
  то же правило, что на бэке (см. §11).

### 17.2 Мобильное приложение (`new_order/car_order`, Flutter)

- **База URL** настраиваемая (`core/constants/app_constants.dart`,
  `core/hosts/endpoints.dart`). На Android-эмуляторе хост `:8000` доступен как
  **`10.0.2.2`**. Это то же приложение, что и attendance — car-orders один из
  модулей.
- **Схема путей** — язык-префиксная `/<lang>/api/v1/...` (напр. `/ru/...`); на
  бэке `MobileLanguagePrefixMiddleware` снимает префикс. То есть это **не**
  отдельный бэкенд — тот же `ark_yandex`.
- **WS** — live-позиция/этапы/уведомления (требует токен в WS-запросе).
- **Карты** — Yandex MapKit native (см. §16.4); водителю — уведомление о
  назначении (WS `ws/notify/<user_id>/`; killed-app требует FCM, пока WS-only).

---

## 18. Внешние сервисы

| Сервис | Через что | Зачем |
|---|---|---|
| **ark-backend** (`demo.ark.glob.uz`) | `config/gateway.py` (`requests`) | источник истины: auth, пользователи, гараж, базовый CRUD заявок |
| **OSRM** (`CAR_ORDER_OSRM_URL`) | `services/routing.py` | расчёт маршрута A→B; кеш; фолбэк haversine |
| **Nominatim** (`CAR_ORDER_NOMINATIM_URL`) | `services/geocode.py` | геокодинг (серверный прокси `/geocode/`) |
| **Yandex Maps JS API** | веб-браузер напрямую | рендер карты в вебе |
| **Yandex MapKit SDK** | мобилка напрямую | рендер карты/маршрут/поиск в мобилке |
| **Redis** (`REDIS_URL`) | `channels_redis` | channel layer для WS в проде |

---

## 19. Конфигурация (env-переменные)

### Бэкенд (`ark_yandex`, `.env`)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `SECRET_KEY` | dev-insecure | секрет Django (обязателен в проде) |
| `DEBUG` | `False` | режим отладки |
| `ALLOWED_HOSTS` | `127.0.0.1,localhost` | разрешённые хосты |
| `DATABASE_URL` | SQLite-файл | БД (прод — `postgres://...`) |
| `REDIS_URL` | пусто (InMemory) | channel layer для WS |
| `UPSTREAM_API_BASE` | `http://host.docker.internal:12001/ru/api/v1` | база upstream (demo), язык-префиксная |
| `UPSTREAM_CONNECT_TIMEOUT` / `UPSTREAM_READ_TIMEOUT` | `5.0` / `120.0` | таймауты прокси |
| `REQUIRE_OVERLAY_AUTH` | `False` | мост overlay→demo-токен (**вкл. в проде!**) |
| `CORS_ALLOWED_ORIGINS` | `localhost:5173`,`127.0.0.1:5173` | CORS для веб-фронта (в DEBUG ещё LAN) |
| `JWT_ACCESS_HOURS` | `24` | TTL access-токена |
| `CAR_ORDER_OSRM_URL` | публичный OSRM | routing-движок |
| `CAR_ORDER_ROUTE_CACHE_TTL` | `180` | TTL мемо-кеша маршрута (0 = off) |
| `CAR_ORDER_OSRM_BEARING_RANGE` | `90` | диапазон snap старта ноги (встречка) |
| `CAR_ORDER_NOMINATIM_URL` | публичный Nominatim | геокодер |
| `CAR_ORDER_GEOCODER_USER_AGENT` | `ark-car-orders/1.0 …` | UA для OSM (обязателен по политике) |
| `CAR_ORDER_GEOCODE_CACHE_TTL` | `86400` | кеш геокодинга |
| `CAR_ORDER_TRAVEL_BUFFER_MIN` / `_DEFAULT_SERVICE_MIN` | `30` / `30` | буфер между заказами / дефолт on-site |
| `CAR_ORDER_ARRIVAL_GEOFENCE_M` / `_GPS_FRESH_S` | `100` / `120` | геозона прибытия / свежесть GPS |
| `CAR_ORDER_PICKUP_WAIT_LIMIT_S` | `1800` | лимит ожидания на подаче → `wait_overdue` |
| `AUTO_DISPATCH_ENABLED` | `True` | kill-switch авто-диспетча (+ DB-флаг) |
| `AUTO_DISPATCH_LEAD_MIN` / `_STALE_SEC` / `_POS_MAX_AGE` | `45`/`180`/`180` | параметры авто-диспетча |
| `CAR_ORDER_ABANDON_SEC` | `3600` | реап брошенного пина |
| `AUTO_SIMULATE_ENABLED` | `False` | фейковый GPS-симулятор |
| `LOG_TRACKING` | `True` | лог каждого api/health-запроса |

### Веб (`ark_yandex_front`, `.env.local`)

| Переменная | Назначение |
|---|---|
| `VITE_API_BASE_URL` | база API (`http://localhost:8000/api/v1`) |
| `VITE_YANDEX_MAPS_KEY` | ключ Yandex Maps JS API |
| `VITE_YANDEX_SUGGEST_ENABLED` | вкл. платные подсказки адреса (по умолч. off) |
| `VITE_FLEET_CENTER` / `VITE_FLEET_RADIUS_DEG` / `VITE_FLEET_FIT_RADIUS_DEG` | центр/радиусы региона карты |
| `VITE_FLEET_SUGGEST_PREFIX` | город-префикс для подсказок (`Ташкент`) |

---

## 20. Локальный запуск

### Бэкенд

```bash
cd ark_yandex
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # затем поправить SECRET_KEY и т.д.
python manage.py migrate        # + сидит права и группы доступа
python manage.py createsuperuser

# HTTP + WebSocket (daphne/ASGI). --noreload, иначе авто-перезагрузка рвёт WS.
python manage.py runserver 0.0.0.0:8000 --noreload
```

Проверка: `GET http://127.0.0.1:8000/health/` → `{"status": "ok"}`.

Тесты / линт:

```bash
.venv/bin/pytest car_orders/ -q     # сервисные + интеграционные тесты
ruff check . && ruff format .
```

Симуляция live-трекинга без телефона:

```bash
python manage.py auto_simulate --interval 1.5                    # везти активные заказы
python manage.py seed_driver_positions --drivers 671,13 --loop   # GPS свободных
python manage.py remind_departures --loop                        # нуджи «пора выезжать»
```

### Веб-клиент

```bash
cd ark_yandex_front
npm install
# .env.local: VITE_API_BASE_URL=http://localhost:8000/api/v1  + VITE_YANDEX_MAPS_KEY=...
npm run dev            # http://localhost:5173 (CORS уже разрешён в DEBUG)
```

### Мобилка

```bash
cd new_order/car_order
flutter pub get
# base URL = 10.0.2.2:8000 на Android-эмуляторе; MapKit key — в нативном проекте
flutter run
```

---

## 21. Деплой

В репозитории есть `Dockerfile`, `docker-compose.yml`,
`docker-compose.deploy.yml`, `docker-entrypoint.sh`, `Caddyfile`. Тестовый контур:
фронт на Vercel + бэкенд на DigitalOcean-droplet за Caddy (`nip.io`). Прод-чеклист:

- `DEBUG=False`, реальный `SECRET_KEY`, `ALLOWED_HOSTS`.
- **PostgreSQL** (`DATABASE_URL`) — НЕ SQLite (иначе claim-гонки не защищены).
- **Redis** (`REDIS_URL`) для WS-channel layer (`redis-py` пин `5.0.x`).
- `REQUIRE_OVERLAY_AUTH=True` (закрыть IDOR overlay-эндпоинтов).
- Свои OSRM/Nominatim или Yandex Router API (снять лимиты публичных).
- `CORS_ALLOWED_ORIGINS` под прод-домены фронта.
- WS обслуживает `daphne` (ASGI), HTTP может `gunicorn` (WSGI) — но WS требует ASGI.

---

## 22. Gotchas (что важно знать при интеграции)

1. **demo — источник истины. Не ломать прокси.** Auth и базовые данные на
   upstream. Любой новый локальный роут ставь **до** gateway catch-all в
   `config/urls.py`, иначе уйдёт на demo (→ 404).
2. **Статус читать только через `effective_status`.** Два хранилища состояния;
   «голый» demo-статус у overlay-claimed заказа врёт. Клиенты повторяют логику
   (web `utils/orderStatus.ts`).
3. **Overlay ключуется по demo id** (`order_id`/`driver_id`), не FK. Локального
   `CarOrder` может не быть вовсе — код это терпит (`status_map_for` → `None`).
4. **Геометрия маршрута — GeoJSON `[lng, lat]`.** Клиенты переворачивают в
   `[lat, lng]` (`toLatLng`). На OSRM-промахе бэк отдаёт **пустую** геометрию (не
   прямую линию) — клиенты рисуют «только пины» (guard `length >= 2`).
5. **Карты рендерятся на клиенте.** Бэк не отдаёт тайлы — только координаты.
   Веб = Yandex JS API (ключ в браузере), мобилка = Yandex MapKit (нативный ключ).
6. **Геокодинг — через наш прокси** `/geocode/`, не напрямую в OSM (бан 429).
7. **WS в проде — только Redis**, `redis-py` 5.0.x (RESP2). InMemory одно-процессный.
8. **Postgres в проде**, не SQLite (`select_for_update()` — no-op).
9. **`REQUIRE_OVERLAY_AUTH=True` в проде** — иначе overlay доверяет `driver_id`
   из тела (IDOR).
10. **Мобильная схема `/<lang>/api/v1/...`** нормализуется middleware — тот же
    роутинг, не отдельный бэкенд.
11. **Слияние в `ark-backend`** — это дельта, а не копипаст: выкинуть локальный
    `auth_core` в пользу `apps.auth_core`, перенести Р1 (`DriverShift`) и Р3
    (location), завернуть роуты в `i18n_patterns`. Детали — раздел 4
    `INTEGRATION.md`.

---

## 23. Карта файлов

### Бэкенд (`ark_yandex`)

| Файл / папка | Что там |
|---|---|
| `config/urls.py` | разводящий маршрутов (локально vs прокси) |
| `config/gateway.py` | прозрачный реверс-прокси на upstream |
| `config/middleware.py` | язык-префикс мобилки + логирование |
| `config/settings.py` | вся конфигурация (env-driven) |
| `config/asgi.py` | HTTP + WebSocket (Channels) |
| `config/auth.py` | `DemoTokenAuthentication` (overlay-мост) |
| `car_orders/models.py` | модели данных (родные + overlay) |
| `car_orders/views.py` | DRF-вьюхи (тонкие адаптеры над сервисами) |
| `car_orders/services/` | логика: `orders`, `overlay`, `trip_state`, `status`, `routing`, `geocode`, `events`(WS), `audit`, `notifications`, `shift` |
| `car_orders/dispatch.py`, `fleet.py` | ранжирование/claim + диспетчерский снимок |
| `car_orders/ws/` | WebSocket: `groups`(рассылка), `tracking`(downlink), `driver`(uplink) |
| `car_orders/permissions.py` | `HasPermission`, гейты |
| `car_orders/management/commands/` | фоновые воркеры и симуляторы |
| `auth_core/` | локальные права/группы (standalone) |
| `INTEGRATION.md` | детальный гайд по API + слияние в ark-backend |

### Веб (`ark_yandex_front/src`)

| Файл / папка | Что там |
|---|---|
| `components/map/ymapsLoader.ts` | загрузка Yandex Maps JS API |
| `components/map/yandex.ts` | ключ, центр/регион, traffic, `toLatLng` |
| `components/map/geocode.ts` | геокодинг: Yandex → прокси/Nominatim |
| `components/map/LiveTrackingMap.tsx` | карта отслеживания одной поездки |
| `components/map/FleetMap.tsx` | диспетчерская карта всей фабрики |
| `components/map/PointPickerMap.tsx` | выбор точки на карте в форме |
| `components/map/leafletSetup.ts` | legacy Leaflet-бутстрап |
| `utils/navLinks.ts` | deep-links в навигаторы (Yandex/2GIS/Google/Apple) |
| `utils/orderStatus.ts` | `effectiveStatus` (зеркало бэкенда) |
| `utils/permissionHierarchy.ts` | иерархия прав (зеркало бэкенда) |
| `hooks/useGpsUplink.ts` | GPS-uplink водителя по WS |
| `pages/car-orders/` | форма заказа, диспетчерская, отслеживание |

### Мобилка (`new_order/car_order/lib`)

| Файл / папка | Что там |
|---|---|
| `core/hosts/endpoints.dart` | сборка URL (`/<lang>/api/v1/...`) |
| `core/constants/app_constants.dart` | base URL по умолчанию |
| `features/car_orders/car_order_request/data/route_service.dart` | маршрут через Yandex MapKit DrivingRouter |
| `features/car_orders/car_order_request/data/reverse_geocoder.dart` | reverse-geocode через MapKit SearchManager |
| `features/car_orders/presentation/widget/driver/driver_car_order_map_view.dart` | карта водителя |
| `features/location_tracking/service` | трекинг/uplink GPS |

---

*Документ описывает текущую (gateway + overlay) архитектуру и весь клиентский
контур. Историю эволюции и пошаговые детали слияния в основной CRM см. в
`INTEGRATION.md`.*
