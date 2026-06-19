"""
Django settings for config project.

Configuration is read from environment variables (see .env.example) using
django-environ, so the same settings module works in dev and production.

https://docs.djangoproject.com/en/5.2/ref/settings/
"""

from datetime import timedelta
from pathlib import Path

import environ

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["127.0.0.1", "localhost"]),
)

# Read .env file if present (not committed; see .env.example).
environ.Env.read_env(BASE_DIR / ".env")


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = env(
    "SECRET_KEY",
    default="django-insecure-0w1=j2ke@=0@f52wsee@$#k2z!-e$w43-3m9dipz$s0)mb#5h!",
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env("DEBUG")

ALLOWED_HOSTS = env("ALLOWED_HOSTS")


# Application definition

INSTALLED_APPS = [
    # daphne first so runserver serves ASGI (HTTP + WebSocket).
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "channels",
    "rest_framework",
    "rest_framework_simplejwt",
    "django_filters",
    "corsheaders",
    # Local
    "core",
    "auth_core",
    "car_orders",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    # Normalise the mobile app's /<lang>/api/v1/... scheme to /api/v1/... so it
    # resolves like the web frontend (must run before URL resolution).
    "config.middleware.MobileLanguagePrefixMiddleware",
    # Logs every api/health request with its source (📱 phone / 🖥 local) — see
    # the whole mobile conversation in one stream. Gated by LOG_TRACKING.
    "config.middleware.RequestLogMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Channels / WebSockets (live driver tracking, fleet dashboard, notifications).
# In-memory is fine for the single-process dev server, but it loses all groups on
# restart and can't span workers. Set REDIS_URL in prod (needs
# `pip install channels_redis`) for a durable, multi-process layer — required once
# the dispatcher fleet view and per-user notifications run for real.
ASGI_APPLICATION = "config.asgi.application"
REDIS_URL = env("REDIS_URL", default="")
if REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [REDIS_URL]},
        }
    }
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


# Database
# Defaults to SQLite; set DATABASE_URL to use Postgres, e.g.
# postgres://user:pass@localhost:5432/dbname
DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    ),
}

# AUDIT C2: on SQLite, `SELECT ... FOR UPDATE` is a silent no-op, so the row locks
# guarding the claim paths provide no protection under concurrency. That's fine for
# dev/tests but unsafe in production — warn loudly so a prod deploy switches to
# Postgres (DATABASE_URL=postgres://…).
if not DEBUG and "sqlite" in DATABASES["default"].get("ENGINE", ""):
    import warnings

    warnings.warn(
        "Running with SQLite while DEBUG=False: select_for_update() is a no-op, so the "
        "car-orders claim concurrency guards are disabled. Use Postgres in production "
        "(set DATABASE_URL). See car_orders/AUDIT.md finding C2.",
        RuntimeWarning,
        stacklevel=2,
    )


# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Internationalization

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Django REST Framework
# Mirrors the ark-backend conventions (JWT auth, IsAuthenticated by default,
# filter/search/ordering backends, limit/offset pagination). See INTEGRATION.md.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
}

# BFF: where to proxy auth (login/refresh/me) — the OLD/real backend.
# Language-prefixed base, e.g. http://host.docker.internal:12001/ru/api/v1
# (host.docker.internal lets the backend container reach the host's :12001),
# or a deployed server URL. Override via the UPSTREAM_API_BASE env var.
UPSTREAM_API_BASE = env(
    "UPSTREAM_API_BASE",
    default="http://host.docker.internal:12001/ru/api/v1",
)

# (connect, read) timeout for the upstream proxy, in seconds. Connect is short
# so a dead/unreachable upstream fails fast (no 30s hang); read is generous so a
# slow-but-alive endpoint isn't cut off. Override per-env if needed.
UPSTREAM_TIMEOUT = (
    env.float("UPSTREAM_CONNECT_TIMEOUT", default=5.0),
    env.float("UPSTREAM_READ_TIMEOUT", default=120.0),
)

# Overlay auth bridge. OFF (default) keeps the open dev behaviour; ON validates
# the demo bearer token via demo /auth/me/ (config.auth.DemoTokenAuthentication)
# and derives the driver from it instead of the request body. Flip on once login
# is verified end-to-end. See car_orders.permissions.
REQUIRE_OVERLAY_AUTH = env.bool("REQUIRE_OVERLAY_AUTH", default=False)

