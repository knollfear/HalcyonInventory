"""The one door for a finished-stock movement: lock, move, write the row.

Every flow that changes `FinishedProduct.number_on_hand` used to do it by
hand at the call site — a dye bath on the recipe page, a bath off a sheet, a
sale off the webhook, a count at the close, a restock check, a fancy
conversion, a retraction. Seven copies of the same four lines, and they had
drifted: three locked the row first, four did not; two knew a passthrough's
count lives on the raw product, the rest wrote to the mirror and let it snap
back. None of that was a decision anybody made. It is what happens when the
operation is a pattern rather than a function.

So it is a function. Two, because the app corrects in two shapes:

- `move(product, delta, ...)` — a *relative* movement. Production and sales
  are deltas by nature: five came out of the pot, one left the tent.
- `count(product, value, ...)` — an *absolute* count. Corrections are always
  absolute here (see *Self-healing* in `CLAUDE.md`): "there are two of these"
  heals whatever went unrecorded in between; "take two off" only works if
  everything before it was right.

Both lock the row that actually holds the number before reading it, because
the same stock is written from phones at a stall and two products of one
recipe share a blank — `apply_row` and `record_recipe_production` both
learned that separately, and the sheet that left a shelf reading 135
instead of 130 is why. For anything dyed that row is the product's own; for a
passthrough it is the raw product, and the `mirror_passthrough_stock` signal
copies it down (see *One pile, and only one row may count it* in
`docs/claude/stock.md`). Callers never need to know which.

Both write the `InventoryLog` row in the same call, so a movement without a
ledger entry — or an entry without a movement — cannot be produced by
forgetting the other half. `source` is required: a row written without one
drops out of every count silently, which `InventoryLogSourceTests` polices.

What this deliberately does **not** cover:

- **Raw stock.** Blanks come off when a run is planned
  (`production.open_rows`) and never through here; raw movements have never
  been ledgered, and `RawStockMove` is the agreed next step, not a field on
  this row.
- **History-only rows.** `private/cards/` writes production rows that move
  nothing, on purpose. That is not a movement, so it does not use a door
  whose whole promise is that the count changed.

`count` returns `None` when the count already matched: a confirmed count is
a fact worth recording on the close's own row, but it is not a movement and
writing a zero adjustment for it would put a correction in the log that
nobody made.
"""
from django.db import transaction

from .models import FinishedProduct, InventoryLog, RawProduct


def _holder(product):
    """Lock and return the row that holds `product`'s count.

    The raw product for a passthrough, the product itself otherwise. Locked
    with `select_for_update`, so this must run inside a transaction — both
    public functions open one.
    """
    if product.is_passthrough:
        return RawProduct.objects.select_for_update().get(pk=product.raw_product_id)
    return FinishedProduct.objects.select_for_update().get(pk=product.pk)


def _settle(product, holder, value):
    """Write `value` to the locked holder and keep the caller's instance honest."""
    holder.number_on_hand = value
    holder.save(update_fields=["number_on_hand"])   # a raw holder mirrors down
    product.number_on_hand = value
    if holder is not product and product.raw_product_id == holder.pk:
        # The caller may go on to read `product.raw_product.number_on_hand`;
        # only refresh a copy it has already fetched, so this costs no query.
        cached = FinishedProduct.raw_product.field.get_cached_value(product, None)
        if cached is not None:
            cached.number_on_hand = value


@transaction.atomic
def move(product, delta, *, log_type, source, notes="", raw_product=None, **fields):
    """Move `product` by `delta` and write the row. Returns the `InventoryLog`.

    Clamped at zero on the way down — a sale of one against a believed zero
    leaves zero, not minus one — but the row records the movement that was
    *reported*, which is what every reader of the ledger wants to know.

    `raw_product` is the product's own unless the caller says otherwise: a
    fancy veil out of a plain bath is logged against the *plain* blank, which
    is how `producedsince.blanks_consumed` knows not to restore the pot twice.
    `fields` are passed to the log row: `sale_reference`, `reverses`,
    `date_precision`.
    """
    holder = _holder(product)
    _settle(product, holder, max(holder.number_on_hand + int(delta), 0))
    return InventoryLog.objects.create(
        finished_product=product,
        raw_product=product.raw_product if raw_product is None else raw_product,
        log_type=log_type,
        source=source,
        quantity=int(delta),
        notes=notes,
        **fields,
    )


@transaction.atomic
def count(product, value, *, source, notes="", log_type=InventoryLog.ADJUSTMENT,
          **fields):
    """Set `product` to an absolute `value`. Returns the row, or `None` if nothing moved.

    The delta on the row is measured against the *locked* count, not the one
    the caller read a moment ago — so if a sale landed between the two, the
    ledger says what this count actually changed.
    """
    value = max(int(value), 0)
    holder = _holder(product)
    delta = value - holder.number_on_hand
    if not delta:
        product.number_on_hand = value
        return None
    _settle(product, holder, value)
    return InventoryLog.objects.create(
        finished_product=product,
        raw_product=product.raw_product,
        log_type=log_type,
        source=source,
        quantity=delta,
        notes=notes,
        **fields,
    )
