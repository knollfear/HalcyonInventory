"""The Sunday-night close: the app's zeros, read against the tags in hand.

Stock at zero is the one state the app is confident about. `number_on_hand`
clamps there on every sale, so a row that has been undercounted all season
eventually funnels into it — the clamp turns a diffuse error into a discrete
list. And the crew keep a product's tag when the last of it goes out to
display, so the paper emits its own zero signal independently. Two systems,
one physical pile, and this is where they are read against each other while
the bags are still in the van.

Three answers per product, and the two disagreements are the point:

    tag in hand, app says 0     agree — genuinely out
    no tag, app says 0          app **undercounts**: count the bag, true it up
    tag in hand, app says n>0   app **overcounts**: adjust to zero

The second is the only one anybody types a number for. The third is what the
zero-trigger can never find on its own, because an overstated row never
reaches zero to be checked — which is exactly the shape a swapped sale leaves
behind, and exactly what a webhook that has quietly stopped delivering looks
like a week later.

**The expected list only ever grows.** New zeros are folded in each time the
page is opened, because a scarf selling out at four o'clock has to reach the
list that gets worked at seven; but a row already on the run is never removed
or rewritten, so nothing shifts under somebody part-way down a pile. That
matters more than it sounds: a row that quietly disappeared would read as one
already checked and passed over.

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
from django.utils import timezone

from .models import CloseRun, CloseRunRow, FinishedProduct, InventoryLog


def expected_products():
    """Everything the app believes is out, and therefore should have a tag.

    The same shape as the front of `production_needed_view` — below par with
    the zeros first — narrowed to the zeros themselves, which is the only
    part a tag says anything about.

    Passthroughs are excluded by the null-recipe test they always fail: an
    undyed skein is ordered rather than made, its count lives on the raw
    product, and there is no kanban card in a bag for it to disagree with.
    """
    return (
        FinishedProduct.objects.filter(
            is_active=True,
            number_on_hand=0,
            par__gt=0,
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
    """Fold any product that has since hit zero into the run. Adds only.

    Called on the way into the page rather than at creation, so a close
    started at noon still asks about the scarf that sold out at four. Rows
    already on the run are left exactly as they are — including their frozen
    `on_hand_before`, which is what the disagreement was measured against.
    """
    if not run.is_open:
        return []

    already = set(run.rows.values_list("finished_product_id", flat=True))
    added = [
        CloseRunRow(
            run=run,
            finished_product=product,
            on_hand_before=product.number_on_hand,
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
    inventory adjustment with a reason on it, not a re-tick of a box on a page
    with no login.

    A row that is merely *confirmed* moved nothing, so within the day it stays
    open to correction — a mis-tapped tick on a list of twenty is an ordinary
    mistake, and letting it back costs nothing.
    """
    if not run.is_open:
        return True
    return row.is_applied or row.outcome in (CloseRunRow.MISSING, CloseRunRow.EXTRA)


def confirm(run, row):
    """Tag in hand, app says zero: the two agree. Nothing moves."""
    if is_frozen(run, row):
        return row
    row.outcome = CloseRunRow.CONFIRMED
    row.decided_at = timezone.now()
    row.save(update_fields=["outcome", "decided_at"])
    return row


def unconfirm(run, row):
    """Back to unanswered. Only ever reachable while nothing has moved."""
    if is_frozen(run, row):
        return row
    row.outcome = CloseRunRow.PENDING
    row.decided_at = None
    row.save(update_fields=["outcome", "decided_at"])
    return row


def record_missing(run, row, counted):
    """No tag for a product the app calls out: count the bag and true it up.

    The delta is measured against the **live** count, not the number frozen
    on the row. The frozen one is what the disagreement was measured against
    and is worth keeping for that; but the log has to say what actually
    moved, or the ledger stops adding up to the shelf.

    A count of zero is allowed and recorded. It means the tag protocol broke
    rather than the app did — no tag, no stock — and it is a real answer, so
    it writes an outcome and no log, because nothing moved.
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
        if delta:
            product.set_on_hand(counted)
            log = _adjustment(
                product,
                delta,
                f"Sunday close: no tag held, bag counted at {counted}. "
                f"The app was under by {delta}.",
            )

        row.outcome = CloseRunRow.MISSING
        row.counted = counted
        row.applied_log = log
        row.decided_at = timezone.now()
        row.save(update_fields=["outcome", "counted", "applied_log", "decided_at"])
    return row


def add_tag(run, product):
    """A tag in hand for a product the close didn't predict.

    Two different things arrive here and telling them apart is the reason
    this isn't a plain "set it to zero". Usually the app has a count above
    zero and the tag says otherwise — that is the overcount this step exists
    to catch. But the product may be at zero already and simply not have made
    the expected list (no par set, say), in which case the tag and the app
    *agree* and filing it as a disagreement would put a fault in the metric
    that the metric exists to measure.

    Returns `(row, created)`. An already-answered row comes back untouched,
    because the second scan of the same tag is a person being thorough.
    """
    if not run.is_open:
        return None, False

    row = run.rows.filter(finished_product=product).first()
    if row is not None:
        return row, False

    with transaction.atomic():
        product = FinishedProduct.objects.select_related("raw_product").get(pk=product.pk)
        on_hand = product.number_on_hand
        row = CloseRunRow.objects.create(
            run=run,
            finished_product=product,
            on_hand_before=on_hand,
            decided_at=timezone.now(),
            outcome=CloseRunRow.EXTRA if on_hand else CloseRunRow.CONFIRMED,
        )
        if on_hand:
            product.set_on_hand(0)
            row.applied_log = _adjustment(
                product,
                -on_hand,
                f"Sunday close: tag held but the app had {on_hand} on hand. "
                f"Adjusted to zero.",
            )
            row.save(update_fields=["applied_log"])
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

    `confirmed` is here because the flow needs it (a ticked row stops coming
    back), not as a denominator. The two disagreements are kept apart rather
    than summed: they point at opposite ends of the pipeline, and a net figure
    lets a bad intake cancel out a dead webhook.
    """
    rows = list(run.rows.all())
    expected = [r for r in rows if r.outcome != CloseRunRow.EXTRA]
    confirmed = [r for r in expected if r.outcome == CloseRunRow.CONFIRMED]
    missing = [r for r in expected if r.outcome == CloseRunRow.MISSING]
    extra = [r for r in rows if r.outcome == CloseRunRow.EXTRA]
    pending = [r for r in expected if r.outcome == CloseRunRow.PENDING]

    answered = len(confirmed) + len(missing)
    disagreements = len(missing) + len(extra)
    return {
        "expected": len(expected),
        "confirmed": len(confirmed),
        "missing": len(missing),
        "missing_rows": missing,
        "extra": len(extra),
        "extra_rows": extra,
        "pending": len(pending),
        "pending_rows": pending,
        "answered": answered,
        "disagreements": disagreements,
        "under_units": sum(r.counted or 0 for r in missing),
        "over_units": sum(r.on_hand_before for r in extra),
    }
