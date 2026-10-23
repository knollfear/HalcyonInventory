"""The show planner: what a show would leave over, and afterwards what it did.

The argument fits in a sentence: **a piece sold is a piece to make again.**
So a show's costs come in two kinds, and the planner keeps them apart because
they behave differently:

- **What it sells costs** — a blank and its dyeing for every dyed piece, the
  replacement cost of anything bought in, and the card fee on every sale.
  These grow with takings, so they are a *share*: about half of what the
  Labor Day run takes, whatever the day.
- **What it costs to be there** — the stall fee, the crew by the day, the
  miles, a motel, wear on the display. These are fixed by the show and paid
  whether it sells or not.

What is left is the takings less both. **Break-even is the second kind
divided by what survives the first**, which is why a $10,000 day is hard to
lose money on and a $1,500 day is easy: the fixed half barely changes, and
the share that has to cover it is the same.

Everything here reads, except `make` which seeds a new show. Nothing moves
stock and nothing writes the ledger.

## Plan and actual are read the same way

`plan(show)` is the show's own figures — a day's takings, the mix, the
per-piece prices copied in when it was made. `actual(show)` reads the ledger
over the show's dates and prices what sold at today's blank costs, since a
piece sold now is replaced at today's price. Both come back as a `Reading`,
so the page lays them side by side and nothing about the subtraction can
differ between the columns.

## The dyeing rate may be empty, and that is said, not hidden

No rate is agreed, so a show starts with the dyeing boxes blank. A blank box
prices dyeing at nothing and the reading **says so** — `dyeing_unset` — so
the bottom line is labelled *before dyeing* instead of looking like a show
that made more than it did. Zero would be the same arithmetic and the
opposite message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from . import seasons
from .models import Faire, SaleLine, Show, ShowCost

ZERO = Decimal("0")
HUNDRED = Decimal("100")

SILK, YARN, BOUGHT = "silk", "yarn", "bought"
GROUPS = [
    (SILK, "Silk"),
    (YARN, "Dyed yarn"),
    (BOUGHT, "Bought in"),
]
#: The two that go through a dye bath. Bought-in stock is sold as it
#: arrives — see *Undyed stock* in `docs/claude/stock.md`.
DYED = (SILK, YARN)

#: The app's own category names for the two dyed groups. Square's name for
#: silk is "Silk Scarves", which is why a line with no product falls back to
#: its own category text rather than this.
SILK_CATEGORY = "Silk"
YARN_CATEGORY = "Yarn"

#: What a new show starts with. Amounts are zero bar the mileage, which is
#: the IRS rate rather than anybody's guess, so the list is a reminder of
#: what to think about, not an estimate of any of it.
DEFAULT_COSTS = [
    ("Booth fee", ShowCost.PER_SHOW, ZERO),
    ("Crew", ShowCost.PER_DAY, ZERO),
    ("Mileage", ShowCost.PER_MILE, Decimal("0.70")),
    ("Lodging and food", ShowCost.PER_SHOW, ZERO),
    ("Display wear", ShowCost.PER_DAY, ZERO),
]


def group_of(line) -> str:
    """Which of the three a ledger line is: silk, dyed yarn, or bought in.

    The product decides where there is one. A colorway with no recipe is a
    passthrough and was never dyed, whatever its category; a line matched
    only to its blank (silk rung up without a colour) is still a dyed piece,
    since undyed stock has colorway rows of its own to match. Without either
    it falls back to Square's category text.
    """
    finished = line.finished_product
    if finished is not None and finished.recipe_id is None:
        return BOUGHT
    blank = line.raw_product or (finished.raw_product if finished else None)
    if blank is not None:
        # Fancy silk counts as silk: it went through a bath before the line
        # work, and the line work is already in its `blank_cost`.
        return {SILK_CATEGORY: SILK, YARN_CATEGORY: YARN}.get(
            blank.category.name, BOUGHT
        )
    category = (line.category or "").lower()
    if "silk" in category:
        return SILK
    if category == "yarn":
        return YARN
    return BOUGHT


def _blank_of(line):
    finished = line.finished_product
    return line.raw_product or (finished.raw_product if finished else None)


def _lines(start, end):
    return (
        SaleLine.objects.filter(sold_at__date__range=(start, end))
        .select_related(
            "raw_product__category",
            "finished_product__raw_product__category",
        )
    )


@dataclass
class Tally:
    """What sold in one group: money, pieces, and what replacing them costs."""
    sales: Decimal = ZERO
    pieces: Decimal = ZERO
    cost: Decimal = ZERO
    costed: Decimal = ZERO

    @property
    def unpriced(self):
        return self.pieces - self.costed


def tally(start, end) -> dict[str, Tally]:
    """Every ledger line between two dates, folded into the three groups."""
    found = {key: Tally() for key, _ in GROUPS}
    # `blank_cost` asks the database about a fancy blank's plain one, and a
    # season is thousands of lines over a few dozen blanks.
    costs = {}
    for line in _lines(start, end):
        t = found[group_of(line)]
        t.sales += line.net
        t.pieces += line.quantity
        blank = _blank_of(line)
        unit = None
        if blank is not None:
            if blank.pk not in costs:
                costs[blank.pk] = blank.blank_cost
            unit = costs[blank.pk]
        if unit:
            t.cost += unit * line.quantity
            t.costed += line.quantity
    return found


@dataclass
class Basis:
    """Per-piece figures from a past season, for seeding a new show."""
    faire: Faire | None
    shares: dict[str, int]
    price: dict[str, Decimal]
    blank: dict[str, Decimal]


def reference() -> Basis:
    """The latest Labor Day run with sales on file, per group.

    Only a seed. The figures are copied onto a show when it is made and can
    be changed there, because a fiber festival sells yarn and almost no
    silk, and this season's mix says nothing about that.
    """
    for faire in Faire.objects.filter(slug=seasons.DEFAULT_FAIRE_SLUG).order_by("-year"):
        days = list(faire.days.filter(traded=True).order_by("date"))
        if not days:
            continue
        found = tally(days[0].date, days[-1].date)
        total = sum((t.sales for t in found.values()), ZERO)
        if total <= 0:
            continue
        shares = {k: int((t.sales / total * HUNDRED).quantize(1, ROUND_HALF_UP))
                  for k, t in found.items()}
        price = {k: _money(t.sales / t.pieces) if t.pieces else ZERO
                 for k, t in found.items()}
        blank = {k: _money(t.cost / t.costed) if t.costed else ZERO
                 for k, t in found.items()}
        return Basis(faire=faire, shares=shares, price=price, blank=blank)
    return Basis(faire=None, shares={k: 0 for k, _ in GROUPS},
                 price={k: ZERO for k, _ in GROUPS},
                 blank={k: ZERO for k, _ in GROUPS})


def make(**fields) -> Show:
    """A new show, seeded from the last season and given the usual cost lines."""
    basis = reference()
    show = Show(**fields)
    show.silk_share = basis.shares[SILK]
    show.yarn_share = basis.shares[YARN]
    show.silk_price, show.silk_blank = basis.price[SILK], basis.blank[SILK]
    show.yarn_price, show.yarn_blank = basis.price[YARN], basis.blank[YARN]
    show.bought_price, show.bought_cost = basis.price[BOUGHT], basis.blank[BOUGHT]
    show.save()
    ShowCost.objects.bulk_create([
        ShowCost(show=show, label=label, per=per, amount=amount, order=i)
        for i, (label, per, amount) in enumerate(DEFAULT_COSTS)
    ])
    return show


def _money(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), ROUND_HALF_UP)


def planned_total(cost: ShowCost, show: Show) -> Decimal:
    """A cost line's rate turned into a total for this show."""
    if cost.per == ShowCost.PER_DAY:
        return cost.amount * show.days_open
    if cost.per == ShowCost.PER_MILE:
        return cost.amount * show.miles
    return cost.amount


