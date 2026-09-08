"""A production row gets three end states and a yield.

The sheet used to ask one yes/no question per bath, and a tick moved a whole
bath's worth of stock. That was right while dyeing was modelled as an atomic
event; it is wrong for a process that takes one to three days and
occasionally produces fewer scarves than it was asked for — or none.

Three things change shape here:

- `done_at` becomes `accepted_at`, because "done" was the ambiguous word.
  Done at the pot and accepted into inventory are one to three days apart,
  and it is the second one the count depends on. Renamed rather than
  replaced, so the timestamps already recorded survive.
- `yielded` says how many actually came out. Null means nobody has accepted
  the row yet; zero means the bath ran and the lot was binned, which is a
  different event from the bath never running at all.
- `cancelled_at` is that other event. No blanks consumed, no log written,
  and the colorway goes back to being asked for on the next sheet.

**`yielded` is backfilled to `quantity` on rows that already moved stock**,
which is not the guess it might look like. Under the old model a ticked row
applied exactly `quantity` units and wrote an `InventoryLog` saying so, so
the number is recorded rather than inferred — unlike `InventoryLog.source`,
which was deliberately left blank for old rows because pattern-matching the
notes would have produced a guess that counted the same as a fact.
"""

from django.db import migrations, models


def fill_yield_from_applied_rows(apps, schema_editor):
    ProductionRunRow = apps.get_model("scarves", "ProductionRunRow")
    ProductionRunRow.objects.filter(applied_log__isnull=False).update(
        yielded=models.F("quantity")
    )


def unfill(apps, schema_editor):
    ProductionRunRow = apps.get_model("scarves", "ProductionRunRow")
    ProductionRunRow.objects.update(yielded=None)


class Migration(migrations.Migration):

    dependencies = [
        ("scarves", "0035_sale_line_variation_id"),
    ]

    operations = [
        migrations.RenameField(
            model_name="productionrunrow",
            old_name="done_at",
            new_name="accepted_at",
        ),
        migrations.AlterField(
            model_name="productionrunrow",
            name="accepted_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text=(
                    "When this bath was accepted into inventory — dyed, dried, "
                    "tagged, bagged and ready for the booth. Not when it went into "
                    "the pot: dyeing takes one to three days and the stock does not "
                    "exist for any of them."
                ),
            ),
        ),
        migrations.AddField(
            model_name="productionrunrow",
            name="cancelled_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text=(
                    "When this bath was called off. The bath never ran, so no blanks "
                    "were consumed and the colorway goes back to being asked for on "
                    "the next sheet.\n\n"
                    "Deliberately not the same as accepting a yield of zero, which "
                    "means the bath ran and the lot was lost."
                ),
            ),
        ),
        migrations.AddField(
            model_name="productionrunrow",
            name="yielded",
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                help_text=(
                    "How many units this bath actually produced, once somebody "
                    "accepted it into inventory. Null means nobody has yet.\n\n"
                    "Zero is a real answer and is not the same as null: it means the "
                    "bath ran and the whole lot was binned, so the blanks really were "
                    "consumed. A cancelled row is the other thing — the bath never "
                    "happened and nothing was used."
                ),
            ),
        ),
        migrations.RunPython(fill_yield_from_applied_rows, unfill),
        migrations.AlterField(
            model_name="productionrun",
            name="submitted_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text=(
                    "When the crew first reported back. A record of the first reply "
                    "and nothing more — it decides no state on this sheet.\n\n"
                    "It used to mean 'closed', on the reasoning that one tick proved "
                    "somebody was working from the paper. That is true and it is not "
                    "the same question: dyeing takes one to three days, so a sheet "
                    "answered once is usually a sheet with most of its baths still "
                    "wet. What is open, overdue or finished is read off the rows."
                ),
            ),
        ),
    ]
