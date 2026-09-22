"""What the app thinks was dyed since a date, and how to say it wasn't.

`private/produced-since/` is the record behind the production flow. Everything
else in this app that reads these rows *consumes* them — the label sheet turns
them into stickers, the raw shelf turns them into a reorder date, the planner
subtracts them from a shortage — and until this page existed none of them
showed the rows themselves. The consequence was not a missing feature, it was
a person who would not use the production form: recording a bath wrote
something she could not read back and could not take back, so every click was
a commitment with no receipt.

Two things, and they are the same thing twice:

- **The list.** One row per entry, grouped by the day it is recorded against,
  saying what the app believes and where the belief came from. A number you
  can check is the difference between a system and an oracle, which is the
  argument the rest of `CLAUDE.md` makes about par and `colorbands` — this is
  that argument applied to the app's own history.
- **The undo.** A retraction writes a compensating entry and touches nothing
  that is already recorded. It exists for exactly the reason
  `closing.undo` does: a mistake somebody cannot fix themselves is a mistake
  they have to go and confess, and that cost is what gets one left
  unmentioned instead. An unreported wrong number is worse for the data than
  any number of retractions, which is why there is no friction in front of
  this and no count of how often it happens.

**And the pair leaves no mark on the page.** A retracted entry and its
compensating row both drop out of the list, so what is shown is the app's
current belief and nothing about how it got there. This is deliberate and it
is the difference between a fix and a confession: a struck-through row reading
*taken back* is a standing note about a mistake, sitting on the page somebody
opens every week, and a correction that leaves one is a correction with a
cost. The whole point of the button is that using it should be cheaper than
saying nothing.

Nothing is hidden from the *record* — both rows are in `InventoryLog` for
good, linked by `reverses`, and the admin shows them the way it shows
everything else. The claim is narrow and it is the one that matters: reading
this page does not tell you what anybody got wrong.

**It reads production only.** `InventoryLog` also carries sales and
adjustments, and an adjustment is a *recount* rather than an arrival — the
Sunday close alone writes dozens every weekend. Folding those in is what made
the label sheet unreadable as a record of what was made, and this page would
have inherited the same fault: a page meant to answer "did I dye this" must
not also be answering "did somebody find a bag of it in a cupboard".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction

from . import ledger
from .models import RawProduct, ProductionRunRow, FinishedProduct, InventoryLog
from .labels import as_datetime


#: Sources whose production rows actually moved stock.
#:
#: Inverted on purpose — the list names the exception, not the rule, so a new
#: flow that dyes something is covered by default and only a deliberately
#: history-only one has to be added here.
#:
#: `private/cards/` is the whole of it. A kanban card is a column of dates and
#: bath counts from 2024, and adding it to `number_on_hand` would inflate
#: current stock by however far back the records go — so the backfill writes
#: log rows and nothing else. Retracting one of those is therefore a
#: correction to *history* and must move no stock either, or an Undo on a
#: two-year-old card would quietly take five scarves off today's shelf.
HISTORY_ONLY_SOURCES = (InventoryLog.SOURCE_CARD_BACKFILL,)


def moved_stock(log) -> bool:
    """Whether this entry put anything on the shelf when it was written."""
    return log.source not in HISTORY_ONLY_SOURCES


def entries(since, category=None, blanks=None):
    """The production rows this page is about, newest first.

    Keyed on `created_at`, which for a back-dated card is the date on the
    card rather than the day it was typed — so an old session appears under
    the month it happened in, and a cutoff of last Monday does not drag 2024
    onto the page.

    **A retracted row is gone from here**, and its compensating row never
    appears because that one is an ADJUSTMENT. So the pair cancels out
    completely and the list shows the app's current belief with no account of
    how it arrived — which is the point, and the reasoning is in the module
    docstring. Elsewhere in this app a row that vanished would be the
    problem; here the row that *stayed* would be, because it would be a note
    about a mistake left on a page somebody has to keep opening.

    The usual caution still applies to the totals: they are what the app
    believes now, not a history, and they will silently get smaller when
    something is taken back. That is what is wanted. Anyone asking the other
    question — what was recorded and then undone — asks `InventoryLog`, where
    both rows are, linked.
    """
    qs = (
        InventoryLog.objects.filter(
            log_type=InventoryLog.PRODUCTION,
            created_at__gte=as_datetime(since),
            reversals__isnull=True,
        )
        .select_related(
            "finished_product",
            "finished_product__raw_product",
            "finished_product__raw_product__category",
            "finished_product__recipe",
            "raw_product",
        )
        .order_by("-created_at", "-pk")
    )
    if category:
        qs = qs.filter(finished_product__raw_product__category=category)
    # Empty means every blank, never "none" — the same rule the label page's
    # blank checklist follows, for the same reason.
    if blanks:
        qs = qs.filter(finished_product__raw_product__in=blanks)
    return qs


@dataclass
class Group:
    """One day's entries, and what they add up to."""

    label: str
    rows: list = field(default_factory=list)
    units: int = 0


