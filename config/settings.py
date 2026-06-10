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

# Channels / WebSockets (live driver tracking). In-memory layer is fine for the
# single-process dev server; use Redis (channels_redis) for multi-process / prod.
ASGI_APPLICATION = "config.asgi.application"
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
