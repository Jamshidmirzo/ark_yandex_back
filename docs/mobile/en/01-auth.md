# 01 — Connection & Authentication

Login and tokens are proxied to the `demo` backend. Format is JWT (access + refresh).

## 1. Login

`POST /auth/login/`

Request:
```json
{ "username": "driver1", "password": "secret" }
```

Response `200`:
```json
{
  "access": "eyJhbGciOi...",
  "refresh": "eyJhbGciOi...",
  "user": {
    "id": 671,
    "username": "driver1",
    "name": "Ivan Driver",
    "is_superuser": false,
    "permissions": ["driver:accept_order", "driver:trip_control", "car_order:list_own"]
  }
}
```

- `access` — put in the `Authorization: Bearer <access>` header on every request.
- `refresh` — store securely (Keychain / EncryptedSharedPreferences); used to refresh.
- `user.permissions` — array of codenames; show/hide buttons by them (below).
- `user.id` — this is the **driver_id**, needed for the scheduling features (`claim-check`, `overlay-claim`).

Error `400`: `{ "detail": "No active account found with the given credentials" }`.

## 2. Refresh token

`POST /auth/refresh/`

```json
{ "refresh": "eyJhbGciOi..." }
```

Response `200`:
```json
{ "access": "eyJ...", "refresh": "eyJ..." }
```

App logic:
1. Put `Authorization: Bearer <access>` on every request.
2. On `401` → call `refresh/` → set the new `access` → **retry** the original request once.
3. If `refresh/` returns `401/400` → token is dead → log the user out.

## 3. Profile & permissions

`GET /auth/me/` → current user (same as `user` from login) with `permissions`.

Key permissions (codename):
| Permission | Who | Allows |
|---|---|---|
| `car_order:create` | requester/dispatcher | create orders |
| `car_order:approve` | dispatcher | approve (`admin-approve`) |
| `car_order:reject` | dispatcher | reject |
| `driver:accept_order` | driver | claim orders |
| `driver:trip_control` | driver | run the trip (`complete`) |
| `driver:list` | manager | see drivers |
| `garage:list` | manager | see garage |

A superuser (`is_superuser: true`) sees everything.

## Headers for every request

```
Authorization: Bearer <access>
Content-Type: application/json
Accept: application/json
```

## Flutter (Dio) — interceptor example

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
