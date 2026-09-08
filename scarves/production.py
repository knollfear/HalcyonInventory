"""The printable production sheet: what to dye, and how the answer gets back.

A dyeing session runs off paper. The dye room has gloves, water and a sink in
it, which makes a phone the wrong thing to be holding, so the sheet is the
work order and a pencil is the input device. What this module builds is that
sheet, plus the one thing that makes it more than a to-do list: a way for the
session's result to come back without anyone typing.

**The row is a bath.** A dye bath is one blank plus one recipe and yields
`number_per_dye_bath` units of a single SKU, so production is not a column of
counts to be entered — it is a handful of yes/no answers. "We only got
through 10 of the 20" is ten ticked boxes. Nobody adds anything up, and the
form on the phone is the same twenty lines that were on the paper, in the
same order, so reporting back is recognition rather than transcription.

**One QR for the sheet, not one per row.** Twenty codes would be twenty
scans to record what is genuinely one session's work. The token in that URL
is what authorises the return: the same bargain as the other `secret/` pages,
except scoped to a single sheet instead of standing open forever, and it
means the crew need no accounts to report production.

**Rows carry a barcode as well as a tick box, and the barcode is the point.**
Nothing in the QR flow reads it — it is there for the next step, where a
photo of the marked sheet fills the same confirmation page in instead of
twenty taps. Barcode decoding already works here and hands back each symbol's
bounding box, which localises its row and gives scale and skew for free; a
tick box at a fixed offset from a barcode is then "is this known rectangle
darker than blank paper?" rather than the general checkbox-recognition
problem. Printing the barcodes now costs nothing and means the sheets already
in circulation work when that lands.

**Marking is positive only — tick what you did, never cross out what you
didn't.** Crossing out is the tempting shorthand and it is wrong twice over:
pen through a Code128 sometimes still decodes and sometimes doesn't, so the
signal that matters is carried by the unreliable mark, and an unmarked row
stops meaning anything definite.
"""

from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from math import ceil

from django.db.models import Count, F, Q, Sum, Value
from django.db.models.functions import Greatest
from django.utils import timezone

from .models import FinishedProduct

#: How many live sheets the picker lists before it stops.
#:
#: This used to be a cap: printing a sixth sheet closed the oldest one
#: automatically, on the reasoning that five sheets out at once already means
#: the reporting loop has stopped working. The reasoning was right and the
#: remedy was the app guessing. Closing a sheet nobody had answered for
#: silently decided that its session never happened — and a sheet with four
#: baths still drying looks exactly like a sheet somebody abandoned.
#:
#: What replaced it is `OVERDUE_AFTER`, which never destroys anything: an old
#: sheet stops *claiming* its baths, gets named on the picker, and waits for
#: a person to accept what came out or cancel what didn't. So this number is
#: now only how long a list gets before it is truncated. Overdue sheets are
#: never truncated — they are the ones that need looking at.
RUNS_LISTED = 5

#: Page furniture, in points (72 to the inch). Plain paper, so unlike the
#: label stock none of this has to line up with anything physical.
PAGE_MARGIN = 40
#: Reserved for the header block. It has to clear the tallest thing in it,
#: which is the QR plus the code plus the URL beneath — not the instructions
#: on the left. Too small and the first list row prints over the URL.
HEADER_HEIGHT = 120
ROW_HEIGHT = 46
QR_SIZE = 74

#: The tick box. Deliberately large and asking to be filled in rather than
#: ticked: a filled box is an ink-density question with an obvious answer,
#: where a small tick that overruns its box is the sort of thing that needs
#: judgement. That matters for the photo path more than for the person.
BOX_SIZE = 22
BOX_LEFT = PAGE_MARGIN

#: Fixed gap from the tick box to the start of its barcode. The photo path
#: works backwards along this: find the barcode, step left by a known
#: distance, and the box is there at a known size.
BOX_TO_BARCODE = 16
BARCODE_WIDTH = 150
BARCODE_HEIGHT = 26

#: Where the box and the bars sit relative to a row's text baseline. Named
#: rather than inlined because `sheetscan` reads back along exactly these
#: numbers — if the drawing and the reading ever disagree the scan lands on
#: blank paper and reports every box empty, which looks like a careful person
#: who ticked nothing.
BOX_BASELINE_OFFSET = -4
BARCODE_BASELINE_OFFSET = 2

#: `(text, bold)` per line. Each page says what to do with *that* page.
#:
#: **The box means "this bath has been accepted into inventory"**, which is
#: not the same claim as "this bath happened". Dyeing is a one-to-three day
#: process — dye, dry, tag, bag — and the app used to model it as one atomic
#: event, so for those days it was wrong in a way the close and the restock
#: walk both read. Marking at the pot puts stock on the books that is still
#: wet on a line and cannot go on a peg.
#:
#: So the box is filled in at the *end* of that process, and a blank box is a
#: complete and honest statement rather than a lost one: 5 of 20 is a sheet
#: that is not finished yet, not a sheet that lost fifteen baths. That is
#: what lets the count be corrected later without anything being reversed,
#: and it is why the same box can carry a bill for the work one day.
BATH_INSTRUCTIONS = (
    ("Fill in a box when that bath is bagged and ready for the booth.", True),
    ("Not when it comes out of the pot — the box means it is counted in stock.", False),
    ("Scan the code, then tap the boxes or photograph this page.", True),
    ("Include the code above in a photo so it can check which sheet.", False),
)
DYE_INSTRUCTIONS = (
    ("Collect these before you start — the baths are on the next page.", False),
    ("Fetch what's listed even if the count looks short; counts drift.", False),
)

#: The work sheet's own instructions. It is the sheet that lives in the dye
#: room for the two or three days the session runs, and it reports nothing.
#:
#: It says the sheet is optional out loud. It exists because a column of
#: boxes is a good way to hold twenty baths at different points across three
#: days — not because anything needs it back.
WORK_INSTRUCTIONS = (
    ("Your working copy — yours to mark up however you like.", True),
    ("Label the columns at the top if you want them named.", False),
    ("Nothing here reports anything. The sheet to send back is the last one.", False),
)

