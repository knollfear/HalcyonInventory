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
from decimal import Decimal
from io import BytesIO
from math import ceil

from django.db import transaction
from django.db.models import Count, F, Q, Sum, Value
from django.db.models.functions import Greatest
from django.utils import timezone

from . import ledger, slowsellers
from .dyeamounts import bath_amounts, format_ounces
from .models import (
    OVERDUE_AFTER,
    CloseRun,
    CloseRunRow,
    FaireDay,
    FinishedProduct,
    InventoryLog,
    ProductionRun,
    ProductionRunRow,
    RawProduct,
)

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

#: How many trays fit in the oven. **One tray is one bath**, so this is a row
#: count and nothing here needs tray arithmetic of its own.
#:
#: Some colorways are made in the oven rather than in a pot, and running it
#: is an *event* — it gets heated once, it holds fifteen trays, and fifteen
#: is what makes the heating worth it. That makes the oven the opposite
#: planning problem from the dye room: a stovetop session is bounded by how
#: much work there is, and an oven session is bounded by the box. So the
#: picker plans *to* this number rather than to a number somebody types.
#:
#: A named constant rather than a model, because there is one oven and a
#: second one is a one-line edit on the day it exists. A `DisplayFixture`
#: for it would be a dimension nothing reads — the fiber-field mistake.
#:
#: **Nothing refuses to print short of it.** A sheet at nine trays is a
#: session somebody has a reason for, and the app arguing with a person who
#: can see the calendar is the same overreach as refusing to print when the
#: blanks look short. The gap is said out loud and the paper still comes out.
OVEN_TRAYS = 15

#: Page furniture, in points (72 to the inch). Plain paper, so unlike the
#: label stock none of this has to line up with anything physical.
PAGE_MARGIN = 40
#: Reserved for the header block. It has to clear the tallest thing in it,
#: which is the QR plus the code plus the URL beneath — not the instructions
#: on the left. Too small and the first list row prints over the URL.
HEADER_HEIGHT = 120
ROW_HEIGHT = 46
QR_SIZE = 74

#: Reserved at the foot of the *first* collection page for the upload code.
#: Has to clear the rule, the QR, its label and the URL under it. The blanks
#: and dyes flow above it; if they run past it they start a fresh page, which
#: carries no footer and gets the full height back.
FOOTER_HEIGHT = 108
#: The second QR is smaller than the header's on purpose. It is scanned off a
#: page held in the hand rather than read out of a photograph, so it needs to
#: be findable rather than robust to a camera at arm's length — and at the
#: header's size two codes on one page start to look like a choice with
#: consequences.
UPLOAD_QR_SIZE = 62

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
    ("To send a photo: scan SEND A PHOTO on the collection page.", False),
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
    ("The dyes for each bath are listed beside it.", False),
    ("Nothing here reports anything. The sheet to send back is the last one.", False),
)

#: How many boxes each bath gets on the working copy.
#:
#: **One. It was four, and four was a guess.** The reasoning for a row of
#: them was that a bath moves through stages over one to three days and paper
#: holds that better than a phone by a sink does — which is true, and it does
#: not follow that the app should decide how many stages there are. It never
#: knew: the count was invented here, the boxes were deliberately left
#: unlabelled *because* nothing could honestly name them, and a ruled line was
#: printed over each column so somebody could name them herself. A column
#: nobody asked for, headed by nothing, is not an invitation — it is four
#: boxes to ignore per row and a wider sheet for the privilege.
#:
#: One box says the one thing the paper is for: this bath is done with. The
#: sheet is still hers to mark up however she likes, and now there is room in
#: the row for something she actually needs, which is what goes in the pot.
WORK_BOXES = 1

#: The work sheet's box. Smaller than the reporting sheet's tick box because
#: nothing photographs it — it is read by the person who made the mark,
#: standing over it.
STAGE_BOX = 15


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
#: The third answer to "which shortages first": judge every product against
#: `DemandPar` and order by how far below *that* it is. One control with
#: three answers on both pages — the first cut made this a checkbox beside
#: the other two, and the user called that a disjointed experience: it is
#: the same question, so it is the same control.
ORDER_SALES_PAR = "sales_par"


#: Par from sales, in one line: **a day's sales rounded up, at least one,
#: doubled, plus one** — so the smallest par this can ask for is three.
#:
#: **The order of operations is the whole of it, and the first cut had it
#: wrong.** Doubling first and rounding after (`ceil(2 × rate) + 1`) gave a
#: colorway selling half a unit a day a par of 2, and two on a shelf is one
#: sale away from a hole — the exact thing the buffer exists to prevent.
#: Rounding the *rate* up first, with a floor of one unit a day, means every
#: live colorway is planned against at least three: one to sell, one behind
#: it, and one spare. The par then steps in twos, which is also how a shelf
#: reads — a pair on the peg or nothing.
#:
#: The daily floor is what makes "sold nothing yet" ask for three rather than
#: one, and that is deliberate: 2026 is year one for colorway data, and a
#: colour nobody has bought may simply never have been on the table.
DEMAND_PAR_DAILY_FLOOR = 1
DEMAND_PAR_MULTIPLE = 2
DEMAND_PAR_FLOOR = 1


@dataclass
class DemandPar:
    """A par derived from this season's sales, offered beside the stored one.

    **This is the checkbox, not a new par.** Nothing here writes
    `FinishedProduct.par`; the stored number stays the deliberate human
    decision it always was, and this is a second reading of the same
    shelf that the planner and `private/production-needed/` can be asked to
    use instead. Untick it and the old arithmetic is exactly what it was.

    Why it exists: the pages rank on sales *pooled by colorway* and then
    filter on a par that is flat across the catalogue, and the two do not
    agree about what matters. A best seller sitting one above par 8 is not
    short, so it never reaches the list it would top; a colorway that sold
    three all season is short by the same rule and does. A par that moves
    with what sold is what makes the ranking and the membership say the same
    thing, which is the incoherence this is trying on for size.

    The formula is `max(ceil(units per faire day), 1) × 2 + 1`, per finished
    product rather than pooled — par is per product, and it is the product's
    own shelf that goes empty. **The rate is rounded up before it is
    doubled**, so the floor is three rather than two; the argument is on
    `DEMAND_PAR_DAILY_FLOOR`, and it is about what two on a peg survives.

    **A stored par of zero derives a par of zero.** Zero is the switch that
    takes a product out of production planning — "we aren't making it to order
    now" — and a second reading of the same shelf does not get to overrule
    that. The floor applies to what is being made. The denominator is faire days with sales recorded,
    so a weekend not yet imported does not drag the rate down, and a day the
    faire did not open (`FaireDay.traded`) does not count either.

    `available` is false before the first faire day has sales, and then the
    stored par is used and the page says so. A rate over zero days is not a
    rate, and a sheet that silently fell back to the other number would be a
    filter working invisibly.

    **Two things the stored-par arithmetic does are switched off here.** The
    ordering is shortage from this par and nothing else — the sales are
    already in the number, so ranking on them a second time would count
    them twice, and "empty shelf first" is what the floor of one is for.
    And the Sunday-night stockout bonus is not added: a sell-out is a sales
    event and this par is built from sales. (The bonus itself is on the
    chopping block — the user's words, 2026-09-22: it made sense in our
    heads. It stays in the stored-par path until somebody removes it.)
    """

    days: int
    sold: dict
    label: str

    @property
    def available(self) -> bool:
        return self.days > 0

    def target(self, product) -> int:
        """The par this product is planned against."""
        if not self.available:
            return product.par or 0
        # **A stored par of zero is a decision, and this has to honour it.**
        # Zero means "not making this to order now" — it is the filter that
        # takes a product out of production planning everywhere else in the
        # app — so a par derived from sales must read zero as zero rather than
        # as an absence to fill in. The floor below is about how thin a shelf
        # may get for something being made; it is not a reason to start making
        # something that was taken off the list.
        #
        # Measured when this was wrong: 83 of the 100 shortages on the
        # sales-par list were par-zero rows, all of them Infinity and Triangle
        # Fringe colorways created as catalogue entries at par zero and never
        # counted. The real list underneath was 17.
        if not product.par:
            return 0
        units = self.sold.get(product.pk, 0)
        # Rounded up *before* it is doubled, and never below one a day. The
        # other order of operations bottoms out at two, which is one sale from
        # an empty peg — see the constants above.
        rate = max(ceil(units / self.days), DEMAND_PAR_DAILY_FLOOR)
        return rate * DEMAND_PAR_MULTIPLE + DEMAND_PAR_FLOOR


