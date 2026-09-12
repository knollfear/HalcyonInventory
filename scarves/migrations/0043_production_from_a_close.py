"""A production list can come out of a Sunday close, and can skip the paper.

Two fields, both additive, and existing rows keep meaning exactly what they
meant: `close_run` null is "planned off par", and `reporting` defaults to
`paper`, which is what every sheet made so far was.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scarves", "0042_inventorylog_reverses"),
    ]

    operations = [
        migrations.AddField(
            model_name="productionrun",
            name="close_run",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "The Sunday close whose cards this list was built from. "
                    "Blank means it was planned from par shortages instead."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="production_runs",
                to="scarves.closerun",
            ),
        ),
        migrations.AddField(
            model_name="productionrun",
            name="reporting",
            field=models.CharField(
                choices=[
                    ("paper", "Printed sheet, reported by QR"),
                    ("direct", "No paper — reported on screen"),
                ],
                default="paper",
                help_text=(
                    "How this list comes back. Paper is the dye-room sheet: "
                    "three printed documents, a pencil, and one QR scanned "
                    "afterwards. Direct skips the printing — the same list, "
                    "reported on screen.\n\n"
                    "Asked once, when the list is made, because it is the one "
                    "thing about a list that cannot be both. A page offering "
                    "a printout *and* an on-screen report is a page asking "
                    "somebody to decide again every time they open it, and "
                    "the reporting flow is identical either way — only the "
                    "door differs."
                ),
                max_length=10,
            ),
        ),
    ]
