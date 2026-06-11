# Per-driver latest GPS — so the dispatcher can find the nearest free driver.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('car_orders', '0014_ordermeta_round_trip'),
    ]

    operations = [
        migrations.CreateModel(
            name='DriverPosition',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('driver_id', models.PositiveIntegerField(db_index=True, unique=True, verbose_name='Driver id')),
                ('lat', models.FloatField(verbose_name='Latitude')),
                ('lng', models.FloatField(verbose_name='Longitude')),
                ('last_seen', models.DateTimeField(verbose_name='Last seen')),
            ],
            options={
                'verbose_name': 'Driver position',
                'verbose_name_plural': 'Driver positions',
            },
        ),
    ]
