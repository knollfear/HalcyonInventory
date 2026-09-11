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

from . import seasonreport
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
    dyed_recently: int = 0
    #: When the most recent production row for this blank was written, at any
    #: depth of history — `dyed_recently` only looks back `RECENT_WEEKS`.
    last_entry: object = None

    @property
    def raw_on_hand(self):
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
        """
        return self.raw_on_hand + self.finished_on_hand

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
        """The shortfall in the unit it will actually be dyed in."""
        if not self.shortfall:
            return 0
        return math.ceil(self.shortfall / (self.blank.number_per_dye_bath or 1))

    @property
    def cost(self):
        if not self.shortfall:
            return Decimal(0)
        return Decimal(self.shortfall) * (self.blank.price or Decimal(0))

    @property
    def floor_short(self):
        """How far below the working floor the shelf is. Never the season."""
        return self.blank.raw_shortage

    @property
    def count_is_stale(self):
        """Has a bath been recorded since anybody counted this shelf?

        The cheap version of a question with no good answer: raw stock only
        goes down when a dye bath is *entered*, so the count is wrong by
        however much has been dyed since — which is unknowable, because the
        baths that have not been entered are exactly the ones nothing knows
        about. What can be said is whether the count predates the dyeing the
        app does know about, and that is enough to stop the number being read
        as a measurement of today.
        """
        if self.blank.counted_at is None:
            return True
        if self.last_entry is None:
            return False
        return self.last_entry > self.blank.counted_at


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
    products = (
        FinishedProduct.objects
        .filter(raw_product__in=blanks, is_active=True, recipe__isnull=False)
        .values_list("raw_product_id", "recipe_id", "number_on_hand")
    )
    for blank_id, recipe_id, number in products:
        on_hand[blank_id] = on_hand.get(blank_id, 0) + number
        if not sold_recipes.get(recipe_id):
            unsold[blank_id] = unsold.get(blank_id, 0) + number
    return on_hand, unsold


def _entered_production(blanks, since):
    """Units entered as production per blank, and when the last one landed.

    Named for what it measures. These rows are written when somebody types a
    session up, not when the dye was mixed — the dye room works in bursts and
    the typing happens afterwards, sometimes weeks afterwards — so a quiet
    fortnight here is a fortnight nobody entered, which is not the same claim
    as a fortnight nobody dyed. The column is labelled *entered* on the page
    for that reason, and a week of zeroes is never a reason to order less.
    """
    units = {blank.pk: 0 for blank in blanks}
    last = {blank.pk: None for blank in blanks}
    rows = (
        InventoryLog.objects
        .filter(log_type=InventoryLog.PRODUCTION,
                finished_product__raw_product__in=blanks)
        .values_list("finished_product__raw_product_id", "quantity", "created_at")
    )
    for blank_id, quantity, created_at in rows:
        if created_at >= since:
            units[blank_id] = units.get(blank_id, 0) + (quantity or 0)
        if last.get(blank_id) is None or created_at > last[blank_id]:
            last[blank_id] = created_at
    return units, last


#: How far back the *entered* column looks. Long enough to span the gap
#: between dyeing something and typing it up, which is the thing being
#: measured whether anybody means it to be or not.
RECENT_WEEKS = 8


def rows(blanks, today=None):
    """An `Outlook` per blank, in the order given."""
    from . import slowsellers

    blanks = list(blanks)
    if not blanks:
        return []

    rng = slowsellers.season_range({})
    sold_recipes = slowsellers.sold_by_recipe(rng)
    outlooks = units_outlook(blanks, today=today)
    finished, unsold = _finished_by_blank(blanks, sold_recipes)
    since = timezone.now() - timedelta(weeks=RECENT_WEEKS)
    entered, last_entry = _entered_production(blanks, since)

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
            dyed_recently=entered.get(blank.pk, 0),
            last_entry=last_entry.get(blank.pk),
        ))
    return out
