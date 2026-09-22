"""What the rest of a season asks of a blank, against what is already owned.

The reorder question for anything dyed is not "how many will we sell" — it is
"how many will we *dye*", and those differ by everything already sitting on a
peg. `private/raw-inventory/` had the shelf count and the par and nothing
about demand at all, so the only reorder signal in the app was a `par_level`
of 100 that reads across every blank as a uniform remnant, in exactly the way
`FinishedProduct.par` did before it got a door.

Everything here reads. Nothing writes a row.

**Two numbers that both get called par, and they are not the same number.**

- The **floor** is `RawProduct.par_level`: what has to stay on the shelf so
  the dye room never stops. It is a level you stay above, it is checked every
  week, and it is what `raw_shortage` and the category page's "below par"
  count already mean.
- The **season requirement** is what the rest of the run will consume, and it
  is a number you fill to once rather than a level you hold. It is derived
  here and deliberately not stored: it moves every weekend the ledger grows,
  and a copy written into a column would go stale silently and then be read
  as somebody's decision.

They are printed in separate columns and **never added together**. A floor
plus a requirement is a number that means nothing and would be ordered
against.

**The forecast is in units, never in dollars.** `seasonreport` projects money
because that is what a season is judged on; yarn is bought by the skein. The
two came apart for real in 2025, when a mid-season reprice lifted takings
while silk lost about a fifth of its unit velocity — a dollar-shaped forecast
would have ordered against the price change.

**Nothing here proposes a par.** The columns are evidence for a person
choosing one: what sold, what the rest of the season is on pace to sell, what
is already dyed, and what is on the shelf. A rule that turned those into a
number would be the production-from-display-capacity failure with a supplier
invoice at the end of it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from . import production, seasonreport, slowsellers
from .models import Faire, FinishedProduct, InventoryLog, RawProduct


@dataclass
class Outlook:
    """One blank's demand and supply, as far as either is known."""

    blank: RawProduct
    sold: int = 0
    #: None when there is nothing to project *from*, which is not zero — see
    #: `units_outlook`. The page prints the difference.
    remaining: int | None = None
    basis_weekends: int = 0
    prior_seasons: int = 0
    finished_on_hand: int = 0
    finished_unsold: int = 0
    #: Whether anything made from this blank is dyed at all. A passthrough —
    #: an undyed yarn, a notion — is bought and resold as it arrives, so its
    #: shortfall is real and its *bath* count is not a thing that exists.
    is_dyed: bool = True
    #: Whether this blank is bought from a supplier at all. A fancy blank is
    #: not — it is a plain blank with line work added — so there is no
    #: reorder to price, the same way `is_dyed` says there is no bath to
    #: count. Both exist because a shortfall is a real number in each case
    #: and the *answer* to it is not the one this page otherwise prints.
    is_bought: bool = True
    #: Units of this blank held by baths that are planned but not yet
    #: reported. Already subtracted from `raw_on_hand`, and printed beside it
    #: so a count that fell without a delivery has its reason on the row.
    claimed: int = 0
    dyed_recently: int = 0
    #: When the most recent production row for this blank was written, at any
    #: depth of history — `dyed_recently` only looks back `RECENT_WEEKS`.
    last_entry: object = None

    @property
    def raw_on_hand(self):
        """Unclaimed yarn. Not the same as skeins on the shelf.

        `production.open_rows` takes a run's blanks off this count when the
        run is created, so anything on an open sheet has already left it —
        see `claimed`, which is the difference between this and what somebody
        standing at the shelf would count.
        """
        return self.blank.number_on_hand

    @property
    def owned(self):
        """Everything already paid for that can meet the rest of the season.

        Undyed stock and dyed stock both count, and they are added here
        because at the blank level they really are interchangeable for this
        one question: a skein sells whether it was dyed last week or last
        winter. They are printed apart as well as together, because they are
        *not* interchangeable the moment the question narrows to a colorway —
        see `finished_unsold`.

        **Claimed yarn counts too, and forgetting it would order it twice.**
        A blank on an open sheet has left `raw_on_hand` and has not yet
        arrived on the finished side, so it falls between the two — and the
        season shortfall is `remaining - owned`, which would then ask for
        skeins that are already in the building, about to be dyed. This is
        the one place the claim has to be added back: the *floor* check is
        the opposite question and deliberately reads the claimed-out number,
        because yarn that is spoken for cannot cover the dye room next week.
        """
        return self.raw_on_hand + self.claimed + self.finished_on_hand

    @property
    def shortfall(self):
        """Raw units to buy to cover the rest of the season, or None.

        None all the way through rather than zero when there is no forecast:
        "nothing more will sell" and "nothing here can say" are different
        answers and only one of them is a reason not to order.
        """
        if self.remaining is None:
            return None
        return max(self.remaining - self.owned, 0)

    @property
    def baths(self):
        """The shortfall in the unit it will actually be dyed in.

        Zero for a passthrough, which has no such unit. `number_per_dye_bath`
        carries its default of 4 on every blank whether or not one is ever
        dyed, so reading it unguarded prints "62 baths" beside a yarn that is
        sold exactly as it arrives — an instruction to do something that does
        not happen, on the page whose whole job is to say what to buy.
        """
        if not self.shortfall or not self.is_dyed:
            return 0
        return math.ceil(self.shortfall / (self.blank.number_per_dye_bath or 1))

    @property
    def cost(self):
        """What filling the shortfall would cost, when it is a thing you buy.

        Zero for a fancy blank, because there is no order to place: you make
        one from a plain veil. Pricing it anyway printed a supplier cost for a
        blank with no supplier, off a `price` nobody maintained — the figure
        was stale by $8.96 to $17.30 and nothing on the page could show it.
        `blank_cost` is what the maintained number is now called.
        """
        if not self.shortfall or not self.is_bought:
            return Decimal(0)
        return Decimal(self.shortfall) * (self.blank.blank_cost or Decimal(0))

    @property
    def floor_short(self):
        """How far below the working floor the shelf is. Never the season.

        Already net of anything on order — see `RawProduct.raw_shortage`. Par
        is an order signal, and a blank with a hundred on a supplier's van is
        not one to order again.
        """
        return self.blank.raw_shortage

    @property
    def on_order(self):
        """Units confirmed on an invoice and not yet arrived."""
        return self.blank.on_order

    @property
    def open_order(self):
        """The oldest invoice holding this blank up, for printing beside it.

        The basis for a shortage that has been held down, because a
        suppressed signal with no visible reason is the `par` mistake wearing
        a delivery note — and a forgotten order is otherwise invisible
        forever.
        """
        return self.blank.oldest_open_order

    @property
    def order_is_late(self):
        return self.blank.order_is_late

    @property
    def never_counted(self):
        """Has anybody ever put an absolute count against this shelf?

        **This is the whole of what can honestly be said**, and it used to
        claim more. The old version asked whether a bath had been *entered*
        since the count, which was a real question while raw stock fell at
        reporting time: an entry then meant skeins had left the shelf after
        somebody counted it.

        Claiming the blanks when a run is created ended that. The skeins now
        leave the count when the sheet is made — see *The blanks come off when
        the run is made* in `docs/claude/production.md` — so an entry written
        afterwards says nothing at all about the count, and the shelf's own
        number already has the claim taken off it. The test fired on the
        ordinary healthy path (plan a sheet, count the shelf, report the baths
        a few days later) and so it fired on the best counts in the system:
        four yarn blanks counted by hand on 13 September read `stale` two days
        later, with nothing wrong with any of them.

        What it was reaching for — baths dyed without a sheet — is exactly the
        thing nothing here can see, and a flag that cannot see its subject
        should not draw a warning. A warning that is always on is one nobody
        reads, which costs the real signal below.

        So the row prints the date and lets a person judge it, and this says
        only the one thing the data supports: nobody has ever looked.
        """
        return self.blank.counted_at is None