def demand_par():
    """`DemandPar` over the running season — the same range every other
    "what sold" figure here reads, from the same sale lines.

    Days are counted rather than taken from the calendar: a traded faire day
    inside the range on which at least one line was recorded. That is the
    honest denominator when the last weekend's export has not landed yet —
    dividing by days with no data would read them as days nothing sold.
    """
    rng = slowsellers.season_range({})
    faire_days = FaireDay.objects.filter(traded=True)
    if rng.start:
        faire_days = faire_days.filter(date__gte=rng.start)
    if rng.end:
        faire_days = faire_days.filter(date__lte=rng.end)
    traded = set(faire_days.values_list("date", flat=True))
    days = len(traded & slowsellers.days_with_sales(rng))
    return DemandPar(days=days, sold=slowsellers.sold_units(rng),
                     label=rng.label)


def _pks(things):
    """`{pk, ...}` from model instances or ids, whichever the caller has."""
    return {thing if isinstance(thing, int) else thing.pk
            for thing in (things or [])}


def blocked_reasons(product, without_blanks=None, without_dyes=None):
    """Why this colorway can't be dyed today, in words, or `[]`.

    **The shelf the app cannot see.** Undyed stock is an opening balance that
    nothing recounts on its own, and a dye's `in_stock` flag is set in the
    admin and nowhere else, so neither number is good enough to plan against —
    a sheet filtered on either would be silently dropping colorways on a
    belief nobody checked. What is reliable is the person standing in the dye
    room, so this takes what she has just said and nothing else.

    Said per session and stored nowhere. A remembered "out of Fuchsia" is a
    step that has to be remembered to be *cleared*, and the failure mode is a
    colour that quietly stops being suggested for a month with nothing
    anywhere saying why. Re-ticking two boxes next week is cheaper than that.
    """
    out_blanks = _pks(without_blanks)
    out_dyes = _pks(without_dyes)
    if not out_blanks and not out_dyes:
        return []

    reasons = []
    if product.raw_product_id in out_blanks:
        reasons.append(f"no {product.raw_product.name}")
    if out_dyes and product.recipe_id:
        # Prefetched by `candidates`; one query per row otherwise, which is
        # why the picked list resolves its own in a single pass instead.
        reasons.extend(
            f"out of {rd.dye.name}"
            for rd in product.recipe.recipe_dyes.all()
            if rd.dye_id in out_dyes
        )
    return reasons


def _needy(category, oven, stockout, demand_par):
    """The population every shortage question is asked of, in one place.

    `dyeable()` is the whole of "what a bath can make": no undyed passthrough
    (ordered, not dyed — without that the sheet put "4 × " with no colorway on
    it), no retired colorway (a retired *product* dropped out and a retired
    *recipe* never did, so a dye room was sent to make a colour somebody had
    decided to stop making), and no fancy veil (dyed, but a shortage of one is
    not answered by dyeing).

    Extracted because `covered_by_claims` has to ask about **the rows
    `candidates` drops**, and a second spelling of the population is how the
    two come to disagree about what was dropped — which is the failure this
    whole area already exists to fix, one page further on.

    The `par`/stockout prefilter is loose on purpose: in-flight baths only
    ever make a product less needy, so this is a superset and the Python pass
    in `annotate_flight` decides. It is skipped entirely for a demand par,
    whose target is per product and computed there.
    """
    qs = (
        FinishedProduct.objects.dyeable()
        .select_related("raw_product", "raw_product__category", "recipe")
        # The dye plan walks every recipe on the sheet; without this it is a
        # query per bath.
        .prefetch_related("recipe__recipe_dyes__dye__brand")
    )
    if demand_par is None:
        qs = qs.filter(
            Q(par__gt=0, number_on_hand__lt=F("par")) | Q(pk__in=stockout)
        )
    else:
        # Exact rather than an optimisation: a derived par honours a stored
        # zero (`DemandPar.target`), so a par-zero product can never come back
        # short and there is nothing for the Python pass to decide. Without
        # this the sales-par path walks the whole catalogue to discard 84 rows.
        qs = qs.filter(par__gt=0)
    if oven is not None:
        # The microwave and the oven are two sessions, never one sheet.
        # `None` is the reporting case, which wants both.
        qs = qs.filter(oven_dyed=oven)
    if category is not None:
        qs = qs.filter(raw_product__category=category)
    return qs


def covered_by_claims(category=None, oven=None, demand_par=None):
    """Products that would be short, but for paper already asking for them.

    **The rows `candidates()` drops, and the reason a colorway goes quiet.**
    A sheet that covers a shortage in full takes the row off the list
    entirely — which is right, because there is nothing left to plan — but
    from the page it is indistinguishable from a product that is fine. That
    is the same confusion the in-flight badge was added to prevent, in the
    one case the badge cannot reach: a row that isn't there carries no badge.

    Same population and same arithmetic as `candidates`, from `_needy` and
    `annotate_flight`, so what this names is exactly what that dropped.

    `include_overshoot` has no counterpart here on purpose. A row whose net
    shortage is a bath's worth of rounding is still *listed* by the page this
    serves, so it is not quiet and does not belong in a list of things that
    vanished.
    """
    stockout = stockout_baths()
    if demand_par is not None and not demand_par.available:
        demand_par = None

    covered = []
    for product in annotate_flight(
        _needy(category, oven, stockout, demand_par),
        stockout=stockout,
        demand_par=demand_par,
    ):
        if not product.in_flight or product.net_shortage:
            continue
        # Short *before* the paper is what makes this a row that vanished.
        # Without it a product sitting comfortably above par with a bath
        # still open on a sheet would be reported as "covered", which says
        # nothing happened to the list.
        target = product.target_par + product.stockout_bonus
        if target - product.number_on_hand > 0:
            covered.append(product)
    return covered


@dataclass
class Claim:
    """One product's outstanding baths on one sheet.

    **A claim is already a record, which is the answer to "should a claim
    write an `InventoryLog` row?" — it doesn't need to.** The row on the sheet
    carries the product, the units, the sheet and the day it was planned, and
    it is never edited: it ends up accepted, cancelled or overdue. What was
    missing was a page *reading* it, not a write. Which also means every sheet
    printed before this existed is described by it, rather than the app
    starting to tell the truth from the next run onwards.

    The reason it must **not** be an `InventoryLog` row is what those rows are
    for: the log is the account of the number, every row a movement that sums
    into `number_on_hand` (`ledger.move` writes the two together, and nothing
    may write one without the other). A claim has moved no finished stock — a
    bath can still be cancelled, or come out short. Putting it in the ledger
    would print barcode labels for scarves that do not exist
    (`labels.produced_since`), offer *take it back* for a bath that never
    happened (`private/produced-since/`), and count a planned bath as a
    duplicate of a card entry (`private/cards/`). Every reader of that table
    would need to learn to subtract it.
    """

    product: object
    run: object
    baths: int
    units: int


def claim_rows(products):
    """Every outstanding claim on these products, newest sheet first.

    The rows `in_flight()` totals, kept whole rather than summed, because a
    page saying a bath is already marked for production has to say *where* to
    go and look at it — and, on the recipe page, when it was asked for.

    Same filter as `in_flight()` deliberately — pending rows on `counted`
    sheets — so the units a page prints and the sheet it names can never
    describe different claims. `ClaimsAgreeWithInFlightTests` pins it.
    """
    products = list(products)
    if not products:
        return []
    by_pk = {p.pk: p for p in products}
    rows = (
        ProductionRunRow.objects.filter(
            finished_product__in=products,
            applied_log__isnull=True,
            cancelled_at__isnull=True,
            run__in=counted_runs().values("pk"),
        )
        .select_related("run")
    )
    # One `Claim` per (product, sheet): two baths of a colorway are two rows
    # on one sheet, and "sheet #21, sheet #21" is a link printed twice.
    grouped = {}
    for row in rows:
        key = (row.finished_product_id, row.run_id)
        claim = grouped.get(key)
        if claim is None:
            grouped[key] = Claim(
                product=by_pk[row.finished_product_id], run=row.run,
                baths=1, units=row.quantity or 0,
            )
        else:
            claim.baths += 1
            claim.units += row.quantity or 0
    return sorted(
        grouped.values(),
        key=lambda c: (c.run.created_at, c.product.name),
        reverse=True,
    )


def open_claims(products):
    """`{finished_product_id: [run, ...]}` — which live sheet is asking.

    `claim_rows` with the units dropped, for the pages that only need to name
    the sheet. One definition of the query, two shapes of the answer.
    """
    claims = {}
    for claim in claim_rows(products):
        claims.setdefault(claim.product.pk, []).append(claim.run)
    return claims


