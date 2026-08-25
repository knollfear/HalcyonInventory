"""Display capacity, and the close rebuilt on top of it.

The old close read a kanban tag as "set this product to zero", which deleted
the one to three units still hanging on the display. That is how an inventory
tracker quietly becomes a backstock tracker: once display stock is invisible,
filling a new rack out of the bags reads exactly like a weekend of sales, and
the app calls for dye baths nobody sold anything to justify.

`display_slots` is what makes the difference sayable — capacity, never a
target. Nothing schedules production to fill it.

The `CloseRun` rows are deleted rather than migrated. There were no valid
closes when this landed (confirmed with the shop), and the old rows record
"adjusted to zero" decisions that are wrong under the new reading — keeping
them would put a fabricated correction into the one number the close history
is for.
"""

from django.db import migrations, models


def clear_closes(apps, schema_editor):
    apps.get_model("scarves", "CloseRunRow").objects.all().delete()
    apps.get_model("scarves", "CloseRun").objects.all().delete()


def seed_display_slots(apps, schema_editor):
    """Give every existing product its blank's default.

    A one-time backfill, not an ongoing rule: after this, `display_slots` is
    the product's own and changing a blank's default never rewrites it — the
    same contract `finished_par_default` has, and for the same reason.
    """
    FinishedProduct = apps.get_model("scarves", "FinishedProduct")
    for product in FinishedProduct.objects.select_related("raw_product"):
        slots = product.raw_product.display_slots_default
        if product.display_slots != slots:
            FinishedProduct.objects.filter(pk=product.pk).update(display_slots=slots)


class Migration(migrations.Migration):

    dependencies = [
        ("scarves", "0024_alter_boothphoto_reason"),
    ]

    operations = [
        migrations.AddField(
            model_name="rawproduct",
            name="display_slots_default",
            field=models.PositiveIntegerField(
                default=2,
                help_text=(
                    "How many units of a new colorway of this blank go on "
                    "display — a peg that holds two skeins, a spot on the "
                    "scarf pole. Only applies at creation, like "
                    "finished_par_default; use the 'Bulk update display "
                    "slots' action to change existing ones. This is display "
                    "*capacity*, and it is not a production target: see "
                    "FinishedProduct.display_slots."
                ),
            ),
        ),
        migrations.AddField(
            model_name="finishedproduct",
            name="display_slots",
            field=models.PositiveIntegerField(
                default=2,
                help_text=(
                    "How many of this go on display when the display is full "
                    "— pegs times what a peg holds, or spots on the pole. "
                    "**Capacity, not a target.** Nothing schedules production "
                    "to fill it: a bigger display is somewhere to put stock, "
                    "never a reason to make more, and par does not move when "
                    "the shop gets a new rack.\n\nWhat it is for is telling "
                    "the Sunday close where the backstock ends. "
                    "`number_on_hand` counts display and backstock together, "
                    "so `number_on_hand <= display_slots` is the app saying "
                    "the bag is empty — which is exactly when the crew should "
                    "be holding this product's kanban tag. Zero means this "
                    "never goes on display, so no tag will ever come up for "
                    "it and the close leaves it alone."
                ),
            ),
        ),
        migrations.RunPython(seed_display_slots, migrations.RunPython.noop),
        migrations.RunPython(clear_closes, migrations.RunPython.noop),
        migrations.AddField(
            model_name="closerunrow",
            name="display_slots",
            field=models.PositiveIntegerField(
                default=0,
                help_text=(
                    "What the display held when this row was made. Frozen for "
                    "the same reason the production sheet freezes its bath "
                    "size: the person answered '0, 1 or 2?' because that is "
                    "what the paper and the pegs said that night, and "
                    "re-reading it later reads back a display that has since "
                    "been rebuilt."
                ),
            ),
        ),
        migrations.AddField(
            model_name="closerunrow",
            name="added_by_tag",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "This row was not predicted — somebody was holding a tag "
                    "the close didn't ask about. Kept apart from the "
                    "*outcome*, which records which way the app was wrong: an "
                    "unpredicted tag usually means an overcount but doesn't "
                    "have to, and a predicted row can come out over too. "
                    "Conflating the two put the wrong thing in the one number "
                    "this page produces."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="closerunrow",
            name="counted",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                help_text=(
                    "The true total found in hand — everything on display "
                    "plus whatever is left in the bag after the display was "
                    "filled. Every answered row has one; the outcome is the "
                    "sign of `counted - on_hand_before`."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="closerunrow",
            name="outcome",
            field=models.CharField(
                choices=[
                    ("pending", "Not counted yet"),
                    ("confirmed", "Counted — the app agreed"),
                    ("missing", "Counted more than the app had — app undercounts"),
                    ("extra", "Counted less than the app had — app overcounts"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