@dataclass
class Record:
    """Everything the page renders, worked out here rather than in the template.

    There is no count of retractions anywhere on it, and that absence is the
    feature — see the module docstring. `entries` has already dropped them, so
    there is nothing here that could report one even by accident.
    """

    since: object
    groups: list = field(default_factory=list)
    units: int = 0
    count: int = 0
    by_category: list = field(default_factory=list)
    by_source: list = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.groups


def build(since, category=None, blanks=None) -> Record:
    """Group the entries by day and total them up.

    Grouping is on `InventoryLog.day`, which says no more than the row's
    `date_precision` knows — so a month-only kanban entry heads a group
    called `Sep 2024` instead of being filed under a day nobody wrote down.
    Walked in query order rather than sorted here, because the queryset is
    already newest-first and re-deriving the order would be a second place
    for it to live.
    """
    record = Record(since=since)
    per_category: dict[str, int] = {}
    per_source: dict[str, int] = {}
    current = None

    for log in entries(since, category=category, blanks=blanks):
        # Attached rather than worked out in the template, which is where the
        # rule about what moved stock would otherwise end up living twice.
        log.moved_stock = moved_stock(log)

        if current is None or current.label != log.day:
            current = Group(label=log.day)
            record.groups.append(current)

        current.rows.append(log)
        record.count += 1
        current.units += log.quantity
        record.units += log.quantity
        cat = log.finished_product.raw_product.category
        per_category[cat.name] = per_category.get(cat.name, 0) + log.quantity
        label = log.get_source_display() or "Not recorded"
        per_source[label] = per_source.get(label, 0) + log.quantity

    record.by_category = sorted(per_category.items())
    record.by_source = sorted(per_source.items(), key=lambda pair: -pair[1])
    return record


# ---------------------------------------------------------------------------
# Taking one back
# ---------------------------------------------------------------------------


def blanks_consumed(log) -> int:
    """How many blanks this entry can vouch for having left the shelf.

    Usually its own quantity: a bath of five puts five on the peg and takes
    five skeins off the shelf. Two cases are not that, and both are read off
    rows already in the database rather than guessed:

    **A short bath.** `production.apply_row` takes the *full* bath off raw and
    puts only `yielded` onto finished, because the blanks are gone whatever
    happened in the pot. So where a `ProductionRunRow` points at this log, the
    row's own `quantity` is what left the shelf — five, not the three that
    survived.

    **A fancy veil out of a plain bath.** That writes a second entry for a
    second product against the *same* pot, and the log's `raw_product` is the
    plain blank rather than the fancy one. Restoring blanks on both would put
    the bath back twice, so a log whose product's blank is not the blank it
    consumed restores nothing — the sibling entry is accounting for the pot.
    """
    if log.raw_product_id is None:
        return 0
    if log.raw_product_id != log.finished_product.raw_product_id:
        return 0
    row = ProductionRunRow.objects.filter(applied_log=log).first()
    return row.quantity if row else log.quantity


