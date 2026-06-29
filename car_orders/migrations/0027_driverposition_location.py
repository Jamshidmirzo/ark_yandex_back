import django.contrib.gis.db.models.fields
from django.db import migrations


class Migration(migrations.Migration):
    """Add the geography PointField mirror of (lat, lng) to DriverPosition. Applied
    only on the `geo` DB (the model lives there); spatial_index (default True) creates
    the GiST index that backs the index-assisted nearest query."""

    dependencies = [
        ("car_orders", "0026_postgis_extension"),
    ]

    operations = [
        migrations.AddField(
            model_name="driverposition",
            name="location",
            field=django.contrib.gis.db.models.fields.PointField(
                blank=True,
                geography=True,
                null=True,
                srid=4326,
                verbose_name="Location (synced from lat/lng)",
            ),
        ),
    ]
