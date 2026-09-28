"""What a dye session was worth: one statement per run, priced when it closed.

Everything here reads. Nothing writes a row — the figures were written once,
at the moment each bath was accepted, by `production.apply_row`.

**This is the shape a bill for the dyeing will take.** Closing a run is
already the load-bearing step: the sheet is checked in, stock moves, and the
same event is the one a piece rate would be paid against — a closed run
carries its rows, their yields and a date, so nothing new has to be recorded
to pay off it. What is missing today is only the rate, and deliberately: no
rate has been agreed, and a number in a column reads as a decision. So a
statement says what the blanks cost and what the output is priced at, and
leaves the labour line for when there is a real rate to put in it.

## The figures are frozen, and that is the break with every other report here

Everything else in this app derives on read, which is right when the question
is "what is true now". A statement asks a different question — *what was this
session worth when it happened* — and a derived answer to that one is wrong
in a way nobody could see: a supplier increase or a reprice in spring would
silently rewrite what every past week earned. So `unit_blank_cost` and
`output_retail` are written onto the row as it is accepted, and this module
only adds them up.

The cost of that choice is a real one and worth naming: a price typed in wrong
is frozen wrong, and fixing the price list will not fix a statement already
written. That is the same bargain an invoice makes, and the alternative is
history that quietly moves.

## A retracted bath is not on the bill

*Take it back* on `private/produced-since/` writes a compensating entry and
leaves the original exactly as written, so the question is never "was a
production row written" but **"does one still stand"** — see
`CLAUDE.md`. A row whose log has been reversed is off the statement entirely.
Getting that wrong stops being an inventory error the moment a statement is a
bill: it pays for a bath somebody has already said did not happen.

## Rows from before the freeze are estimated, and the estimate is marked

Baths accepted before the figures were kept have no frozen cost or retail, and
they are real work — 133 baths of it. Leaving them blank makes a page of
genuine sessions read as though they earned nothing, so they are valued at
**today's** prices instead.

Two rules keep that from quietly becoming a lie:

- **Nothing is written back.** The estimate is computed on read and the null
  columns stay null, so a row either carries what it was worth then or carries
  nothing at all. Backfilling the columns would make a guess and a record
  indistinguishable from the moment after it ran, which is the one thing the
  freeze exists to prevent.
- **It is marked everywhere it appears.** Each statement says how many of its
  baths are estimated and how much of its money is, and the totals do too.
  A number that cannot be told from a measurement is the `par` failure — see
  *The app advises, a person decides* in `CLAUDE.md`.

The estimate moves whenever the price list does, which is correct for an
estimate and would be a bug in a record. That difference is the whole reason
the two are kept in separate fields rather than merged into one column.

## The window is on the bath, not on the session

`statements(rng)` selects **rows** by `accepted_at`, so a session straddling
a boundary reports the baths that fall inside and says how many it is
reporting. Filtering whole runs by their last accept would be simpler and
wrong in the way this app cares about: a sheet worked over ten days would
land all of it on the day the last bath was checked in, which is the
`import_square_sales` failure — a Monday carrying Saturday's work — arriving
at the other end of the shop.

`accepted_at` is when the bath was *reported*, not when the pot was lit. A
session dyed on Saturday and checked in on Monday is a Monday here, and the
page says so; nothing in the app records when a bath was actually dyed.

## What sold over the same window, and why nothing is subtracted from it

`sold(rng)` is on this page because the question a date range makes askable
is the one the statements alone cannot answer: over these dates, did the
dyed shelves gain or lose. Two rules keep it from becoming a scoreboard:

- **The units are compared and the money is not.** Units are the same thing
  counted twice — one came out of a pot, one went over the counter — so
  `made − sold` is a real figure about the shelves. The money is two
  different bases: output is at the **asking** price, and the till took
  **net**, after whatever it actually went out at. Subtracting one from the
  other produces a number that looks like a margin and is not one, so the two
  are printed and never differenced.
- **The difference is a direction the stock moved, not a result.** It is
  reported as the shelves ending fuller or lighter, which is what the
  arithmetic says, and it is qualified: a close, a recount or a bath nobody
  recorded moves the same stock and is not in either column.

**Sold means a line that named a colorway**, i.e. one tying to a
`FinishedProduct` with a recipe. That excludes notions and undyed
passthroughs, which never saw a pot and would otherwise inflate the sold
side — and it excludes the flat-price buttons, which did. So `Sold.colourless`
counts the units of dyed styles that rang up with no colorway attached, and
the page prints it beside the comparison: it is the one bias in here big
enough to change the reading, and it runs against the dyeing. Same call
`slowsellers.unattributed` makes — name the blind spot rather than work
around it.

**A trading day nobody has imported is a gap, not a day nothing sold.** This
is `Weekend.is_gap` on the season page arriving by another door, and it lands
harder here: the two failures look identical in the sold column and only one
of them is true, so an export sitting unloaded quietly makes the shelves read
fuller than they are. `Sold.missing_days` counts faire days inside the window
marked traded with no line against them, and the page names them rather than
quietly counting them as zero. Outside the faire there are no trading days to
be missing, which is why the test is on the calendar and not on whether any
lines turned up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum

from . import sales, slowsellers
from .models import FaireDay, FinishedProduct, ProductionRunRow

ZERO = Decimal("0")


def _dec(value):
    """A Decimal from whatever a price field hands over.

    A model instance built in memory still holds whatever was assigned to it —
    often a string — and only becomes a Decimal once it has been round-tripped
    through the database. An estimate reads live objects, so it cannot assume
    the coercion has happened.
    """
    return Decimal(value or 0)


def _money(value):
    return (value or ZERO).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _is_unpriced(row):
    """A row accepted before the figures were kept.

    Both columns, because a row carrying one and not the other would be a
    half-written record, and there is no way for `apply_row` to produce one —
    it writes them together or not at all.
    """
    return row.unit_blank_cost is None and row.output_retail is None


def _today_retail(row):
    """What this bath's output would be worth at today's prices."""
    fancied = row.fancy_yield or 0
    plain = (row.yielded or 0) - fancied
    total = Decimal(plain) * _dec(row.finished_product.price)
    if fancied:
        target = row.fancy_target
        # No counterpart any more — the blank stopped having a fancy version
        # since. Those units exist and were sold as something, so they are
        # valued as plain rather than dropped.
        price = target.price if target is not None else row.finished_product.price
        total += Decimal(fancied) * _dec(price)
    return total


@dataclass
class Statement:
    """One run's closed baths, priced as they were when they closed."""

    run: object
    #: Accepted rows whose log still stands, in sheet order.
    rows: list = field(default_factory=list)
    #: Accepted rows with no frozen figures — real work, not priceable now.
    unpriced: int = 0
    #: Accepted rows whose entry was taken back afterwards. Counted so the
    #: gap between a sheet's baths and its statement's baths has a reason on
    #: the page rather than reading as an arithmetic mistake.
    retracted: int = 0

    @property
    def closed(self):
        """When the last bath on this run was accepted.

        The run's own close, as far as anything here is concerned: a sheet is
        reported bath by bath from a phone, so there is no single closing
        event to read — only the last one to arrive.
        """
        return max(row.accepted_at for row in self.rows) if self.rows else None

    @property
    def baths(self):
        return len(self.rows)

    @property
    def yielded(self):
        return sum(row.yielded or 0 for row in self.rows)

    @property
    def fancy(self):
        return sum(row.fancy_yield or 0 for row in self.rows)

    @property
    def lost(self):
        """Units the baths were asked for and did not make.

        Printed because the blanks were consumed either way, so it is part of
        what the session cost — never as a score. Nothing here counts failed
        baths or totals anybody's misses.
        """
        return sum(row.quantity - (row.yielded or 0) for row in self.rows)

    @property
    def frozen_cost(self):
        """The blanks, at what they cost then.

        `quantity` and not `yielded`: a bath eats its blanks whether or not
        the pot came good.
        """
        return _money(sum(
            (Decimal(row.quantity) * row.unit_blank_cost
             for row in self.rows if row.unit_blank_cost is not None),
            ZERO,
        ))

    @property
    def frozen_retail(self):
        return _money(sum(
            (row.output_retail for row in self.rows
             if row.output_retail is not None),
            ZERO,
        ))

    @property
    def estimated_cost(self):
        """Pre-freeze baths, valued at today's blank cost."""
        return _money(sum(
            (Decimal(row.quantity) * _dec(row.finished_product.raw_product.blank_cost)
             for row in self.rows if _is_unpriced(row)),
            ZERO,
        ))

    @property
    def estimated_retail(self):
        """Pre-freeze baths, valued at today's asking prices.

        The fancy split is honoured here as it is in the freeze: those units
        left as a different product at a different price, and valuing them as
        plain would understate a session for having made the better thing.
        """
        return _money(sum(
            (_today_retail(row) for row in self.rows if _is_unpriced(row)),
            ZERO,
        ))

    @property
    def cost(self):
        return _money(self.frozen_cost + self.estimated_cost)

    @property
    def retail(self):
        return _money(self.frozen_retail + self.estimated_retail)

    @property
    def is_estimated(self):
        """Whether any of this session's money is an estimate rather than a
        record. What the page marks the row with."""
        return self.unpriced > 0

    @property
    def value_added(self):
        """Retail less the blanks. Not profit, and not yet a wage.

        What a piece rate would be paid out of, which is the only reason it
        is printed — the dye, the labour and the stall are all still in here.
        """
        return _money(self.retail - self.cost)


