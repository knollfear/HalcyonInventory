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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from .models import ProductionRunRow

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


def statements(limit=None):
    """A `Statement` per run that has closed baths, most recent first.

    One query for the rows, grouped in Python: a statement needs the rows
    themselves — a run is a handful of baths, and per-run aggregates in SQL
    would still have to come back to them to name the unpriced ones.
    """
    rows = (
        ProductionRunRow.objects
        .filter(applied_log__isnull=False)
        .select_related("run", "finished_product",
                        "finished_product__raw_product")
        .prefetch_related("applied_log__reversals")
        .order_by("-run_id", "order", "pk")
    )

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
