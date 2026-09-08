"""What isn't selling, and whether it was ever on the table.

The inverse of `sales.py`, and a different question rather than the same one
sorted the other way. Top sellers can be built by aggregating rows that exist;
**a product that sold nothing has no row at all**, so this has to start from
the catalogue and subtract, which is a different query and a different set of
things that can go wrong.

## Zero means two opposite things

A colorway that sold nothing either sat on the display all weekend and nobody
wanted it, or was never out there to be wanted. Those lead to opposite
decisions — stop dyeing it, or dye more of it — so the page never reports a
zero without the stock beside it.

The shop's own answer is that everything on hand goes out, so **stock on hand
is a good proxy for "it was on the table"**. A zero with stock is a real slow
seller. A zero with none is a colorway nobody could buy, which is a candidate
winner rather than a dud, and is the reason this page exists at all.

## It reads the till ledger, not the stock log

`sales.py` reads `InventoryLog`, which is the app's belief about stock. This
reads `SaleLine`, which is what Square says was rung up, for three reasons
that all matter more at the bottom of a list than the top:

- **`sold_at` is Square's own timestamp.** `InventoryLog.created_at` is when
  the row was written, so an import lands a Saturday's sales on Monday — and
  a season-scoped window would then attribute them to the wrong weekend.
- **A sale the app could not identify never reaches `InventoryLog`.** It goes
  to `UnmatchedSale`. Missing sales inflate a top-sellers list by nothing at
  all, but they invent bottom sellers out of products that did sell.
- It is the ledger that was checked against Square's own API and agreed.

## The unit is the colorway, not the product

**A zero on one blank is not a dog.** A colour that sells on three yarns and
not the fourth is absorbed by the production cadence, and it is still earning
its place on the display — a full, colourful stall is worth something no sales
column can show, and pulling colours to make the numbers tidy would cost more
than it saved.

So the page pools every blank a colorway is dyed on and asks the question of
the total. That is also the unit a recipe is retired in: nobody stops dyeing a
colour for one yarn while the others move. `rows()` still answers per blank
and is one click away, but it is the opt-in.

## Scope: what this app tracks

Active products with a recipe, and nothing else. Roughly 43% of this season's
Square lines tie to nothing here — accessories, unsynced items, flat-price
buttons — and that is a known gap being worked separately rather than
something this page should try to reason about. The question here is the
narrow one: **of the colorways the app knows about, which are not selling.**

## The blind spot, which is stated rather than worked around

**Some blanks are rung up with no colorway at all** — a flat "Regular Price"
button rather than a variation. Every one of those units is a real sale that
cannot be attributed to any colorway, so the colorways of that blank can read
as zero when they did sell. It is not a matching bug and no amount of work
here fixes it: the information was never captured.

This is not the same thing as the untracked 43% above, and it is worth the
distinction: those blanks *are* tracked, and their colorways are on this list.
Sash Belt is the worked case — 43 of its colorways read zero while the blank
itself sold 26 units with no colorway attached, so up to 26 of those zeros are
wrong and nothing can say which.

`unattributed()` counts exactly that, per blank, and the page prints one line
of it under the table. A line rather than a panel: it qualifies the list
without competing with it. Naming it is the whole mitigation — the same call
`colorbands` makes about an unconfirmed band. Silently dropping those blanks
would hide real products; silently including them would accuse products that
sold.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Count, Sum
from django.utils import timezone

from .models import FinishedProduct, SaleLine

#: Price-point values Square writes when nothing was chosen. A line carrying
#: one of these named an item and no variation, so it says nothing about
#: which colorway left the tent.
NO_COLORWAY = ("", "Regular", "Regular Price")


@dataclass
class SlowRow:
    """One colorway that barely sold, with what was there to sell."""

    product: FinishedProduct
    units: int

    @property
    def on_hand(self) -> int:
        return self.product.number_on_hand

    @property
    def never_out(self) -> bool:
        """Nothing sold and nothing on hand — it could not have been bought.

        The interesting half of the page. This is not a slow seller; it is a
        colorway that was never on the table, which is an argument for dyeing
        it rather than against.
        """
        return self.units == 0 and self.on_hand == 0


def season_range(params):
    """The range this page answers over, defaulting to the season so far.

    "Through two weekends" is the question, so a bare visit has to mean the
    running season rather than all time — a lifetime total would bury a
    colorway that has not sold *this year* under one that sold well in 2022.

    Every other range key is `sales.resolve_range`'s, so the two report pages
    take the same parameters and a link off one works on the other.
    """
    from . import sales
    from .models import Faire, FaireDay

    asked = params.get("range") or params.get("from") or params.get("to")
    if asked and params.get("range") != "season":
        return sales.resolve_range(params)

    today = timezone.localdate()
    day = FaireDay.objects.filter(date__lte=today).order_by("-date").first()
    faire = day.faire if day else Faire.objects.order_by("-year").first()
    days = list(FaireDay.objects.filter(faire=faire).order_by("date")) if faire else []
    if not days:
        return sales.resolve_range(params)

    start = days[0].date
    # Never past today: a season runs into the future and the empty half
    # would read as weekends where nothing sold.
    end = min(days[-1].date, today)
    return sales.DateRange(
        "season", start, end, f"{faire.year} season so far",
    )


def _lines(rng):
    """Sale lines inside a range, on Square's own clock."""
    lines = SaleLine.objects.filter(event_type=SaleLine.PAYMENT)
    if rng.start:
        lines = lines.filter(sold_at__date__gte=rng.start)
    if rng.end:
        lines = lines.filter(sold_at__date__lte=rng.end)
    return lines