#: How many stage boxes each bath gets on the working copy.
#:
#: **A count, not a list of names.** The obvious version of this printed
#: DYED / DRIED / TAGGED / BAGGED across the top, and that is the app telling
#: somebody how to do a job it does not do — the stages are hers, they vary
#: by what is in the pot, and a printed name is an instruction whether or not
#: it was meant as one.
#:
#: So the boxes are blank and there is a ruled line above each column for her
#: to write her own heading, once per page. Nothing in the app stores what
#: she writes, reads it back, or knows how many stages a bath "should" have.
#: The one transition that matters to the count has its own box on the
#: reporting sheet.
WORK_BOXES = 4

#: The work sheet's boxes. Smaller than the reporting sheet's tick box
#: because nothing photographs them — they are read by the person who made
#: the marks, standing over them.
STAGE_BOX = 15
STAGE_GAP = 9


@dataclass
class Bath:
    """One bath to go and do: a product, and how many it yields."""

    product: FinishedProduct
    quantity: int

    @property
    def recipe_name(self):
        return self.product.recipe.name

    @property
    def blank_name(self):
        return self.product.raw_product.name


#: How a sheet decides which shortages to put on the paper first.
#:
#: `sold` is the default, and it is the same call `private/production-needed/`
#: makes: **par was never dialled in**, so ordering a dye session by shortage
#: ranks it on a number nobody chose — a colorway that sold three all season
#: ahead of one that sold forty, purely for crossing an arbitrary line first.
#: Sales are measured.
#:
#: The two pages must agree by default, which is the other half of the reason.
#: Somebody reads the list ordered one way and then asks the picker for "the
#: first twenty" — and if the picker is sorting by something else, they get
#: twenty baths that are not the ones they were looking at, with nothing on
#: either page to say so.
ORDER_SOLD = "sold"
ORDER_PAR = "par"


def candidates(category=None, include_overshoot=False, order=ORDER_SOLD):
    """Products worth putting on a sheet, most urgent first.

    The default is `FinishedProduct.behind_a_bath` — products where a whole
    bath still lands at or under par, which is where a session's work is
    fully used. `include_overshoot` widens it to everything below par,
    including the ones a bath would take past it.

    That second group is not sloppiness. A bath is a fixed size, so overshoot
    is rounding rather than overproduction, and those shortages get rounded
    away anyway the next time the recipe is dyed. Printing them is worth it
    when the session has capacity to spare; leaving them off is worth it when
    it doesn't. Hence a checkbox rather than a judgement baked in here.
    """
    qs = (
        FinishedProduct.objects.filter(
            is_active=True,
            par__gt=0,
            number_on_hand__lt=F("par"),
            # Undyed passthroughs are ordered, not dyed. Without this the
            # sheet would put "4 × " with no colorway on it and send somebody
            # to the dye room to make something that arrives in a box.
            recipe__isnull=False,
            # Fancy veils are dyed scarves with extra line work added, so
            # they carry a colorway and slip past the test above. You cannot
            # answer a shortage of one by dyeing.
            raw_product__made_in_a_dye_bath=True,
        )
        .select_related("raw_product", "raw_product__category", "recipe")
        # The dye plan walks every recipe on the sheet; without this it is a
        # query per bath.
        .prefetch_related("recipe__recipe_dyes__dye__brand")
    )
    if category is not None:
        qs = qs.filter(raw_product__category=category)

    if not include_overshoot:
        # The SQL form of behind_a_bath, matching the production page's own
        # expression — Greatest keeps a bath size of 0 from making it true
        # for everything, the same `or 1` the model property uses.
        qs = qs.filter(
            par__gte=F("number_on_hand") + Greatest(
                F("raw_product__number_per_dye_bath"), Value(1)
            )
        )

    # Everything above is a prefilter, and it is deliberately loose: baths
    # already in flight only ever make a product *less* needy, so the SQL
    # result is a superset of the answer and the Python pass below narrows
    # it. Doing it here rather than in a Subquery keeps one copy of the
    # arithmetic, which the sort and `plan_baths` both read.
    claimed = in_flight()
    wanted = []
    for product in qs:
        product.in_flight = claimed.get(product.pk, 0)
        product.net_shortage = max(
            product.par - product.number_on_hand - product.in_flight, 0
        )
        if not product.net_shortage:
            continue
        if not include_overshoot:
            expected = product.number_on_hand + product.in_flight
            if product.par < expected + product.bath_size:
                continue
        wanted.append(product)

    if order == ORDER_PAR:
        return sorted(wanted, key=_urgency)

    # Sales first, urgency as the tie-break — so an empty shelf still leads
    # among colorways that sell alike, and a colour nobody buys does not jump
    # the queue for being emptier. Pooled by recipe, because that is the unit
    # a bath is planned in and the unit the other page reports.
    from . import slowsellers

    sold = slowsellers.sold_by_recipe(slowsellers.season_range({}))
    return sorted(
        wanted,
        key=lambda p: (-sold.get(p.recipe_id, 0),) + _urgency(p),
    )


def in_flight():
    """`{finished_product_id: units}` already asked for on a live sheet.

    **Without this a second sheet re-asks for the first sheet's baths**, which
    is the bug this whole area exists to fix. Dyeing takes one to three days,
    so printing a sheet on Saturday and another on Monday used to put the same
    colorway on both — the stock has not arrived yet, so it still reads as
    short — and the session dyes it twice.

    Only `counted_runs` are subtracted: open, and recent enough that the
    baths are still plausibly happening. A sheet that has gone overdue
    releases its claim so the colorway starts being asked for again, which is
    the right answer for paper that has been lost, and is why going overdue
    has to be said out loud rather than happening quietly.

    Pending rows only. An accepted row has already landed in
    `number_on_hand`, so counting it here would subtract it twice, and a
    cancelled row is a bath that is never coming.
    """
    from .models import ProductionRunRow

    rows = (
        ProductionRunRow.objects.filter(
            applied_log__isnull=True,
            cancelled_at__isnull=True,
            run__in=counted_runs().values("pk"),
        )
        .values("finished_product")
        .annotate(units=Sum("quantity"))
    )
    return {row["finished_product"]: row["units"] or 0 for row in rows}


