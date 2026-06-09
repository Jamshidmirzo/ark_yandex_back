# 01 — Подключение и авторизация

Логин и токены проксируются на `demo`-бэкенд. Формат — JWT (access + refresh).

## 1. Логин

`POST /auth/login/`

Запрос:
```json
{ "username": "driver1", "password": "secret" }
```

Ответ `200`:
```json
{
  "access": "eyJhbGciOi...",
  "refresh": "eyJhbGciOi...",
  "user": {
    "id": 671,
    "username": "driver1",
    "name": "Иван Водитель",
    "is_superuser": false,
    "permissions": ["driver:accept_order", "driver:trip_control", "car_order:list_own"]
  }
}
```

- `access` — кладёшь в заголовок `Authorization: Bearer <access>` в каждый запрос.
- `refresh` — храни безопасно (Keychain / EncryptedSharedPreferences), нужен для обновления.
- `user.permissions` — массив прав; по ним показывай/скрывай кнопки (см. ниже).
- `user.id` — **driver_id**, нужен для фич расписания (`claim-check`, `overlay-claim`).

Ошибка `400`: `{ "detail": "No active account found with the given credentials" }`.

## 2. Обновление токена

`POST /auth/refresh/`

```json
{ "refresh": "eyJhbGciOi..." }
```

Ответ `200`:
```json
{ "access": "eyJ...", "refresh": "eyJ..." }
```

Алгоритм в приложении:
1. На каждый запрос ставь `Authorization: Bearer <access>`.
2. Получил `401` → вызови `refresh/` → подставь новый `access` → **повтори** исходный запрос один раз.
3. `refresh/` вернул `401/400` → токен протух → разлогинь пользователя.

## 3. Профиль и права

`GET /auth/me/` → текущий пользователь (как `user` из логина) с `permissions`.

Ключевые права (codename):
| Право | Кому | Что разрешает |
|---|---|---|
| `car_order:create` | заказчик/диспетчер | создавать заявки |
| `car_order:approve` | диспетчер | согласовывать (`admin-approve`) |
| `car_order:reject` | диспетчер | отклонять |
| `driver:accept_order` | водитель | принимать заказы (`claim`) |
| `driver:trip_control` | водитель | управлять поездкой (`complete`) |
| `driver:list` | менеджер | видеть список водителей |
| `garage:list` | менеджер | видеть гараж |

Суперюзер (`is_superuser: true`) видит всё.

## Заголовки для всех запросов

```
Authorization: Bearer <access>
Content-Type: application/json
Accept: application/json
```

## Flutter (Dio) — пример интерсептора

```dart
dio.interceptors.add(InterceptorsWrapper(
  onRequest: (o, h) {
    final t = storage.access;
    if (t != null) o.headers['Authorization'] = 'Bearer $t';
    h.next(o);
  },
  onError: (e, h) async {
    if (e.response?.statusCode == 401 && !e.requestOptions.extra.containsKey('retried')) {
      final ok = await auth.refresh();           // POST /auth/refresh/
      if (ok) {
        e.requestOptions.extra['retried'] = true;
        e.requestOptions.headers['Authorization'] = 'Bearer ${storage.access}';
        return h.resolve(await dio.fetch(e.requestOptions));
      }
    }
    h.next(e);
  },
));
```