def sold_units(rng):
    """`{finished_product_id: units}` for everything that sold in the range."""
    return {
        row["finished_product"]: int(row["q"] or 0)
        for row in _lines(rng)
        .filter(finished_product__isnull=False)
        .values("finished_product")
        .annotate(q=Sum("quantity"))
    }


def sold_by_recipe(rng):
    """`{recipe_id: units}` pooled across every blank a colorway is dyed on.

    The unit a dye bath is planned in, so it is also the unit a production
    list should be ordered by. Shared with `production_needed_view` rather
    than recomputed there — two answers to "what sold" is how a page that
    orders by it disagrees with the page that reports it.
    """
    counted = (
        _lines(rng)
        .filter(finished_product__recipe__isnull=False)
        .values("finished_product__recipe")
        .annotate(q=Sum("quantity"))
        .order_by()
    )
    return {
        row["finished_product__recipe"]: int(row["q"] or 0) for row in counted
    }


def rows(rng, max_units=1, category=None):
    """Colorways that sold `max_units` or fewer, quietest first.

    Scoped to products with a recipe, because the question is about
    colorways. An undyed passthrough has none and is a reorder decision
    rather than a dyeing one — `private/raw-inventory/` is where its shortfall
    belongs.

    Sorted by units, then by **most stock first**, because that is the order
    of what the answer costs: a colorway sitting on forty units nobody wants
    is a bigger fact than one sitting on two.
    """
    catalogue = (
        FinishedProduct.objects.filter(is_active=True, recipe__isnull=False)
        .select_related("raw_product", "raw_product__category", "recipe")
    )
    if category is not None:
        catalogue = catalogue.filter(raw_product__category=category)

    sold = sold_units(rng)
    found = [
        SlowRow(product=product, units=sold.get(product.pk, 0))
        for product in catalogue
        if sold.get(product.pk, 0) <= max_units
    ]
    return sorted(found, key=lambda r: (r.units, -r.on_hand, r.product.name))


@dataclass
class ColorwayRow:
    """One colorway pooled across every blank it is dyed on.

    The retirement unit. A colorway that sells nowhere is a recipe to stop
    dyeing; a colorway that sells on Heavenly and not on Artisan is a fact
    about that blank, not about the colour — and nobody retires a recipe for
    one yarn while the others move.
    """

    recipe: object
    units: int
    on_hand: int
    products: int
    stocked: int          # how many of its blanks had something to sell

    @property
    def never_out(self) -> bool:
        """Sold nothing anywhere, and had nothing anywhere to sell."""
        return self.units == 0 and self.on_hand == 0

    @property
    def name(self):
        return self.recipe.name


def colorway_rows(rng, max_units=1, category=None):
    """Colorways whose *total* across every blank is `max_units` or fewer.

    **Aggregated before the threshold, never after.** Regrouping the rows of
    `rows()` would be wrong in the direction that costs the most: a colorway
    selling twenty on Heavenly and none on Artisan would have its Artisan row
    kept and its Heavenly row dropped, and would then read as a colour nobody
    wants. It is the opposite — a colour that works, on a blank that didn't.

    So this pools every active product of the recipe first and asks the
    question of the total. `stocked` says how many of those blanks had stock,
    because "sold none anywhere, and was on four boards" is a far stronger
    argument than the same zero on one empty peg.
    """
    catalogue = (
        FinishedProduct.objects.filter(is_active=True, recipe__isnull=False)
        .select_related("recipe", "raw_product")
    )
    if category is not None:
        catalogue = catalogue.filter(raw_product__category=category)

    sold = sold_units(rng)
    pooled = {}
    for product in catalogue:
        row = pooled.setdefault(
            product.recipe_id,
            {"recipe": product.recipe, "units": 0, "on_hand": 0,
             "products": 0, "stocked": 0},
        )
        row["units"] += sold.get(product.pk, 0)
        row["on_hand"] += product.number_on_hand
        row["products"] += 1
        if product.number_on_hand:
            row["stocked"] += 1

    found = [
        ColorwayRow(
            recipe=row["recipe"], units=row["units"], on_hand=row["on_hand"],
            products=row["products"], stocked=row["stocked"],
        )
        for row in pooled.values()
        if row["units"] <= max_units
    ]
    return sorted(found, key=lambda r: (r.units, -r.on_hand, r.name))


