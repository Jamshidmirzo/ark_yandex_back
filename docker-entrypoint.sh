#!/bin/sh
set -e

echo "→ migrate (default)"
python manage.py migrate --noinput
# Telemetry split: the geo DB holds ONLY DriverPosition + OrderLiveLocation (routed
# by car_orders.routers.GeoRouter). Its tables are never created on `default`, so it
# must be migrated explicitly. Only the backend runs this — the dispatcher worker
# overrides the entrypoint and waits for backend to be healthy.
echo "→ migrate (geo / PostGIS)"
python manage.py migrate --database=geo --noinput

echo "→ seed demo data (idempotent)"
python manage.py shell -c "
from django.contrib.auth import get_user_model
from auth_core.models import AccessGroup, UserAccessGroup
from car_orders.models import Car, CarOrder, CarType
U = get_user_model()

def mkuser(username, password, *groups, **flags):
    u, _ = U.objects.get_or_create(username=username, defaults={'is_active': True, **flags})
    u.is_active = True
    for k, v in flags.items():
        setattr(u, k, v)
    u.set_password(password)
    u.save()
    for g in groups:
        UserAccessGroup.objects.get_or_create(user=u, group=AccessGroup.objects.get(name=g))
    return u

admin = mkuser('admin', 'admin12345', is_staff=True, is_superuser=True)
dispatcher = mkuser('dispatcher', 'dispatcher12345', 'Car Admin')
requester = mkuser('requester', 'requester12345', 'Car Requester')
driver = mkuser('driver', 'driver12345', 'Driver')

types = {}
for name in ('Легковая', 'Минивэн', 'Грузовая'):
    types[name], _ = CarType.objects.get_or_create(name=name)

cars = []
for model, plate, tname in (('Damas', '01A001AA', 'Легковая'), ('Faw', '01A002AA', 'Минивэн'), ('Cobalt', '01A003AA', 'Легковая')):
    c, _ = Car.objects.get_or_create(plate_number=plate, defaults={'model': model, 'type': types[tname], 'status': 'active', 'num_seats': 4})
    c.drivers.add(driver)
    cars.append(c)

if CarOrder.objects.count() == 0:
    S = CarOrder.Status
    CarOrder.objects.create(created_by=requester, address='Ул. Амир Темур 67', project_name='Turandot', car_type=types['Легковая'], status=S.DRAFT)
    CarOrder.objects.create(created_by=requester, address='Ул. Шота Руставели 12', project_name='NRG', car_type=types['Минивэн'], status=S.PENDING)
    CarOrder.objects.create(created_by=requester, address='Пр. Мустакиллик 5', project_name='Akay City', car_type=types['Легковая'], status=S.AWAITING_DRIVER)
    from django.utils import timezone
    CarOrder.objects.create(created_by=requester, address='Ул. Бабура 33', project_name='Boulevard', car_type=types['Легковая'], driver=driver, car=cars[0], status=S.COMPLETED, started_at=timezone.now(), finished_at=timezone.now())

print('seed ok | logins: admin/admin12345, dispatcher/dispatcher12345, requester/requester12345, driver/driver12345')
"

# Serve ASGI (HTTP + WebSocket) via gunicorn + UvicornWorker — same as ark-backend.
# gunicorn manages the worker pool / graceful restarts; the UvicornWorker speaks
# ASGI so HTTP (incl. the gateway) AND WebSocket (live map / auto-dispatch push,
# routed by config/asgi.py) both work. With >1 worker the cross-process WS groups
# require the Redis channel layer (REDIS_HOST/REDIS_PORT).
echo "→ gunicorn+uvicorn :8000 (ASGI: HTTP + WebSocket)"
exec gunicorn config.asgi:application \
  -k uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --workers 4 \
  --timeout 120
