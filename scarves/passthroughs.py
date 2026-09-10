"""Making something Square already sells into something this app tracks.

The bits and bobs — a yarn bowl, a wood spindle, a needle case — are bought
and sold exactly as they arrive. No dye bath, no colorway, no production step
of any kind. They were rung up at the till long before the app had heard of
them, so every sale of one landed in `private/unidentified-sales/` and the
count of what is on the shelf was never anything at all.

**A passthrough is marked by a null `recipe`, and that is the only marker it
needs.** Every dyed-only query in the app joins through that FK, so these drop
out of production planning, the rainbow sheets, the colour pages and the games
*by construction* rather than by each of them remembering to exclude a notion.
See `FinishedProduct.recipe`.

**What it must not be marked with is `made_in_a_dye_bath=False`.** That reads
like the right flag and is the wrong one: it is the fancy-veil marker, for a
thing that *has* a colorway and still cannot be dyed into existence, and
`fancy.fancy_blanks()` filters on exactly it. Setting it here would offer a
yarn bowl on the fancy conversion page as something a silk scarf could be
turned into. Two markers, two different claims — a passthrough was never dyed
at all, a fancy veil was dyed and then worked on.

## Why the Square ids are the whole point

A new row here is worth little on its own; what it buys is that the *next*
sale identifies itself. `square_webhook` matches a line's `catalog_object_id`
against `FinishedProduct.square_variation_id`, so a passthrough carrying that
id stops arriving in the queue at all. And carrying the parent
`square_item_id` is what stops a later `sync_to_square` deciding the item is
new and creating a second one beside it — the duplicate-shelf failure, which
is silent and splits the sale history.

So this never *creates* anything in Square. It only ever writes down what
Square already has.

## One pile, one row

`FinishedProduct.is_passthrough` means the raw row and the finished row
describe the same physical object, and `mirror_passthrough_stock` keeps the
finished count in step with the raw one. That only works while a raw product
has **at most one** passthrough finished row — two would mirror the same pile
and disagree about it silently.

So a Square item with several variations does not become one raw product with
several finished rows. It becomes a `CatalogGroup` for the item and **one raw
product per variation**, which is what `CatalogGroup` already exists for: the
item is the shared name and the variations are the things underneath. Same
shape as the undyed yarns, arrived at for the same reason.
"""

from decimal import Decimal

from django.db import transaction

from .models import CatalogGroup, FinishedProduct, RawProduct


def tracked_variation_ids():
    """Every Square variation id this app already has a product for."""
    return set(
        FinishedProduct.objects
        .exclude(square_variation_id="")
        .values_list("square_variation_id", flat=True)
    )


def tracked_item_ids():
    """Every Square item id this app already knows, blanks and groups alike."""
    ids = set(
        RawProduct.objects.exclude(square_item_id="")
        .values_list("square_item_id", flat=True)
    )
    ids |= set(
        CatalogGroup.objects.exclude(square_item_id="")
        .values_list("square_item_id", flat=True)
    )
    return ids


def untracked(items, known_item_ids):
    """Square items worth offering, out of everything `search` returned.

    Two exclusions, and the second is the one worth writing down.

    **Anything already tracked**, by item id — the ordinary case.

    **Anything archived.** Square keeps an archived item out of the POS but
    still returns it from a catalogue search, so it reads exactly like an
    untracked item somebody has yet to deal with. It is the opposite: it is
    one that has been dealt with, by being retired. Last season's `Undyed
    Yarn` is the worked example — archived, ten variations at $0.00, 122 sale
    lines that all stop in October 2025, sitting beside this season's live
    item of the same name. Listing it invites somebody to "fix" a split that
    is really a succession, and importing it would create products for ten
    yarns at a price of nothing.

    Returns `(rows, archived_count)` so a caller can say how many it passed
    over. Counted rather than silent: a list that quietly shrinks is one
    nobody can check.
    """
    rows, archived = [], 0
    for obj in items:
        if obj["id"] in known_item_ids:
            continue
        data = obj.get("item_data") or {}
        if data.get("is_archived"):
            archived += 1
            continue
        rows.append((data, obj["id"]))
    return rows, archived


def for_variation(variation_id):
    """The product already tracking this Square variation, or `None`.

    What makes creation idempotent. The queue holds one row per order line,
    so the same item sells again and again — and the second time it comes up
    the answer is "you already made this", not a second product splitting the
    count with the first.
    """
    if not variation_id:
        return None
    return (
        FinishedProduct.objects
        .filter(square_variation_id=variation_id)
        .select_related("raw_product", "recipe")
        .first()
    )


@transaction.atomic
def create(
    *,
    name,
    category,
    price,
    variation_id="",
    item_id="",
    group=None,
    cost=Decimal("0"),
):
    """Make the raw row and the sellable row for one passthrough.

    Returns `(product, created)`. `created` is False when the variation was
    already tracked, in which case the existing product comes back untouched.

    `price` is what it sells for and is not guessed: the caller takes it from
    what Square actually charged. `cost` defaults to zero because the sale
    cannot say what it cost, and zero is honest there in a way it would not be
    on the selling side — a cost of nothing overstates margin on a report
    nobody is looking at yet, where a *price* of nothing rings up free at the
    till with a queue behind it. It gets filled in on
    `private/raw-inventory/`, which is already the reorder workflow and where
    a bought-in thing belongs.
    """
    existing = for_variation(variation_id)
    if existing is not None:
        return existing, False

    name = (name or "").strip()[:150]
    if not name:
        raise ValueError("A passthrough needs a name — Square's line had none.")

    raw = RawProduct.objects.create(
        name=name,
        category=category,
        price=Decimal(cost),
        suggested_price=Decimal(price),
        # Ordered, not made. Par lives here rather than on the finished row
        # (see `create_passthrough_products`), and it starts unset: 100 is the
        # field default and would put every new notion on the reorder page
        # claiming a shortage of a hundred on the day it was created.
        par_level=0,
        finished_par_default=0,
        display_slots_default=0,
        # Deliberately NOT made_in_a_dye_bath=False — see the module docstring.
        # The null recipe below is what keeps this off the production lists.
        catalog_group=group,
        square_item_id="" if group is not None else (item_id or ""),
    )

    product = FinishedProduct.objects.create(
        name=name,
        raw_product=raw,
        recipe=None,                 # the passthrough marker, and the only one
        price=Decimal(price),
        par=0,                       # you order these; par here reads nothing
        display_slots=0,             # not on a peg, so the close leaves it be
        square_variation_id=variation_id or "",
    )
    return product, True


def create_from_sale(sale, category, price=None, cost=Decimal("0")):
    """Make a passthrough for an unidentified sale, and hand it back.

    The name and the price come off the line Square sent, because that is the
    best evidence there is about a thing nobody wrote down: it is what the
    customer was actually charged. Nothing is invented.
    """
    if price is None:
        # `amount_cents` is the line total, so the unit price is what a single
        # one of these sells for. A line of three at $60 is a $20 item.
        units = max(sale.quantity, 1)
        price = Decimal(sale.amount_cents) / Decimal(100) / units
        price = price.quantize(Decimal("0.01"))

    return create(
        name=sale.name or sale.variation_name,
        category=category,
        price=price,
        variation_id=sale.square_variation_id,
        cost=cost,
    )