def _urgency(product):
    """Empty shelf first, then biggest shortfall, then a stable name.

    Out of stock leads because it is the only state a customer can see: a
    colorway at zero is missing from the table, where one at half par is just
    a shorter stack. That test stays on the **physical** shelf rather than on
    what is in flight — a bath two days from being bagged is not something a
    customer can buy today, and this is the ordering of a list somebody works
    down now.

    The size of the shortfall is the other way round, because that is a
    question about what still has to be made rather than about what is on the
    table.
    """
    return (
        product.number_on_hand > 0,
        -product.net_shortage,
        product.name,
    )


def plan_baths(limit, category=None, include_overshoot=False, order=ORDER_SOLD):
    """The next `limit` baths, grouped so consecutive rows share a dye pot.

    Baths of the same recipe sit together because that is how the work is
    actually cheaper: one mix, one pot, one temperature, several loads. The
    order *between* recipes is urgency; the order within one is just the
    products that need it.

    A recipe can be cut in half by the limit, and that is fine — the sheet
    was asked for a number of baths and it delivers exactly that number.
    """
    by_recipe = {}
    for product in candidates(category, include_overshoot, order):
        # `net_shortage`, not `shortage`: what is already out being dyed has
        # been taken off, so a sheet asks for the baths still missing rather
        # than reprinting the ones on last week's paper.
        needed = ceil(product.net_shortage / product.bath_size)
        for _ in range(needed):
            by_recipe.setdefault(product.recipe_id, []).append(
                Bath(product=product, quantity=product.bath_size)
            )

    baths = []
    for recipe_baths in by_recipe.values():
        baths.extend(recipe_baths)
        if len(baths) >= limit:
            break
    return baths[:limit]


def baths_from_picks(picks):
    """`[(product, how_many_baths), ...]` -> the flat list of baths.

    The hand-picked counterpart to `plan_baths`, and it deliberately answers a
    different question. `plan_baths` derives what is *needed*; this takes what
    somebody decided. So there is no par test, no shortage arithmetic and no
    in-flight subtraction — a colorway already on another sheet is allowed
    here, because asking for it twice may be exactly what was meant.

    Grouped by recipe for the one reason that is physical rather than a
    judgement: one mix and one pot serve several loads, so baths of a
    colorway belong together on the paper however they were chosen. Order
    between recipes is the order they were picked in, which is the only
    ordering anybody could expect from a list they built themselves.
    """
    by_recipe = {}
    for product, count in picks:
        by_recipe.setdefault(product.recipe_id, []).extend(
            Bath(product=product, quantity=product.bath_size)
            for _ in range(count)
        )

    baths = []
    for recipe_baths in by_recipe.values():
        baths.extend(recipe_baths)
    return baths


def blank_demand(rows):
    """`[(raw_product, needed, believed_on_hand), ...]` for a printed sheet.

    The blanks half of the collection list. Every raw product the sheet's
    baths consume, however many baths want it.

    **Nothing is filtered on stock.** A blank we believe is out is far more
    likely to be a number nobody has updated than an empty shelf, and leaving
    it off the list would turn a stale count into a bath that doesn't get
    dyed. The belief is printed beside the requirement so a real shortage is
    still visible, but the instruction is what to fetch.
    """
    totals = {}
    for row in rows:
        raw = row.finished_product.raw_product
        _, running = totals.get(raw.pk, (raw, 0))
        totals[raw.pk] = (raw, running + row.quantity)
    return [
        (raw, needed, raw.number_on_hand)
        for raw, needed in sorted(totals.values(), key=lambda pair: pair[0].name)
    ]


def raw_demand(baths):
    """`[(raw_product, needed, on_hand), ...]` for the blanks this sheet eats.

    Printed on the picker rather than enforced. A sheet asking for more baths
    than there are blanks to dye is worth knowing about before somebody walks
    to the dye room, but it is not wrong — the order may already be placed,
    and refusing to print would be the app second-guessing a person who can
    see the shelf.
    """
    totals = {}
    for bath in baths:
        raw = bath.product.raw_product
        _, running = totals.get(raw.pk, (raw, 0))
        totals[raw.pk] = (raw, running + bath.quantity)
    return [
        (raw, needed, raw.number_on_hand)
        for raw, needed in sorted(totals.values(), key=lambda pair: pair[0].name)
    ]


def short_blanks(baths):
    """Just the blanks the sheet would run out of."""
    return [(raw, needed, on_hand)
            for raw, needed, on_hand in raw_demand(baths)
            if needed > on_hand]


@dataclass
class DyePlan:
    """Everything to fetch off the shelf before a session starts.

    The point is one walk to the dye shelf instead of twenty. Twenty baths
    across a dozen colorways typically need far fewer than twenty dyes, and
    the ones they share are exactly the ones you don't want to go back for.

    `unrecorded` is the load-bearing field. A recipe with no dyes recorded
    contributes *nothing* to this list, so without saying so the sheet would
    quietly hand over a short list — you'd collect twelve dyes, walk to the
    dye room and find baths whose requirements were never written down. A
    collection list that is silently incomplete is worse than no list,
    because you stop checking.
    """

    entries: list          # (dye, how many baths use it), shelf order
    unrecorded: list       # recipe names with no dyes on file
    unrecorded_baths: int

    @property
    def out_of_stock(self):
        """Dyes this run needs that are marked not in stock.

        Worth surfacing before anyone walks anywhere: a missing dye is a bath
        that cannot run, and finding that out at the sink is the expensive
        version of finding it out here.
        """
        return [dye for dye, _count in self.entries if not dye.in_stock]

    @property
    def is_complete(self) -> bool:
        return not self.unrecorded


