# Local overlay for «driver on shift» (Р1) — demo has no set-shift endpoint.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('car_orders', '0015_driverposition'),
    ]

    operations = [
        migrations.CreateModel(
            name='DriverShiftState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('driver_id', models.PositiveIntegerField(db_index=True, unique=True, verbose_name='Driver id')),
                ('car_id', models.PositiveIntegerField(verbose_name='Car id')),
                ('car_model', models.CharField(blank=True, max_length=255)),
                ('car_plate', models.CharField(blank=True, max_length=64)),
                ('car_type_id', models.PositiveIntegerField(blank=True, null=True)),
                ('car_type_name', models.CharField(blank=True, max_length=255)),
                ('status', models.CharField(default='online', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'Driver shift (overlay)',
                'verbose_name_plural': 'Driver shifts (overlay)',
            },
        ),
    ]