@dataclass
class Group:
    key: str
    label: str
    sales: Decimal
    pieces: Decimal
    blanks: Decimal
    dyeing: Decimal
    #: Dyed pieces with no rate to price their dyeing at.
    dyeing_unset: bool = False
    #: Pieces sold with no blank cost on file — priced at the plan's figure.
    estimated: Decimal = ZERO


@dataclass
class Line:
    label: str
    amount: Decimal
    note: str = ""
    as_planned: bool = False


@dataclass
class Reading:
    """One column of the page: takings, what they cost, and what is left."""
    days: int
    groups: list[Group]
    card: Decimal
    costs: list[Line] = field(default_factory=list)

    @property
    def sales(self):
        return sum((g.sales for g in self.groups), ZERO)

    @property
    def pieces(self):
        return sum((g.pieces for g in self.groups), ZERO)

    @property
    def dyed_pieces(self):
        return sum((g.pieces for g in self.groups if g.key in DYED), ZERO)

    # A plan's pieces are fractions — $1,200 of silk at $67.42 is 17.8 — and
    # the arithmetic keeps them that way so break-even comes out exact. The
    # page prints whole pieces, and these make its totals the sum of the
    # rows it prints rather than a rounding of the unrounded total.
    @property
    def whole_pieces(self):
        return sum(round(g.pieces) for g in self.groups)

    @property
    def whole_dyed_pieces(self):
        return sum(round(g.pieces) for g in self.groups if g.key in DYED)

    @property
    def blanks(self):
        return sum((g.blanks for g in self.groups), ZERO)

    @property
    def dyeing(self):
        return sum((g.dyeing for g in self.groups), ZERO)

    @property
    def dyeing_unset(self):
        return any(g.dyeing_unset for g in self.groups)

    @property
    def making(self):
        """Replacing what sold: blanks, dyeing and the card fee."""
        return self.blanks + self.dyeing + self.card

    @property
    def being_there(self):
        return sum((c.amount for c in self.costs), ZERO)

    @property
    def left(self):
        return self.sales - self.making - self.being_there

    @property
    def per_day(self):
        return self.sales / self.days if self.days else ZERO

    @property
    def left_per_day(self):
        return self.left / self.days if self.days else ZERO

    @property
    def as_planned(self):
        return sum(1 for c in self.costs if c.as_planned)