def retract(log, note=""):
    """Say this entry did not happen. Returns the compensating row, or None.

    **Nothing is edited and nothing is deleted.** The original row stays
    exactly as written and a negative entry is created beside it pointing
    back at it, so the history says a thing was recorded and then taken back
    — which is true — instead of quietly ceasing to mention it. That also
    makes `reversals` the single answer to "has this been undone", which is
    what a double-tapped button and a reopened page need it to be.

    **The finished side is reversed as a delta, never as a restored
    absolute**, because a sale can land between the mistake and the Undo.
    `set_on_hand` clamps at zero, so the arithmetic degrades the right way,
    and it writes to the raw row for a passthrough — one pile, one number.

    **The raw side goes back too**, which the Sunday close's Undo has no
    equivalent of. There it would be wrong: a recount says what is on the
    shelf now and implies nothing about blanks. Here the entry is a claim
    that a pot was run, so retracting it is a claim that the blanks were
    never wet — and raw stock in this shop is an opening balance that is
    counted about once a year, so leaving it short would stay wrong until
    somebody reached for skeins that were on the shelf all along.

    **The `ProductionRunRow` is left alone.** Its `applied_log` is what stops
    a re-scanned sheet dyeing the same bath twice on paper, and handing that
    guard back to get a tidier-looking row would trade a silent
    double-count for a cosmetic one. The sheet goes on saying what the
    session reported; this says what was done about it afterwards. The
    colorway comes back on the next sheet regardless, because the planner
    reads `number_on_hand` and this just reduced it.

    A history-only entry — a kanban card typed up wrong — moves nothing in
    either direction, and still writes its compensating row so the retraction
    is recorded the same way every other one is.
    """
    if log.log_type != InventoryLog.PRODUCTION:
        return None

    with transaction.atomic():
        # Locked and re-read before the guard is checked. A double-tapped
        # button is two requests, and `is_retracted` read outside the
        # transaction would let both of them pass it — taking five scarves
        # off the shelf twice, silently, for one bath.
        locked = InventoryLog.objects.select_for_update().get(pk=log.pk)
        if locked.is_retracted:
            return None

        product = FinishedProduct.objects.select_related("raw_product").get(
            pk=log.finished_product_id
        )
        moved = moved_stock(log)
        returned = 0

        if moved:
            returned = blanks_consumed(log)
            if returned:
                raw = RawProduct.objects.select_for_update().get(pk=log.raw_product_id)
                raw.number_on_hand = raw.number_on_hand + returned
                raw.save(update_fields=["number_on_hand"])

        # Written even when nothing moved (quantity 0), because the row is
        # the retraction: `reverses` is what takes the original off the list.
        return ledger.move(
            product, -log.quantity if moved else 0,
            log_type=InventoryLog.ADJUSTMENT,
            source=InventoryLog.SOURCE_PRODUCTION_UNDO,
            notes=_note(log, moved, returned, note),
            reverses=log,
        )


def _note(log, moved, returned, typed):
    """The sentence on the compensating row.

    It says what the retraction *did*, not just that one happened — the
    numbers are the part somebody reading the history six months from now
    cannot reconstruct, and the raw side especially, since raw movements are
    not otherwise ledgered anywhere in this app.
    """
    parts = [
        f"Taken back on the produced-since page: the {log.quantity:+d} "
        f"recorded on {log.when} did not happen."
    ]
    if not moved:
        parts.append("History only, so no stock moved either way.")
    else:
        parts.append(f"{log.quantity} off {log.finished_product.name}.")
        if returned:
            parts.append(
                f"{returned} {log.raw_product.name} back on the raw shelf."
            )
    typed = (typed or "").strip()
    if typed:
        parts.append(f"Reason given: {typed}")
    return " ".join(parts)


#: There is deliberately no `retracted_total()` here, and it is worth saying
#: so out loud because it is the obvious next function to write. A count of
#: how many entries got taken back is a count of somebody's mistakes, and
#: putting it anywhere a page could read it undoes the whole reason the button
#: exists. The rows are in `InventoryLog` and can be counted by anyone who
#: needs to; nothing in this app is going to do it for them.