#: The presets on `private/dye-statements/`, in the order they appear. Every
#: key is `sales.resolve_range`'s (plus `season`, which is
#: `slowsellers.season_range`'s), so a link built on one report page means the
#: same window on this one.
RANGES = [
    ("7", "Last 7 days"),
    ("30", "Last 30 days"),
    ("season", "This season"),
    ("all", "All time"),
    ("custom", "Choose dates"),
]


def resolve_range(params):
    """The window this page answers over, defaulting to every session on file.

    The default is the one thing that differs from `private/sales/`, and it
    differs because the pages are asked different questions. A till page
    opened cold means today. Sessions happen weekly at best and run to a few
    hundred baths in total, so a bare visit here means the whole book — which
    is also what this page answered before it had a date filter at all, and
    changing that under a bookmark would be a silent edit to somebody's page.
    """
    if not (params.get("range") or params.get("from") or params.get("to")):
        return sales.DateRange("all", None, None, "All time")
    if params.get("range") == "season":
        return slowsellers.season_range(params)
    return sales.resolve_range(params)


def statements(rng=None, limit=None):
    """A `Statement` per run with closed baths in the window, most recent first.

    One query for the rows, grouped in Python: a statement needs the rows
    themselves — a run is a handful of baths, and per-run aggregates in SQL
    would still have to come back to them to name the unpriced ones.

    **The window selects baths, not runs**, so a session worked across a
    boundary reports the part of itself that falls inside. See the module
    docstring: dating a whole sheet by its last accept would pile ten days of
    work onto one afternoon.
    """
    rows = (
        ProductionRunRow.objects
        .filter(applied_log__isnull=False)
        .select_related("run", "finished_product",
                        "finished_product__raw_product")
        .prefetch_related("applied_log__reversals")
        .order_by("-run_id", "order", "pk")
    )
    if rng is not None:
        lower, upper = sales.window(rng)
        if lower:
            rows = rows.filter(accepted_at__gte=lower)
        if upper:
            rows = rows.filter(accepted_at__lt=upper)

    found = {}
    for row in rows:
        statement = found.setdefault(row.run_id, Statement(run=row.run))
        # **Does the entry still stand.** A retraction leaves the row and its
        # `applied_log` untouched on purpose, so `accepted_at` is not the
        # question — the reversal is.
        if row.applied_log.reversals.all():
            statement.retracted += 1
            continue
        if _is_unpriced(row):
            statement.unpriced += 1
        statement.rows.append(row)

    ordered = [s for s in found.values() if s.rows or s.retracted]
    ordered.sort(key=lambda s: (s.closed is not None, s.closed, s.run.pk),
                 reverse=True)
    return ordered[:limit] if limit else ordered


