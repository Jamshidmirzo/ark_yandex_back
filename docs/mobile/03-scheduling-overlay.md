# 03 — Оверлей: расписание, приём, этапы поездки

Эти эндпоинты обслуживаются **шлюзом локально** (у demo их нет). Все работают по
**id заказа из demo**. Формат ошибок — `{"error": {code, message, details}}`.

> Длительность везде — целое число **минут**. Время — ISO-8601 UTC.
> `geometry` — массив `[lng, lat]` (GeoJSON); для карт переворачивай в `[lat, lng]`.

## Зачем оверлей (коротко)
demo хранит базовый заказ, но не умеет: маршрут A→B, длительность/окна, **этапы поездки**
и **последовательные заказы одной машиной**. Это всё мы храним в `OrderMeta` по id заказа.

---

## 3.1 Объект `OrderMeta`

```json
{
  "order_id": 88,
  "driver_id": 671,
  "car_id": 5,
  "car_label": "Cobalt (01A777AA)",
  "overlay_claimed": true,
  "origin_lat": 41.311, "origin_lng": 69.240,
  "address_lat": 41.351, "address_lng": 69.290,
  "estimated_duration": 43,
  "service_time": 30,
  "planned_datetime": "2026-06-11T09:00:00Z",
  "latest_start": null,
  "trip_state": "to_client",
  "planned_end": "2026-06-11T09:43:00Z"
}
```
- `origin_*` — координаты точки **подачи** (откуда 🟢); `address_*` — координаты точки
  **назначения** (куда 🔴). Префикс `address` = назначение. У точки подачи отдельного текстового
  адреса нет — только координаты. Текстовый адрес назначения хранит demo в поле `address` (раздел 02).
- `driver_id` — выставляется при ЛЮБОМ приёме (для проверки окон).
- `overlay_claimed` — `true` **только** если заказ принят нашим слоем (`overlay-claim`), а не demo.
  По нему отличай «ведём у себя» от обычного demo-claim.
- `trip_state` — этап поездки (см. §3.6). Терминальные: `completed`, `cancelled`.

### Прочитать / записать
- `GET /car-orders/{id}/meta/` → объект или `null`.
- `POST /car-orders/{id}/meta/` — upsert, шли только нужные поля.

**Когда писать meta:** сразу **после создания заявки** (`POST /car-orders/` вернул `id`) сохрани
координаты точек и длительность — иначе маршрут/трекинг для заказа не построятся:
```json
{ "origin_lat":41.311, "origin_lng":69.240, "address_lat":41.351, "address_lng":69.290,
  "estimated_duration":43, "service_time":30, "planned_datetime":"2026-06-11T09:00:00Z" }
```

---

## 3.2 Авто-расчёт маршрута и длительности — `estimate`

`POST /car-orders/estimate/` — **без авторизации**.
```json
{ "origin_lat":41.311, "origin_lng":69.240, "dest_lat":41.351, "dest_lng":69.290, "service_minutes":30 }
```
Ответ:
```json
{ "distance_m":8508, "drive_minutes":13, "service_minutes":30, "duration_minutes":43,
  "geometry":[[69.240,41.311], ...], "source":"osrm" }
```
`source`: `osrm` (точный маршрут) или `haversine` (запасной прямой расчёт).

---

## 3.3 Проверка окна перед приёмом — `claim-check`

`POST /car-orders/{id}/claim-check/` `{ "driver_id": 671 }`
```json
{ "ok": true,  "conflict": null }
{ "ok": false, "conflict": { "order_id":90, "planned_start":"...", "planned_end":"...", "address":"Заказ #90" } }
```
Считает окно `[planned_datetime, planned_end]` заказа и сверяет с **другими активными** заказами
водителя (+ запас на переезд). Завершённые/снятые окна не считаются. Вызывай **перед** приёмом:
`ok:false` → покажи конфликт, не давай принять.

---

## 3.4 Приём заказа — два пути

| Случай | Как принимать | Результат |
|---|---|---|
| Машина **свободна** | demo-`claim`: `POST /car-orders/{id}/claim/` `{car_id}` (раздел 02) → потом `POST /meta/ {driver_id}` | demo `in_progress` |
| Машина **занята** (своя, ведёшь её на другом заказе) | **`overlay-claim`** (ниже) | принят нашим слоем, `overlay_claimed=true`, `trip_state=assigned` |

demo запрещает «одна машина — один активный заказ», поэтому второй заказ той же машиной берётся
только через `overlay-claim`.

### `overlay-claim`
`POST /car-orders/{id}/overlay-claim/`
```json
{ "driver_id":671, "car_id":5, "car_label":"Cobalt (01A777AA)" }
```
- `{ "ok": true, "conflict": null, "meta": {...} }` — принят.
- `{ "ok": false, "conflict": {...} }` — пересечение по времени.
- `400 ALREADY_CLAIMED` — заказ уже взят **другим** водителем (и ещё активен).
- Повторный вызов тем же водителем **не сбрасывает** текущий этап (не откатывает поездку).

