# ark_yandex backend (Django + DRF + Channels). Postgres (default) + PostGIS (geo),
# Redis channel layer; served via gunicorn + UvicornWorker.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# GeoDjango runtime libs (GDAL/GEOS/PROJ). Without these `django.contrib.gis` fails
# to import at startup; binutils provides the ctypes library-discovery helper.
RUN apt-get update && apt-get install -y --no-install-recommends \
        binutils \
        gdal-bin \
        libgdal-dev \
        libgeos-dev \
        libproj-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
CMD ["/entrypoint.sh"]
