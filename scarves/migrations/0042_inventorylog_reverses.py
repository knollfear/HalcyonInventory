"""A production entry can be taken back, and the way it is taken back is a row.

Nothing is edited and nothing is deleted: the original `InventoryLog` stays
where it is and a compensating entry is written beside it pointing back at it,
so the ledger says a thing was recorded and then retracted — which is what
happened — rather than quietly ceasing to mention it. Same bargain
`closing.undo` already makes for the Sunday close.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scarves", "0041_raw_counted_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventorylog",
            name="reverses",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Set on a compensating entry: the earlier row this one "
                    "takes back. Blank on everything that happened on its "
                    "own account."
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="reversals",
                to="scarves.inventorylog",
            ),
        ),
        migrations.AlterField(
            model_name="inventorylog",
            name="source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("production_sheet", "Production sheet"),
                    ("production_needed", "Production-needed page"),
                    ("recipe_page", "Recipe page"),
                    ("card_backfill", "Kanban card backfill"),
                    ("bulk_update", "Bulk inventory update"),
                    ("sunday_close", "Sunday close"),
                    ("restock", "Restocking the display"),
                    ("fancy_conversion", "Converted to fancy"),
                    ("square_webhook", "Square webhook"),
                    ("square_import", "Square sales import"),
                    ("unmatched_sale", "Unidentified sale, resolved"),
                    ("production_undo", "Production, taken back"),
                    ("test", "Simulated (fake_sale)"),
                ],
                db_index=True,
                help_text=(
                    "Which flow wrote this row. Blank means it predates the "
                    "field — not that nobody knows, since the notes usually "
                    "say. Left blank rather than back-filled by "
                    "pattern-matching those notes, because a guessed "
                    "provenance counts identically to a recorded one and "
                    "there is nothing on the row to say which it was."
                ),
                max_length=30,
            ),
        ),
    ]