# AUDIT H5: with overlay auth off, the overlay endpoints trust a body-supplied
# driver_id and expose the whole order board to everyone. Fine for dev; a serious
# IDOR surface in production — warn loudly if it ships with DEBUG off.
if not DEBUG and not REQUIRE_OVERLAY_AUTH:
    import warnings

    warnings.warn(
        "REQUIRE_OVERLAY_AUTH is OFF while DEBUG=False: overlay endpoints trust the "
        "request-body driver_id and expose all orders. Set REQUIRE_OVERLAY_AUTH=True in "
        "production. See car_orders/AUDIT.md finding H5.",
        RuntimeWarning,
        stacklevel=2,
    )


# Car-order scheduling & routing.
# Travel buffer reserved between two consecutive orders of one driver, so the
# overlap check leaves room to drive from one job to the next.
CAR_ORDER_TRAVEL_BUFFER = timedelta(minutes=env.int("CAR_ORDER_TRAVEL_BUFFER_MIN", default=30))
# Default on-site service time the duration auto-estimate adds on top of travel.
CAR_ORDER_DEFAULT_SERVICE = timedelta(minutes=env.int("CAR_ORDER_DEFAULT_SERVICE_MIN", default=30))
# Routing engine used by POST /car-orders/estimate/ and the simulator. OSRM's
# public demo server by default; point at a self-hosted OSRM or swap to the
# Yandex Router API in production.
CAR_ORDER_OSRM_URL = env("CAR_ORDER_OSRM_URL", default="https://router.project-osrm.org")

# Arrival geofence (server-side): the driver may mark «at_client» / «at_destination»
# only within this many metres of the point AND with a fresh GPS fix. 0 disables it
# (handy for testing without being physically at the point).
CAR_ORDER_ARRIVAL_GEOFENCE_M = env.int("CAR_ORDER_ARRIVAL_GEOFENCE_M", default=100)
# A GPS fix older than this (seconds) is too stale to confirm arrival.
CAR_ORDER_GPS_FRESH_S = env.int("CAR_ORDER_GPS_FRESH_S", default=120)


# Backend auto-dispatch worker (`manage.py auto_dispatch`). Assigns awaiting orders
# to the nearest free driver server-side, so it works with no dispatcher tab open.
AUTO_DISPATCH_ENABLED = env.bool("AUTO_DISPATCH_ENABLED", default=True)
# Assign a SCHEDULED order this many minutes before its pickup time.
AUTO_DISPATCH_LEAD_MIN = env.int("AUTO_DISPATCH_LEAD_MIN", default=45)
# Assign an ASAP order once it has waited this long unclaimed.
AUTO_DISPATCH_STALE_SEC = env.int("AUTO_DISPATCH_STALE_SEC", default=180)
# Ignore driver GPS fixes older than this when ranking by distance.
AUTO_DISPATCH_POS_MAX_AGE = env.int("AUTO_DISPATCH_POS_MAX_AGE", default=180)

# Driver GPS simulator (`manage.py auto_simulate`). OFF by default now that real
# phones stream their position to /drivers/me/location/ — the fake feed would fight
# the real one. Set AUTO_SIMULATE_ENABLED=1 (or pass --force) only to test tracking
# without a phone.
AUTO_SIMULATE_ENABLED = env.bool("AUTO_SIMULATE_ENABLED", default=False)

# Print a console line for each driver GPS heartbeat + trip-state change, so you
# can watch in real time what the mobile app sends. Set LOG_TRACKING=0 to silence.
LOG_TRACKING = env.bool("LOG_TRACKING", default=True)


# CORS — allow the Vite dev frontend (ark_yandex_front) to call the API.
CORS_ALLOWED_ORIGINS = env(
    "CORS_ALLOWED_ORIGINS",
    default=["http://localhost:5173", "http://127.0.0.1:5173"],
)
CORS_ALLOW_CREDENTIALS = True
# Dev only: let a teammate on the same LAN (private 192.168.x / 10.x / 172.16-31.x
# subnets) hit the API from their browser. Origins are reflected (not "*"), so
# credentials keep working. Direct curl/Postman calls don't need this.
if DEBUG:
    CORS_ALLOWED_ORIGIN_REGEXES = [
        # Any local dev port — vite picks 5174+ when 5173 is taken (e.g. by the
        # dockerized frontend), which would otherwise be CORS-blocked.
        r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
        r"^http://192\.168\.\d{1,3}\.\d{1,3}(:\d+)?$",
        r"^http://10\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?$",
        r"^http://172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}(:\d+)?$",
    ]


# Simple JWT — same token shape/lifetimes as ark-backend's auth_core.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=env.int("JWT_ACCESS_HOURS", default=24)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "AUTH_HEADER_TYPES": ("Bearer",),
}


# django-debug-toolbar (dev only)
if DEBUG:
    INSTALLED_APPS += ["debug_toolbar"]
    MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")
    INTERNAL_IPS = ["127.0.0.1"]
