"""The rows a blank implies, and the one place that makes them.

**A blank is rarely one row.** A plain silk veil is one `RawProduct`. A veil that
also comes fancy is two, because the fancy version is its own blank with its
own cost. A yarn sold undyed is a `RawProduct` *and* a `FinishedProduct` with
no recipe, because the pile and the thing Square sells are different objects.
And a blank that gets dyed is one row *per colourway* — a few hundred of
them, which is the whole shape of this catalogue.

None of those could be made from a page. The fancy one meant creating a
blank, remembering to mark it made-here, and then going back to the first to
point at it — an order nothing announced and an empty dropdown that read as
*this is broken*. The undyed one meant running a management command. The
colourways meant typing forty products by hand, which is the sort of job
that gets half done. All three are mechanical the moment somebody says the
blank needs them, which is what these functions are: **say it on the blank,
and the rest follows.**

All three are additive and idempotent, and none ever un-makes anything.
Unticking a box does not delete a fancy blank, a sellable row or a colourway
— those are rows with history, and `CLAUDE.md` has the argument. Retiring is
how one goes away.
"""

from __future__ import annotations

from decimal import Decimal

from .models import FinishedProduct, RawProduct, Recipe

#: What a passthrough is priced at when the blank has no suggested price.
#: Deliberately conspicuous rather than plausible — the same figure and the
#: same reasoning as `create_passthrough_products`, which now calls this.
#:
#: A pound rather than nothing, because zero is the dangerous kind of wrong:
#: valid, syncs to Square, and rings up free at the till with a queue behind
#: it. Null and zero are different here — a deliberate zero is honoured,
#: because a giveaway is a real product; only a missing price is replaced.
FALLBACK_PRICE = Decimal("1.00")


def ensure_fancy_counterpart(blank, fancying_cost=None):
    """Give `blank` a fancy version, if it hasn't got one. Returns it.

    **Made rather than chosen, because there is nothing to choose from.** The
    fancy version of a half circle veil is a blank that exists only to be
    that — one to one, and a veil cannot become a fancy shawl — so asking
    somebody to create it first and then come back to point at it was asking
    them to do the app's bookkeeping in the right order. Saying *this also
    comes in a fancy version* is the whole of the decision.

    **It costs nothing to be wrong.** A fancy blank nobody sells is a row
    with par 0, no stock and no colorways: it appears on no production list
    (nothing can dye it), asks for no order (nothing buys it), and is retired
    with a checkbox. That asymmetry is the argument for making it eagerly —
    the cost of the missing row is a page that reads as broken, and the cost
    of a spare one is a line in a picker.

    `price` is zero on purpose and not a gap: a fancy blank has no supplier
    and no supplier price. What one costs is the plain blank's cost plus
    `fancying_cost`, derived on every read by `RawProduct.blank_cost`, which
    is what keeps the silk priced in exactly one row.
    """
    if blank.fancy_counterpart_id:
        existing = blank.fancy_counterpart
        if fancying_cost is not None and existing.fancying_cost != fancying_cost:
            existing.fancying_cost = fancying_cost
            existing.save(update_fields=["fancying_cost"])
        return existing

    fancy = RawProduct.objects.create(
        name=f"Fancy {blank.name}",
        category=blank.category,
        price=Decimal("0"),
        suggested_price=blank.suggested_price,
        number_per_dye_bath=blank.number_per_dye_bath,
        made_in_a_dye_bath=False,
        fancying_cost=fancying_cost,
        # No supplier and no order URL: nobody sells one of these. Copying
        # the plain blank's would put a fancy veil on a reorder page pointing
        # at a page that sells the plain one.
        finished_par_default=blank.finished_par_default,
        display_slots_default=blank.display_slots_default,
        # Par 0: production never proposes one, and an order can't fill it.
        par_level=0,
    )
    blank.fancy_counterpart = fancy
    blank.save(update_fields=["fancy_counterpart"])
    return fancy


def ensure_undyed_product(blank):
    """Give `blank` the row that makes it sellable undyed. Returns it.

    **One physical pile, two rows**, which is the shape `docs/claude/stock.md`
    describes: the `RawProduct` is the pile and this `FinishedProduct` is the
    thing Square sells, with the count mirrored between them by the
    `post_save` in `signals.py`. A null `recipe` is what marks it as never
    having been dyed.

    Keyed on the blank having *a passthrough*, not any finished product at
    all: a yarn that is both sold undyed and dyed into forty colorways is the
    normal case, and its colorways must not stop the undyed row being made.

    `par` is 0 here because the par that matters lives on the blank, as
    `par_level` — you order these rather than making them, and a par on this
    row would be a number nothing reads.
    """
    existing = FinishedProduct.objects.filter(
        raw_product=blank, recipe__isnull=True
    ).first()
    if existing is not None:
        return existing

    price = blank.suggested_price
    if price is None:
        price = FALLBACK_PRICE
    return FinishedProduct.objects.create(
        name=blank.name,
        raw_product=blank,
        recipe=None,
        price=price,
        par=0,
    )


def ensure_colorways(blank, recipes):
    """Make this blank in each of these colourways. Returns what was made.

    **This is the shape of the catalogue, and the reason a new blank is a
    day's work rather than a row.** `CLAUDE.md` puts it the other way round —
    a new product is almost never a new *style*, it is another colour of
    something that already exists — and the corollary is the expensive case:
    when a genuinely new blank does arrive, it needs a finished product per
    colourway, and there are a few hundred colourways. Forty of those typed
    by hand is the sort of job that gets half done.

    Everything about the new row is already decided by the blank, which is
    what makes this bookkeeping rather than a decision:

    - **name** is `{blank} - {colourway}`, the convention every existing row
      follows;
    - **par** is `finished_par_default` and **display slots** is
      `display_slots_default` — the two fields whose entire purpose is to be
      inherited at creation, and which deliberately never rewrite a product
      that already exists;
    - **price** is the blank's `suggested_price`, or a conspicuous
      `FALLBACK_PRICE` when there isn't one, the same rule the undyed row
      uses and for the same reason: zero syncs happily and rings up free;
    - **sku** fills itself in `FinishedProduct.save()`.

    **`oven_dyed` is left alone, at False.** It is a typed flag on the
    blank-and-colourway pair, and the same colour is oven in one yarn and not
    in another — inheriting it from another blank's row would be a guess that
    reads as a fact, and the guess is wrong often enough to matter.

    Additive and idempotent: a colourway this blank already has is skipped,
    never duplicated and never rewritten. Nothing is ever removed — a
    finished product that sold is pointed at by its sales, and retiring is
    how one goes away.
    """
    recipes = list(recipes)
    if not recipes:
        return [], []

    existing = set(
        FinishedProduct.objects
        .filter(raw_product=blank, recipe__in=recipes)
        .values_list("recipe_id", flat=True)
    )
    price = blank.suggested_price
    if price is None:
        price = FALLBACK_PRICE

    made, skipped = [], []
    for recipe in recipes:
        if recipe.pk in existing:
            skipped.append(recipe)
            continue
        made.append(FinishedProduct.objects.create(
            name=f"{blank.name} - {recipe.name}",
            raw_product=blank,
            recipe=recipe,
            price=price,
            par=blank.finished_par_default,
            display_slots=blank.display_slots_default,
        ))
    return made, skipped
