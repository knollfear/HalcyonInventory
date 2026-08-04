# Hand-written to stay isolated from the known migration drift
# (square_category_id is unmigrated in git). This migration only touches
# FinishedProductImage and adds ProductImageUpload — it never references
# square_category_id, so it can't collide with prod's existing column.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scarves', '0007_inventorylog_delete_productionlog'),
    ]

    operations = [
        migrations.AddField(
            model_name='finishedproductimage',
            name='image',
            field=models.ImageField(
                blank=True,
                help_text='Uploaded image file (stored in the bucket).',
                upload_to='finished_products/',
            ),
        ),
        migrations.AlterField(
            model_name='finishedproductimage',
            name='image_url',
            field=models.URLField(
                blank=True,
                help_text='Optional: URL of an externally-hosted image.',
            ),
        ),
        migrations.CreateModel(
            name='ProductImageUpload',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(help_text='Object key in the bucket.', max_length=255, unique=True)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('matched', 'Matched (barcode)'), ('assigned', 'Assigned (manual)'), ('failed', 'Failed')], default='pending', max_length=20)),
                ('detected_sku', models.CharField(blank=True, help_text='SKU decoded from the barcode, if any.', max_length=50)),
                ('error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('finished_product', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='image_uploads', to='scarves.finishedproduct')),
                ('product_image', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='scarves.finishedproductimage')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