def candidates(category=None, include_overshoot=False, order=ORDER_SOLD,
               oven=False, without_blanks=None, without_dyes=None,
               demand_par=None):
    """Products worth putting on a sheet, most urgent first.

    `demand_par` is a `DemandPar` when the caller ticked *par from sales*:
    every product is then measured against `max(ceil(a day's sales), 1) × 2 + 1`
    instead of its stored par, and the SQL prefilter below is skipped
    because the target is per product and computed in Python. `None` — or a
    `DemandPar` with no days to divide by — is the stored par, unchanged.

    The default is `FinishedProduct.behind_a_bath` — products where a whole
    bath still lands at or under par, which is where a session's work is
    fully used. `include_overshoot` widens it to everything below par,
    including the ones a bath would take past it.

    That second group is not sloppiness. A bath is a fixed size, so overshoot
    is rounding rather than overproduction, and those shortages get rounded
    away anyway the next time the recipe is dyed. Printing them is worth it
    when the session has capacity to spare; leaving them off is worth it when
    it doesn't. Hence a checkbox rather than a judgement baked in here.

    `oven=None` means both, which is what a page that *reports* shortages
    wants — `private/production-needed/` covers the whole catalogue and badges
    the oven rows rather than hiding them.

    **`oven` partitions rather than filters, and it defaults to the pot.**
    An oven colorway cannot be made at a sink and a stovetop one cannot be
    made in the oven, so these are two disjoint populations and every sheet
    is one or the other. Defaulting to `False` is what makes the existing
    callers keep meaning what they meant: the dye-room sheet asks for the
    dye room's work, and an oven colorway appearing on it would send
    somebody to a sink to make a thing that is not made there — the failure
    `made_in_a_dye_bath` already exists to stop, one technique further in.

    **`without_blanks` and `without_dyes` annotate; they never filter.** They
    are what somebody has just said she cannot dye today — an empty yarn box,
    a dye jar with nothing in it — and each product comes back carrying
    `blocked_by`, a list of reasons in words. `suggest()` is what acts on it,
    because this function also feeds the page that *reports* shortages and a
    colorway that cannot be dyed this afternoon is still short.
    """
    # A Sunday-night zero adds a bath, and it has to widen the prefilter as
    # well as the arithmetic. A product sitting *at* par that still sold out
    # would fail `number_on_hand < par` and never reach the Python pass below
    # — which is the case the rule exists for, since par being adequate on
    # paper is exactly what a stockout disproves.
    stockout = stockout_baths()

    # A demand par with nothing to divide by is the stored par, and it is
    # dropped here so every branch below asks one question of one object.
    if demand_par is not None and not demand_par.available:
        demand_par = None

    qs = _needy(category, oven, stockout, demand_par)

    if not include_overshoot and demand_par is None:
        # The SQL form of behind_a_bath, matching the production page's own
        # expression — Greatest keeps a bath size of 0 from making it true
        # for everything, the same `or 1` the model property uses.
        qs = qs.filter(
            Q(par__gte=F("number_on_hand") + Greatest(
                F("raw_product__number_per_dye_bath"), Value(1)
            )) | Q(pk__in=stockout)
        )

    # Everything above is a prefilter, and it is deliberately loose: baths
    # already in flight only ever make a product *less* needy, so the SQL
    # result is a superset of the answer and the Python pass below narrows
    # it. Doing it here rather than in a Subquery keeps one copy of the
    # arithmetic, which the sort and `plan_baths` both read.
    wanted = []
    for product in annotate_flight(qs, stockout=stockout,
                                   demand_par=demand_par):
        if not product.net_shortage:
            continue
        if not include_overshoot and not product.behind_a_bath_net:
            continue
        wanted.append(product)

    # What this *blank* sold, beside what the colorway sold. The ordering is
    # pooled and must stay pooled — a bath is planned in colorway units — but
    # pooling is also blind in one specific way: a hot colorway drags every
    # blank it is dyed on up the list, including one with a full shelf and no
    # sales of its own. Measured on the live catalogue that is one or two rows
    # in the first twenty, which is too few for a rule and too many to leave
    # unsaid, so the number rides on the row and a person strikes it.
    #
    # Deliberately not a filter. It sold none *here* over a handful of days,
    # and absence over a short window supports almost no inference — the
    # skip rule this replaced could not survive a confidence bound on its own
    # numbers. Two counted facts on the row can't be wrong that way.
    per_blank = sold_per_blank()
    # Resolved once rather than per product: these arrive as querysets from
    # the form, and asking each row to re-read them is a pass over the same
    # two selects a few hundred times for one set of ids.
    out_blanks, out_dyes = _pks(without_blanks), _pks(without_dyes)
    for product in wanted:
        product.sold_here = per_blank.get(product.pk, 0)
        # **Annotated, never filtered out here.** `private/production-needed/`
        # reads this same function and reports the whole catalogue; dropping a
        # row at this level would take a colorway off the page that reports
        # shortages as well as off the sheet that plans them, and a shortage
        # that stops being printed is one nobody can act on later. `suggest()`
        # is where the skipping happens, and it says how many it skipped.
        product.blocked_by = blocked_reasons(product, out_blanks, out_dyes)

    if demand_par is not None:
        # The sales are already in the par, so the order is how far below it
        # a product is and nothing else. `order` is ignored on purpose: sales
        # first would count them twice, and `_urgency`'s empty-shelf-first
        # would put a colorway that sold nothing (par 1, none on hand) ahead
        # of the best seller twelve short. The floor of one is what keeps
        # the empty shelf on the list at all.
        return sorted(wanted, key=lambda p: (-p.net_shortage, p.name))

    if order == ORDER_PAR:
        return sorted(wanted, key=_urgency)

    # Sales first, urgency as the tie-break — so an empty shelf still leads
    # among colorways that sell alike, and a colour nobody buys does not jump
    # the queue for being emptier. Pooled by recipe, because that is the unit
    # a bath is planned in and the unit the other page reports.
    sold = slowsellers.sold_by_recipe(slowsellers.season_range({}))
    return sorted(
        wanted,
        key=lambda p: (-sold.get(p.recipe_id, 0),) + _urgency(p),
    )


def annotate_flight(products, claimed=None, stockout=None, demand_par=None):
    """Set `in_flight`, `net_shortage` and `behind_a_bath_net` on each product.

    Also `target_par`: the par the shortage was measured against, which is
    the stored one unless `demand_par` is a `DemandPar` with days to divide
    by. The row prints that rather than `par` so what it shows is what it
    was judged on.

    **One definition of "what is still short", for every page that asks.**
    `private/production-needed/` used to carry its own SQL version — a plain
    `par - number_on_hand` that knew nothing about printed paper — so printing
    a sheet that covered a colorway entirely left that page still reporting the
    full shortage. The planner was right and the page somebody reads was
    wrong, which is the worst way round.

    Two answers to one question is the failure this codebase keeps naming
    (`sold_by_recipe` for what sold, `refill_plan` for what to carry). This is
    the same fix: the page reports exactly what the sheet would ask for.

    `claimed` and `stockout` are passed in when the caller already has them,
    so a page rendering one row doesn't re-query every live sheet or every
    close.
    """
    if claimed is None:
        claimed = in_flight()
    if stockout is None:
        stockout = stockout_baths()

    annotated = []
    for product in products:
        product.in_flight = claimed.get(product.pk, 0)
        # **The target, not par.** A Sunday-night zero adds one bath on top of
        # whatever par asks for, and it is added to the *target* rather than
        # written into `par` — par stays a deliberate human decision, and a
        # number that moved on its own is the failure this app keeps naming.
        #
        # Adding exactly `bath_size` adds exactly one bath, always:
        # `ceil((n + b) / b) == ceil(n / b) + 1` for any n. So the rule is
        # denominated in the only unit that exists, with nothing to round.
        from_sales = demand_par is not None and demand_par.available
        # The stored par unless the caller asked for the one from sales. The
        # stockout bonus rides on the stored par only: a sell-out is a sales
        # event, and a par built from sales has already counted it.
        product.stockout_bonus = 0 if from_sales else stockout.get(product.pk, 0)
        product.target_par = (
            demand_par.target(product) if from_sales else (product.par or 0)
        )
        target = product.target_par + product.stockout_bonus
        product.net_shortage = max(
            target - product.number_on_hand - product.in_flight, 0
        )
        # `behind_a_bath` asked of what is left after the paper: a whole bath
        # still lands at or under par. The model property answers the same
        # question about the shelf alone, which double-counts a printed sheet.
        expected = product.number_on_hand + product.in_flight
        product.behind_a_bath_net = target >= expected + product.bath_size
        annotated.append(product)
    return annotated


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


