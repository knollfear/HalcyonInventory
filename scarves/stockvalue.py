"""What the stock in the building is worth, at cost and at retail.

Everything here reads. Nothing writes a row.

The app could say how many of a thing were on hand and never what the pile
was worth, so the only number anyone could put on a year's dyeing was the
supplier invoices going out. Stock is the other side of that: skeins bought
and colour added is money moved from a bank account onto a shelf, and over
twenty years nearly all of it has left the shelf again as takings. That is
the reading this page is for.

**It is a valuation, not a forecast, and two of its numbers are soft.**

- **Retail is the asking price, not the price.** Some of it will go at a
  discount, at the end of a season or years late, and a write-down is an
  ordinary thing rather than a failure. Printed because the asking price is
  the only honest upper bound anybody has, and cost alone understates a
  shelf whose whole value add is the dyeing.
- **Undyed yarn has no retail price of its own.** It has one *once dyed*, so
  the figure on those rows is what the colour would be sold for, and the row
  says which basis it came from — a price somebody set on the blank, or the
  average of what its colorways actually ring at. A blank with neither gets a
  dash rather than a zero: "nothing is priced" and "it is worth nothing" are
  different answers.

## Baths, because that is the unit the dye room counts in

`units / number_per_dye_bath`, naively. It is not a plan and nothing reads
it — a season's dyeing is planned off par and off the Sunday close, and a
bath count derived from what is on the shelf would be capacity proposing
production, which is the coupling `CLAUDE.md` exists to keep broken. It is
here because "ninety baths of silk" is a size a person can hold and 450
skeins is not.

## Claimed yarn is added back, unlike everywhere else

`number_on_hand` means *unclaimed* — a blank on an open sheet has already
left it. That is right for ordering and wrong here: those skeins are in the
building and they are owned, and a planned bath does not spend anything. So
the raw side counts `number_on_hand + claimed`, which is what somebody
standing at the shelf would count.

## One pile is counted once

A passthrough — an undyed yarn, a notion — is one physical pile with a raw
row and a mirroring finished row (see *Undyed stock* in
`docs/claude/stock.md`). Valuing both would double it, so passthroughs are
their own section, valued off the finished row that carries the selling
price, and their blanks are kept out of the undyed section. The undyed
section is *yarn waiting for a dye bath*, which is the only thing a bath
count means anything for anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from . import production
from .models import FinishedProduct, RawProduct

ZERO = Decimal("0")

#: Where a raw row's retail figure came from. Printed on the row, because a
#: derived number with no visible basis is a decision wearing a fact's
#: clothes — the argument is in `CLAUDE.md`.
BASIS_SET = "set on the blank"
BASIS_CATALOGUE = "average of its colorways"
BASIS_NONE = ""


def _money(value):
    return (value or ZERO).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class Row:
    """One blank's pile, or one passthrough's."""

    name: str
    category: str
    units: int = 0
    per_bath: int = 0
    unit_cost: Decimal = ZERO
    #: None means nothing anywhere prices this, which is not zero.
    unit_retail: Decimal | None = None
    retail_basis: str = BASIS_NONE
    #: Of `units`, how many are held by a planned-but-unreported bath. Part
    #: of the count here, unlike everywhere else that reads a shelf.
    claimed: int = 0

    @property
    def baths(self):
        """Whole baths this pile would fill, or None where a bath is not a
        thing that happens to it."""
        if not self.per_bath:
            return None
        return self.units // self.per_bath

    @property
    def cost(self):
        return _money(Decimal(self.units) * (self.unit_cost or ZERO))

    @property
    def retail(self):
        if self.unit_retail is None:
            return None
        return _money(Decimal(self.units) * self.unit_retail)

    @property
    def unpriced_units(self):
        return self.units if self.unit_retail is None else 0


@dataclass
class Section:
    """A heading, its rows, and the totals that go under them."""

    key: str
    title: str
    blurb: str
    rows: list = field(default_factory=list)
    #: Whether a bath column means anything for these rows.
    shows_baths: bool = True

    @property
    def units(self):
        return sum(row.units for row in self.rows)

    @property
    def baths(self):
        return sum(row.baths or 0 for row in self.rows)

    @property
    def cost(self):
        return _money(sum((row.cost for row in self.rows), ZERO))

    @property
    def retail(self):
        return _money(sum((row.retail or ZERO for row in self.rows), ZERO))

    @property
    def unpriced_units(self):
        return sum(row.unpriced_units for row in self.rows)


def _catalogue_price(finished):
    """The average price of this blank's active colorways, or None.

    Stock-weighted deliberately: what is on the shelf is what is being
    valued, so a colorway with forty of them should count forty times. A
    blank with priced colorways but none in stock falls back to the plain
    average, which is the only thing left to say.
    """
    priced = [f for f in finished if f.price]
    if not priced:
        return None
    held = sum(f.number_on_hand for f in priced)
    if held:
        total = sum(f.price * f.number_on_hand for f in priced)
        return Decimal(total) / Decimal(held)
    return sum((f.price for f in priced), ZERO) / Decimal(len(priced))


def _retail_for_blank(blank, finished):
    """`(unit_retail, basis)` for undyed yarn — what it sells for once dyed."""
    if blank.suggested_price is not None:
        return blank.suggested_price, BASIS_SET
    average = _catalogue_price(finished)
    if average is not None:
        return average, BASIS_CATALOGUE
    return None, BASIS_NONE


def sections():
    """The three piles, each as a `Section`.

    Undyed yarn, dyed finished stock, and passthroughs — disjoint by
    construction, so the totals underneath them add up without anything
    being counted twice.
    """

    blanks = list(
        RawProduct.objects.active().select_related("category")
    )
    claimed = production.claimed_units(blanks)

    finished = list(
        FinishedProduct.objects.active()
        .select_related("raw_product", "raw_product__category", "recipe")
    )
    by_blank = {}
    for product in finished:
        by_blank.setdefault(product.raw_product_id, []).append(product)

    #: A blank whose finished rows mirror its raw count. Kept out of the
    #: undyed section so the pile is valued once.
    passthrough_blanks = {
        product.raw_product_id
        for product in finished
        if product.recipe_id is None
    }

    undyed = Section(
        key="undyed",
        title="Undyed, waiting for a dye bath",
        blurb="Blanks on the shelf, including any a planned bath has already "
              "claimed — those skeins are still in the building. Retail is "
              "what the colour would sell for once it is dyed.",
    )
    for blank in blanks:
        if blank.pk in passthrough_blanks:
            continue
        units = blank.number_on_hand + claimed.get(blank.pk, 0)
        if not units:
            continue
        unit_retail, basis = _retail_for_blank(blank, by_blank.get(blank.pk, []))
        undyed.rows.append(Row(
            name=blank.name,
            category=blank.category.name,
            units=units,
            per_bath=blank.number_per_dye_bath if blank.made_in_a_dye_bath else 0,
            unit_cost=blank.blank_cost,
            unit_retail=unit_retail,
            retail_basis=basis,
            claimed=claimed.get(blank.pk, 0),
        ))

    dyed = Section(
        key="dyed",
        title="Dyed and on the shelf",
        blurb="Finished colorways, pooled by blank. Cost is what the blank "
              "cost — the dye and the labour that made it worth more are not "
              "priced anywhere, so this side is understated at cost by "
              "exactly the value add.",
        shows_baths=False,
    )
    for blank in blanks:
        products = [
            p for p in by_blank.get(blank.pk, [])
            if p.recipe_id is not None and p.number_on_hand
        ]
        if not products:
            continue
        units = sum(p.number_on_hand for p in products)
        retail = sum((p.price * p.number_on_hand for p in products), ZERO)
        dyed.rows.append(Row(
            name=blank.name,
            category=blank.category.name,
            units=units,
            unit_cost=blank.blank_cost,
            unit_retail=Decimal(retail) / Decimal(units),
            retail_basis=BASIS_SET,
        ))

    bought = Section(
        key="passthrough",
        title="Bought and resold as it arrives",
        blurb="Undyed yarns and notions — no bath, so no bath count. One "
              "physical pile with two rows, valued here only.",
        shows_baths=False,
    )
    for product in finished:
        if product.recipe_id is not None or not product.number_on_hand:
            continue
        bought.rows.append(Row(
            name=product.name,
            category=product.raw_product.category.name,
            units=product.number_on_hand,
            unit_cost=product.raw_product.blank_cost,
            unit_retail=product.price,
            retail_basis=BASIS_SET,
        ))

    return [undyed, dyed, bought]


@dataclass
class Totals:
    """The bottom line, across every section."""

    units: int = 0
    #: Units of the piles a bath is still ahead of. Kept apart from `units`
    #: because the bath count is *only* about those: the headline read
    #: "4,201 units, of which 387 baths still to dye", which makes the baths
    #: sound like a share of everything owned when dyed stock and bought-in
    #: stock can never be part of it. Derived from the same flag that decides
    #: whether a section prints a bath column, so the two cannot drift.
    undyed_units: int = 0
    baths: int = 0
    cost: Decimal = ZERO
    retail: Decimal = ZERO
    unpriced_units: int = 0

    @property
    def margin(self):
        """Retail less cost. Not profit — the dye, the labour and the stall
        are all missing, and some of the retail will be discounted."""
        return _money(self.retail - self.cost)


def totals(found):
    return Totals(
        units=sum(section.units for section in found),
        undyed_units=sum(
            section.units for section in found if section.shows_baths
        ),
        baths=sum(section.baths for section in found),
        cost=_money(sum((section.cost for section in found), ZERO)),
        retail=_money(sum((section.retail for section in found), ZERO)),
        unpriced_units=sum(section.unpriced_units for section in found),
    )