def _rate(show, key):
    return {SILK: show.silk_dyeing, YARN: show.yarn_dyeing}.get(key)


def _per_piece(show, key):
    return {
        SILK: (show.silk_price, show.silk_blank),
        YARN: (show.yarn_price, show.yarn_blank),
        BOUGHT: (show.bought_price, show.bought_cost),
    }[key]


def _shares(show):
    bought = max(0, 100 - show.silk_share - show.yarn_share)
    return {SILK: show.silk_share, YARN: show.yarn_share, BOUGHT: bought}


def _group(show, key, label, sales, pieces, blanks, estimated=ZERO):
    rate = _rate(show, key)
    unset = key in DYED and pieces > 0 and rate is None
    dyeing = pieces * rate if key in DYED and rate is not None else ZERO
    return Group(key=key, label=label, sales=sales, pieces=pieces,
                 blanks=blanks, dyeing=dyeing, dyeing_unset=unset,
                 estimated=estimated)


def plan(show: Show) -> Reading:
    """The show as planned: a day's takings, times the days, split by mix."""
    takings = show.sales_per_day * show.days_open
    shares = _shares(show)
    groups = []
    for key, label in GROUPS:
        sales = takings * shares[key] / HUNDRED
        price, blank = _per_piece(show, key)
        pieces = sales / price if price else ZERO
        groups.append(_group(show, key, label, sales, pieces, pieces * blank))
    costs = [
        Line(c.label, planned_total(c, show), note=_rate_note(c, show))
        for c in show.costs.all()
    ]
    return Reading(days=show.days_open, groups=groups,
                   card=takings * show.card_fee_percent / HUNDRED, costs=costs)


def actual(show: Show) -> Reading | None:
    """What the ledger says the show took, or None if it says nothing.

    Read only for a show marked going: two candidates on one weekend would
    otherwise both claim it. And None, not a column of zeros, when nothing is
    on file — a show whose sales were never pulled has not made nothing.
    """
    if show.decision != Show.GOING:
        return None
    found = tally(show.first_day, show.last_day)
    if not any(t.pieces for t in found.values()):
        return None
    groups = []
    for key, label in GROUPS:
        t = found[key]
        _, plan_blank = _per_piece(show, key)
        # A piece with no blank cost on file is priced at the plan's
        # figure, and counted, rather than replaced for nothing.
        blanks = t.cost + t.unpriced * plan_blank
        groups.append(_group(show, key, label, t.sales, t.pieces, blanks,
                             estimated=t.unpriced))
    sales = sum((g.sales for g in groups), ZERO)
    costs = []
    for c in show.costs.all():
        if c.actual is not None:
            costs.append(Line(c.label, c.actual))
        else:
            costs.append(Line(c.label, planned_total(c, show),
                              note="as planned", as_planned=True))
    return Reading(days=show.days_open, groups=groups,
                   card=sales * show.card_fee_percent / HUNDRED, costs=costs)


def _rate_note(cost, show):
    if cost.per == ShowCost.PER_DAY:
        return f"${cost.amount:,.0f} × {show.days_open} day{'s' if show.days_open != 1 else ''}"
    if cost.per == ShowCost.PER_MILE:
        return f"${cost.amount:,.2f} × {show.miles:,} miles"
    return ""


def keep_share(show: Show) -> Decimal:
    """Of each dollar taken, what is left after replacing what sold.

    The planned mix, at the planned prices, less the card fee. Unset dyeing
    counts as nothing — the page already says so beside it.
    """
    shares = _shares(show)
    spent = show.card_fee_percent / HUNDRED
    for key, _ in GROUPS:
        price, blank = _per_piece(show, key)
        if not price or not shares[key]:
            continue
        per_piece = blank + (_rate(show, key) or ZERO)
        spent += Decimal(shares[key]) / HUNDRED * per_piece / price
    return 1 - spent


def break_even_per_day(show: Show) -> Decimal | None:
    """The day's takings at which the planned show leaves exactly nothing.

    None when nothing of a dollar survives replacing what sold — no amount
    of selling covers the fixed costs, and a number would hide that.
    """
    keep = keep_share(show)
    if keep <= 0:
        return None
    fixed = sum((planned_total(c, show) for c in show.costs.all()), ZERO)
    return fixed / keep / show.days_open