def stockout_baths():
    """`{finished_product_id: units}` a Sunday-night zero is asking for.

    **A `CloseRunRow` answered at zero is the record that a product was sold
    out on a Sunday night**, and this turns that into one more bath on the
    next sheet. It is the whole of the demand response, and it is deliberately
    the only part of this module that reacts to what sold.

    Three things make it safe where a rate-based target was not:

    - **It is denominated in baths.** A bath is atomic — there is no half
      bath, because the labour makes one not worth doing — so any rule that
      has to be *rounded* into baths is expressing a precision that has
      nowhere to land. Measured on the live catalogue, a weekend of demand
      crosses a bath boundary against par for four products out of 333, and
      all four have no par set at all. One bath is the smallest thing this
      question can be asked in, so it is what the rule is written in.
    - **It fires on an observed event, not on inferred silence.** A physical
      count of zero, on a named night, is a measurement; `n = 1` is fine
      because nothing is estimating a rate. The mirror-image rule — skip a
      bath because a product *only sold one* — dies on the same data: not one
      of 76 such products survives a 95% bound on its own sales figure.
    - **It cannot run away.** Only the latest close proposes, so each Sunday
      supersedes the last, and `in_flight()` nets off a bath already claimed
      by printed paper. The pressure is a steady +1 while the stockout
      persists and it stops the week it does not.

    **Answered rows only.** A pending row is "nobody looked", never a zero —
    the close is routinely worked in passes and left part-done, and reading
    an unanswered row as a stockout would manufacture baths out of the pile
    somebody did not get to.

    Par is untouched, and so is the close: `expected_products()` gates on
    `display_slots` rather than par, so nothing here changes which rows come
    up to be counted. The two are on separate circuits on purpose. (Par zero
    *and* nothing on hand is the one exception, and it cannot reach this
    function: a row that never appears never comes back zero, and it was
    never going to — see `closing.expected_products`.)
    """
    run = CloseRun.objects.order_by("-day").first()
    if run is None:
        return {}
    rows = CloseRunRow.objects.filter(run=run, counted=0).select_related(
        "finished_product__raw_product"
    )
    return {row.finished_product_id: row.finished_product.bath_size
            for row in rows}


def sold_per_blank():
    """`{finished_product_id: units}` this season, per blank rather than pooled.

    The companion to `slowsellers.sold_by_recipe`, which is what the *ordering*
    reads. Both are needed and they answer different questions: a bath is
    planned in colorway units, so pooling is right for deciding which colour
    gets a pot — and pooling is blind to a colorway's individual blanks, so a
    hot colour drags a fully-stocked blank onto the sheet with it.

    Printed on the row rather than acted on. Same range and same function as
    every other "what sold" figure in the app, because two answers to that
    question is how the page that ranks on it comes to disagree with the page
    that reports it.
    """
    return slowsellers.sold_units(slowsellers.season_range({}))


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


@dataclass
class Suggestion:
    """What the planner proposes, and what it left out on purpose.

    `skipped` is the load-bearing half. Ticking "I am out of 608 Pink" drops
    every colorway that needs it and pulls others up to fill the sheet, so
    without saying which ones went the list just quietly *changes* — and a
    filter you cannot see the effect of is the same failure as advice you
    cannot inspect. It carries the products themselves, each with its
    `blocked_by` reasons, so the page can name them and their sales.
    """

    baths: list
    skipped: list


def suggest(limit, category=None, include_overshoot=False, order=ORDER_SOLD,
            oven=False, without_blanks=None, without_dyes=None,
            demand_par=None):
    """`plan_baths`, plus what the day's shortages took off the list.

    Only shortages that would have *reached* the sheet are reported as
    skipped: the walk stops at the same recipe the limit stops at, so asking
    for twenty baths cannot come back saying it declined to suggest forty
    colorways nobody was going to see anyway.
    """
    by_recipe = {}
    for product in candidates(category, include_overshoot, order, oven,
                              without_blanks, without_dyes, demand_par):
        # `net_shortage`, not `shortage`: what is already out being dyed has
        # been taken off, so a sheet asks for the baths still missing rather
        # than reprinting the ones on last week's paper.
        needed = ceil(product.net_shortage / product.bath_size)
        for _ in range(needed):
            by_recipe.setdefault(product.recipe_id, []).append(
                Bath(product=product, quantity=product.bath_size)
            )

    baths, skipped = [], {}
    for recipe_baths in by_recipe.values():
        for bath in recipe_baths:
            # A blocked colorway takes no place in the limit — the sheet was
            # asked for twenty baths that can be dyed, not twenty minus the
            # ones there is no yarn for.
            if bath.product.blocked_by:
                skipped.setdefault(bath.product.pk, bath.product)
            else:
                baths.append(bath)
        if len(baths) >= limit:
            break
    return Suggestion(baths=baths[:limit], skipped=list(skipped.values()))


def plan_baths(limit, category=None, include_overshoot=False, order=ORDER_SOLD,
               oven=False, without_blanks=None, without_dyes=None,
               demand_par=None):
    """The next `limit` baths, grouped so consecutive rows share a dye pot.

    Baths of the same recipe sit together because that is how the work is
    actually cheaper: one mix, one pot, one temperature, several loads. The
    order *between* recipes is urgency; the order within one is just the
    products that need it.

    A recipe can be cut in half by the limit, and that is fine — the sheet
    was asked for a number of baths and it delivers exactly that number.
    """
    return suggest(limit, category, include_overshoot, order, oven,
                   without_blanks, without_dyes, demand_par).baths


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


def top_ups(current, gap, category=None, oven=True,
            without_blanks=None, without_dyes=None):
    """Oven colorways worth adding when the shortages don't fill the box.

    `current` is the list as it stands (products already on it), `gap` is how
    many trays are still empty. Returns `[(product, units_sold)]`, best
    sellers first — nothing below par, because anything short is already on
    the list by the time there is a gap at all.

    **This is the one place in the app that suggests making something that
    is not short**, and it is worth being precise about why that is not the
    display-capacity mistake wearing a hat.

    The rule that matters is that *furniture must never reach production*: a
    new rack gets filled from the bags, a stored backstock figure reads empty
    and calls for dye, and nothing sold. What makes that bad is that it is
    unbounded and it is mistaken for demand. The oven is neither. It is a
    fixed box that costs one heating whether it holds four trays or fifteen,
    so the last eleven are the cheapest eleven of the year — the same
    argument `include_overshoot` already makes about a bath being a fixed
    size, at the scale of a session rather than a pot. And it is bounded
    absolutely, at `OVEN_TRAYS`, by a thing nobody can enlarge by building
    another one.

    It also runs *with* the northstar rather than against it. The goal is a
    flat year instead of a fast week — as much of a season pre-dyed as
    possible — and an oven run in February that comes out full is exactly
    that. What would be the regression is topping up with whatever sold last
    weekend, which is why the ranking is season sales pooled by colorway and
    not recent movement.

    Three things keep it advice rather than a decision:

    - **Nothing is added.** These are offered with a `+` beside them and the
      list only ever changes because somebody clicked one. A suggestion that
      auto-filled the sheet would be the app deciding to make ninety skeins.
    - **The sales figure is printed beside each one**, so the basis of the
      ranking can be checked by looking rather than trusted. Advice you
      cannot inspect is a decision in disguise.
    - **The panel disappears when the box is full**, so it is only ever
      answering a question the page is already asking.
    """
    if gap <= 0:
        return []

    on_list = {product.pk for product in current}
    qs = (
        FinishedProduct.objects.dyeable().filter(oven_dyed=oven)
        .select_related("raw_product", "recipe")
        .exclude(pk__in=on_list)
    )
    if category is not None:
        qs = qs.filter(raw_product__category=category)

    # Filtered rather than flagged, unlike the list above it. Nothing here is
    # short, so a topped-up tray is a free choice among colorways — and
    # offering one that cannot be dyed today is offering nothing at all.
    out_blanks = _pks(without_blanks)
    out_dyes = _pks(without_dyes)
    if out_blanks:
        qs = qs.exclude(raw_product_id__in=out_blanks)
    if out_dyes:
        qs = qs.exclude(recipe__recipe_dyes__dye_id__in=out_dyes)

    sold = slowsellers.sold_by_recipe(slowsellers.season_range({}))
    ranked = sorted(
        qs,
        # Sales pooled by colorway, then the name — deliberately *not*
        # `_urgency` as the tie-break. Urgency reads a shortage, and by
        # definition nothing here has one; sorting on how far above par
        # something is would rank the topping-up on par, which is the number
        # this app does not trust.
        key=lambda product: (-sold.get(product.recipe_id, 0), product.name),
    )
    # A few more than the gap, so there is something to choose between
    # rather than a list to work down. Each `+` is one tray, because a bath
    # is the unit and any other number here would be invented.
    return [(product, sold.get(product.recipe_id, 0)) for product in ranked[:gap + 5]]


