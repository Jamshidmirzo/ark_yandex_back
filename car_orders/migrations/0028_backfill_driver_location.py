from django.db import migrations


def forwards(apps, schema_editor):
    """Populate `location` from existing (lat, lng) on the geo DB.

    On the `default` migrate this RunPython is never invoked (GeoRouter.allow_migrate
    blocks car_orders no-model ops on default); the alias guard is belt-and-suspenders.
    The historical DriverPosition has the PointField but NOT the save() override, so we
    set `location` explicitly. Point takes (x=lng, y=lat).
    """
    if schema_editor.connection.alias != "geo":
        return

    from django.contrib.gis.geos import Point

    DriverPosition = apps.get_model("car_orders", "DriverPosition")
    for p in DriverPosition.objects.using("geo").filter(location__isnull=True):
        if p.lat is not None and p.lng is not None:
            p.location = Point(p.lng, p.lat, srid=4326)
            p.save(using="geo", update_fields=["location"])


class Migration(migrations.Migration):
    dependencies = [
        ("car_orders", "0027_driverposition_location"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
