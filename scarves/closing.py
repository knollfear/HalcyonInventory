"""The Sunday-night close: the app's empty bags, read against the tags in hand.

The crew keep a product's kanban tag when the last of it leaves the bag and
goes onto the display. **That is a statement about the bag, not the shelf.**
There are still one to three units hanging on the pegs, and the close's old
reading — tag in hand means set it to zero — deleted them.

Which mattered far more than a few miscounted skeins. Once display stock is
invisible, an inventory tracker is really a backstock tracker, and a backstock
tracker lets the shop's own furniture order dye baths: fill a newly built rack
out of the bags and the app sees a weekend of sales that never happened. Every
year the display grows, and every year that growth billed itself as demand and
got paid for in dyeing. `number_on_hand` counts display and backstock together
so that moving a skein from bag to peg changes nothing at all.

So the app's version of "the bag is empty" is `number_on_hand <=
display_slots`, and that is what puts a product on the list. Three situations,
one shape of answer:

    tag in hand              bag's empty — count the display        0 … slots
    no tag                   fill the display, count the bag        slots + rest
    tag nobody predicted     app overcounts — count the display     0 … slots

**Every answered row is a count, and the count is the total.** The tag is no
longer the answer; it is what puts the product in front of somebody. The
outcome is the sign of `counted - on_hand_before`, which is why a predicted
row can come out as an overcount and an unpredicted tag doesn't have to — see
`CloseRunRow.added_by_tag` for why those are two axes and not one.

**Display capacity is not a production target.** Nothing here reports how many
displays came up short, and `tally()` deliberately has no key for it. A hook
that holds four is worth having precisely so that three is allowed to be
enough; a number on this page counting the gap would be acted on whatever the
caption said, and would put back the coupling the whole rebuild removes. Par
stays the production trigger, par is about demand, and par does not move
because the shop got a new rack.

**The expected list only ever grows.** New rows are folded in each time the
page is opened, because a scarf whose bag empties at four o'clock has to reach
the list that gets worked at seven; but a row already on the run is never
removed or rewritten, so nothing shifts under somebody part-way down a pile.
That matters more than it sounds: a row that quietly disappeared would read as
one already answered and get passed over.

**A run is a calendar day, and yesterday's is closed.** Everything here
refuses to touch a run that isn't today's, so the guard holds whether the
caller is the page, a stale form left open overnight, or a re-submitted POST.

Every adjustment written from here carries `InventoryLog.SOURCE_SUNDAY_CLOSE`,
which is the whole reason that field exists: the counts coming off these runs
are meant to be compared against the other ways stock moves, and a comparison
built on matching English in `notes` stops being true the first time somebody
rewords a message.
"""

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import CloseRun, CloseRunRow, FinishedProduct, InventoryLog


def expected_products():
    """Everything whose bag the app believes is empty, so a tag should exist.

    `number_on_hand <= display_slots` is the app saying every unit it knows
    about is hanging on the display. That is the same claim the crew make by
    holding the tag, which is what makes the two comparable.

    Gated on having a display at all rather than on `par`, which is what used
    to gate it. A product with no display slots never goes out on the stall,
    so no tag will ever come up for it and asking would be asking about a
    pile nobody can see. Par is a production number and has no business
    deciding what gets audited.

    Passthroughs are excluded by the null-recipe test they always fail: an
    undyed skein is ordered rather than made, its count lives on the raw
    product, and there is no kanban card in a bag for it to disagree with.
    """
    return (
        FinishedProduct.objects.filter(
            is_active=True,
            display_slots__gt=0,
            number_on_hand__lte=F("display_slots"),
            recipe__isnull=False,
        )
        .select_related("raw_product", "recipe")
        .order_by("sku", "raw_product__name", "recipe__name")
    )


def run_for_today(employee=None):
    """Today's close, opened if it isn't already. Returns `(run, created)`.

    `get_or_create` on the day rather than a new row per visit: reopening the
    page an hour later is the same evening's work, and a second run would
    split one night's findings across two records that each read as the whole
    night.
    """
    run, created = CloseRun.objects.get_or_create(
        day=timezone.localdate(),
        defaults={"employee": employee},
    )
    if not created and employee is not None and run.employee_id is None:
        run.employee = employee
        run.save(update_fields=["employee"])
    sync_expected(run)
    return run, created


