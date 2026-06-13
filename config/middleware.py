"""Gateway request normalisation.

The web frontend talks to us with ``/api/v1/...`` (no language segment; the
gateway adds ``/ru/`` upstream via ``UPSTREAM_API_BASE``). The **mobile app**
uses the demo backend's native scheme — ``/<lang>/api/v1/...`` (language in the
path) and probes ``/<lang>/healthcheck/``. Stripping a leading language segment
lets BOTH schemes resolve to the same routes: our local feature views first,
then the gateway proxy. See [[dev-runtime-setup]] / INTEGRATION.md.
"""

import re

# A leading 2-letter language segment, only when it precedes our real paths
# (api/v1 or the health/healthcheck probes) — so we never strip a genuine path.
_LANG_PREFIX = re.compile(r"^/[a-z]{2}/(?=api/v1/|health)")


class MobileLanguagePrefixMiddleware:
    """Drop a leading ``/<lang>/`` so ``/ru/api/v1/...`` (mobile) routes exactly
    like ``/api/v1/...`` (web). No-op for paths without a language prefix, so the
    web frontend is unaffected."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        stripped = _LANG_PREFIX.sub("/", request.path_info, count=1)
        if stripped != request.path_info:
            request.path_info = stripped
            request.path = stripped
        return self.get_response(request)


def _source_tag(request):
    """``📱 <ip>`` (a real phone / remote client) vs ``🖥 локально`` (our server or
    the web frontend on 127.0.0.1) — so mobile traffic stands out in the log."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    ip = xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR", "?")
    return "🖥 локально" if ip in ("127.0.0.1", "::1", "localhost") else f"📱 {ip}"


# (method, path-regex, human label) — what each request actually DOES, so the log
# reads in plain Russian. First match wins, so order specific → generic.
_ACTIONS = [
    ("POST", r"/auth/login/?$", "вход (login)"),
    ("POST", r"/auth/refresh/?$", "обновить токен"),
    ("GET", r"/auth/me/permissions/?$", "права пользователя"),
    ("GET", r"/auth/me/groups/?$", "группы пользователя"),
    ("GET", r"/auth/me/?$", "профиль (кто я)"),
    ("GET", r"/car-orders/drivers/me/overlay-orders/", "📂 МОИ ЗАКАЗЫ (overlay)"),
    ("GET", r"/car-orders/drivers/me/cars/", "мои машины"),
    ("GET", r"/car-orders/drivers/me/shift/", "моя смена"),
    ("PATCH", r"/car-orders/drivers/me/shift/", "🚗 выйти на смену / сменить машину"),
    ("DELETE", r"/car-orders/drivers/me/shift/", "завершить смену"),
    ("POST", r"/car-orders/drivers/me/location/", "📍 GPS-позиция (хартбит)"),
    ("GET", r"/car-orders/drivers/positions/", "позиции всех водителей"),
    ("GET", r"/car-orders/drivers/", "список водителей"),
    ("POST", r"/car-orders/estimate/", "расчёт маршрута/длительности"),
    ("GET", r"/car-orders/fleet/live/", "снимок диспетчерской"),
    ("POST", r"/car-orders/claim-check-batch/", "проверка окон (пачка)"),
    ("POST", r"/car-orders/meta-batch/", "overlay для списка (пачка)"),
    ("GET", r"/car-orders/car-types/", "типы машин"),
    ("POST", r"/car-orders/\d+/claim/?$", "✋ ВЗЯТЬ ЗАКАЗ (claim)"),
    ("POST", r"/car-orders/\d+/overlay-claim/", "✋ взять/назначить (overlay)"),
    ("POST", r"/car-orders/\d+/overlay-release/", "↩️ вернуть заказ в очередь"),
    ("POST", r"/car-orders/\d+/trip-state/", "🚦 СМЕНИТЬ ЭТАП ПОЕЗДКИ"),
    ("POST", r"/car-orders/\d+/complete/", "🏁 завершить заказ"),
    ("POST", r"/car-orders/\d+/submit/", "отправить на согласование"),
    ("POST", r"/car-orders/\d+/admin-approve/", "согласовать заказ"),
    ("POST", r"/car-orders/\d+/reject/", "отклонить заказ"),
    ("POST", r"/car-orders/\d+/reassign/", "переназначить (диспетчер)"),
    ("POST", r"/car-orders/\d+/extend/", "продлить заказ"),
    ("POST", r"/car-orders/\d+/claim-check/", "проверка окна перед приёмом"),
    ("POST", r"/car-orders/\d+/meta/", "сохранить overlay (координаты/окно)"),
    ("GET", r"/car-orders/\d+/meta/", "читать overlay"),
    ("POST", r"/car-orders/\d+/live-location/", "позиция в заказ (per-order)"),
    ("GET", r"/car-orders/\d+/live-location/", "читать позицию заказа"),
    ("POST", r"/car-orders/?$", "➕ СОЗДАТЬ ЗАКАЗ"),
    ("GET", r"/car-orders/\d+/?$", "деталь заказа"),
    ("GET", r"/car-orders/?$", "📋 список заказов"),
    ("GET", r"/storage/\d+/url/", "файл/аватар"),
    ("GET", r"/menu-items/", "меню (общее приложение)"),
    ("GET", r"/notifications/count/", "счётчик уведомлений"),
    ("GET", r"/employees/me/?$", "профиль сотрудника"),
    ("HEAD", r"/health", "проверка связи (healthcheck)"),
    ("GET", r"/health", "проверка связи (healthcheck)"),
]


def _describe(method, path):
    """Plain-Russian label of what this request does (or '' if unknown)."""
    for m, pat, label in _ACTIONS:
        if method == m and re.search(pat, path):
            return label
    return ""


class RequestLogMiddleware:
    """Log EVERY api/health request with its source, so the WHOLE mobile
    conversation is visible in one stream (not just GPS/status) — e.g. «get all
    orders», claim, detail. Grep «📱» to see only the phone, or «📱|🖥» for all.
    Gated by settings.LOG_TRACKING (default on in dev; LOG_TRACKING=0 to silence)."""

    def __init__(self, get_response):
        from django.conf import settings

        self.get_response = get_response
        self.enabled = getattr(settings, "LOG_TRACKING", False)

    @staticmethod
    def _loggable(path):
        return path.startswith("/api/v1/") or "/health" in path

    def __call__(self, request):
        import time

        if not self.enabled or not self._loggable(request.path):
            return self.get_response(request)
        t0 = time.monotonic()
        response = self.get_response(request)
        ms = (time.monotonic() - t0) * 1000
        qs = request.META.get("QUERY_STRING", "")
        path = request.path + ("?" + qs if qs else "")
        desc = _describe(request.method, request.path)
        label = f"  · {desc}" if desc else ""
        print(
            f"🌐 [{_source_tag(request)}] {request.method} {path} → {response.status_code} ({ms:.0f}ms){label}",
            flush=True,
        )
        return response
