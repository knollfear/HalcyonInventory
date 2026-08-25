"""Converting a plain scarf into a fancy one, by colorway.

A fancy veil is an already-dyed scarf with extra line work added. Physically
one object changes what it is; in the catalogue one product goes down and
another goes up. Nothing about that is derivable — no dye bath happens, no
sale happens, and the two products share only a recipe.

**This is the part worth systematising.** Roughly a hundred conversions went
unrecorded, which left the plain colorways overstated and the fancy ones
invisible, and nobody could say which. The rest of the app treats that as
something to *heal* (see CLAUDE.md on self-healing): the plain side turns up
as an overcount on its peg, the fancy side as an undercount on its, weeks
apart and never meeting. That backstop stays, and it is what makes it safe
for this page to be optional.

But a backstop is not a reason to make recording it hard. One number and one
button, per colorway, is cheaper than the correction it prevents — and unlike
the healing it says *what happened*, which is the only way "how many did we
fancy this season" is ever answerable.

## What it writes

Two `InventoryLog` rows, both `SOURCE_FANCY_CONVERSION`, carrying the same
sentence from opposite sides. Two rows rather than one because they are two
products and every other stock movement in this app is per product — a single
row would need a second foreign key that nothing else reads.

**The source clamps at zero and the target does not.** If somebody fancied
five and the app believed there were three, five really did get line work put
on them: the fancy side is right at +5, and the plain side was already wrong.
Refusing the conversion would protect a number that was wrong to begin with
and lose the one piece of evidence about it — so it goes through, the plain
side floors, and the discrepancy is reported rather than swallowed.
"""

from django.db import transaction

from .models import FinishedProduct, InventoryLog, RawProduct


def fancy_blanks():
    """Blanks a scarf can be converted *into* — the ones no dye bath makes.

    Keyed on `made_in_a_dye_bath` rather than on a name or a category,
    because that is already the marker the production lists use. A second way
    of saying "this is a fancy one" would be a second thing to keep in step.
    """
    return RawProduct.objects.filter(
        is_active=True, made_in_a_dye_bath=False
    ).order_by("name")


def convertible():
    """Plain products with stock that have a fancy counterpart to become.

    Ordered by colorway, because the conversion is a colorway decision — you
    decide to fancy some Aegean Sea, and only then which blank it comes off.
    """
    recipes = set(
        FinishedProduct.objects.filter(
            is_active=True,
            raw_product__made_in_a_dye_bath=False,
            recipe__isnull=False,
        ).values_list("recipe_id", flat=True)
    )
    return (
        FinishedProduct.objects.filter(
            is_active=True,
            raw_product__made_in_a_dye_bath=True,
            recipe_id__in=recipes,
            number_on_hand__gt=0,
        )
        .select_related("raw_product", "recipe")
        .order_by("recipe__name", "raw_product__name")
    )


def target_for(source, blank):
    """The fancy product this one becomes: same colorway, chosen blank."""
    if source.recipe_id is None:
        return None
    return (
        FinishedProduct.objects.filter(
            is_active=True, raw_product=blank, recipe_id=source.recipe_id
        )
        .select_related("raw_product", "recipe")
        .first()
    )


@transaction.atomic
def convert(source, blank, quantity, employee=None):
    """Move `quantity` from a plain product to its fancy counterpart.

    Returns `(target, shortfall)` — the product that went up, and how many
    more were fancied than the app believed existed. A shortfall is not an
    error: it is the app having been wrong, discovered by somebody doing the
    work, and it is reported so the plain side can be trued up.
    """
    quantity = max(int(quantity), 0)
    target = target_for(source, blank)
    if target is None or not quantity:
        return None, 0

    source = FinishedProduct.objects.select_related("raw_product").get(pk=source.pk)
    target = FinishedProduct.objects.select_related("raw_product").get(pk=target.pk)

    taken = min(quantity, source.number_on_hand)
    shortfall = quantity - taken

    who = f" by {employee.name}" if employee is not None else ""
    note = (
        f"Fancied{who}: {quantity} × {source.recipe.name} moved from "
        f"{source.raw_product.name} to {target.raw_product.name}."
    )
    if shortfall:
        note += (
            f" The app only had {source.number_on_hand} of the plain one, so "
            f"it was under by {shortfall}."
        )

    source.set_on_hand(source.number_on_hand - taken)
    _log(source, -taken, note)

    target.set_on_hand(target.number_on_hand + quantity)
    _log(target, quantity, note)

    return target, shortfall


def _log(product, delta, notes):
    return InventoryLog.objects.create(
        finished_product=product,
        raw_product=product.raw_product,
        log_type=InventoryLog.ADJUSTMENT,
        source=InventoryLog.SOURCE_FANCY_CONVERSION,
        quantity=delta,
        notes=notes,
    )