def units_outlook(blanks, today=None):
    """`{blank_id: (sold, remaining, basis_weekends, prior_seasons)}`.

    The projection is `seasonreport`'s own: what share of a season the
    weekends already banked took in prior complete seasons, applied to what
    this season has banked. Re-derived on units here rather than reusing
    `_project` because that one spreads money across the weekends still to
    come for the chart to draw; this needs one scalar and no side effects.

    **A blank with no prior complete season gets `None`, not a number.** Every
    yarn colorway's history starts in 2025 and the silk's runs back to 2021,
    so half the catalogue would otherwise be projected off a single season and
    half off five, printed in the same column at the same weight.
    `prior_seasons` rides along so the page can say which it is.
    """
    today = today or timezone.localdate()
    faire = Faire.objects.order_by("-year").first()
    if faire is None:
        return {}

    out = {}
    for blank in blanks:
        seasons = seasonreport.build(faire.slug, blank=blank.pk, today=today)
        focus = next((s for s in seasons if s.year == faire.year), None)
        if focus is None:
            continue
        banked_numbers = {w.number for w in focus.weekends if w.has_data}
        banked = int(sum(w.units for w in focus.weekends if w.has_data))
        priors = [
            s for s in seasons
            if s.year < faire.year and s.is_complete and s.units
        ]
        to_come = [w for w in focus.weekends if w.to_come]
        out[blank.pk] = (banked, None, len(banked_numbers), len(priors))
        if not (priors and to_come and banked):
            continue

        shares = []
        for season in priors:
            part = sum(
                w.units for w in season.weekends if w.number in banked_numbers
            )
            shares.append(Decimal(part) / Decimal(season.units))
        share = sum(shares) / len(shares)
        if not share:
            continue
        projected = Decimal(banked) / share
        out[blank.pk] = (
            banked,
            int(round(projected - Decimal(banked))),
            len(banked_numbers),
            len(priors),
        )
    return out


