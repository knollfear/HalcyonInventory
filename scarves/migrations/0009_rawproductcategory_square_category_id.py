# Closes the known migration drift: RawProductCategory.square_category_id has
# existed on the model (and in prod, added out of band) but was never migrated,
# so any database built from migrations — every test run — lacked the column.
#
# A plain AddField would fix fresh databases and crash on prod, where the column
# already exists. So the schema change is done as idempotent raw SQL, while
# state_operations tells Django's migration state that the field now exists.
# Both a fresh test database and prod end up correct, and this migration is
# safe to run against either.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scarves', '0008_productimage_upload'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE scarves_rawproductcategory "
                        "ADD COLUMN IF NOT EXISTS square_category_id "
                        "varchar(100) NOT NULL DEFAULT '';"
                        "ALTER TABLE scarves_rawproductcategory "
                        "ALTER COLUMN square_category_id DROP DEFAULT;"
                    ),
                    reverse_sql=(
                        "ALTER TABLE scarves_rawproductcategory "
                        "DROP COLUMN IF EXISTS square_category_id;"
                    ),
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='rawproductcategory',
                    name='square_category_id',
                    field=models.CharField(
                        blank=True,
                        help_text='Square CATEGORY catalog object ID.',
                        max_length=100,
                    ),
                ),
            ],
        ),
    ]