def dye_plan(recipes):
    """The dyes `recipes` need between them, one recipe per bath.

    Duplicates in `recipes` are meaningful — they are how many baths want
    that dye, which is the difference between "get the black out" and "get a
    lot of the black out".
    """
    counts = {}
    dyes = {}
    unrecorded = set()
    unrecorded_baths = 0

    for recipe in recipes:
        recipe_dyes = list(recipe.recipe_dyes.all())
        if not recipe_dyes:
            unrecorded.add(recipe.name)
            unrecorded_baths += 1
            continue
        for recipe_dye in recipe_dyes:
            dye = recipe_dye.dye
            dyes[dye.pk] = dye
            counts[dye.pk] = counts.get(dye.pk, 0) + 1

    entries = sorted(
        ((dyes[pk], count) for pk, count in counts.items()),
        # Shelf order: brands sit together, which is how they are stored and
        # so how they are collected.
        key=lambda pair: (pair[0].brand.name, pair[0].name),
    )
    return DyePlan(
        entries=entries,
        unrecorded=sorted(unrecorded),
        unrecorded_baths=unrecorded_baths,
    )


def dye_plan_for_baths(baths):
    """`dye_plan` for a previewed sheet."""
    return dye_plan([bath.product.recipe for bath in baths])


def dye_plan_for_run(run):
    """`dye_plan` for a printed sheet."""
    return dye_plan([row.finished_product.recipe for row in run.rows.all()])


# ---------------------------------------------------------------------------
# What state a sheet is in
# ---------------------------------------------------------------------------
#
# Four questions, not one open/closed flag, and all four answered off the
# rows in this one place. A sheet used to be "closed" the moment any row came
# back, which reads a session as finished on its first reply — and since
# dyeing takes one to three days, most of the sheet is still wet at that
# point. Splitting the questions is what lets the planner subtract baths that
# are genuinely in flight without also subtracting baths on paper nobody has
# seen for a fortnight.


#: How long a sheet's baths keep counting as in flight.
#:
#: Dyeing runs one to three days, so ten is generous on purpose: the bound is
#: not a guess at how long the work takes, it is the point past which an
#: unanswered sheet is better explained by lost paper than by a slow session.
#:
#: What makes the number safe is that crossing it is **visible**. An overdue
#: sheet stops claiming its baths, so the colorways on it start being asked
#: for again — and if that happened quietly, a sheet that really was in
#: progress would get re-dyed behind somebody's back. So the picker names
#: overdue sheets and asks for one of the two answers that exist: accept what
#: came out, or cancel what didn't. No escalation and no count of how often
#: it happens — the same bargain `_drained_at` makes on the restock board.
OVERDUE_AFTER = timedelta(days=10)


def with_row_states(queryset=None):
    """Runs, annotated with the row counts every state question reads.

    One annotation pass rather than a property per question, because these
    are asked of lists — the picker shows several groups at once, and a
    per-run property there is a query per sheet per group.
    """
    from .models import ProductionRun

    if queryset is None:
        queryset = ProductionRun.objects.all()
    return queryset.annotate(
        pending_rows=Count(
            "rows",
            filter=Q(rows__applied_log__isnull=True, rows__cancelled_at__isnull=True),
        ),
        accepted_rows=Count("rows", filter=Q(rows__applied_log__isnull=False)),
    )


def open_runs(queryset=None):
    """Sheets with at least one bath still to settle."""
    return with_row_states(queryset).filter(pending_rows__gt=0)


def closed_runs(queryset=None):
    """Sheets where every bath was either accepted or cancelled.

    There is no separate retired flag behind this. Retiring a sheet *is*
    cancelling what is left on it, so "closed" has one meaning and cannot
    disagree with the rows it is derived from.
    """
    return with_row_states(queryset).filter(pending_rows=0)


def unreported_runs(queryset=None):
    """Sheets nothing has ever been accepted from.

    Deliberately not a subset of `open_runs`: a sheet whose every row was
    cancelled also had nothing accepted, and that is a true and useful thing
    to be able to list. The question is "did any of this reach the shelf",
    which is different from "is any of it outstanding".
    """
    return with_row_states(queryset).filter(accepted_rows=0)


def overdue_runs(queryset=None):
    """Open sheets old enough that their baths have stopped being expected.

    This is the list that has to be looked at, because these are exactly the
    sheets whose colorways the planner has started asking for again.
    """
    return open_runs(queryset).filter(
        created_at__lt=timezone.now() - OVERDUE_AFTER
    )


def counted_runs(queryset=None):
    """Open, recent sheets — the ones whose baths the planner subtracts.

    The only one of the four that changes what the app does. Everything else
    here is a list somebody reads; this one decides whether a colorway gets
    asked for again, which is why it has an age bound at all.
    """
    return open_runs(queryset).filter(
        created_at__gte=timezone.now() - OVERDUE_AFTER
    )