def sync_expected(run):
    """Fold in any product whose bag has since emptied. Adds only.

    Called on the way into the page rather than at creation, so a close
    started at noon still asks about the scarf whose bag ran out at four.
    Rows already on the run are left exactly as they are — including their
    frozen `on_hand_before` and `display_slots`, which are what the
    disagreement was measured against and what the question was asked in
    terms of.
    """
    if not run.is_open:
        return []

    already = set(run.rows.values_list("finished_product_id", flat=True))
    added = [
        CloseRunRow(
            run=run,
            finished_product=product,
            on_hand_before=product.number_on_hand,
            display_slots=product.display_slots,
        )
        for product in expected_products()
        if product.pk not in already
    ]
    if added:
        CloseRunRow.objects.bulk_create(added)
    return added


def is_frozen(run, row) -> bool:
    """Whether this row's answer is settled and must not be re-read.

    Two ways to be settled. **The day is over** — yesterday's close is a
    record, and every answer on it is final whatever it was. Or **stock has
    moved**, which is permanent even today: taking a movement back is an
    explicit Undo that writes a compensating entry, not a number retyped over
    the top of one that already moved the shelf.

    A row counted at exactly what the app already believed moved nothing, so
    within the day it stays open to correction — a fat-fingered number on a
    list of twenty is an ordinary mistake, and letting it be retyped costs
    nothing.
    """
    if not run.is_open:
        return True
    return row.is_applied


def record_count(run, row, counted):
    """The one answer this page takes: how many of these are actually here.

    `counted` is the **total** — the display plus whatever is left in the bag
    once the display has been filled. It is one number whichever situation
    the person is in, because the physical acts differ but the quantity being
    reported does not, and a form that asks two different questions depending
    on a tag is a form somebody answers in the wrong box.

    Filling the display *first* is what makes the no-tag case honest: the
    restocking and the measurement are the same act, so the count is taken
    from a display that is now in the state next weekend needs it in.

    The delta is measured against the **live** count, not the number frozen
    on the row. The frozen one is what the disagreement was measured against
    and is worth keeping for that; but the log has to say what actually
    moved, or the ledger stops adding up to the shelf.

    An answer equal to what the app already believed is a real answer and is
    recorded as an agreement. It moves nothing, writes no log, and so stays
    correctable for the rest of the day.
    """
    if is_frozen(run, row):
        return row

    counted = max(int(counted), 0)
    with transaction.atomic():
        product = FinishedProduct.objects.select_related("raw_product").get(
            pk=row.finished_product_id
        )
        delta = counted - product.number_on_hand
        log = None
        if delta > 0:
            outcome = CloseRunRow.MISSING
            note = (
                f"Sunday close: counted {counted} in hand against the app's "
                f"{product.number_on_hand}. The app was under by {delta}."
            )
        elif delta < 0:
            outcome = CloseRunRow.EXTRA
            note = (
                f"Sunday close: counted {counted} in hand against the app's "
                f"{product.number_on_hand}. The app was over by {-delta}."
            )
        else:
            outcome = CloseRunRow.CONFIRMED
            note = None

        if delta:
            product.set_on_hand(counted)
            log = _adjustment(product, delta, note)

        row.outcome = outcome
        row.counted = counted
        row.applied_log = log
        row.decided_at = timezone.now()
        row.save(update_fields=["outcome", "counted", "applied_log", "decided_at"])
    return row


def undo(run, product_row):
    """Take back an answer this close applied, same day, on the page itself.

    The earlier rule here was that a movement could only be reversed through
    a bulk inventory adjustment with a reason attached. That rule quietly
    assumed the person holding the phone had a staff login and knew where the
    adjustment screen was — and the crew have neither. What it actually
    produced was an employee who mis-tapped, could not fix it, and had to go
    and tell somebody. **That is a data-quality problem before it is a
    kindness one:** the cost of admitting a mistake is exactly the pressure
    that gets one left unmentioned, and a wrong count nobody reports is the
    failure this whole page exists to catch.

    So the movement is reversed here, and the history is not touched. The
    original `InventoryLog` stays exactly where it is and a **compensating
    entry** is written beside it, so the ledger says a thing happened and was
    put back — which is true — rather than quietly ceasing to mention it.
    Nothing is deleted, which keeps faith with the rest of the app.

    The reversal is an inverse *delta*, never a restored absolute, because a
    sale can land between the mistake and the undo. `set_on_hand` clamps at
    zero, so the arithmetic degrades the right way.

    Scope is deliberately narrow, and this is not a general licence to edit
    stock from an unauthenticated page: it reverses **this close's own
    movement**, on **this close's own day**, and nothing else. Correcting
    anything older still goes through a bulk adjustment.

    Returns the row, or `None` when the row is gone — an unpredicted tag that
    was added by mistake leaves nothing behind to be pending about.
    """
    if not run.is_open:
        return product_row
    if not product_row.is_applied:
        return product_row

    with transaction.atomic():
        log = product_row.applied_log
        product = FinishedProduct.objects.select_related("raw_product").get(
            pk=product_row.finished_product_id
        )
        product.set_on_hand(product.number_on_hand - log.quantity)
        _adjustment(
            product,
            -log.quantity,
            f"Sunday close: answer taken back on the page "
            f"(reverses the {log.quantity:+d} logged a moment earlier).",
        )

        # A row the close never predicted was invented by somebody holding a
        # tag, so undoing it leaves nothing to be pending about — the page
        # forgets, and the two log entries are what remember. Anyone
        # genuinely holding the tag can add it again.
        if product_row.added_by_tag:
            product_row.delete()
            return None

        product_row.outcome = CloseRunRow.PENDING
        product_row.counted = None
        product_row.applied_log = None
        product_row.decided_at = None
        product_row.save(
            update_fields=["outcome", "counted", "applied_log", "decided_at"]
        )
    return product_row