@dataclass
class Totals:
    """Every statement added up, with the estimated part kept visible."""

    runs: int = 0
    baths: int = 0
    yielded: int = 0
    cost: Decimal = ZERO
    retail: Decimal = ZERO
    #: How much of `retail` above is an estimate at today's prices rather
    #: than a record of what the session was worth. Printed, always: money
    #: that cannot be told from a measurement is the `par` failure.
    estimated_retail: Decimal = ZERO
    unpriced: int = 0

    @property
    def value_added(self):
        return _money(self.retail - self.cost)


def totals(found):
    return Totals(
        runs=len(found),
        baths=sum(s.baths for s in found),
        yielded=sum(s.yielded for s in found),
        cost=_money(sum((s.cost for s in found), ZERO)),
        retail=_money(sum((s.retail for s in found), ZERO)),
        estimated_retail=_money(sum((s.estimated_retail for s in found), ZERO)),
        unpriced=sum(s.unpriced for s in found),
    )


@dataclass
class Sold:
    """What went over the counter in the same window, as far as it is known.

    `units` and `net` count only lines that named a colorway. `colourless` is
    the rest of the dyed catalogue's sales — a flat price button on a style
    that is only ever dyed — and it is carried separately rather than folded
    in because nothing can say which colorway those units were, and because
    it is the one number here big enough to change how the comparison reads.
    """

    units: int = 0
    net: Decimal = ZERO
    colourless: int = 0
    #: Faire days inside the window that the shop traded.
    trading_days: int = 0
    #: How many of those have any sale line at all against them. Counted with
    #: no product filter, because the question is whether the export arrived
    #: and not whether it contained a colorway.
    days_on_file: int = 0

    @property
    def missing_days(self):
        """Trading days with nothing on file — almost certainly unimported.

        Named rather than folded in, because a day nobody exported and a day
        nothing sold are the same zero here and lead to opposite readings.
        """
        return max(self.trading_days - self.days_on_file, 0)