def blank_demand(rows):
    """`[(raw_product, needed, believed_on_hand), ...]` for a printed sheet.

    The blanks half of the collection list. Every raw product the sheet's
    baths consume, however many baths want it.

    **Nothing is filtered on stock.** A blank we believe is out is far more
    likely to be a number nobody has updated than an empty shelf, and leaving
    it off the list would turn a stale count into a bath that doesn't get
    dyed. The belief is printed beside the requirement so a real shortage is
    still visible, but the instruction is what to fetch.

    **The belief is the count as it stands, and this run's own claim is not
    added back into it.** That was tried: a sheet whose blanks were claimed at
    planning reads `fetch 20 · we think 0`, and printing `20` instead looks
    friendlier. It destroys the signal the number exists for. The count is
    clamped at zero, so a shelf that could not cover the sheet and a shelf
    that covered it exactly both sit at 0 — and adding the requirement back
    turns the first one into a shelf that looks full. `12 (we think 2 on
    hand)` is the case `CLAUDE.md` cites, and a version that renders it as
    `12 (we think 14)` is worse than no figure at all.

    A shortage is caught before this: `short_blanks` runs on the picker,
    before the claim, where the arithmetic is still unclamped.
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
# `OVERDUE_AFTER` itself is defined in `models`, beside the run whose
# `is_overdue` reads it, and re-exported here so `production.OVERDUE_AFTER`
# stays the name the rest of the app and the docs use.


def with_row_states(queryset=None):
    """Runs, annotated with the row counts every state question reads.

    One annotation pass rather than a property per question, because these
    are asked of lists — the picker shows several groups at once, and a
    per-run property there is a query per sheet per group.
    """
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


def claimed_units(blanks):
    """`{blank_pk: units}` held by baths that are planned but not reported.

    The raw-side twin of `in_flight`, and answered the same way — from
    `ProductionRunRow`, because that is the one claim either signal makes,
    whichever list the bath came from.

    **Already subtracted from `number_on_hand`**, which is what makes this a
    display and a reconciliation figure rather than an input to any decision:
    the count is the number to plan and order against, and this says how far
    it is from what somebody standing at the shelf would count.

    Overdue sheets are included. They stop claiming against the *planner*
    after `OVERDUE_AFTER` so a forgotten colorway is asked for again, which is
    a judgement about what to dye next. The yarn is a physical question with a
    different answer — those skeins are either dyed or still there — and it is
    settled by reporting the sheet or striking it, not by a clock.
    """
    claimed = {}
    rows = (
        ProductionRunRow.objects
        .filter(accepted_at__isnull=True, cancelled_at__isnull=True,
                finished_product__raw_product__in=blanks)
        .values_list("finished_product__raw_product_id", "quantity")
    )
    for blank_id, quantity in rows:
        claimed[blank_id] = claimed.get(blank_id, 0) + (quantity or 0)
    return claimed


def _move_blanks(rows, sign):
    """Take `rows`' blanks off the shelf (`sign` -1) or put them back (+1).

    Aggregated per blank and applied in pk order, so a run whose rows share a
    blank touches it once and two of these can never take each other's rows in
    opposite orders. Locked and re-read immediately before the write, for the
    reason spelled out in `apply_row`: `select_related` hands every row its own
    copy of the same blank, and a read-modify-write across those copies loses
    all but the last silently.
    """
    totals = {}
    for row in rows:
        blank_id = row.finished_product.raw_product_id
        totals[blank_id] = totals.get(blank_id, 0) + row.quantity
    _shift_blanks(totals, sign)


def _shift_blanks(totals, sign):
    """`{blank_pk: units}` off the shelf (`sign` -1) or back on (+1), locked, in pk order."""
    for blank_id in sorted(totals):
        raw = RawProduct.objects.select_for_update().get(pk=blank_id)
        raw.number_on_hand = max(raw.number_on_hand + sign * totals[blank_id], 0)
        raw.save(update_fields=["number_on_hand"])


@transaction.atomic
def open_rows(run, plan):
    """Put `plan` on `run` as one row per bath, and claim the blanks. Returns them.

    `plan` is `[(finished_product, quantity), ...]` in sheet order.

    **This is the only door, and that is the point.** The blanks come off the
    shelf when the run is *created*, not when the dyeing is reported, because
    a run is an intent to make something and the yarn it needs is spoken for
    from that moment. Two things depend on it and neither survives a claim
    that lands at the end:

    - **Planning happens in passes.** Half a week goes on a list, the shelf is
      read again, and the rest is planned against what is left. If the first
      list has not moved the count, the second one plans the same skeins twice.
    - **Ordering has a lead time.** A run created Monday and finished Friday
      that takes a blank under its floor has to say so on Monday — by Friday
      the window to order and have it on hand has gone. `raw_shortage` reads
      `number_on_hand`, so the claim is what makes the reorder signal fire in
      time. (It also subtracts what is already on order — see
      `RawProduct.raw_shortage` — which suppresses the signal for a blank
      whose replacement is on a van, and is the other half of the same rule:
      the signal answers "should I order", never "what is on the shelf".)

    So `number_on_hand` now means **unclaimed yarn**, not skeins on the shelf.
    The two differ by whatever is on open sheets, and the difference is real
    but bounded — the gap between planning a bath and dyeing it is never more
    than about a week. `private/raw-inventory/` prints the claimed figure
    beside the count so the person reading it can see both, and so somebody
    standing at the shelf knows what they should find there.

    Creation and release are the *same* pair of doors on purpose: this, and
    `cancel_row`. Nothing else creates a `ProductionRunRow` and nothing else
    destroys one — a third site that made rows its own way would claim nothing
    and the shelf would drift down by exactly the yarn it forgot, which is the
    silent failure this whole file is arranged against.
    """
    start = 1 + max((row.order for row in run.rows.all()), default=0)
    rows = ProductionRunRow.objects.bulk_create([
        ProductionRunRow(
            run=run, finished_product=product, order=order, quantity=quantity,
        )
        for order, (product, quantity) in enumerate(plan, start=start)
    ])
    # `bulk_create` sends no `post_save`, which is why claiming is written here
    # rather than in a signal: a signal would be silently skipped by exactly
    # the two sites that create the most rows.
    _move_blanks(rows, -1)
    return rows


def open_row(run, product, quantity=None):
    """One more bath on a run. The single-row form of `open_rows`."""
    quantity = product.bath_size if quantity is None else quantity
    return open_rows(run, [(product, quantity)])[0]


@transaction.atomic
def apply_row(row, yielded=None, fancy=0, via=""):
    """Accept one bath into inventory, once. Returns the `InventoryLog`.

    **Applying twice is the failure this guards.** The return URL is a piece
    of paper that can be scanned again, the submit button can be
    double-tapped, and somebody who remembers one more bath will re-open the
    page and submit again. All three are normal, and all three used to be how
    a bath got counted into stock twice — the same shape as the Square
    webhook and redelivered orders.

    **`yielded` is what actually came out, and only the finished side moves.**
    The blanks came off the shelf when the run was created — see `open_rows` —
    so reporting a bath adds `yielded` to the product and touches raw not at
    all. A bath consumes its blanks whatever happens in the pot: dye four
    scarves, ruin one, and there are still four gone. That is already paid
    for, which is why a short bath needs no correction here and the shortfall
    is a loss rather than a discrepancy. `None` means the full bath, which is
    what a plain tick claims.

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

    `via` is appended to the log's note when the bath was reported from
    somewhere other than the sheet's own page — see `report()`.
    """
    if row.applied_log_id is not None:
        return row.applied_log
    # The in-memory check above is the cheap one; this is the real one. Two
    # requests for the same pending row — a double-tapped submit, two people
    # holding one printed sheet — both read `applied_log_id is None` before
    # either commits, and both would move stock. Locking the row and reading
    # the column again under the lock means the second waits and then sees
    # the first's log. The caller's instance is kept, not replaced, because
    # `accept_line` has already set `finished_product` on it.
    applied_id = (
        ProductionRunRow.objects.select_for_update()
        .filter(pk=row.pk).values_list("applied_log_id", flat=True).first()
    )
    if applied_id is not None:
        row.applied_log_id = applied_id
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

    lost = row.quantity - made
    notes = f"Dye bath accepted from production sheet run {row.run_id}."
    if lost:
        notes += (
            f" {lost} of {row.quantity} did not make it —"
            f" the blanks were claimed when the run was planned either way."
        )
    if fancied:
        notes += (
            f" {fancied} of {made} finished as {target.raw_product.name}."
        )
    if via:
        notes += f" {via}"

    # Through the ledger, which locks and re-reads the row before each write:
    # two products of one recipe often share a blank, `lines_for_run` hands
    # every row its own copy of the product, and a read-modify-write across
    # those copies is how sheet #2 left a shelf of 150 reading 135 instead of
    # 130. Product then fancy product, always, so two of these can't take
    # each other's rows in opposite orders.
    #
    # The plain row's log is written even at zero — it is what `applied_log`
    # points at, and so what stops the bath being counted twice. A bath whose
    # whole output went out fancy still writes it.
    log = ledger.move(
        product, plain,
        log_type=InventoryLog.PRODUCTION,
        source=InventoryLog.SOURCE_PRODUCTION_SHEET,
        notes=notes,
    )
    if fancied:
        # A second product, so a second row — every stock movement in this app
        # is per product, the same shape the conversion page writes. It is
        # PRODUCTION rather than a conversion because nothing was converted:
        # this scarf was never plain.
        ledger.move(
            target, fancied,
            log_type=InventoryLog.PRODUCTION,
            source=InventoryLog.SOURCE_PRODUCTION_SHEET,
            notes=notes,
            # Against the *plain* blank: one pot, and `blanks_consumed` reads
            # a log whose blank is not its product's as the sibling entry.
            raw_product=raw,
        )

    # **The money is frozen here, at the one event that is the payment
    # handoff.** Accepting a bath is what enters the stock and what a bill for
    # the dyeing is drawn from, so this row has to keep saying what the
    # session was worth when it happened — a supplier increase or a reprice
    # next spring must not rewrite what a past week earned. Same bargain
    # `quantity` already makes with the printed sheet, one step further: the
    # paper says how big the bath was, and this says what it cost and what it
    # made.
    #
    # Cost is per unit and retail is a total, and the asymmetry is real: a
    # bath eats `quantity` of one blank at one cost however its output was
    # finished, while a split bath's output sells at two prices, so no single
    # unit price describes it. A lost bath still consumed its blanks, which
    # is why the cost basis is `quantity` and not `made`.
    row.unit_blank_cost = raw.blank_cost
    row.output_retail = (
        Decimal(plain) * Decimal(product.price or 0)
        + Decimal(fancied) * Decimal((target.price if fancied else None) or 0)
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
        "unit_blank_cost", "output_retail",
    ])
    return log


@dataclass
class Report:
    """What `report()` did with the units it was handed."""
    rows: list            # sheet rows accepted on the way, in sheet order
    direct: int           # units booked with no row to accept
    logs: list            # every `InventoryLog` written, rows first

    @property
    def units(self) -> int:
        return sum(row.quantity for row in self.rows) + self.direct

    @property
    def sheets(self):
        """The runs whose rows were accepted, deduplicated, in order."""
        seen = []
        for row in self.rows:
            if row.run not in seen:
                seen.append(row.run)
        return seen


@transaction.atomic
def report(product, units, *, source, notes):
    """Book `units` of `product` into stock from outside a sheet. Returns a `Report`.

    Two pages record a bath after the fact rather than off a sheet: the
    recipe page's production form and the *Bagged a bath* button on
    `private/production-needed/`. Both used to take the blanks off the shelf
    and add the output, which is right for a bath nobody planned and wrong
    for one that is on a sheet — its blanks came off when the run was made
    (`open_rows`), so recording it here took them twice, and the row stayed
    open, still subtracting from the planner, for a bath that was already in
    a bag. Nothing said so; the shelf simply read low and the sheet read
    unfinished.

    So a report **honours the claim first.** Pending rows for this product
    are accepted in sheet order, whole rows only, for as long as the units
    cover them, and only what is left over is booked directly. A person who
    says "three baths of this are bagged" when two are on a sheet has ticked
    the sheet's two and recorded one more, which is what happened. A report
    smaller than the next row leaves that row alone — a short bath is the
    sheet's own page's job, because it is the one that can say how short.

    The remainder is the old path, and it is still not a run: nothing was
    planned, so there is nothing to claim and nothing to accept. The blanks
    come off now, locked in pk order like `open_rows` takes them, and the
    output goes on through the ledger.

    **Lock order is finished product, then blank**, and it is the order
    everything else keeps: `apply_row` takes the product (and its fancy
    twin) and never the blank; `retract` takes the product and then the
    blank. A second function taking the same pair the other way is a
    deadlock the first time two of them land together.
    """
    units = max(int(units), 0)
    pending = list(
        ProductionRunRow.objects.select_for_update(of=("self",))
        .filter(
            finished_product=product,
            accepted_at__isnull=True,
            cancelled_at__isnull=True,
            applied_log__isnull=True,
        )
        .select_related("run")
        .order_by("run__created_at", "run_id", "order")
    )
    rows, logs = [], []
    for row in pending:
        if row.quantity > units:
            break
        row.finished_product = product
        logs.append(apply_row(row, via=notes))
        rows.append(row)
        units -= row.quantity

    if units:
        _shift_blanks({product.raw_product_id: units}, -1)
        logs.append(ledger.move(
            product, units,
            log_type=InventoryLog.PRODUCTION,
            source=source,
            notes=notes,
        ))
    return Report(rows=rows, direct=units, logs=logs)


@transaction.atomic
def cancel_row(row):
    """Call a bath off: the blanks go back on the shelf and it is asked for again.

    **Cancelled and binned are different, and both are needed.** Cancelled
    means the bath never ran, so the yarn it claimed at planning was never
    wet and is still there — this is the release half of `open_rows`, and the
    only one. A bath that ran and lost the whole lot is the other thing
    entirely: those blanks really are gone, so it goes through `apply_row` at
    a yield of zero and the claim stands.

    The claim on the *planner* is released at the same moment, so the colorway
    comes back on the next sheet. One row, one bath, one claim — released on
    both sides together or the two answers drift apart.

    A row that has already moved stock is left alone. Taking that back is an
    inventory adjustment with a reason attached — `producedsince.retract` —
    which is the same refusal the crew's tick boxes make.
    """
    if row.applied_log_id is not None or row.cancelled_at is not None:
        return False
    row.cancelled_at = timezone.now()
    row.save(update_fields=["cancelled_at"])
    _move_blanks([row], +1)
    return True


@transaction.atomic
def uncancel_row(row):
    """Put a called-off bath back to pending, and claim its blanks again.

    **This exists for the same reason the Sunday close has an Undo button.**
    Cancelling is reachable with nothing but the code printed on the paper,
    which means it is reachable by somebody with no account and no way to
    reach an admin screen — and a mis-tap they cannot fix is a mistake they
    have to go and tell somebody about. That cost is exactly the pressure
    that gets one left unmentioned instead.

    It is free to offer here in a way undoing an *acceptance* is not: a
    cancelled row produced nothing, so putting it back is a row going from one
    unreported state to another. Nothing is erased and nothing is compensated
    — the blanks are simply claimed again, exactly as they were when the run
    was created, because an un-cancelled bath is a bath that is going to
    happen after all.
    """
    if row.applied_log_id is not None or row.cancelled_at is None:
        return False
    row.cancelled_at = None
    row.save(update_fields=["cancelled_at"])
    _move_blanks([row], -1)
    return True


# ---------------------------------------------------------------------------
# Lines: identical baths, reported once
# ---------------------------------------------------------------------------


@dataclass
class Line:
    """Every bath of one colorway on one blank, as a single thing to report.

    **Three baths of Artisan Cabernet is not three groups of five, it is one
    group of fifteen.** The sheet used to print a row per bath with a tick
    box each, because a bath is what somebody physically does — which is
    true, and is why the *work* sheet still reads that way. But the reporting
    sheet asks a different question. Three boxes for one colorway is three
    marks for one answer, three chances to tick the wrong line, and three
    lines a photograph has to resolve where the crew are holding one pile of
    fifteen scarves.

    **The blank is half the identity.** Artisan Peacock and Noble Peacock are
    two different things that came out of two different pots, so they stay
    two lines. That is exactly `finished_product` — blank × colorway — which
    is the axis the whole catalogue is organised on, so the grouping key
    needed no new concept.

    Where this does *not* apply:

    - **The work sheet.** Its boxes hold a bath at a point in a one-to-three
      day process, and three baths of Cabernet really are three pots that dry
      separately. Grouping there would ask one row of boxes to say that two
      are dry and one is still wet.
    - **The rows in the database.** A bath stays a row: `quantity` is frozen
      per bath because the paper said `× 5`, striking releases one bath's
      claim rather than a colorway's, and `applied_log` stops one bath being
      counted twice. Grouping is how they are asked about, not how they are
      kept.
    """

    product: object
    rows: list
    number: int

    @property
    def key(self):
        """What a tick posts, and what a scan resolves to.

        The first row's pk, rather than a new identifier: `?done=` already
        carries row pks from the photo path, so the URL shape and the
        checkbox name are unchanged and a stale link degrades the same way.
        """
        return self.rows[0].pk

    @property
    def quantity(self) -> int:
        """What the paper asked for — every bath, whatever state it is in.

        The PDF reads this, and reads nothing else about state: a reprint
        mid-session has to be the same document as the first print.
        """
        return sum(r.quantity for r in self.rows)

    @property
    def pending(self):
        return [r for r in self.rows if r.is_pending]

    @property
    def expected(self) -> int:
        """What is still open plus what has already been banked.

        The live figure, for the crew's page. It differs from `quantity` once
        a bath has been struck — the paper still says fifteen and the screen
        says ten, which is the paper being frozen rather than either being
        wrong.
        """
        return sum(r.quantity for r in self.rows if not r.is_cancelled)

    @property
    def open_quantity(self) -> int:
        return sum(r.quantity for r in self.pending)

    @property
    def yielded(self) -> int:
        return sum(r.yielded for r in self.rows if r.is_accepted)

    @property
    def fancy_yield(self) -> int:
        return sum(r.fancy_yield for r in self.rows if r.is_accepted)

    @property
    def loss(self) -> int:
        return sum(r.loss for r in self.rows if r.is_accepted)

    @property
    def baths(self) -> int:
        return len(self.rows)

    @property
    def is_accepted(self) -> bool:
        return bool(self.rows) and all(r.is_accepted for r in self.rows)

    @property
    def is_cancelled(self) -> bool:
        return bool(self.rows) and all(r.is_cancelled for r in self.rows)

    @property
    def is_pending(self) -> bool:
        return bool(self.pending)

    @property
    def is_part_done(self) -> bool:
        """Some baths settled and some not — a line that is neither state.

        Reachable by striking one bath of three, or by a submit that landed
        halfway. Said out loud on the page rather than rounded to one of the
        two clean answers, because rounding it either way is the page
        claiming something nobody reported.
        """
        return self.is_pending and any(
            r.is_accepted or r.is_cancelled for r in self.rows
        )

    @property
    def fancy_target(self):
        return self.rows[0].fancy_target


def lines_for(rows):
    """`rows` folded to one line per colorway, in the order they first appear.

    First appearance rather than a re-sort, because `plan_baths` already
    clumps a recipe's baths together — one mix and one pot serve several
    loads, which is what makes a session cheaper — and the order between
    recipes is the urgency the planner chose. Re-sorting here would throw
    that away to achieve the grouping it already has.
    """
    order = []
    grouped = {}
    for row in rows:
        pk = row.finished_product_id
        if pk not in grouped:
            grouped[pk] = []
            order.append(pk)
        grouped[pk].append(row)
    return [
        Line(product=grouped[pk][0].finished_product, rows=grouped[pk], number=n)
        for n, pk in enumerate(order, start=1)
    ]


def lines_for_run(run):
    return lines_for(
        run.rows.select_related(
            "finished_product__recipe", "finished_product__raw_product"
        )
    )


def accept_line(line, yielded=None, fancy=0):
    """Bank a whole line, spreading what came out across its open baths.

    **One tick means the whole group came in.** Fifteen were asked for and
    fifteen arrived, which is the answer nearly every time. The write-in is
    for the session that produced thirteen, and the app decides which baths
    those thirteen belong to — because nobody standing at a sink knows or
    cares whether the two that were lost came out of the first pot or the
    third, and asking would be the app demanding a fact to satisfy its own
    schema.

    Greedy in sheet order, the same way `restock.expected_fill` spreads stock
    across pegs: fill the first bath, then the next. **That is not a
    tie-break, it is the likeliest story.** Ten out of fifteen almost
    certainly means one pot failed, not that each of three lost 1.67 — so
    5/5/0 records one bath at zero and two that came out whole, which is the
    event. Spreading the shortfall evenly would invent a bad afternoon out of
    a single ruined lot, and `ProductionRunRow` is the only place a scrap
    question can be answered from.

    Somebody turning up ten short because a bath never ran is the other
    reading and the rarer one, and it costs nothing here: the rows say the
    same thing either way, and if it matters the bath can be struck instead.

    Cancelled baths are not filled, and already-accepted ones are left alone:
    `apply_row` is a no-op on a row that has a log, so a re-tapped submit
    still moves nothing twice.
    """
    pending = line.pending
    if not pending:
        return []

    asked = sum(r.quantity for r in pending)
    made = asked if yielded is None else min(max(int(yielded), 0), asked)
    fancied = min(max(int(fancy or 0), 0), made)
    if line.fancy_target is None:
        fancied = 0

    # **One product instance across the whole line**, which saves a query per
    # bath — `select_related` has already handed every row its own copy.
    #
    # It is no longer what makes the arithmetic right, and it never could have
    # been. It was the guard against two rows of a line each reading
    # `number_on_hand` as it stood before either ran and saving its own total,
    # and two *lines* on the same blank have no instance to share — which is
    # any sheet carrying two colorways of one yarn. `apply_row` locks and
    # re-reads each count immediately before writing it, so the guarantee sits
    # at the write instead of in each caller, and holds across requests as
    # well as within one.
    product = line.product
    logs = []
    for row in pending:
        row.finished_product = product
        take = min(made, row.quantity)
        fancy_take = min(fancied, take)
        logs.append(apply_row(row, yielded=take, fancy=fancy_take))
        made -= take
        fancied -= fancy_take
    return logs


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def line_code(line):
    """What a line's barcode carries: its SKU *and* its place on the sheet.

    This used to be `row_code`, one code per bath, and the reason for the
    position half was that a decoder returns one result per distinct symbol
    rather than per printed symbol — three identical barcodes came back as
    one, and a sheet printed the same SKU several times because `plan_baths`
    groups repeated baths of a colorway together.

    Grouping removed that hazard at the source: a SKU appears on exactly one
    line now, so the code would be unique on the SKU alone. The position
    stays anyway, because it is a second check on being pointed at the right
    sheet and costs a handful of bars.
    """
    return f"{line.product.sku or 'ROW'}#{line.number}"


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


def render_sheet(run, return_url, upload_url) -> bytes:
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
        .select_related(
            "finished_product__recipe",
            "finished_product__raw_product",
            # The dye amounts are scaled by the blank's table — silk has no
            # figures, so the category decides whether a row prints ounces at
            # all. Without this it is a query per bath.
            "finished_product__raw_product__category",
        )
        .prefetch_related("finished_product__recipe__recipe_dyes__dye__brand")
    )
    # **The reporting sheet groups; the work sheet does not.** Three baths of
    # Artisan Cabernet come back as one line of fifteen with one box, because
    # the pile at the end of the session is one pile and three boxes for it
    # are three chances to mark the wrong one. The work sheet keeps a row per
    # bath, because its boxes hold a pot at a point in a three-day process
    # and two dry with one still wet is exactly what it has to be able to
    # say.
    lines = lines_for(rows)
    per_page = int((page_h - PAGE_MARGIN * 2 - HEADER_HEIGHT) // ROW_HEIGHT)
    per_page = max(per_page, 1)

    # Collection comes first because that is the order the work happens in:
    # one walk to the shelf, then the session. It is its own page rather than
    # a block above the rows so a long list can't squeeze them, and so it can
    # be carried to the shelf on its own.
    _draw_collection_page(pdf, run, return_url, upload_url, rows, page_w, page_h)
    _draw_pages(pdf, run, return_url, rows, page_w, page_h, per_page,
                instructions=WORK_INSTRUCTIONS, draw=_draw_work_row, qr=False)
    _draw_pages(pdf, run, return_url, lines, page_w, page_h, per_page,
                instructions=BATH_INSTRUCTIONS, draw=_draw_line, qr=True)

    pdf.save()
    buf.seek(0)
    return buf.read()


def _draw_pages(pdf, run, return_url, rows, page_w, page_h, per_page,
                instructions, draw, qr):
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
        for index, row in enumerate(rows[start:start + per_page]):
            draw(pdf, row, start + index + 1, y, page_w)
            y -= ROW_HEIGHT
        pdf.showPage()


def _draw_collection_page(pdf, run, return_url, upload_url, rows, page_w,
                          page_h):
    """The shelf list: the blanks and the dyes this run needs.

    Both halves of one errand, in the order the work happens — you carry
    scarves to the dye room, and you carry dye to them.

    **It also carries the upload code, and this is the page that can.**
    Nothing on paper said where a photo of a marked sheet goes: the reporting
    sheet's QR opens that run, and the upload page was reachable by knowing
    the URL or having bookmarked it — a step that has to be remembered, which
    is the one thing this app takes as given that nobody will do.

    The obvious home for it was the reporting sheet, and that is the one page
    it must not go on. That sheet is the one photographed, so a second code
    lands in every shot: `_read` keeps the first QR that yields a token, so
    which of the two names the run becomes a coin flip. `token_in` now
    refuses anything that isn't the run route, which closes that — but the
    collection page is never photographed at all, so here the question does
    not arise, and structural beats guarded. It is also where the paper
    already is: this is the sheet that goes to the shelf and stays in the dye
    room, so it is in the room when the session ends.
    """
    plan = dye_plan([row.finished_product.recipe for row in rows])
    blanks = blank_demand(rows)

    _draw_header(pdf, run, return_url, page_w, page_h,
                 page_no=None, page_count=None,
                 instructions=DYE_INSTRUCTIONS)
    # Drawn now, at the foot of the page, rather than after the lists — a
    # canvas takes marks anywhere on the page it is on, and doing it here is
    # what makes the footer unconditional. Drawn at the end it would depend
    # on where the dye list happened to stop, and a long list would leave the
    # page it belongs on without it.
    _draw_upload_footer(pdf, upload_url, page_w)

    y = page_h - PAGE_MARGIN - HEADER_HEIGHT

    # The floor the lists stop at. It starts above the footer and drops to the
    # ordinary margin as soon as a second page starts, because only the first
    # collection page carries one.
    floor = [PAGE_MARGIN + FOOTER_HEIGHT]

    def carry_on(cursor):
        """Start a fresh page when the current one runs out."""
        if cursor >= floor[0] + 20:
            return cursor
        pdf.showPage()
        _draw_header(pdf, run, return_url, page_w, page_h,
                     page_no=None, page_count=None,
                     instructions=DYE_INSTRUCTIONS)
        floor[0] = PAGE_MARGIN
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
    # What this code *does*, said on every page that carries it. It was
    # unlabelled while it was the only code in the print; the collection page
    # now carries a second one, and two unlabelled QRs on one sheet is a
    # choice with consequences and nothing to make it by.
    pdf.setFont("Helvetica-Bold", 7)
    pdf.drawRightString(page_w - PAGE_MARGIN, top - QR_SIZE - 34,
                        "OPEN THIS SHEET")


def _draw_upload_footer(pdf, upload_url, page_w):
    """Where a photo of the finished sheet goes, on paper.

    The upload page is camera-first by design — the photo is what names the
    sheet, so there is nothing to navigate to before taking it — and that
    only pays off if you can get to it without already knowing the address.
    Bookmarking it was the answer and a bookmark is a step that has to be
    remembered, which this app takes as a step that will not happen.

    The URL is printed under the code for the same reason the run's is: the
    QR is the convenience and the paper is the record, and a dead phone
    should not be the reason a session goes unreported.
    """
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode import qr as qr_module
    from reportlab.graphics.shapes import Drawing

    band = PAGE_MARGIN + FOOTER_HEIGHT

    pdf.setLineWidth(0.5)
    pdf.line(PAGE_MARGIN, band - 8, page_w - PAGE_MARGIN, band - 8)

    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(PAGE_MARGIN, band - 28, "WHEN THE SESSION IS DONE")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(PAGE_MARGIN, band - 44,
                   "Scan to send a photo of the marked reporting sheet.")
    pdf.drawString(PAGE_MARGIN, band - 58,
                   "It reads the boxes and ticks them for you — check them "
                   "and submit.")
    pdf.setFont("Helvetica", 7)
    pdf.drawString(PAGE_MARGIN, band - 76, upload_url)

    widget = qr_module.QrCodeWidget(upload_url, barLevel="M")
    bounds = widget.getBounds()
    drawing = Drawing(UPLOAD_QR_SIZE, UPLOAD_QR_SIZE, transform=[
        UPLOAD_QR_SIZE / (bounds[2] - bounds[0]), 0, 0,
        UPLOAD_QR_SIZE / (bounds[3] - bounds[1]), 0, 0,
    ])
    drawing.add(widget)
    renderPDF.draw(drawing, pdf,
                   page_w - PAGE_MARGIN - UPLOAD_QR_SIZE, PAGE_MARGIN + 12)

    pdf.setFont("Helvetica-Bold", 7)
    pdf.drawRightString(page_w - PAGE_MARGIN, PAGE_MARGIN + 2, "SEND A PHOTO")


def _draw_work_row(pdf, row, number, y, page_w):
    """One bath on the working copy: what to make, what goes in it, one box.

    No barcode and no tick box that anything reads — the absence is the
    feature, because it is what stops a marked-up working copy being
    photographed and read as a report.

    **The dyes are printed per bath, and that is what this row is for.** The
    collection page already lists them pooled, which is the right shape for
    one walk to the shelf and the wrong shape at the sink: standing over a pot
    the question is what goes in *this* one, and the answer used to be on a
    different page in a different order. A recipe with nothing on file says so
    rather than printing an empty line — a blank there reads as "no dyes
    needed", which is a bath somebody starts and cannot finish.
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

    # The one box, hard against the margin where the column of four used to
    # start, so a stack of these sheets still fans with the boxes in line.
    box_x = page_w - PAGE_MARGIN - STAGE_BOX
    pdf.setLineWidth(1)
    pdf.rect(box_x, baseline + 2, STAGE_BOX, STAGE_BOX)

    dyes = recipe_bath_dyes(product, row.quantity)
    pdf.setFont("Helvetica" if dyes else "Helvetica-Oblique", 8)
    text = ", ".join(dyes) if dyes else "no dyes on file"
    # Bounded so a five-dye recipe cannot run under the box and out of the
    # page. reportlab will happily draw past the margin and say nothing.
    room = box_x - 8 - (text_x + 170)
    pdf.drawString(text_x + 170, baseline + 8, _clipped(pdf, text, room))