def add_tag(run, product):
    """A tag in hand for a product the close didn't predict. Moves nothing.

    It adds the row and stops. The old version adjusted straight to zero on
    the strength of the tag alone, which was wrong twice over: the tag says
    the *bag* is empty, not the shelf, and there are still units on the
    display to be counted. Now the tag only puts the product on the list, and
    the same count everything else gets is what settles it.

    Which also dissolves a special case that used to live here — a product
    already at zero whose tag therefore "agreed". There is nothing to guess
    at any more, because nothing is decided before somebody counts.

    Returns `(row, created)`. An already-listed product comes back untouched,
    because the second scan of the same tag is a person being thorough.
    """
    if not run.is_open:
        return None, False

    row = run.rows.filter(finished_product=product).first()
    if row is not None:
        return row, False

    product = FinishedProduct.objects.select_related("raw_product").get(pk=product.pk)
    row = CloseRunRow.objects.create(
        run=run,
        finished_product=product,
        on_hand_before=product.number_on_hand,
        display_slots=product.display_slots,
        added_by_tag=True,
    )
    return row, True


def _adjustment(product, delta, notes):
    """One stock movement, tagged as this close's."""
    return InventoryLog.objects.create(
        finished_product=product,
        raw_product=product.raw_product,
        log_type=InventoryLog.ADJUSTMENT,
        source=InventoryLog.SOURCE_SUNDAY_CLOSE,
        quantity=delta,
        notes=notes,
    )


def tally(run):
    """The numbers this run exists to produce: what was found wrong.

    Absolute counts, deliberately, and no rate anywhere. Ten corrections in a
    weekend is ten corrections whether the list was twelve products long or
    two hundred — the work done and the errors caught are the same either way,
    and dividing by the rows that were already right converts a number worth
    acting on into a reassuring one. Nobody is being graded on the saves.

    `confirmed` is here because the flow needs it (an answered row stops
    coming back), not as a denominator. The two disagreements are kept apart
    rather than summed: they point at opposite ends of the pipeline, and a net
    figure lets a bad intake cancel out a dead webhook.

    **Predicted and unpredicted is a different axis from over and under**, and
    they used to be the same one. `expected` counts the rows the close asked
    about; the direction buckets count which way the app was wrong, wherever
    the row came from.

    There is no key for displays left short, and that is a decision rather
    than an omission. See the module docstring: capacity is not a target, and
    a gap counted here would be acted on.
    """
    rows = list(run.rows.all())
    expected = [r for r in rows if not r.added_by_tag]
    unpredicted = [r for r in rows if r.added_by_tag]
    confirmed = [r for r in rows if r.outcome == CloseRunRow.CONFIRMED]
    missing = [r for r in rows if r.outcome == CloseRunRow.MISSING]
    extra = [r for r in rows if r.outcome == CloseRunRow.EXTRA]
    pending = [r for r in rows if r.outcome == CloseRunRow.PENDING]

    answered = len(confirmed) + len(missing) + len(extra)
    disagreements = len(missing) + len(extra)
    return {
        "expected": len(expected),
        "unpredicted": len(unpredicted),
        "unpredicted_rows": unpredicted,
        "confirmed": len(confirmed),
        "confirmed_rows": confirmed,
        "missing": len(missing),
        "missing_rows": missing,
        "extra": len(extra),
        "extra_rows": extra,
        "pending": len(pending),
        "pending_rows": pending,
        "answered": answered,
        "disagreements": disagreements,
        "under_units": sum(
            max((r.counted or 0) - r.on_hand_before, 0) for r in missing
        ),
        "over_units": sum(
            max(r.on_hand_before - (r.counted or 0), 0) for r in extra
        ),
    }