---

## 3.5 Снять заказ / вернуть в очередь — `overlay-release`

`POST /car-orders/{id}/overlay-release/` (без тела)
```json
{ "ok": true, "meta": { "overlay_claimed": false, "driver_id": null, "trip_state": "cancelled", ... } }
```
Очищает наш claim: заказ перестаёт занимать расписание и его перестаёт вести симулятор.

**Вызывай его на teardown-действиях:** при demo-`reject`, при «отменить», при «вернуть в очередь»,
а также можно после завершения. Идемпотентно (если meta нет — просто `{ "ok": true }`).

---

## 3.6 Этапы поездки — `trip-state`

`POST /car-orders/{id}/trip-state/` `{ "trip_state": "to_client" }` → обновлённый `meta`.
Изменение **пушится в реальном времени** по WebSocket (раздел 04).

Подписи **зависят от роли**: водитель видит действие от первого лица, заказчик/наблюдатель —
нейтральный статус. Каждой фазе — свой цвет тега (соседние фазы различимы, пауза выделена).

| trip_state | Водитель видит | Заказчик видит | Цвет тега | Кнопка водителя → следующий |
|---|---|---|---|---|
| `assigned` | Принят | Назначен водитель | default | «Выехал к клиенту» → `to_client` |
| `to_client` | Еду к клиенту | В пути к подаче | geekblue | «Я на месте» → `at_client` |
| `at_client` | Жду клиента | На подаче | cyan | «Начать поездку» → `in_trip` |
| `in_trip` | Везу клиента | В пути к месту | blue | «Прибыли на место» → `at_destination` |
| `at_destination` | На месте | Прибыл на место | lime | «На ожидание» → `waiting` |
| `waiting` | На паузе | Пауза — ожидание | orange | «Продолжить» → `in_trip` |
| `completed` | Завершил | Завершён | green | — |
| `cancelled` | Отменён | Отменён | red | — (выставляется `overlay-release`) |

- `400 INVALID_STATUS` — нельзя сменить статус у уже **завершённого** заказа.
- Геозона (по желанию): кнопки «Я на месте»/«Прибыли» подсвечивай по расстоянию (≈400 м) до
  точки подачи/назначения — как подсказку, не как жёсткий блок.

---

## 3.7 Завершение заказа

- Заказ принят через **demo** (`overlay_claimed=false`): `POST /car-orders/{id}/complete/` (demo) **и**
  `POST /trip-state/ {completed}` — чтобы оверлей не разошёлся.
- Заказ принят через **наш слой** (`overlay_claimed=true`): только `POST /trip-state/ {completed}`
  (demo про него не знает).
> demo разрешает `complete` **только назначенному водителю** — админ/диспетчер demo-заказ не завершит.

---

## 3.8 «Мои заказы» — активные заказы водителя

`GET /car-orders/drivers/me/overlay-orders/?driver_id=671` → массив `OrderMeta` водителя, исключая
`completed`/`cancelled`:
```json
[ { "order_id":88, "trip_state":"to_client", "car_label":"Cobalt (01A777AA)",
    "planned_datetime":"...", "planned_end":"...", "overlay_claimed":true }, ... ]
```
Включает **и demo-принятые, и overlay-принятые** заказы (у обоих есть `driver_id`). Используй для
экрана «Мои заказы» — показывай №, этап (`trip_state`), окно времени, машину, ссылку на деталь.

---

## 3.9 Продлить / Переназначить заказ

`POST /car-orders/{id}/extend/` `{ "minutes": 30 }` → `{ "ok": true, "meta": {...}, "conflict": null }`
Добавляет минуты к `estimated_duration` (сдвигает `planned_end`). Продление применяется **всегда**;
`conflict` (если не `null`) — предупреждение, что новое окончание пересеклось со следующим заказом
водителя. Доступно водителю или диспетчеру. `400 VALIDATION` — `minutes` не положительное или у
заказа нет сохранённого окна.

`POST /car-orders/{id}/reassign/` (без тела) → `{ "ok": true, "meta": {...} }`
Диспетчер снимает заказ с водителя и возвращает в очередь (то же, что `overlay-release`, но это
действие **диспетчера**): `overlay_claimed=false`, `driver_id=null`, `trip_state=cancelled`, пуш
`cancelled` по WS — заказ снова доступен другому водителю. Работает для overlay-принятых заказов
(demo-claim принадлежит demo и отсюда не переназначается). `400 NOT_FOUND` — нет meta.

---

## Эффективный статус для UI (важно)
У overlay-принятого заказа демо-статус остаётся `awaiting_driver`. Не показывай его — считай так:
- если `meta.overlay_claimed && trip_state ∉ {completed, cancelled}` → показывай **как «в пути»**,
  а конкретный этап бери из `trip_state`;
- иначе — показывай статус заказа из demo.