def _finished_by_blank(blanks, sold_recipes):
    """Dyed stock per blank, and how much of it is in colorways nobody bought.

    The second figure is the whole of this module's honesty about the first.
    Undyed stock is fungible — a skein becomes whatever sells — and dyed stock
    is not, so netting a blank's finished count against a blank's forecast
    quietly assumes every colorway is as good as every other. It mostly is,
    and when it is not this is the column that says so.

    It is reported rather than deducted. The reading is unreliable in a known
    direction: a colorway shows as unsold when it did not sell *and* when the
    line carried no colorway at all, which is most of a Sash Belt's season.
    Subtracting on that basis would order yarn against a gap in the data.
    """
    on_hand = {blank.pk: 0 for blank in blanks}
    unsold = {blank.pk: 0 for blank in blanks}
    dyed = set()
    products = (
        FinishedProduct.objects
        .filter(raw_product__in=blanks, is_active=True, recipe__isnull=False)
        .values_list("raw_product_id", "recipe_id", "number_on_hand")
    )
    for blank_id, recipe_id, number in products:
        dyed.add(blank_id)
        on_hand[blank_id] = on_hand.get(blank_id, 0) + number
        if not sold_recipes.get(recipe_id):
            unsold[blank_id] = unsold.get(blank_id, 0) + number
    return on_hand, unsold, dyed


def _entered_production(blanks, since):
    """Units entered as production per blank, and when the last one landed.

    Named for what it measures. These rows are written when somebody types a
    session up, not when the dye was mixed — the dye room works in bursts and
    the typing happens afterwards, sometimes weeks afterwards — so a quiet
    fortnight here is a fortnight nobody entered, which is not the same claim
    as a fortnight nobody dyed. The column is labelled *entered* on the page
    for that reason, and a week of zeroes is never a reason to order less.

    **An entry taken back is not an entry.** A retraction on
    `private/produced-since/` writes an ADJUSTMENT, which this query never
    reads, so without the exclusion an undone bath would go on reporting
    itself here as dyed — and the date of the last one is what decides when
    to reorder.
    """
    units = {blank.pk: 0 for blank in blanks}
    last = {blank.pk: None for blank in blanks}
    rows = (
        InventoryLog.objects
        .filter(log_type=InventoryLog.PRODUCTION,
                reversals__isnull=True,
                finished_product__raw_product__in=blanks)
        .values_list("finished_product__raw_product_id", "quantity", "created_at")
    )
    for blank_id, quantity, created_at in rows:
        if created_at >= since:
            units[blank_id] = units.get(blank_id, 0) + (quantity or 0)
        if last.get(blank_id) is None or created_at > last[blank_id]:
            last[blank_id] = created_at
    return units, last


def _claimed(blanks):
    """Claimed units per blank, zero-filled for every blank asked about.

    `production.claimed_units` is the one query; this only guarantees a key
    per blank so the page never renders a missing figure as blank.
    """

    claimed = production.claimed_units(blanks)
    return {blank.pk: claimed.get(blank.pk, 0) for blank in blanks}


#: How far back the *entered* column looks. Long enough to span the gap
#: between dyeing something and typing it up, which is the thing being
#: measured whether anybody means it to be or not.
RECENT_WEEKS = 8


def rows(blanks, today=None):
    """An `Outlook` per blank, in the order given."""

    blanks = list(blanks)
    if not blanks:
        return []

    rng = slowsellers.season_range({})
    sold_recipes = slowsellers.sold_by_recipe(rng)
    outlooks = units_outlook(blanks, today=today)
    finished, unsold, dyed = _finished_by_blank(blanks, sold_recipes)
    since = timezone.now() - timedelta(weeks=RECENT_WEEKS)
    entered, last_entry = _entered_production(blanks, since)
    claimed = _claimed(blanks)
    # One query for the whole page rather than `is_bought_in` per blank —
    # this runs over a whole category.
    made_here = set(
        RawProduct.objects
        .filter(pk__in=[b.pk for b in blanks], plain_counterparts__isnull=False)
        .values_list("pk", flat=True)
    )

    out = []
    for blank in blanks:
        sold, remaining, weekends, priors = outlooks.get(blank.pk, (0, None, 0, 0))
        out.append(Outlook(
            blank=blank,
            sold=sold,
            remaining=remaining,
            basis_weekends=weekends,
            prior_seasons=priors,
            finished_on_hand=finished.get(blank.pk, 0),
            finished_unsold=unsold.get(blank.pk, 0),
            is_dyed=blank.pk in dyed,
            is_bought=blank.pk not in made_here,
            claimed=claimed.get(blank.pk, 0),
            dyed_recently=entered.get(blank.pk, 0),
            last_entry=last_entry.get(blank.pk),
        ))
    return out
