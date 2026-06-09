"""Idempotent demo seed: a requester, dispatcher, driver, one car type + car."""

from django.contrib.auth import get_user_model

from auth_core.models import AccessGroup, UserAccessGroup
from car_orders.models import Car, CarType

User = get_user_model()


def user(username, *groups):
    obj, _ = User.objects.get_or_create(username=username)
    obj.set_password("pw")
    obj.is_active = True
    obj.save()
    for g in groups:
        grp = AccessGroup.objects.get(name=g)
        UserAccessGroup.objects.get_or_create(user=obj, group=grp)
    return obj


user("req", "Car Requester")
user("disp", "Car Admin")
drv = user("drv", "Driver")

ct, _ = CarType.objects.get_or_create(name="Легковая")
car, _ = Car.objects.get_or_create(
    plate_number="01A777AA",
    defaults={"model": "Chevrolet Cobalt", "type": ct, "status": "active"},
)
car.type = ct
car.status = "active"
car.save()
car.drivers.add(drv)

print(f"SEED_OK CAR_ID={car.id} CT_ID={ct.id}")