def apply_row(row, yielded=None, fancy=0):
    """Accept one bath into inventory, once. Returns the `InventoryLog`.

    **Applying twice is the failure this guards.** The return URL is a piece
    of paper that can be scanned again, the submit button can be
    double-tapped, and somebody who remembers one more bath will re-open the
    page and submit again. All three are normal, and all three used to be how
    a bath got counted into stock twice — the same shape as the Square
    webhook and redelivered orders.

    **`yielded` is what actually came out, and the raw side does not use it.**
    A bath consumes its blanks whatever happens in the pot: dye four scarves,
    ruin one, and there are still four blanks gone off the shelf. So raw goes
    down by the full `quantity` and finished goes up by `yielded`, and the
    difference is a loss rather than a discrepancy. `None` means the full
    bath, which is what a plain tick claims.

    **The log is written even at a yield of zero**, so `applied_log` stays the
    single answer to "has this row moved anything". A short-circuit that
    skipped the write would need a second mechanism to stop a total loss
    being reported twice, and two guards is how one of them goes stale.
    Nothing downstream minds: `labels.produced_since` already filters on
    `quantity__gt=0`, so a zero row asks for no stickers.

    There is deliberately **no `FAILED_BATH` log type**. A total loss is the
    same event as a partial loss at the end of its range, and giving it its
    own type would record 5→0 and 5→3 in two different shapes — so any
    question about scrap would have to union them, and would get the answer
    wrong the first time somebody forgot. One shape: `PRODUCTION` carrying
    what entered inventory.

    **`fancy` is a subset of `yielded`, never an addition.** Five came out of
    a bath of five and one of them got line work, so `yielded` is 5 and
    `fancy` is 1 — the plain product gets 4 and the fancy one gets 1. Keeping
    it a subset is what leaves `loss` meaning what it says: the bath is short
    only if fewer came out than were asked for, and how they were finished is
    a different question from whether they survived.

    This is the second route to a fancy veil and the better-evidenced one.
    `scarves/fancy.py` records the other — a plain scarf already in stock that
    had line work added later — which has to be inferred after the fact and
    leaves the plain side overstated until somebody notices. Here the scarf is
    routed before it was ever counted as plain, by the person who made it.
    Anything asking about fancy supply has to read both.

    Un-ticking is still not the inverse of this. Once stock has moved the
    correction is an inventory adjustment with a reason attached.
    """
    from .models import InventoryLog

    if row.applied_log_id is not None:
        return row.applied_log

    product = row.finished_product
    raw = product.raw_product
    made = row.quantity if yielded is None else max(int(yielded), 0)
    # The paper said how big the bath was, and a yield above that is somebody
    # answering a different question. Clamped rather than refused: a number
    # too large is a typo at a sink, and losing the whole report to it would
    # cost far more than the unit it trims.
    made = min(made, row.quantity)

    # Fancy can only be some of what came out, and only where there is a
    # counterpart to be. Clamped for the same reason the yield is.
    target = row.fancy_target
    fancied = 0 if target is None else min(max(int(fancy or 0), 0), made)
    plain = made - fancied

    # The full bath, never the yield. The blanks left the shelf regardless of
    # what came back out of the pot — and a fancy veil is a plain scarf with
    # line work on it, so routing one changes nothing on the raw side.
    raw.number_on_hand = max(raw.number_on_hand - row.quantity, 0)
    raw.save(update_fields=["number_on_hand"])

    product.number_on_hand += plain
    product.save(update_fields=["number_on_hand"])

    lost = row.quantity - made
    notes = f"Dye bath accepted from production sheet run {row.run_id}."
    if lost:
        notes += (
            f" {lost} of {row.quantity} did not make it —"
            f" the blanks were used either way."
        )
    if fancied:
        notes += (
            f" {fancied} of {made} finished as {target.raw_product.name}."
        )

    # The plain row's log, written even at zero — it is what `applied_log`
    # points at, and so what stops the bath being counted twice. A bath whose
    # whole output went out fancy still writes it.
    log = InventoryLog.objects.create(
        finished_product=product,
        raw_product=raw,
        log_type=InventoryLog.PRODUCTION,
        source=InventoryLog.SOURCE_PRODUCTION_SHEET,
        quantity=plain,
        notes=notes,
    )
    if fancied:
        # A second product, so a second row — every stock movement in this app
        # is per product, the same shape the conversion page writes. It is
        # PRODUCTION rather than a conversion because nothing was converted:
        # this scarf was never plain.
        target.number_on_hand += fancied
        target.save(update_fields=["number_on_hand"])
        InventoryLog.objects.create(
            finished_product=target,
            raw_product=raw,
            log_type=InventoryLog.PRODUCTION,
            source=InventoryLog.SOURCE_PRODUCTION_SHEET,
            quantity=fancied,
            notes=notes,
        )

    row.fancy_yield = fancied
    row.yielded = made
    row.accepted_at = timezone.now()
    # Accepting says the bath ran and here is what came out of it. Cancelling
    # says it never ran. If both have been claimed the physical one wins,
    # because somebody is standing there holding the scarves — the same rule
    # that lets a decoded barcode beat the batch page's blank picker.
    row.cancelled_at = None
    row.applied_log = log
    row.save(update_fields=[
        "yielded", "fancy_yield", "accepted_at", "cancelled_at", "applied_log",
    ])
    return log


def cancel_row(row):
    """Call a bath off. Nothing moves, and the colorway is asked for again.

    **Cancelled and binned are different, and both are needed.** Cancelled
    means the bath never ran: no blanks were consumed, because nothing was
    ever decremented, and the claim this row had on the planner is released
    so the colorway comes back on the next sheet. A bath that ran and lost
    the whole lot is the other thing entirely — the blanks are gone — and it
    goes through `apply_row` at a yield of zero.

    A row that has already moved stock is left alone. Taking that back is an
    inventory adjustment with a reason attached, which is the same refusal
    the crew's tick boxes make.
    """
    if row.applied_log_id is not None or row.cancelled_at is not None:
        return False
    row.cancelled_at = timezone.now()
    row.save(update_fields=["cancelled_at"])
    return True


def uncancel_row(row):
    """Put a called-off bath back to pending. Nothing moved, so nothing undoes.

    **This exists for the same reason the Sunday close has an Undo button.**
    Cancelling is reachable with nothing but the code printed on the paper,
    which means it is reachable by somebody with no account and no way to
    reach an admin screen — and a mis-tap they cannot fix is a mistake they
    have to go and tell somebody about. That cost is exactly the pressure
    that gets one left unmentioned instead.

    It is free to offer here in a way undoing an *acceptance* is not: a
    cancelled row moved no stock, so putting it back is a row going from one
    unreported state to another. Nothing is erased and nothing is
    compensated, because nothing happened.
    """
    if row.applied_log_id is not None or row.cancelled_at is None:
        return False
    row.cancelled_at = None
    row.save(update_fields=["cancelled_at"])
    return True


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def row_code(row):
    """What a row's barcode carries: its SKU *and* its place on the sheet.

    The order matters because a decoder returns one result per distinct
    symbol, not per printed symbol — three identical barcodes on one page
    come back as one. A sheet routinely prints the same SKU several times
    (`plan_baths` groups repeated baths of a colorway together on purpose),
    so a SKU-only barcode would silently collapse them and the scan would
    report one bath where four were marked.

    Carrying the SKU as well as the position keeps the code self-describing
    and gives the scan a second check on being pointed at the right sheet,
    which is worth the handful of extra bars.
    """
    return f"{row.finished_product.sku or 'ROW'}#{row.order}"


