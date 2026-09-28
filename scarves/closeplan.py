"""From a Sunday close to a production list, and back on Friday night.

The close is the third way to start a list on `private/production-sheet/`,
beside a par suggestion and a hand pick. It used to have a page of its own,
`private/production-from-close/`, and that was one job split across two
pages: the same list, the same claim, the same print — only the first draft
differed. So it is a seed now (`seed`), and everything after the seed is the
sheet's ordinary editing.

This is the shop's own production loop, which the app spent a long time not
modelling. **The signal is the close.** The crew walk the display on Sunday
night, count what is there, and end the evening holding a stack of kanban
cards — one per product whose bag is empty. That stack *is* the week's work
order, and it has been for years. Everything gets dyed against it and the
week ends with "which of those did I make".

What the app offered instead was `private/production-needed/`: shortages
against par, ranked on season sales, printed as a dye-room sheet. That is a
good page and it is somebody else's question. Par was never dialled in, it
reads across the catalogue as a uniform remnant, and a page built on it asks
somebody to trust a number she did not choose about a shelf she walked past
twelve hours ago. So it went unused, and pushing harder on it was pushing the
wrong way round: the close had already been built, and it turned out to be the
model for how she actually works rather than one more report.

**Two signals, one claim.** Par and the close both propose, and they compete
only if both can plan the same colorway. What stops that is not a rule about
which signal wins — it is that a `ProductionRunRow` is the single claim,
matched on finished product, whoever wrote it. A card that lands on any list
is accounted for and drops off the pool; a colorway already out on a par-based
sheet never enters it. That is the clean division that lets both exist, and it
is why nothing here needs to know which page a competing row came from.

**One close, several lists.** Take five cards onto list A and those five are
gone. Ten more onto list B, the balance onto list C. Nothing is planned twice
unless somebody deliberately adds it back — the pool shrinks as lists are
made, which is the same arithmetic `production.in_flight` does for the
planner, asked of a stack of cards instead.

**And a list is paper or it is not.** `ProductionRun.reporting` is asked once,
when the list is made: print the dye-room sheet and get it back by QR, or skip
the paper and report it on screen. The reporting flow is identical either way
— same rows, same end states, same `accept_line` — so this is a door and not
a mode, and it is stored so nothing asks again.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import production, slowsellers
from .models import CloseRun, CloseRunRow, ProductionRunRow


#: A count at or below this is the top of the list.
#:
#: An empty peg and a peg with one on it are the same news — there is nothing
#: behind them and the next customer is the last customer — so they lead
#: whatever anything sold. Everything else is ordered on what it sold this
#: season, which is the only measured number in the question.
#:
#: Two rather than one because the band is about the *shelf*, not about the
#: arithmetic: `counted` is the total, display plus bag, so 1 is "the last
#: one is hanging there" and 0 is "it has gone". A third value would start
#: being a judgement about how much stock is enough, which is par's job and
#: par is the number this whole page exists to route around.
CRITICAL_AT = 1


def latest_close():
    """The close a bare visit plans from. The most recent one, by day."""
    return CloseRun.objects.order_by("-day").first()


def claims(close):
    """`{finished_product_id: [ProductionRunRow, ...]}` already accounted for.

    **However it got onto a list, it is accounted for.** Matched on finished
    product and nothing else — not on the close, not on which page planned it
    — because "have I already arranged to dye this" has one answer and two
    pages that can arrange it. A colorway sitting on a par-based sheet from
    Tuesday is as claimed as one on list A.

    **Cancelled rows release the claim**, which is the same promise
    `production.in_flight` makes and the same one the crew's *not coming*
    button relies on: a bath that was called off never ran, so the card goes
    back in the pool.

    **Accepted rows keep it.** This deliberately differs from `in_flight`,
    which counts pending rows only — there the question is "how much is still
    out being dyed", and stock that has landed is already in
    `number_on_hand`. Here the question is "is this card dealt with", and a
    card she made and reported on Friday is the most dealt-with a card gets.
    Counting only pending rows would put every finished bath back on the pool
    the moment it was reported, which is the exact opposite of what reporting
    means.

    **The window starts at the close's own day.** A list made last Tuesday was
    answering last week's walk, and a card that came up again on Sunday came
    up again for a reason — the shelf was counted and it was still empty. So
    an older list does not claim a newer card. The boundary is the close's
    day rather than its creation time because a close *is* a day
    (`CloseRun.day` is unique and locks at midnight), and a list planned that
    same evening has to count.
    """
    rows = (
        ProductionRunRow.objects.filter(
            cancelled_at__isnull=True,
            run__created_at__date__gte=close.day,
        )
        .select_related("run")
        .order_by("run__created_at", "pk")
    )
    out: dict[int, list] = {}
    for row in rows:
        out.setdefault(row.finished_product_id, []).append(row)
    return out


@dataclass
class Card:
    """One card off the stack, with everything the choice needs beside it."""

    row: CloseRunRow
    sold: int = 0
    claimed_by: list = field(default_factory=list)

    @property
    def product(self):
        return self.row.finished_product

    @property
    def counted(self) -> int:
        return self.row.counted or 0

    @property
    def is_critical(self) -> bool:
        """Nothing behind it and nothing much on it."""
        return self.counted <= CRITICAL_AT

    @property
    def is_claimed(self) -> bool:
        return bool(self.claimed_by)

    @property
    def bath_size(self) -> int:
        return self.product.bath_size


def cards(close):
    """The stack that close left, ordered the way the shelf reads.

    **The pool is frozen at the close.** A card is an answered row whose
    `counted` came in at or below the `display_slots` the row froze that
    night — the app's own words for an empty bag, asked of the number
    somebody physically counted.

    This is deliberately *not* `closing.card_status()`, which asks the same
    question of the **live** `number_on_hand` because it is answering "what
    should be in the stack right now" for somebody holding the cards. Here
    the question is "what did Sunday find", and it has to stay still: dye a
    bath on Wednesday and the live test flips, so a live pool would quietly
    drop rows out from under a half-made plan and there would be nothing to
    say whether a missing card was made, claimed or never there. Two
    questions, two tests, and the difference is the whole reason both exist.

    **Pending rows are not cards.** A walk that covered 23 of 40 pegs leaves
    the rest unanswered, and an unanswered row is "nobody looked" — never a
    zero. Manufacturing work out of the pile somebody did not get to is the
    same mistake `production.stockout_baths` refuses to make.

    Ordering: critical first — an empty peg or the last one hanging — then
    what the colorway sold this season, most first. Nothing is hidden either
    way, including claimed cards; the page lists those separately so the pool
    stays short, and `is_claimed` is what it reads.
    """

    answered = (
        CloseRunRow.objects.filter(run=close)
        .exclude(outcome=CloseRunRow.PENDING)
        .exclude(counted__isnull=True)
        .select_related(
            "finished_product",
            "finished_product__raw_product",
            "finished_product__raw_product__category",
            "finished_product__recipe",
        )
    )
    sold = slowsellers.sold_by_recipe(slowsellers.season_range({}))
    held = claims(close)

    out = [
        Card(
            row=row,
            sold=sold.get(row.finished_product.recipe_id, 0),
            claimed_by=held.get(row.finished_product_id, []),
        )
        for row in answered
        if row.counted <= row.display_slots and _is_dyeable(row.finished_product)
    ]
    # Critical first, then best sellers, then a stable name so two colorways
    # that sold alike don't shuffle between page loads.
    out.sort(key=lambda c: (not c.is_critical, -c.sold, c.product.name))
    return out


def _is_dyeable(product) -> bool:
    """Whether a bath can answer a shortage of this at all.

    The same three refusals `production.candidates` makes, and for the same
    reasons: an undyed passthrough is ordered rather than made, a fancy veil
    is line work on a scarf that already exists, and a retired colorway is
    one nobody dyes any more. A card for one of those is a real card — it
    genuinely ran out and the crew are genuinely holding the tag — it just
    cannot be answered by heating anything, so putting it on a dye list would
    send somebody to a sink to make a thing that is not made there.
    """
    return (
        product.recipe_id is not None
        and product.recipe.is_active
        and product.raw_product.made_in_a_dye_bath
    )


def partition(stack):
    """`(to choose from, already on a list)` out of one `cards()` call.

    One call rather than two helpers that each re-query: the pool and the
    accounted-for half are the same stack read twice, and two queries is how
    a card ends up in neither list or both.
    """
    pool = [card for card in stack if not card.is_claimed]
    listed = [card for card in stack if card.is_claimed]
    return pool, listed


def lists_for(close):
    """The production lists made from this close, oldest first.

    Oldest first because they were made in an order that meant something —
    list A, then B, then C — and newest-first would read the plan backwards.
    """
    return (
        close.production_runs.all()
        .prefetch_related("rows__finished_product__raw_product",
                          "rows__finished_product__recipe")
        .order_by("created_at", "pk")
    )


def seed(pool):
    """`[(product, 1), ...]` — the pool as the first draft of a list.

    **A bath each, in the shelf's order**, and everything after that is the
    sheet's own editing: strike the ones you are not doing, bump the ones
    that want two. The close page this replaced put an empty box beside every
    card and asked for a number, which is the same decision made the long way
    round — almost every answer was 1, and a list forty long with forty
    blanks to fill reads as a form rather than a plan.
    """
    return [(card.product, 1) for card in pool]


def add_bath(run, product):
    """One more bath of something already on the list. Returns the new row.

    **"If I made more, let me say so."** The list said one bath and the
    session ran two, which is an ordinary thing — a pot with room beside it,
    a colour that came out badly the first time. The alternative was the
    catalogue search, which is the right tool for a colour that was never on
    the list and a poor one for a row already on screen.

    Appended rather than folded into the existing row, for the reason every
    row is one bath: two baths reported as `5, 0` is a pot that failed, and a
    single row of eight cannot say it. `production.lines_for` groups them back
    into one question to answer, so the reporting side sees one colorway with
    two baths on it, which is what somebody is standing in front of.
    """
    return production.open_row(run, product)
