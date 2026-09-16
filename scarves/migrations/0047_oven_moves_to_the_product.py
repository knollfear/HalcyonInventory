from django.db import migrations, models


def carry_the_flag_over(apps, schema_editor):
    """Copy each colorway's oven flag onto its products, minus the silk.

    **The category test belongs here and nowhere else.** Silk is always
    microwaved, so an oven-flagged colorway dyed on silk was never oven work on
    that blank — the old flag simply could not say so. Reading the category is
    the right way to apply that fact *once*, to the rows as they stand today; it
    would be the wrong way to decide it from here on, which is why nothing in
    the app tests a category and the answer is typed per product instead. Same
    line `RawProduct.made_in_a_dye_bath` draws.

    Silent either way, so it is worth being explicit about what this does to a
    misfiled row: an oven colorway on silk comes out as microwave work, which
    puts it back on the ordinary sheet it had dropped off. If any of those
    really were oven-dyed, the box on the recipe row is how it gets said.
    """
    FinishedProduct = apps.get_model("scarves", "FinishedProduct")
    for product in (
        FinishedProduct.objects
        .filter(recipe__oven_dyed=True)
        .exclude(raw_product__category__name="Silk")
        .iterator()
    ):
        product.oven_dyed = True
        product.save(update_fields=["oven_dyed"])


def put_it_back_on_the_colourway(apps, schema_editor):
    """Reverse: a colorway is oven-dyed if any of its products is.

    Lossy on purpose and in the one direction that cannot hurt — going back
    means going back to a model that has no way to hold the distinction, so the
    honest reversal is the union. Nothing is lost that the old schema could
    have stored.
    """
    Recipe = apps.get_model("scarves", "Recipe")
    for recipe in Recipe.objects.filter(
        finished_products__oven_dyed=True
    ).distinct().iterator():
        recipe.oven_dyed = True
        recipe.save(update_fields=["oven_dyed"])


class Migration(migrations.Migration):
    """Move the oven flag from the colorway to the blank-and-colorway pair.

    Add, carry over, then drop — in that order, because the seed has to read
    the old column while it is still there.
    """

    dependencies = [
        ("scarves", "0046_row_money_frozen_at_accept"),
    ]

    operations = [
        migrations.AddField(
            model_name="finishedproduct",
            name="oven_dyed",
            field=models.BooleanField(
                default=False,
                db_index=True,
                help_text=(
                "Tick for a thing made in the oven rather than the microwave, and "
                "it moves from one kind of session to the other.\n\n"
                "**It lives here, on the blank-and-colorway pair, because that is "
                "what decides it.** It was on `Recipe` first, on the reasoning "
                "that a technique is a property of the colour and so needs "
                "answering once rather than a few hundred times. That is half "
                "true and the missing half is silent: silk is never oven-dyed — it "
                "always goes in the microwave — so a colorway dyed on both silk "
                "and yarn is oven work on one and not the other, and a flag on "
                "the colour cannot say so. It said 'oven' for both, which took "
                "the silk off the microwave sheet as well as putting it on an oven "
                "sheet for an appliance it never enters. The row looked like every "
                "other row either way.\n\n"
                "The same shape as `RawProduct.made_in_a_dye_bath` and for the "
                "same reason: a category test ('silk means microwave') is right "
                "today and wrong the day something arrives that breaks it, and it "
                "breaks silently. A typed answer per pair is always right and can "
                "be looked at.\n\n"
                "**Typed, never derived.** There is a rule — a colour name goes in "
                "the oven, an idea doesn't — and it is a rule about the world "
                "rather than about the string: `Forest Fire` is two colour words "
                "and is not oven work, `Burnt Orange` is two words and is. Every "
                "version of guessing is confidently wrong on the cases that "
                "decide it, and wrong here is silent — the row lands on the other "
                "session's sheet and reads like any other.\n\n"
                "**It partitions both ways, which is the load-bearing half.** An "
                "oven colorway on a microwave sheet sends somebody to make a thing "
                "that is not made there, and a microwave one missing from that "
                "sheet is a shortage nobody can see. `production.candidates(oven=)` "
                "returns two disjoint populations and defaults to the microwave, "
                "which keeps every existing caller meaning what it meant."
                ),
            ),
        ),
        migrations.RunPython(carry_the_flag_over, put_it_back_on_the_colourway),
        migrations.RemoveField(
            model_name="recipe",
            name="oven_dyed",
        ),
    ]