def barcode_symbol(value):
    """Exactly the Code128 symbol a sheet prints for this value.

    Shared with `sheetscan`, which re-derives it to know where the tick box
    sits relative to the *bars*. That matters because the quiet zones don't
    scale with the rest: reportlab pins them at a quarter inch, so the drawn
    symbol is wider than `BARCODE_WIDTH` by a margin that depends on the SKU.
    A scanner that assumed otherwise would measure a window offset by several
    points and quietly read the wrong patch of paper.
    """
    from reportlab.graphics.barcode import code128

    trial = code128.Code128(
        value, barHeight=BARCODE_HEIGHT, barWidth=1.0, humanReadable=False
    )
    return code128.Code128(
        value,
        barHeight=BARCODE_HEIGHT,
        barWidth=BARCODE_WIDTH / trial.width,
        humanReadable=False,
    )


def box_geometry(value):
    """Where the tick box is, measured from the *bars* of this row's barcode.

    Returns `(right_gap, below, size)` in points:

    - `right_gap`  bars' left edge back to the box's right edge
    - `below`      how far the box's bottom sits under the bars' bottom
    - `size`       the box, square

    All three are read off the same constants the drawing uses, so there is
    no second copy of the layout to keep in step.
    """
    symbol = barcode_symbol(value)
    right_gap = BOX_TO_BARCODE + symbol.lquiet
    below = BARCODE_BASELINE_OFFSET - BOX_BASELINE_OFFSET
    return right_gap, below, BOX_SIZE


def bars_width(value):
    """Width of just the bars, with the quiet zones taken off.

    This is what a decoder's bounding box actually covers, and so what the
    scale of a photo has to be worked out from.
    """
    symbol = barcode_symbol(value)
    return symbol.width - symbol.lquiet - symbol.rquiet