def recipe_bath_dyes(product, quantity):
    """The dye line for one bath: names, with ounces where there are any.

    **The amount is this bath's, not the book's.** The dye book writes every
    figure for a five-skein bath and the pot in front of somebody is often
    not five, so printing the book number here would send a four-skein bath
    out with a fifth too much dye in it — a mistake that is invisible until
    the colour comes out wrong. `dyeamounts` does the division; a blank whose
    table has no basis (silk) prints the names alone, because an ounces
    figure beside a silk bath would be read at the sink as a measurement
    somebody took on silk, and nobody has.
    """
    parts = []
    for recipe_dye, ounces in bath_amounts(
        product.recipe, product.raw_product, quantity
    ):
        text = recipe_dye.dye.name
        if ounces is not None:
            text = f"{text} {format_ounces(ounces)}"
        parts.append(text)
    return parts


def recipe_dye_names(recipe):
    """The dyes on a recipe, in slot order, for anything printing a bath.

    One function because the sheet and `private/production-needed/` both ask
    it and a second copy is how the paper and the screen come to disagree
    about what goes in a pot.
    """
    if recipe is None:
        return []
    return [rd.dye.name for rd in recipe.recipe_dyes.all()]


def _clipped(pdf, text, room):
    """`text`, shortened with an ellipsis until it fits `room` points.

    reportlab draws past the margin and off the page without complaining, so
    nothing downstream would have told anybody the dye list was cut off —
    it would simply have ended mid-word at the paper's edge.
    """
    if room <= 0:
        return ""
    font, size = pdf._fontname, pdf._fontsize
    if pdf.stringWidth(text, font, size) <= room:
        return text
    while text and pdf.stringWidth(text + "…", font, size) > room:
        text = text[:-1]
    return (text + "…") if text else ""


