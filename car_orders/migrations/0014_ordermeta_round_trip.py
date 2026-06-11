# Round-trip («туда-обратно») as one order: has_return + return point + returning.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('car_orders', '0013_ordermeta_parent_order_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='ordermeta',
            name='has_return',
            field=models.BooleanField(default=False, verbose_name='Has return leg'),
        ),
        migrations.AddField(
            model_name='ordermeta',
            name='return_lat',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='ordermeta',
            name='return_lng',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='ordermeta',
            name='returning',
            field=models.BooleanField(default=False, verbose_name='On the return leg'),
        ),
    ]