def _dyed_blank_ids():
    """Blanks that have at least one colorway, for the colourless count.

    Not `made_in_a_dye_bath`, which answers a different question and answers
    it yes for notions and for undyed yarn — both of which sell, neither of
    which came out of a pot. Having a colorway on file is the test that means
    what is wanted here.
    """
    return set(
        FinishedProduct.objects
        .filter(recipe__isnull=False, raw_product__isnull=False)
        .values_list("raw_product_id", flat=True)
    )


def sold(rng):
    """Dyed units rung up inside the window, on Square's own clock.

    Reads `SaleLine` rather than `InventoryLog` for the reason
    `slowsellers` does: `sold_at` is when the till took the money, where a
    log row is stamped when it was written, so an export loaded on Monday
    would land a Saturday's sales on the wrong side of a weekly boundary —
    exactly the boundary this page's range is drawn on.
    """
    found = slowsellers.lines(rng)
    counted = found.filter(finished_product__recipe__isnull=False).aggregate(
        units=Sum("quantity"), net=Sum("net_cents"),
    )
    colourless = found.filter(
        finished_product__isnull=True,
        raw_product_id__in=_dyed_blank_ids(),
    ).aggregate(units=Sum("quantity"))
    traded = FaireDay.objects.filter(traded=True)
    if rng.start:
        traded = traded.filter(date__gte=rng.start)
    if rng.end:
        traded = traded.filter(date__lte=rng.end)
    traded = set(traded.values_list("date", flat=True))

    return Sold(
        units=int(counted["units"] or 0),
        net=_money(Decimal(counted["net"] or 0) / 100),
        colourless=int(colourless["units"] or 0),
        trading_days=len(traded),
        days_on_file=len(traded & slowsellers.days_with_sales(rng)),
    )


@dataclass
class Alongside:
    """The dyeing and the till over one window, counted in units.

    **Units only.** The money on each side is a different base — output is at
    the asking price, the till is net of whatever it actually went out at —
    so differencing the two would produce something that reads as a margin
    and is not one. Units are the same thing counted twice and subtract
    honestly.

    What the difference is: **the direction the dyed shelves moved over these
    dates, by these two movements alone.** A close, a recount, a bath nobody
    recorded and stock that went out unrung all move the same shelves and are
    in neither column. It is not a comparison of two people's weeks, and the
    page must not render it as one.
    """

    made: int = 0
    sold: int = 0

    @property
    def change(self):
        """Signed, made first. Positive means the shelves ended fuller."""
        return self.made - self.sold

    @property
    def size(self):
        """The change without its sign, for a sentence that supplies one."""
        return abs(self.change)

    @property
    def direction(self):
        """`fuller`, `lighter` or `level` — what the template prints."""
        if self.change > 0:
            return "fuller"
        if self.change < 0:
            return "lighter"
        return "level"


def alongside(made, counter):
    """The unit comparison, from a `Totals` and a `Sold`."""
    return Alongside(made=made.yielded, sold=counter.units)


@dataclass
class Estimate:
    """What a session being planned would consume and produce.

    The same two figures a statement reports, derived from today's prices
    because nothing has happened yet — the freeze is for what *did* happen.
    Its job is to answer "what is this session worth" before the blanks come
    off the shelf, which is where a piece rate would eventually tell somebody
    what the work will pay before they start it.
    """

    baths: int = 0
    units: int = 0
    cost: Decimal = ZERO
    retail: Decimal = ZERO

    @property
    def value_added(self):
        return _money(self.retail - self.cost)


def estimate(baths):
    """An `Estimate` for a list of `production.Bath`.

    Deliberately blind to yield. A plan is what is being asked for, and
    discounting it by a failure rate would be the app pricing in a loss
    nobody has agreed happens — and printing that reading beside somebody's
    work.
    """
    cost = ZERO
    retail = ZERO
    units = 0
    for bath in baths:
        product = bath.product
        units += bath.quantity
        cost += Decimal(bath.quantity) * _dec(product.raw_product.blank_cost)
        retail += Decimal(bath.quantity) * _dec(product.price)
    return Estimate(
        baths=len(baths), units=units, cost=_money(cost), retail=_money(retail),
    )