def unattributed(rng, category=None):
    """`[(blank name, units)]` sold with no colorway recorded, biggest first.

    **This is the page's own blind spot, printed rather than worked around.**
    A blank with a flat price button sells without ever naming a variation, so
    those units belong to some colorway and the ledger cannot say which. Every
    colorway of that blank can therefore appear in the list above having
    actually sold.

    Matched on the blank, not the finished product, because that is all a line
    like this carries.
    """
    lines = _lines(rng).filter(price_point__in=NO_COLORWAY)
    if category is not None:
        lines = lines.filter(raw_product__category=category)
    counted = (
        lines.values("item_name")
        .annotate(q=Sum("quantity"))
        .order_by("-q")
    )
    return [(row["item_name"], int(row["q"] or 0)) for row in counted if row["q"]]


#: When one colorway holds this much of a blank's attributed sales, the
#: attribution is worth reading before the zeros under it are believed.
#:
#: A majority is the line because the alternative reading — one runaway
#: colorway — is a real thing that happens, and 50% is where "popular" stops
#: being a sufficient explanation on a blank carrying dozens of colours. It is
#: paired with a catalogue-size test rather than used alone: a blank with
#: three colorways can honestly put 60% on one of them.
LOPSIDED_SHARE = 0.5

#: Below this many colorways on the blank, concentration says nothing.
LOPSIDED_MIN_COLORWAYS = 10


def lopsided(rng, category=None):
    """Blanks whose sales nearly all landed on one colorway. Facts, not a verdict.

    **A third failure mode, and the one that is confidently wrong.** Missing
    sales leave a gap and colorway-less sales announce themselves, but a
    miscoded variation looks like ordinary, complete data — every unit of the
    blank attributed to a single colour, which reads as one runaway hit and
    forty-odd duds. Both halves of that are false, and the duds are what this
    page is for.

    Detected rather than listed by name, so the next blank to break is caught
    without anybody remembering to add it. Reported as the arithmetic — this
    many of that many, on this colorway — with no conclusion attached, because
    a genuinely popular colour and a miscoded button produce the same numbers
    and only a person knows which shop they are in. Same call `colorbands`
    makes: fill the form in, a person decides.
    """
    lines = _lines(rng).filter(raw_product__isnull=False).exclude(
        price_point__in=NO_COLORWAY
    )
    if category is not None:
        lines = lines.filter(raw_product__category=category)

    counted = (
        lines.values("raw_product__name", "raw_product", "price_point")
        .annotate(q=Sum("quantity"))
        .order_by()
    )
    by_blank = {}
    for row in counted:
        blank = by_blank.setdefault(
            row["raw_product"],
            {"name": row["raw_product__name"], "total": 0, "colorways": []},
        )
        units = int(row["q"] or 0)
        blank["total"] += units
        blank["colorways"].append((row["price_point"], units))

    catalogue = {
        row["raw_product"]: row["n"]
        for row in FinishedProduct.objects.filter(is_active=True, recipe__isnull=False)
        .values("raw_product")
        .annotate(n=Count("id"))
        .order_by()
    }

    out = []
    for pk, blank in by_blank.items():
        known = catalogue.get(pk, 0)
        if known < LOPSIDED_MIN_COLORWAYS or not blank["total"]:
            continue
        name, units = max(blank["colorways"], key=lambda pair: pair[1])
        if units / blank["total"] < LOPSIDED_SHARE:
            continue
        out.append({
            "blank": blank["name"],
            "colorway": name,
            "units": units,
            "total": blank["total"],
            "seen": len(blank["colorways"]),
            "known": known,
        })
    return sorted(out, key=lambda row: -row["units"])


def tally(found):
    """Counts for the header, over the rows on screen.

    Serves both groupings, because a product row and a colorway row expose
    the same three things — units, stock, and whether there was anything to
    sell. A second tally would be a second place for the header to disagree
    with the list under it.

    Scoped to what is being shown for the same reason the colour page's pills
    are: a number over a list it does not describe is the page contradicting
    itself.
    """
    zero = [row for row in found if row.units == 0]
    return {
        "listed": len(found),
        "zero": len(zero),
        "zero_with_stock": sum(1 for row in zero if row.on_hand > 0),
        "never_out": sum(1 for row in zero if row.never_out),
        "units_sitting": sum(row.on_hand for row in found),
    }
