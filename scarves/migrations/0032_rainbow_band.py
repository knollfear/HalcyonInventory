"""Rainbow becomes a section, and the colorways already filed as "no section"
become rainbows.

Adding the choice is a no-op at the database level. The data half is not, and
it is a **one-time reading of this database on 2026-08-26**, not a rule:

Confirmed-with-no-bands normally means "this colorway belongs in no section",
which is a decision somebody took and which `_by_color_counts` deliberately
keeps apart from "nobody has looked at it yet". Here it meant something else.
Every row in that state was a rainbow — Pastel Rainbow and Rainbow — filed
that way by giving up rather than by deciding, because until now the only
alternatives were claiming all eight sections or claiming none. The owner
confirmed that reading before this ran.

So this converts them and stops. Afterwards the state means what it always
meant, and a colorway that genuinely belongs nowhere can say so again.

The reverse is a no-op on purpose. Going back would have to set every
rainbow-only colorway to confirmed-and-empty, including ones filed after this
ran, which would be a different and wrong claim about them.
"""

import django.contrib.postgres.fields
from django.db import migrations, models


def rainbows_filed_as_nothing(apps, schema_editor):
    Recipe = apps.get_model("scarves", "Recipe")
    Recipe.objects.filter(
        bands_confirmed_at__isnull=False, color_bands=[]
    ).update(color_bands=["rainbow"])


class Migration(migrations.Migration):

    dependencies = [
        ('scarves', '0031_fancy_conversion_source'),
    ]

    operations = [
        migrations.AlterField(
            model_name='recipe',
            name='color_bands',
            field=django.contrib.postgres.fields.ArrayField(base_field=models.CharField(choices=[('red', 'Red'), ('orange', 'Orange'), ('yellow', 'Yellow'), ('green', 'Green'), ('blue', 'Blue'), ('purple', 'Purple'), ('pink', 'Pink'), ('brown', 'Brown'), ('grey', 'Grey'), ('black', 'Black'), ('rainbow', 'Rainbow')], max_length=12), blank=True, default=list, help_text='Which sections of the rainbow reference sheet this colorway is printed in. A red-and-orange scarf claims both and prints twice.', size=None),
        ),
        migrations.RunPython(rainbows_filed_as_nothing, migrations.RunPython.noop),
    ]
