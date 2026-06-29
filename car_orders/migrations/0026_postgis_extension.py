from django.contrib.postgres.operations import CreateExtension
from django.db import migrations


class Migration(migrations.Migration):
    """Enable the PostGIS extension in the `geo` database.

    Runs ONLY on `geo`: GeoRouter.allow_migrate permits car_orders no-model ops on
    geo and blocks them on default. CreateExtension also no-ops on a non-postgresql
    connection, so it is safe regardless. This must precede the PointField column.
    """

    dependencies = [
        ("car_orders", "0025_ordermeta_car_type_name_ordermeta_created_by_name_and_more"),
    ]

    operations = [
        CreateExtension("postgis"),
    ]
