"""Add the ``car_order:create_direct`` permission — a requester who holds it
creates an order that skips the draft→pending→approve dance and lands straight in
the driver queue (``awaiting_driver``). Granted to the "Car Requester" group so the
default customer can self-serve, WITHOUT giving them any dispatch/driver authority.
"""

from django.db import migrations

CODENAME = "car_order:create_direct"
DESCRIPTION = "Create a car order that skips approval (straight to the driver queue)"
REQUESTER_GROUP = "Car Requester"


def seed(apps, schema_editor):
    Permission = apps.get_model("auth_core", "Permission")
    AccessGroup = apps.get_model("auth_core", "AccessGroup")

    perm, _ = Permission.objects.get_or_create(
        codename=CODENAME, defaults={"description": DESCRIPTION}
    )
    group, _ = AccessGroup.objects.get_or_create(name=REQUESTER_GROUP)
    group.permissions.add(perm)


def unseed(apps, schema_editor):
    Permission = apps.get_model("auth_core", "Permission")
    Permission.objects.filter(codename=CODENAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("auth_core", "0002_seed_permissions"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
