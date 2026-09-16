"""Hours say what kind of work they were, and the day key grows a column.

Every row written before this one was booth hours — that is all the form
would take — so the `booth` default is a statement of fact about the existing
data rather than a guess, and no data migration is needed.

The constraint swap is the part with teeth. It has to drop before the column
lands, because the new one keys on a field that does not exist yet; Django
orders it that way already and the order is load-bearing rather than
incidental. Widening the key is safe on existing rows (one row per person per
day is still one row per person per day per kind), and it is what lets
somebody dye in the morning and work the booth in the afternoon without
adding the two together.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scarves', '0049_ounces_to_the_tenth'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='timeentry',
            name='one_time_entry_per_employee_per_day',
        ),
        migrations.AddField(
            model_name='timeentry',
            name='kind',
            field=models.CharField(choices=[('booth', 'Booth'), ('dyeing', 'Dyeing')], default='booth', help_text='What the hours were for. Both are paid the same hourly rate.', max_length=16),
        ),
        migrations.AddConstraint(
            model_name='timeentry',
            constraint=models.UniqueConstraint(fields=('employee', 'work_date', 'kind'), name='one_time_entry_per_employee_per_day_per_kind'),
        ),
    ]
