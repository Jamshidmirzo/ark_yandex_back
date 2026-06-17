# Заказы по ролям: водитель видит свои, админ — все

Ролевой доступ к списку активных заказов оверлея. Один эндпоинт обслуживает оба
сценария:

- **водитель** → только свои активные заказы;
- **админ / диспетчер** → вся активная доска (опционально — фильтр по водителю).

> Затрагивает бэкенд (`ark_yandex`) и веб-клиент (`ark_yandex_front`). Мобильный
> экран «Мои заказы» (driver-only) описан в
> [mobile/03-scheduling-overlay.md §3.8](mobile/03-scheduling-overlay.md).

---

## 1. Эндпоинт

`GET /api/v1/car-orders/drivers/me/overlay-orders/`

Обслуживается локально шлюзом (не проксируется на `demo`). Возвращает массив
`OrderMeta`, **исключая** терминальные (`completed` / `cancelled`), отсортированный
по `planned_datetime`, затем `order_id`.

### Параметры

| Параметр | Кто использует | Описание |
|---|---|---|
| `driver_id` | водитель (dev) / админ | В режиме без enforced-auth водитель скоупит выборку на себя, передавая свой id. Админ может сузить доску до одного водителя. При enforced-auth для **водителя** игнорируется (личность берётся из токена — защита от IDOR). |

### Правило выборки

```
qs = OrderMeta, кроме completed/cancelled
если запрашивающий — диспетчер (OverlayDispatcher):
    если передан driver_id → фильтр по нему
    иначе               → вся доска
иначе (водитель):
    driver_id = acting_driver_id(request, ?driver_id)   # токен при enforced-auth
    если driver_id пуст → []
    иначе → фильтр driver_id == свой
```

Реализация — `MyOverlayOrdersView.get` в
[car_orders/views.py](../car_orders/views.py).

### Кто «диспетчер»

`OverlayDispatcher` (см. [car_orders/permissions.py](../car_orders/permissions.py)):
суперюзер **или** право, удовлетворяющее `car_order:approve` по иерархии ARK
(`administrator` ⊇ всё, `X_all` ⊇ `X`). То есть доску целиком получают держатели
`car_order:approve`, `car_order:approve_all`, `administrator` и суперюзеры.

> ⚠️ **Контракт иерархии.** `OverlayDispatcher` раскрывает иерархию через
> `expand_permission_codename("car_order:approve")` поверх in-memory набора прав
> `DemoUser` — **ровно так же**, как `useMyPermissions.hasPermission` на фронте.
> Это намеренно: если бэкенд проверяет право буквально, а фронт — по иерархии, то
> `administrator` без буквального `car_order:approve` получит админский UI, но
> данные с правами водителя (свой пустой список). Любую новую серверную проверку,
> которую зеркалит веб-UI, раскрывай по той же иерархии.

### Поведение по режимам и ролям

| Режим | Кто | Запрос фронта | Результат |
|---|---|---|---|
| enforced | водитель | `?driver_id=<свой>` (или без) | свои заказы (личность из токена; чужой `driver_id` игнорируется) |
| enforced | диспетчер | без `driver_id` | вся доска |
| enforced | диспетчер | `?driver_id=99` | заказы водителя 99 |
| enforced | суперюзер | без `driver_id` | вся доска |
| dev (auth off) | водитель | `?driver_id=<свой>` | свои (в dev все «диспетчеры», но `driver_id` сужает до него) |
| dev (auth off) | админ-инструмент | без `driver_id` | вся доска |

### Пример ответа `200`

```json
[
  {
    "order_id": 831,
    "driver_id": 99,
    "trip_state": "in_trip",
    "planned_datetime": "2026-06-17T14:00:00Z",
    "planned_end": "2026-06-17T15:00:00Z",
    "car_label": "Cobalt (01A777AA)",
    "at_risk": false,
    "is_late": false
  }
]
```

Полная схема `OrderMeta` — в
[mobile/03-scheduling-overlay.md](mobile/03-scheduling-overlay.md).

---

## 2. Веб-клиент (`ark_yandex_front`)

Страница **«Мои заказы»** теперь ролевая и обслуживает оба сценария.

| Файл | Что делает |
|---|---|
| `src/api/endpoints/carOrders.ts` | `myOverlayOrders(driverId?)` — `driverId` опционален; **без** него бэкенд возвращает всю доску (админ) |
| `src/pages/car-orders/DriverSchedulePage.tsx` | `isDispatcher = hasPermission("car_order:approve")`. Диспетчер → вся доска, заголовок «Все заказы», метки этапов в нейтральной перспективе, тег «Водитель #id». Водитель → свои, «Мои заказы», метки от первого лица |
| `src/router.tsx` | гард `/orders/schedule` расширен до `["driver:accept_order", "car_order:approve"]`; в `CAR_ANY` добавлен `car_order:approve` (чтобы диспетчер мог открыть деталь заказа `/orders/car/:id`) |
| `src/layouts/DashboardLayout.tsx` | пункт меню показывается водителю и диспетчеру; ярлык «Все заказы» при `car_order:approve`, иначе «Мои заказы» |

Логика роли на фронте (`isDispatcher = hasPermission("car_order:approve")`) совпадает
с бэкендом (`OverlayDispatcher`) благодаря единой иерархии прав.

> Диспетчер получает плоский список «Все заказы» **в дополнение** к live-карте
> «Диспетчерская» (`FleetLivePage`) — это разные представления (список vs карта).

---

## 3. Тесты

`car_orders/tests/test_auth_bridge.py` (enforced-auth):

- `test_enforced_my_orders_ignores_query_driver_id` — водитель не может перечислить чужие заказы через `?driver_id=`;
- `test_admin_overlay_orders_sees_the_whole_board` — диспетчер (`car_order:approve`) получает все активные;
- `test_admin_overlay_orders_can_filter_to_one_driver` — фильтр `?driver_id=`;
- `test_admin_overlay_orders_honours_permission_hierarchy` — `administrator` и `car_order:approve_all` тоже получают доску (иерархия).

Запуск:

```bash
.venv/bin/pytest car_orders/tests/test_auth_bridge.py car_orders/tests/test_overlay.py -q
```

---

## 4. Заметки и крайние случаи

- **Терминальные заказы** (`completed` / `cancelled`) не попадают в доску — ни у водителя, ни у админа. Если нужна история, потребуется отдельный флаг (`?include_terminal=1`) — пока не реализован.
- **Двойная роль** (и `driver:accept_order`, и `car_order:approve`): пользователь считается диспетчером и видит **всю** доску (ярлык «Все заказы»).
- **`OverlayDispatcher` — общий гард**: расширение иерархии действует и на `reassign`, тумблер авто-распределения, удаление meta. Это намеренное расширение (буквальный `car_order:approve` и суперюзер по-прежнему проходят) — права у `administrator`/`*_all` только добавляются.