def render_sheet(run, return_url) -> bytes:
    """The sheet, as PDF bytes: collect, then work, then report.

    **Three documents in one print, in the order the job happens.** The
    collection page is a walk to the shelf; the work sheet lives in the dye
    room for the two or three days the session runs; the reporting sheet is
    what comes back at the end of it.

    Splitting them is what the one-to-three-day process needs. A single sheet
    had to be both the thing marked up mid-session and the thing photographed
    at the end, and those want opposite properties: the first gets wet, gets
    scribbled on and is only read by the person holding it, while the second
    has to survive a phone camera and mean exactly one thing per mark.

    **The work sheet carries no barcodes and no QR, and that absence is the
    feature.** It is the only thing stopping the wrong sheet being
    photographed — a marked-up working copy read as a report would tick baths
    that are still on a drying line. With nothing on it to decode, a photo of
    it cannot name a run, so the upload page asks for the code instead of
    quietly filing a session that hasn't finished.

    **Nothing here reads the run's state.** Every row prints, accepted or
    cancelled or neither, because the sheet is the work order and not a
    report on itself. Information flows paper → app; the moment a PDF starts
    hiding rows that have come back, a reprint stops being the same document
    as the print it replaces.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    page_w, page_h = letter
    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=letter)
    pdf.setTitle(f"Production sheet — run {run.pk}")

    # **Every row, whatever state it is in.** The sheet renders what the run
    # asked for and reads nothing back — a reprint mid-session is the same
    # document as the first print, not a rendering of what has happened
    # since. Information flows paper → app, and a PDF that started hiding
    # rows that had come back would flow it the other way: two sheets for one
    # run that disagree about how many baths are on it, where the one that
    # disagrees is the one already in somebody's hand.
    rows = list(
        run.rows
        .select_related("finished_product__recipe", "finished_product__raw_product")
        .prefetch_related("finished_product__recipe__recipe_dyes__dye__brand")
    )
    per_page = int((page_h - PAGE_MARGIN * 2 - HEADER_HEIGHT) // ROW_HEIGHT)
    per_page = max(per_page, 1)

    # Collection comes first because that is the order the work happens in:
    # one walk to the shelf, then the session. It is its own page rather than
    # a block above the rows so a long list can't squeeze them, and so it can
    # be carried to the shelf on its own.
    _draw_collection_page(pdf, run, return_url, rows, page_w, page_h)
    _draw_pages(pdf, run, return_url, rows, page_w, page_h, per_page,
                instructions=WORK_INSTRUCTIONS, draw=_draw_work_row, qr=False,
                headings=True)
    _draw_pages(pdf, run, return_url, rows, page_w, page_h, per_page,
                instructions=BATH_INSTRUCTIONS, draw=_draw_row, qr=True)

    pdf.save()
    buf.seek(0)
    return buf.read()


def _draw_pages(pdf, run, return_url, rows, page_w, page_h, per_page,
                instructions, draw, qr, headings=False):
    """One list of baths, paginated, with `draw` doing each row.

    The work sheet and the reporting sheet are the same rows in the same
    order — that is what makes transcribing between them recognition rather
    than reading — so only the row itself and the header differ.
    """
    pages = max(ceil(len(rows) / per_page), 1)
    for start in range(0, max(len(rows), 1), per_page):
        _draw_header(pdf, run, return_url, page_w, page_h,
                     page_no=start // per_page + 1,
                     page_count=pages,
                     instructions=instructions, qr=qr)
        y = page_h - PAGE_MARGIN - HEADER_HEIGHT
        if headings:
            _draw_stage_headings(pdf, page_w, y + 6)
        for index, row in enumerate(rows[start:start + per_page]):
            draw(pdf, row, start + index + 1, y, page_w)
            y -= ROW_HEIGHT
        pdf.showPage()


def _draw_collection_page(pdf, run, return_url, rows, page_w, page_h):
    """The shelf list: the blanks and the dyes this run needs.

    Both halves of one errand, in the order the work happens — you carry
    scarves to the dye room, and you carry dye to them.
    """
    plan = dye_plan([row.finished_product.recipe for row in rows])
    blanks = blank_demand(rows)

    _draw_header(pdf, run, return_url, page_w, page_h,
                 page_no=None, page_count=None,
                 instructions=DYE_INSTRUCTIONS)

    y = page_h - PAGE_MARGIN - HEADER_HEIGHT

    def carry_on(cursor):
        """Start a fresh page when the current one runs out."""
        if cursor >= PAGE_MARGIN + 20:
            return cursor
        pdf.showPage()
        _draw_header(pdf, run, return_url, page_w, page_h,
                     page_no=None, page_count=None,
                     instructions=DYE_INSTRUCTIONS)
        return page_h - PAGE_MARGIN - HEADER_HEIGHT

    # --- blanks ------------------------------------------------------------
    pdf.setFont("Helvetica-Bold", 13)
    units = sum(needed for _, needed, _ in blanks)
    pdf.drawString(
        PAGE_MARGIN, y,
        f"Blanks to collect — {units} item{'' if units == 1 else 's'}",
    )
    y -= 20

    pdf.setFont("Helvetica", 10)
    for raw, needed, on_hand in blanks:
        pdf.drawString(PAGE_MARGIN + 22, y, raw.name)
        note = f"{needed}"
        if on_hand < needed:
            # Printed, not acted on: a blank we think is out is more likely a
            # number nobody updated than an empty shelf.
            note += f"   (we think {on_hand} on hand)"
        pdf.drawRightString(page_w - PAGE_MARGIN, y, note)
        y -= 17
        y = carry_on(y)

    y -= 10
    y = carry_on(y)

    # --- dyes --------------------------------------------------------------
    pdf.setFont("Helvetica-Bold", 13)
    count = len(plan.entries)
    pdf.drawString(
        PAGE_MARGIN, y,
        f"Dyes to collect — {count} dye{'' if count == 1 else 's'}",
    )
    y -= 20

    if not plan.is_complete:
        # Said on the paper, not just on the screen that printed it. The
        # person at the shelf is the one who needs to know the list is short,
        # and they are holding this rather than looking at a browser.
        pdf.setFont("Helvetica-Bold", 9.5)
        recipes = ", ".join(plan.unrecorded[:6])
        more = len(plan.unrecorded) - 6
        if more > 0:
            recipes += f", and {more} more"
        for line in (
            f"INCOMPLETE — {plan.unrecorded_baths} "
            f"bath{'' if plan.unrecorded_baths == 1 else 's'} on this sheet "
            f"{'has' if plan.unrecorded_baths == 1 else 'have'} no dyes on "
            f"file, so this list does not cover "
            f"{'it' if plan.unrecorded_baths == 1 else 'them'}:",
            recipes,
        ):
            pdf.drawString(PAGE_MARGIN, y, line[:110])
            y -= 12
        y -= 6

    pdf.setFont("Helvetica", 10)
    for dye, uses in plan.entries:
        # A colour chip, because a jar is found by eye long before its label
        # is read. The sheets print in colour anyway.
        try:
            pdf.setFillColor(dye.hex_color)
        except Exception:
            pdf.setFillColorRGB(1, 1, 1)
        pdf.rect(PAGE_MARGIN, y - 3, 14, 12, fill=1, stroke=1)
        pdf.setFillColorRGB(0, 0, 0)

        label = f"{dye.name} · {dye.brand.name}"
        if not dye.in_stock:
            label += "   ** NOT IN STOCK **"
        pdf.drawString(PAGE_MARGIN + 22, y, label)
        pdf.drawRightString(
            page_w - PAGE_MARGIN, y,
            f"{uses} bath{'' if uses == 1 else 's'}",
        )
        y -= 17
        before = y
        y = carry_on(y)
        if y != before:
            pdf.setFont("Helvetica", 10)

    if not plan.entries:
        pdf.drawString(PAGE_MARGIN, y, "No dyes are recorded for any of these recipes.")

    pdf.showPage()


def _draw_header(pdf, run, return_url, page_w, page_h, page_no, page_count,
                 instructions=(), qr=True):
    """The block every page carries, with or without the code.

    `qr=False` is the work sheet, and it is deliberate rather than tidy: the
    working copy must carry nothing a camera can read, so a photo of it can't
    be mistaken for a report of a session that is still drying. It still says
    the run number and code in plain text, because the two sheets have to be
    matchable by a person holding both.
    """
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode import qr as qr_module
    from reportlab.graphics.shapes import Drawing

    top = page_h - PAGE_MARGIN

    pdf.setFont("Helvetica-Bold", 17)
    pdf.drawString(PAGE_MARGIN, top - 14, f"Production sheet — run {run.pk}")

    total = run.rows.count()
    where = "" if page_no is None else f" · page {page_no} of {page_count}"
    pdf.setFont("Helvetica", 10)
    pdf.drawString(
        PAGE_MARGIN, top - 30,
        f"{total} bath{'' if total == 1 else 's'}"
        f" · printed {run.created_at:%d %b %Y}{where}",
    )
    # The instructions belong to the page, not to the run. A collection page
    # telling somebody to fill in tick boxes is describing a different sheet
    # to the one in their hand.
    y = top - 44
    for text, bold in instructions:
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", 10)
        pdf.drawString(PAGE_MARGIN, y, text)
        y -= 14

    if not qr:
        # The working copy. Its identity is text only — anything scannable
        # here is a way for the wrong sheet to be photographed.
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawRightString(page_w - PAGE_MARGIN, top - 14,
                            f"WORKING COPY — run {run.pk}")
        pdf.setFont("Helvetica", 8)
        pdf.drawRightString(page_w - PAGE_MARGIN, top - 26,
                            f"code {run.token} · do not photograph this page")
        return

    # The URL in plain text under the code, because the QR is the convenience
    # and the paper is the record. A cracked camera or a dead phone shouldn't
    # be the reason a session goes unreported.
    widget = qr_module.QrCodeWidget(return_url, barLevel="M")
    bounds = widget.getBounds()
    drawing = Drawing(QR_SIZE, QR_SIZE, transform=[
        QR_SIZE / (bounds[2] - bounds[0]), 0, 0,
        QR_SIZE / (bounds[3] - bounds[1]), 0, 0,
    ])
    drawing.add(widget)
    renderPDF.draw(drawing, pdf, page_w - PAGE_MARGIN - QR_SIZE, top - QR_SIZE)

    # 8pt, not the 6.5 this started at. It is the last resort when the code
    # won't scan and the run has already dropped off `secret/production/`, and
    # a fallback nobody can read off the page is not a fallback. The token is
    # sixteen mixed-case characters, so it needs all the legibility it can get.
    # The code in plain text, big enough to read off a photocopy. This is
    # what somebody types when the QR won't scan, and typing it *from the
    # sheet* is what ties a photo to the run — so it has to be legible and
    # unmistakably part of this sheet, not buried in a URL.
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawRightString(page_w - PAGE_MARGIN, top - QR_SIZE - 13,
                        f"CODE: {run.token}")
    pdf.setFont("Helvetica", 7)
    pdf.drawRightString(page_w - PAGE_MARGIN, top - QR_SIZE - 24, return_url)


def _stage_columns(page_w):
    """Left edge of each stage box, right-aligned off the margin.

    Shared by the row and the heading rule so the two cannot drift apart —
    a heading over the wrong column is worse than no heading.
    """
    width = WORK_BOXES * STAGE_BOX + (WORK_BOXES - 1) * STAGE_GAP
    start = page_w - PAGE_MARGIN - width
    return [start + i * (STAGE_BOX + STAGE_GAP) for i in range(WORK_BOXES)]


def _draw_stage_headings(pdf, page_w, y):
    """A ruled line over each column, for her to name it herself.

    Blank on purpose. Printing DYED / DRIED / TAGGED / BAGGED here would be
    the app telling somebody how to do a job it doesn't do — the stages vary
    with what is in the pot, and a printed name is an instruction whether or
    not it was meant as one. A line is an invitation.

    Once per page rather than once per sheet, because pages get separated.
    """
    pdf.setLineWidth(0.6)
    for x in _stage_columns(page_w):
        pdf.line(x - 2, y, x + STAGE_BOX + 2, y)
    pdf.setLineWidth(1)


def _draw_work_row(pdf, row, number, y, page_w):
    """One bath on the working copy: what to make, and blank boxes.

    No barcode and no tick box — nothing here is read by anything. What the
    sheet is *for* is holding twenty baths at different points across three
    days, which is a thing paper does well and a phone by a sink does not.
    """
    product = row.finished_product
    baseline = y - ROW_HEIGHT + 12

    pdf.setFont("Helvetica", 8)
    pdf.drawString(PAGE_MARGIN, baseline + 14, f"#{number}")

    text_x = PAGE_MARGIN + 24
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(text_x, baseline + 14, f"{row.quantity} × {product.recipe.name}")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(text_x, baseline + 2, product.raw_product.name)

    pdf.setLineWidth(1)
    for x in _stage_columns(page_w):
        pdf.rect(x, baseline + 2, STAGE_BOX, STAGE_BOX)


def _draw_row(pdf, row, number, y, page_w):
    """One bath on the reporting sheet: box, barcode, and what came out.

    The box and the barcode are at exactly the offsets `sheetscan` reads back
    along — `box_geometry` derives them from these same constants, so there
    is no second copy of the layout to drift.
    """
    product = row.finished_product
    baseline = y - ROW_HEIGHT + 12

    # The box. Heavy stroke so a photo of it has something unambiguous to
    # measure against, and empty inside so "filled in" is the only ink there.
    pdf.setLineWidth(1.6)
    pdf.rect(BOX_LEFT, baseline + BOX_BASELINE_OFFSET, BOX_SIZE, BOX_SIZE)
    pdf.setLineWidth(1)

    barcode_x = BOX_LEFT + BOX_SIZE + BOX_TO_BARCODE
    drawn_width = BARCODE_WIDTH
    if product.sku:
        symbol = barcode_symbol(row_code(row))
        symbol.drawOn(pdf, barcode_x, baseline + BARCODE_BASELINE_OFFSET)
        # The real width, not the target: quiet zones don't scale, so the
        # symbol runs wider than BARCODE_WIDTH and the text has to start
        # after where it actually ends.
        drawn_width = symbol.width

    text_x = barcode_x + drawn_width + 14

    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(text_x, baseline + 14, f"{row.quantity} × {product.recipe.name}")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(
        text_x, baseline + 2,
        f"{product.raw_product.name} · {product.sku or 'no SKU'} · "
        f"{product.number_on_hand} on hand, par {product.par}",
    )

    # A ruled space to write the number that actually came out, against what
    # the bath was asked for. The tick alone still means the full bath, which
    # is what the photo path reads — this is for the session that produced
    # three of four, where the box says "accepted" and the line says how many.
    rule_w = 34
    rule_x = page_w - PAGE_MARGIN - rule_w
    pdf.setLineWidth(0.8)
    pdf.line(rule_x, baseline + 1, rule_x + rule_w, baseline + 1)
    pdf.setFont("Helvetica", 7)
    pdf.drawCentredString(rule_x + rule_w / 2, baseline - 8,
                          f"OF {row.quantity}")
    pdf.setFont("Helvetica", 8)
    pdf.drawRightString(rule_x - 10, baseline + 2, f"#{number}")