def _draw_line(pdf, line, number, y, page_w):
    """One colorway on the reporting sheet: box, barcode, and what came out.

    **One box for every bath of it.** Three baths of Cabernet print as
    `15 × Cabernet` with a single tick, not as three fives — the crew are
    holding one pile and answering one question about it.

    The box and the barcode are at exactly the offsets `sheetscan` reads back
    along — `box_geometry` derives them from these same constants, so there
    is no second copy of the layout to drift.
    """
    product = line.product
    baseline = y - ROW_HEIGHT + 12

    # The box. Heavy stroke so a photo of it has something unambiguous to
    # measure against, and empty inside so "filled in" is the only ink there.
    pdf.setLineWidth(1.6)
    pdf.rect(BOX_LEFT, baseline + BOX_BASELINE_OFFSET, BOX_SIZE, BOX_SIZE)
    pdf.setLineWidth(1)

    barcode_x = BOX_LEFT + BOX_SIZE + BOX_TO_BARCODE
    drawn_width = BARCODE_WIDTH
    if product.sku:
        symbol = barcode_symbol(line_code(line))
        symbol.drawOn(pdf, barcode_x, baseline + BARCODE_BASELINE_OFFSET)
        # The real width, not the target: quiet zones don't scale, so the
        # symbol runs wider than BARCODE_WIDTH and the text has to start
        # after where it actually ends.
        drawn_width = symbol.width

    text_x = barcode_x + drawn_width + 14

    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(text_x, baseline + 14, f"{line.quantity} × {product.recipe.name}")
    pdf.setFont("Helvetica", 9)
    # The bath count is stated where the line is more than one, because the
    # collection page and the work sheet are both still counted in baths and
    # somebody comparing the two needs to see where fifteen came from.
    baths = f" · {line.baths} baths" if line.baths > 1 else ""
    pdf.drawString(
        text_x, baseline + 2,
        f"{product.raw_product.name} · {product.sku or 'no SKU'}{baths} · "
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
                          f"OF {line.quantity}")
    pdf.setFont("Helvetica", 8)
    pdf.drawRightString(rule_x - 10, baseline + 2, f"#{number}")
