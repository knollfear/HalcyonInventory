"""One blank, every colour it comes in: what sold, what got made, what is left.

The question is "how is the Artisan line doing", and the honest unit for it
is the blank. A colorway is the axis the catalogue is organised on, but it is
a few hundred wide and nobody reads a few hundred rows; the blanks somebody
actually follows are about ten. So this pins one blank and lists its
colorways — the narrow half of `BLANK-DYEBATH` held still, the wide half
ranked.

Nothing in this module writes. It is a report, and it puts three numbers that
already have pages of their own side by side, because the reading worth
having is between them:

- **Sold** is the till ledger (`SaleLine`), on Square's own clock, through
  `slowsellers.lines()` — the same definition of "sold in this range" the slow
  sellers and dye statements use, so the three pages cannot disagree about it.
  Not `InventoryLog`: a sale the app could not identify never reaches the log,
  and a missing sale is exactly how a colorway that sold gets read as a dud.
- **Made** is the standing production rows in the stock log — anything taken
  back is gone (`reversals__isnull=True`), because a bath that was retracted
  did not happen. Units, not baths: a recipe-page entry can be several baths
  in one row, so a bath count off rows would be a guess wearing an integer.
- **On hand / par / on a sheet** is now, whatever the range says. A range
  selects activity; stock has no history here to select.

Sold and made are printed and never differenced. The gap between them over a
window is a direction the shelf moved, and a fact about a few weeks — the
same bargain `dyebill.alongside` makes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Count, Max, Q, Sum

from . import production, sales, slowsellers
from .models import FinishedProduct, InventoryLog, RawProduct, SaleLine


@dataclass
class Row:
    """One colorway of the blank, over the range."""

    product: FinishedProduct
    sold: int
    sales: int
    last_sold: datetime | None
    made: int
    last_made: InventoryLog | None
    in_flight: int

    @property
    def on_hand(self) -> int:
        return self.product.number_on_hand

    @property
    def par(self) -> int:
        return self.product.par or 0

    @property
    def retired(self) -> bool:
        """Off the planner — the product, or the colour on every blank."""
        recipe = self.product.recipe
        return not self.product.is_active or bool(recipe and not recipe.is_active)

    @property
    def never_out(self) -> bool:
        """Sold none and has none: nobody could have bought it.

        The same reading `slowsellers.SlowRow.never_out` gives, for the same
        reason — a zero with nothing on the table argues for dyeing, not for
        dropping, and the two must not look alike.
        """
        return self.sold == 0 and self.on_hand == 0

    @property
    def name(self) -> str:
        return self.product.recipe.name if self.product.recipe else self.product.name


def blanks(rng):
    """The picker: every blank with a colorway, busiest first.

    Ordered by units sold over the range, so the ten that matter float to the
    top on their own and nobody keeps a list of favourites. A blank with no
    sales still appears — new, or out of season — under the ones that did.
    """
    sold = {
        row["raw_product"]: int(row["q"] or 0)
        for row in slowsellers.lines(rng)
        .filter(raw_product__isnull=False)
        .values("raw_product")
        .annotate(q=Sum("quantity"))
        .order_by()
    }
    found = (
        RawProduct.objects.active()
        .filter(finished_products__recipe__isnull=False)
        .annotate(
            colorways=Count(
                "finished_products",
                filter=Q(finished_products__recipe__is_active=True,
                         finished_products__is_active=True),
                distinct=True,
            ),
            on_hand=Sum(
                "finished_products__number_on_hand",
                filter=Q(finished_products__recipe__is_active=True,
                         finished_products__is_active=True),
            ),
        )
        .select_related("category")
        .order_by()
        .distinct()
    )
    out = [
        {"blank": blank, "sold": sold.get(blank.pk, 0),
         "colorways": blank.colorways, "on_hand": blank.on_hand or 0}
        for blank in found
    ]
    return sorted(out, key=lambda r: (-r["sold"], r["blank"].name.casefold()))


def _sold(blank, rng):
    """`{finished_product_id: (units, sales, last sold)}` off the till."""
    counted = (
        slowsellers.lines(rng)
        .filter(finished_product__raw_product=blank)
        .values("finished_product")
        .annotate(
            q=Sum("quantity"),
            n=Count("sale", distinct=True),
            last=Max("sold_at"),
        )
        .order_by()
    )
    return {
        row["finished_product"]: (int(row["q"] or 0), row["n"], row["last"])
        for row in counted
    }


def _made(blank, rng):
    """`{finished_product_id: (units, latest standing log)}` in the range.

    The latest row is kept whole rather than as a `Max`, so the page can print
    `log.when` — a card entry known only to the month must not be shown with
    the 1st as though somebody wrote that day down.
    """
    logs = InventoryLog.objects.filter(
        log_type=InventoryLog.PRODUCTION,
        reversals__isnull=True,
        finished_product__raw_product=blank,
    )
    lower, upper = sales.window(rng)
    if lower:
        logs = logs.filter(created_at__gte=lower)
    if upper:
        logs = logs.filter(created_at__lt=upper)

    out = {}
    for log in logs.order_by("-created_at", "-pk"):
        units, latest = out.get(log.finished_product_id, (0, log))
        out[log.finished_product_id] = (units + log.quantity, latest)
    return out


def rows(blank, rng):
    """Every colorway of the blank, with its range figures beside its stock.

    The live colorways always appear, sold or not — a zero is half of what
    this page is for. A retired one appears only if something happened to it
    in the range, so a colour dropped years ago does not pad the list, and one
    dropped last week still shows what it did before it went.

    Undyed rows are left off: they have no colour, and their shortfall is a
    reorder rather than a dye bath.
    """
    sold = _sold(blank, rng)
    made = _made(blank, rng)
    flying = production.in_flight()

    catalogue = (
        FinishedProduct.objects.filter(raw_product=blank, recipe__isnull=False)
        .select_related("recipe", "raw_product")
    )
    out = []
    for product in catalogue:
        units, count, last = sold.get(product.pk, (0, 0, None))
        made_units, latest = made.get(product.pk, (0, None))
        row = Row(
            product=product, sold=units, sales=count, last_sold=last,
            made=made_units, last_made=latest,
            in_flight=flying.get(product.pk, 0),
        )
        if row.retired and not (row.sold or row.made):
            continue
        out.append(row)
    return out


def unattributed(blank, rng):
    """Units of this blank rung up with no colorway at all.

    Every one is a real sale belonging to *some* row on the page, and nothing
    can say which — so the zeros above it may be wrong by up to this much.
    Printed rather than spread across the rows; see `slowsellers.unattributed`.
    """
    total = (
        slowsellers.lines(rng)
        .filter(raw_product=blank, price_point__in=slowsellers.NO_COLORWAY)
        .aggregate(q=Sum("quantity"))["q"]
    )
    return int(total or 0)


def lopsided(blank, rng):
    """The miscoded-button warning for this blank, or None."""
    return next(
        (r for r in slowsellers.lopsided(rng) if r["blank"] == blank.name), None
    )


#: Sortable columns. Sold first by default, best at the top; the same heading
#: clicked again turns it over, which is the worst sellers.
SORTS = {
    "name": lambda r: r.name.casefold(),
    "sold": lambda r: r.sold,
    "sales": lambda r: r.sales,
    "last_sold": lambda r: r.last_sold.timestamp() if r.last_sold else 0,
    "made": lambda r: r.made,
    "on_hand": lambda r: r.on_hand,
    "par": lambda r: r.par,
    "in_flight": lambda r: r.in_flight,
}
DEFAULT_SORT = "sold"
DESCENDING_FIRST = {"sold", "sales", "last_sold", "made", "on_hand", "par",
                    "in_flight"}


def sort_rows(found, sort, descending):
    """Sort, ties broken by name so a table does not shuffle between loads."""
    found = sorted(found, key=lambda r: r.name.casefold())
    key = SORTS.get(sort) or SORTS[DEFAULT_SORT]
    return sorted(found, key=key, reverse=descending)


def totals(found):
    """The strip above the table. Counts, no rates."""
    return {
        "colorways": len(found),
        "sold": sum(r.sold for r in found),
        "made": sum(r.made for r in found),
        "on_hand": sum(r.on_hand for r in found),
        "in_flight": sum(r.in_flight for r in found),
        "sold_none": sum(1 for r in found if r.sold == 0),
        "never_out": sum(1 for r in found if r.never_out),
    }
