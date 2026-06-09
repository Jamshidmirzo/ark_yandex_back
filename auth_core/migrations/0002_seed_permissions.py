"""Seed the ARK permission catalog and the access groups used by the
car-orders block. Codenames and group compositions mirror
ark-system-requirements/permissions.md so this block lines up with ark-backend.
"""

from django.db import migrations

PERMISSIONS = {
    "administrator": "Full system access",
    # Car orders
    "car_order:create": "Create a car order",
    "car_order:list_own": "View own car orders",
    "car_order:list": "View all car orders (dispatcher)",
    "car_order:approve": "Approve a pending car order",
    "car_order:reject": "Reject a car order",
    "car_order:dispatch": "Access the live dispatch / tracking screen",
    # Drivers
    "driver:accept_order": "Accept broadcast car orders (driver)",
    "driver:trip_control": "Control own trip: complete (driver)",
    "driver:list": "View the driver registry",
    "driver:assign_to_user": "Make / unmake a user a driver",
    # Garage
    "garage:list": "View cars and car types",
    "garage:retrieve": "Open a car card",
    "garage:create": "Create a car / car type",
    "garage:update": "Edit a car (incl. responsible drivers)",
    "garage:delete": "Decommission a car",
    # Vehicle reports
    "vehicle_report:create": "Submit a daily vehicle report",
    "vehicle_report:list_own": "View own vehicle reports",
    "vehicle_report:list": "View all vehicle reports",
    "vehicle_report:retrieve": "Open a single vehicle report",
}

ACCESS_GROUPS = {
    "Administrator": ["administrator"],
    "Car Requester": ["car_order:create", "car_order:list_own"],
    "Car Admin": [
        "car_order:create",
        "car_order:list",
        "car_order:approve",
        "car_order:reject",
        "car_order:dispatch",
    ],
    "Driver": [
        "driver:accept_order",
        "driver:trip_control",
        "vehicle_report:create",
        "vehicle_report:list_own",
        "vehicle_report:retrieve",
    ],
    "Garage Manager": [
        "garage:list",
        "garage:retrieve",
        "garage:create",
        "garage:update",
        "garage:delete",
        "driver:list",
        "vehicle_report:list",
        "vehicle_report:retrieve",
    ],
}


def seed(apps, schema_editor):
    Permission = apps.get_model("auth_core", "Permission")
    AccessGroup = apps.get_model("auth_core", "AccessGroup")

    perms = {}
    for codename, description in PERMISSIONS.items():
        perm, _created = Permission.objects.get_or_create(
            codename=codename, defaults={"description": description},
        )
        perms[codename] = perm

    for name, codenames in ACCESS_GROUPS.items():
        group, _created = AccessGroup.objects.get_or_create(name=name)
        group.permissions.set([perms[c] for c in codenames])


def unseed(apps, schema_editor):
    AccessGroup = apps.get_model("auth_core", "AccessGroup")
    Permission = apps.get_model("auth_core", "Permission")
    AccessGroup.objects.filter(name__in=ACCESS_GROUPS.keys()).delete()
    Permission.objects.filter(codename__in=PERMISSIONS.keys()).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("auth_core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
