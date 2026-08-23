"""Printable barcode labels: which items to print, and where the ink goes.

The weekly job this exists for: after a dyeing session, print one sticker per
finished item added to inventory, and stick them on. A dye bath is one blank
plus one recipe, so it yields 3–5 units of a *single* SKU — which is why the
sheet is sorted by SKU and nothing more clever. The stickers then come off the
sheet in the same clumps the scarves come off the rack.

Three things are load-bearing here.

**Nothing in this module records that a print happened.** The obvious design
is a table of past runs, defaulting the next cutoff to the end of the last
one. It's wrong: the printer jams, a sheet gets ruined, and now the recorded
boundary is a lie only fixable in the database. Instead the state lives in two
places that can both be overruled — the browser pre-fills the last cutoff and
the last resume point, and the *sheet itself* carries a printed marker sticker
saying where to start next. Reprinting is changing a date. Double labels cost
a fraction of a cent; a boundary you can't edit costs an evening.

**Nothing assumes you own the printer, either.** These get printed at a copy
shop: a different machine each time, no chance to calibrate beforehand, and no
computer to hand when a sheet comes out 2mm high. So registration offsets ride
in the query string rather than only living on the model, and every sheet
prints registration ticks against its own die-cuts — the scaled-print failure
is progressive, so a short ruler can't see it and the check has to span the
whole sheet.

**The barcode is sized per label, not per run.** SKUs are `SLUG6-SLUG6`, so
they're bounded at ~14 characters but most are shorter, and a short one
deserves fat, forgiving bars rather than being shrunk to match the longest in
the batch. `MIN_MODULE_MIL` is the floor; anything under it raises rather than
printing a sheet of stickers no scanner will read.

**Geometry is trusted but verified.** `LabelStock` holds numbers a human
transcribed off a vendor page, and the failure mode of a transposed digit is a
ruined sheet. `LabelStock.overflow_in()` catches the gross version; the
calibration sheet (outlines only, printed on plain paper) catches the rest
before it costs anything.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from io import BytesIO

from django.db.models import Sum
from django.utils import timezone

from .models import FinishedProduct, InventoryLog

#: Narrowest bar we'll print, in mils (thousandths of an inch). Below roughly
#: this, general-purpose scanners get unreliable and toner spread on label
#: stock eats what's left. On the 1.75in stock a 14-character SKU comes out
#: around 7.3 mil, so this is a guard against a future too-small stock or a
#: hand-typed overlong SKU, not a limit anything currently bumps into.
#: What goes on each sticker.
#:
#: The barcode and the text answer different questions, and for a while the
#: barcode was assumed to be the important half. It isn't, and the reason is
#: physical: labels are applied by hand, off a sheet, onto a pile of scarves.
#: You cannot apply a sticker you cannot read — so the *text* is what makes the
#: sheet usable at all, and the barcode is what makes the scarf usable later.
#:
#: What the text should say follows from what actually gets confused. Nobody
#: mistakes a rectangle for a half-circle, so the blank is visible and the SKU
#: spends half a small label encoding it. Two similar reds named Valentine and
#: L Word is exactly the confusion a label can fix and eyes can't — so the text
#: is the colorway.
#: The two are independent runs over the same picker, not two halves of one
#: job — print either, or both, for whatever product set is selected. Nothing
#: pairs a sticker on one sheet with a sticker on the other.
BARCODE = "barcode"     # bars + SKU text. Unchanged, and still the default.
NAME = "name"           # the colorway, set as large as the stock allows.
STYLE_CHOICES = [
    (BARCODE, "Barcode + SKU"),
    (NAME, "Recipe name"),
]

#: A name-only sticker has no bars, so nothing constrains it but legibility.
#: Bounded at the top because a two-word colorway would otherwise set itself
#: absurdly large, and at the bottom because below this it stops doing the job
#: it exists for — being read across a booth rather than held to the light.
NAME_MAX_PT = 20.0
NAME_MIN_PT = 6.0

MIN_MODULE_MIL = 6.0

#: Inset from the die-cut edge on every side. Die cutting has its own
#: tolerance and a barcode running to the very edge of the label is the first
#: thing to get clipped.
PAD_IN = 0.05

#: Human-readable SKU under the bars. It is not decoration: when a barcode
#: won't scan someone types this, and it doubles as how you find one sticker
#: in a sheet of eighty.
TEXT_PT = 5.5

POINTS_PER_INCH = 72.0
POINTS_PER_MM = POINTS_PER_INCH / 25.4


def _pt(inches) -> float:
    return float(inches) * POINTS_PER_INCH


def _mm_pt(mm) -> float:
    return float(mm) * POINTS_PER_MM


# ---------------------------------------------------------------------------
# What to print
# ---------------------------------------------------------------------------


@dataclass
class LabelRow:
    """One product and how many stickers it wants."""
    product: FinishedProduct
    base: int      # what the data says
    quantity: int  # base + the extras asked for


@dataclass
class LabelRun:
    """A fully resolved print job, plus everything the page needs to explain
    itself before anyone commits a sheet to the printer."""
    rows: list
    skipped_no_sku: list          # products that can't be printed at all
    ambiguous_month_logs: int     # see `month_precision_ambiguity`
    row_break_on_group: bool = False
    style: str = BARCODE

    @property
    def needs_barcode(self) -> bool:
        """Whether this run prints bars, which decides two rules that
        otherwise fire wrongly: the density guard, and whether a product with
        no SKU can be printed at all."""
        return self.style == BARCODE

    @property
    def total(self) -> int:
        """Stickers actually printed. Excludes any padding — see `sequence`."""
        return sum(r.quantity for r in self.rows)

    def sequence(self, columns=None):
        """Sheet positions in order: a product per sticker, `None` for a gap.

        With `row_break_on_group`, each blank starts on a fresh row, so a
        stack of sheets can be split by blank without a seam falling
        mid-row. The group is the SKU prefix rather than the finished
        product: SKUs read `BLANK-DYEBATH` and sort alphabetically, so the
        prefix is exactly what's already contiguous on the sheet — and
        breaking per *product* instead would cost a partial row for each of
        266 of them, several hundred labels, which is the opposite of the
        point.

        Only worth it when the run is long. Across 31 sheets ~20 padding
        labels round to nothing; across 3 sheets the same number is a third
        of the run, which is why this is off for anything but the whole
        export.
        """
        out = []
        group = None
        for row in self.rows:
            key = row.product.sku.split("-")[0]
            if (
                self.row_break_on_group
                and columns
                and group is not None
                and key != group
                and len(out) % columns
            ):
                out.extend([None] * (columns - len(out) % columns))
            group = key
            out.extend([row.product] * row.quantity)
        return out

    def flat(self, columns=None):
        """Just the stickers, skipping any padding."""
        return [p for p in self.sequence(columns) if p is not None]


def _finish(products_and_counts, extra, style=BARCODE):
    """Shared tail of every dataset: drop what can't be printed, apply the
    extras, sort by SKU.

    Sorting by SKU is what groups a bath's 3–5 identical items into one
    contiguous clump on the sheet, matching the pile they're going onto. It
    stays the sort key for a name-only run too: the clumping is the point, and
    a SKU sorts by blank then colorway, which is the same order the pile is in.

    **A missing SKU only disqualifies a product that is going to be scanned.**
    A barcode of nothing is unprintable, but a sticker reading "Stormy Sea"
    needs no SKU at all, and withholding one over a field it never prints
    would be the app refusing a label it could perfectly well make. The two
    styles are independent runs — nothing pairs a sticker on one sheet with a
    sticker on another — so they are free to disagree about this.

    Either way a skipped product is reported in `skipped_no_sku` and shown on
    the page rather than vanishing, and `generate_skus` is the fix.
    """
    rows, skipped = [], []
    for product, base in products_and_counts:
        if style == BARCODE and not product.sku:
            skipped.append(product)
            continue
        rows.append(LabelRow(product=product, base=base, quantity=base + extra))
    rows = [r for r in rows if r.quantity > 0]
    rows.sort(key=lambda r: (r.product.sku, r.product.variation_name))
    return rows, sorted(skipped, key=lambda p: p.name)


def inventory_run(extra=0, category=None, raw_products=None, include_zero=False,
                  style=BARCODE):
    """Every active finished product, one sticker per unit on hand.

    The bulk re-label case. `include_zero` is off by default because with
    extras switched on, every dormant-but-still-active SKU would otherwise
    quietly eat labels.
    """
    qs = (
        FinishedProduct.objects.filter(is_active=True, raw_product__is_active=True)
        .select_related("raw_product", "recipe")
    )
    if category:
        qs = qs.filter(raw_product__category=category)
    # Empty means every blank, never "none" — a filter that silently prints
    # nothing when you forget to tick something is a wasted trip to the shop.
    if raw_products:
        qs = qs.filter(raw_product__in=raw_products)
    if not include_zero:
        qs = qs.filter(number_on_hand__gt=0)

    rows, skipped = _finish(((p, p.number_on_hand) for p in qs), extra, style)
    return LabelRun(
        style=style,
        rows=rows,
        skipped_no_sku=skipped,
        ambiguous_month_logs=0,
        # Only the unfiltered export is long enough for the padding to be
        # free; a filtered run is short, and there the same few labels are a
        # noticeable fraction of it.
        row_break_on_group=not (category or raw_products),
    )


def specific_items(pairs, style=BARCODE):
    """A run built by hand: exactly these products, exactly these counts.

    Takes no `extra`. The bulk datasets add spares because their counts are
    derived and a little slack is cheap; here somebody typed the number, so
    quietly printing more than they asked for would be the surprising thing.
    The page hides the extras control for this dataset rather than letting it
    sit there doing nothing.
    """
    rows, skipped = _finish(pairs, extra=0, style=style)
    return LabelRun(rows=rows, skipped_no_sku=skipped, ambiguous_month_logs=0,
                    style=style)


#: Log types that mean "an item arrived on the shelf and needs a sticker".
#:
#: PRODUCTION is the obvious one. ADJUSTMENT is the other door, and leaving it
#: out was a hole: stock counted in through `bulk_inventory_update` — a bag
#: found in a cupboard, a display rack folded back into inventory, anything
#: that existed before this app did — got no labels at all, and nothing said
#: so. The only symptom is a scarf that won't scan at the till with a queue
#: behind it, which is the failure the whole of this module is arranged to
#: prevent.
#:
#: SALE stays out deliberately. A sale doesn't reduce the need for a sticker;
#: the item exists and left wearing one. Netting sales in would subtract
#: labels for scarves that already have them.
LABELLED_LOG_TYPES = (InventoryLog.PRODUCTION, InventoryLog.ADJUSTMENT)


def produced_since(cutoff, extra=0, style=BARCODE):
    """One sticker per unit that arrived at or after `cutoff`.

    Sums log quantities rather than reading `number_on_hand`, so what already
    sold still gets a sticker — the items exist, they just aren't on the shelf
    by the time anyone prints.

    **Only rows that add.** A label answers "what is this thing in my hand",
    so it is wanted by items arriving and by nothing else; stock leaving needs
    no barcode. Negative rows are therefore skipped rather than netted, which
    also stops a correction in one week silently eating the stickers owed to a
    bath in the same week.

    Counts both dyeing and stock counted in — see `LABELLED_LOG_TYPES`. That
    widening prints some spares: an upward recount of items that were already
    labelled asks for stickers they already have. It is the right side of the
    trade by a wide margin. A spare sticker costs a fraction of a cent and
    sits in a drawer; a missing one is discovered at the till, in front of a
    customer, with no way to sell the thing in hand. Erring toward printing is
    the same bargain `UnmatchedSale` makes about capture.

    Back-dated entries take care of themselves: a kanban backfill carries
    `created_at` set to the date on the card, not the day it was typed, so a
    2024 card entered this morning correctly does not ask for stickers.
    """
    cutoff_dt = _as_datetime(cutoff)
    totals = (
        InventoryLog.objects.filter(
            log_type__in=LABELLED_LOG_TYPES,
            quantity__gt=0,
            created_at__gte=cutoff_dt,
            finished_product__is_active=True,
        )
        .values("finished_product")
        .annotate(n=Sum("quantity"))
    )
    counts = {t["finished_product"]: t["n"] or 0 for t in totals}
    products = (
        FinishedProduct.objects.filter(pk__in=counts)
        .select_related("raw_product", "recipe")
    )

    rows, skipped = _finish(((p, max(counts[p.pk], 0)) for p in products), extra, style)
    return LabelRun(
        style=style,
        rows=rows,
        skipped_no_sku=skipped,
        ambiguous_month_logs=month_precision_ambiguity(cutoff_dt),
    )


def _as_datetime(value):
    """A date from a form means the start of that day, locally."""
    if isinstance(value, datetime):
        return value
    return timezone.make_aware(datetime.combine(value, time.min))


def month_precision_ambiguity(cutoff_dt) -> int:
    """How many production rows the cutoff might be wrongly excluding.

    A `month`-precision log is stored on the 1st so that it sorts — that day
    is padding, not a record (see `InventoryLog.when`). So a cutoff mid-month
    excludes a same-month row whose real date could have been either side of
    it. Scoped to exactly the rows the run itself counts — same log types,
    same positive-only rule — so the warning can never describe rows the run
    was never going to print anyway. The page reports the count instead of silently dropping them, which is
    the same bargain the rest of the app makes with these rows: say only what
    is actually known.

    Realistic cutoffs are recent and old backfills are irrelevant, so this is
    almost always zero — it just shouldn't be *silently* nonzero.
    """
    month_start = cutoff_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start == cutoff_dt:
        return 0
    return InventoryLog.objects.filter(
        log_type__in=LABELLED_LOG_TYPES,
        quantity__gt=0,
        date_precision=InventoryLog.MONTH,
        created_at__gte=month_start,
        created_at__lt=cutoff_dt,
    ).count()


# ---------------------------------------------------------------------------
# Where the ink goes
# ---------------------------------------------------------------------------


@dataclass
class Slot:
    """One label's position, in points from the bottom-left of the page."""
    page: int
    row: int
    column: int
    x: float
    y: float


def slot_for(stock, index) -> Slot:
    """Position of the `index`-th label on the sheet, counting from 0 in
    reading order and rolling onto a new page at the end of each sheet."""
    page, within = divmod(index, stock.labels_per_sheet)
    row, column = divmod(within, stock.columns)

    x = _pt(stock.margin_left_in) + column * _pt(stock.pitch_x_in) + _mm_pt(stock.x_offset_mm)
    top = _pt(stock.margin_top_in) + row * _pt(stock.pitch_y_in)
    y = _pt(stock.page_height_in) - top - _pt(stock.label_height_in) + _mm_pt(stock.y_offset_mm)
    return Slot(page=page, row=row, column=column, x=x, y=y)


def slots(stock, count, start_at=0):
    """`count` positions in reading order, beginning at label `start_at`."""
    return [slot_for(stock, start_at + i) for i in range(count)]


def sheets_needed(stock, count, start_at=0) -> int:
    if count <= 0:
        return 0
    return slot_for(stock, start_at + count - 1).page + 1


# ---------------------------------------------------------------------------
# Where the run stops, and how the next one finds out
# ---------------------------------------------------------------------------


@dataclass
class SheetPlan:
    """What the run does to the physical sheets, for the page to draw.

    Exists so "what am I about to use up?" is answered before the print dialog
    rather than inferred from a label count afterwards.

    `sheets` holds only the ones worth looking at — the partly-used sheet you
    start on and the one you finish on. A run of 2435 labels covers 31 sheets,
    29 of which are simply full, and drawing 29 identical grids buries the two
    that carry information. The rest are counted in `full_sheets` and said in
    a sentence.
    """
    sheets: list          # per sheet: {"number": n, "cells": [...]}
    sheet_count: int
    resume_at: int        # 1-indexed label the next run should start on
    marker_index: int     # 0-indexed absolute position of the marker, or None
    free_after: int       # unused labels left on the final sheet
    finishes_sheet: bool
    full_sheets: int = 0  # sheets omitted from `sheets` as entirely used
    full_from: int = 0    # 1-indexed range of those omitted sheets
    full_to: int = 0


def marker_index_for(stock, count, start_at=0):
    """Where the "resume here" sticker goes: straight after the last label.

    Returns None when the run ends exactly on a sheet boundary. There'd be
    nowhere to put the marker but the first label of a fresh sheet, and a
    fresh sheet needs no marker — it starts at 1, which is the one case
    nobody can get wrong.

    Also None on a continuous roll, where a marker would print after every
    run, be read by nobody, and cost a label each time — the saving it exists
    for only appears when there's a part-used sheet to come back to.
    """
    if count <= 0 or stock.is_continuous:
        return None
    after = start_at + count
    if after % stock.labels_per_sheet == 0:
        return None
    return after


def plan_sheets(stock, count, start_at=0, blanks=frozenset()) -> SheetPlan:
    """Lay the run out over sheets, including the marker sticker.

    `count` is sheet positions consumed, not stickers printed: `blanks` holds
    the offsets within the run that are row-break padding.
    """
    per_sheet = stock.labels_per_sheet
    if count <= 0:
        return SheetPlan([], 0, start_at + 1, None, per_sheet - start_at, False)

    marker = marker_index_for(stock, count, start_at)
    last_index = (marker if marker is not None else start_at + count - 1)
    sheet_count = last_index // per_sheet + 1

    first_printed, last_printed = start_at, start_at + count - 1

    def is_full(page):
        """Every label on this sheet gets printed on — nothing to look at.

        The marker check is belt-and-braces: a marker sits immediately after
        the last label, so its sheet always has an unprinted cell and fails
        the range test anyway. Stated explicitly because "the sheet telling
        you where to resume is never hidden" is the rule, not a side effect.
        """
        lo, hi = page * per_sheet, (page + 1) * per_sheet - 1
        if marker is not None and marker // per_sheet == page:
            return False
        return first_printed <= lo and last_printed >= hi

    def cells_for(page):
        cells = []
        for within in range(per_sheet):
            index = page * per_sheet + within
            if first_printed <= index <= last_printed:
                state = "skipped" if (index - start_at) in blanks else "printing"
            elif index == marker:
                state = "marker"
            elif index < start_at:
                state = "used"
            else:
                state = "free"
            cells.append({"number": within + 1, "state": state})
        return cells

    # Only the sheets that differ from "all 80 used" are built at all; the
    # rest would be identical grids and aren't worth the markup or the scroll.
    drawn, full_pages = [], []
    for page in range(sheet_count):
        if is_full(page):
            full_pages.append(page + 1)
        else:
            drawn.append({"number": page + 1, "cells": cells_for(page)})

    return SheetPlan(
        sheets=drawn,
        sheet_count=sheet_count,
        resume_at=1 if marker is None else marker % per_sheet + 2,
        marker_index=marker,
        free_after=0 if marker is None else per_sheet - (marker % per_sheet) - 1,
        finishes_sheet=marker is None,
        full_sheets=len(full_pages),
        full_from=full_pages[0] if full_pages else 0,
        full_to=full_pages[-1] if full_pages else 0,
    )


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def barcode_for(sku, stock):
    """A Code128 barcode sized to this stock, and its module width in mils.

    reportlab computes the symbol width for us including quiet zones, so the
    scale factor is just "how many points of label per module".

    """
    from reportlab.graphics.barcode import code128

    avail_w = _pt(Decimal(str(stock.label_width_in)) - Decimal(str(PAD_IN)) * 2)
    avail_h = _pt(Decimal(str(stock.label_height_in)) - Decimal(str(PAD_IN)) * 2)
    bar_h = max(avail_h - TEXT_PT - 1.5, 1.0)

    modules = code128.Code128(sku, barHeight=bar_h, barWidth=1.0).width
    bar_width = avail_w / modules
    mil = bar_width / POINTS_PER_INCH * 1000

    barcode = code128.Code128(
        sku, barHeight=bar_h, barWidth=bar_width, humanReadable=False
    )
    return barcode, mil


def density_problems(run, stock):
    """SKUs in this run whose bars would come out under `MIN_MODULE_MIL`.

    Checked before rendering so the answer is "these three SKUs are too long
    for this stock" rather than eighty stickers nothing will scan.

    **A run that prints no bars has no such problem**, and must not be refused
    for one. The guard exists because unscannable bars fail silently at the
    till; a sticker reading "Stormy Sea" has nothing to fail. Leaving this
    unchecked would have blocked exactly the style invented to cope with a
    stock too narrow for the SKU — refusing the fix on the grounds of the
    thing it fixes.
    """
    if not run.needs_barcode:
        return []

    problems = []
    for sku in sorted({r.product.sku for r in run.rows}):
        _, mil = barcode_for(sku, stock)
        if mil < MIN_MODULE_MIL:
            problems.append((sku, mil))
    return problems


def render_run(run, stock, start_at=0) -> bytes:
    """The sheet(s) of stickers, as PDF bytes, ending in a marker sticker.

    The marker is the whole answer to resuming a part-used sheet. A weekly run
    of 60–100 labels leaves one nearly every time, and a sheet nobody can
    confidently resume gets binned — which reads as waste whatever it cost.
    Writing the number on in pen works right up until someone doesn't; a
    sticker that says where to start next is printed by the same job that
    created the situation, sits on the sheet it describes, and is read by
    whoever picks that sheet up regardless of which laptop or browser they
    printed from.

    It costs exactly one label per run, which is less than the rounding waste
    of the row-aligned alternative it replaced, and it buys single-label
    offsets on top.
    """
    from reportlab.pdfgen import canvas

    page_size = (_pt(stock.page_width_in), _pt(stock.page_height_in))
    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=page_size)
    pdf.setTitle(f"Barcode labels — {stock.name}")

    sequence = run.sequence(stock.columns)
    positions = slots(stock, len(sequence), start_at)

    # (slot, draw) pairs in page order, marker last. Gaps consume a position
    # and draw nothing, which is what leaves a row start free for the next
    # blank.
    work = [(slot, lambda p, s, fp=fp: _draw_label(p, fp, s, stock, run.style))
            for fp, slot in zip(sequence, positions) if fp is not None]

    marker = marker_index_for(stock, len(sequence), start_at)
    if marker is not None:
        resume_at = marker % stock.labels_per_sheet + 2
        work.append((
            slot_for(stock, marker),
            lambda p, s: _draw_marker(p, s, stock, resume_at),
        ))

    current_page = 0
    for slot, draw in work:
        if slot.page != current_page:
            _draw_scale_check(pdf, stock)   # every page carries its own check
            pdf.showPage()
            current_page = slot.page
        draw(pdf, slot)

    if sequence:
        _draw_scale_check(pdf, stock)
        pdf.showPage()
    pdf.save()
    buf.seek(0)
    return buf.read()


def _wrap_to_fit(pdf, text, max_w, max_pt, min_pt, max_lines=2):
    """Largest size at which `text` fits `max_w` in at most `max_lines`.

    Returns `(size, lines)`, and always returns something: at `min_pt` the
    text is truncated rather than allowed to run off the sticker. A colorway
    that spills over the die-cut is worse than one shortened, because the
    overflow lands on the *next* label and mislabels a second scarf.
    """
    words = text.split()
    size = max_pt
    while size >= min_pt:
        lines, current = [], ""
        for word in words:
            trial = f"{current} {word}".strip()
            if pdf.stringWidth(trial, "Helvetica-Bold", size) <= max_w or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        if len(lines) <= max_lines and all(
            pdf.stringWidth(ln, "Helvetica-Bold", size) <= max_w for ln in lines
        ):
            return size, lines
        size -= 0.5

    # Floor reached: keep the size legible and cut the text instead.
    lines, current = [], text
    while current and pdf.stringWidth(current, "Helvetica-Bold", min_pt) > max_w:
        current = current[:-1]
    return min_pt, [current]


def _draw_name(pdf, product, slot, stock, top, height):
    """The colorway, centred in `height` points above `slot.y + top`."""
    pad = _pt(PAD_IN)
    label_w = _pt(stock.label_width_in)
    max_w = label_w - pad * 2

    size, lines = _wrap_to_fit(
        pdf, product.variation_name, max_w,
        max_pt=min(NAME_MAX_PT, height), min_pt=NAME_MIN_PT,
    )
    leading = size * 1.1
    block_h = leading * len(lines)
    # Centre the block in its band, then walk down from its top line.
    y = slot.y + top + (height - block_h) / 2 + block_h - size
    pdf.setFont("Helvetica-Bold", size)
    for line in lines:
        pdf.drawCentredString(slot.x + label_w / 2, y, line)
        y -= leading


def _draw_label(pdf, product, slot, stock, style=BARCODE):
    """One sticker, in whichever of the three styles the run asked for.

    The split is here and nowhere else on purpose: everything upstream —
    dataset, extras, SKU sort, sheet plan, marker placement, registration
    ticks, offsets — is identical whatever ends up inside the die-cut. A style
    is a flavour of one label, not a second label system.
    """
    pad = _pt(PAD_IN)
    label_w = _pt(stock.label_width_in)
    label_h = _pt(stock.label_height_in)
    avail_h = label_h - pad * 2

    if style == NAME:
        # Nothing to scan, so the whole sticker is the colorway.
        _draw_name(pdf, product, slot, stock, top=pad, height=avail_h)
        return

    barcode, _ = barcode_for(product.sku, stock)

    # Centre the symbol: reportlab's quiet zones are generous, so the drawn
    # width is usually a shade under the space available.
    x = slot.x + (label_w - barcode.width) / 2
    barcode.drawOn(pdf, x, slot.y + pad + TEXT_PT)

    pdf.setFont("Helvetica", TEXT_PT)
    pdf.drawCentredString(slot.x + label_w / 2, slot.y + pad - 0.5, product.sku)


def _draw_marker(pdf, slot, stock, resume_at):
    """The "start here next time" sticker.

    Dated, because a sheet used over several weeks accumulates one of these
    per run and they sit in reading order — the date is what says which one is
    current without having to reason about position.
    """
    from reportlab.lib import colors

    label_w = _pt(stock.label_width_in)
    label_h = _pt(stock.label_height_in)
    centre = slot.x + label_w / 2

    pdf.setStrokeColor(colors.black)
    pdf.setLineWidth(0.7)
    pdf.rect(slot.x + 1, slot.y + 1, label_w - 2, label_h - 2)

    pdf.setFillColor(colors.black)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawCentredString(centre, slot.y + label_h - 12, f"NEXT RUN: START AT {resume_at}")
    pdf.setFont("Helvetica", 6)
    pdf.drawCentredString(
        centre, slot.y + 5,
        f"printed {timezone.localdate():%d %b %Y} · peel me last",
    )


def _draw_scale_check(pdf, stock):
    """Registration ticks down the left margin, one per row, on every page.

    "Fit to page" is a common print-dialog default and it takes a few percent
    off. That failure is *progressive*: at 98% the first row is out by a
    twentieth of a millimetre and the twentieth row is out by five, cut clean
    through the labels. So it is invisible at the top of the sheet and ruinous
    at the bottom, and the check has to span the whole length to catch it.

    A one-inch ruler doesn't: 98% of an inch is half a millimetre, which
    nobody reads reliably off a rule. The die-cuts do — there are twenty of
    them spread over ten inches, already positioned to a tolerance better than
    anything being diagnosed here. Printing a tick beside each row turns the
    sheet into its own measuring instrument, and the two failure modes come
    apart by eye:

        ticks drift further off as you go down  -> the print was scaled
        ticks are all off by the same amount    -> registration, use a nudge

    Ticks land in the left margin and the caption on the liner below the last
    row, so this costs no labels. Skipped on rolls and on any stock without
    the margin to hold it.
    """
    from reportlab.lib import colors

    if stock.is_continuous:
        return

    used = (
        float(stock.margin_top_in)
        + (stock.rows - 1) * float(stock.pitch_y_in)
        + float(stock.label_height_in)
    )
    bottom_free = float(stock.page_height_in) - used
    left_free = float(stock.margin_left_in)
    if bottom_free < 0.35 or left_free < 0.18:
        return

    pdf.setStrokeColor(colors.HexColor("#888888"))
    pdf.setFillColor(colors.HexColor("#666666"))
    pdf.setLineWidth(0.5)

    # One tick per row, aligned to that row's top die-cut edge.
    tick_out = min(_pt(left_free) - 4, 12)
    for row in range(stock.rows):
        slot = slot_for(stock, row * stock.columns)
        y = slot.y + _pt(stock.label_height_in)
        pdf.line(3, y, 3 + tick_out, y)

    # A rule joining them, so a drift reads as a taper rather than needing the
    # ticks compared one at a time.
    top = slot_for(stock, 0).y + _pt(stock.label_height_in)
    bottom = slot_for(stock, (stock.rows - 1) * stock.columns).y
    pdf.setLineWidth(0.3)
    pdf.line(3, top, 3, bottom)

    pdf.setFont("Helvetica", 6)
    pdf.drawString(
        _pt(stock.margin_left_in), _pt(bottom_free / 2) - 3,
        f"Every tick in the left margin must line up with a row of die-cuts. "
        f"Drifting further off toward the bottom = the print was scaled, "
        f"don't use it.   ·   {stock.name}   ·   "
        f"nudge {stock.x_offset_mm}/{stock.y_offset_mm} mm   ·   "
        f"{timezone.localdate():%d %b %Y}",
    )


def _draw_orientation_banner(pdf, stock):
    """Which edge the printer treated as the top, stated unmissably.

    This is the *first* calibration question and it comes before millimetres:
    a sheet can go into a bypass tray four ways, and three of them are wrong.
    The vernier below can't help until this is settled — a 180° error reads as
    a plausible-looking offset and sends you chasing a nudge that will never
    converge.

    The procedure this supports: write TOP and a feed-direction arrow on a
    blank sheet in pen, run it through, then compare your mark against this
    banner. Wherever the banner landed is where the printer thinks the top is.
    """
    from reportlab.lib import colors

    top_free = float(stock.margin_top_in)
    if top_free < 0.3:
        return

    page_w = _pt(stock.page_width_in)
    page_h = _pt(stock.page_height_in)
    y = page_h - _pt(top_free) / 2

    pdf.setFillColor(colors.HexColor("#b03030"))
    pdf.setStrokeColor(colors.HexColor("#b03030"))

    # Triangles rather than an arrow glyph: the base-14 fonts don't carry one,
    # and a missing character here is a silently blank orientation marker.
    for direction in (-1, 1):
        cx = page_w / 2 + direction * 78
        path = pdf.beginPath()
        path.moveTo(cx, y + 5)
        path.lineTo(cx - 5, y - 3)
        path.lineTo(cx + 5, y - 3)
        path.close()
        pdf.drawPath(path, fill=1, stroke=0)

    pdf.setFont("Helvetica-Bold", 11)
    # "of print", not "of sheet": a sheet has no top until someone puts it in
    # a tray. What this marks is where the printer put the top of its output,
    # which is the thing you compare your pen mark against to work out which
    # way the tray wants it.
    pdf.drawCentredString(page_w / 2, y - 4, "TOP OF PRINT")
    pdf.setFont("Helvetica", 6)
    pdf.drawCentredString(
        page_w / 2, y - 13,
        "Write TOP and a feed arrow on a blank sheet, run it through, and "
        "compare — that tells you which way the tray wants it.",
    )


def _draw_vernier(pdf, stock):
    """Millimetre scales through the first label's top-left corner.

    Turns dialling the offset in from a loop into one measurement. Without it
    the procedure is print, eyeball, guess a nudge, print again — and when the
    printing happens at a shop, every lap costs a trip. With it: hold this
    page on top of a label sheet against a light, see where the die-cut corner
    falls on the scale, type those two numbers in, done.

    Numbers read as the nudge to *enter*, not as the error observed, so
    there's no sign to get backwards at the point where getting it backwards
    doubles the error.
    """
    from reportlab.lib import colors

    slot = slot_for(stock, 0)
    ox, oy = slot.x, slot.y + _pt(stock.label_height_in)

    pdf.setStrokeColor(colors.HexColor("#b03030"))
    pdf.setFillColor(colors.HexColor("#b03030"))
    pdf.setFont("Helvetica", 4.5)

    for mm in range(-6, 7):          # half-millimetre steps, ±3mm
        d = _mm_pt(mm / 2)
        whole = mm % 2 == 0
        length = 7 if whole else 3.5
        pdf.setLineWidth(0.4 if whole else 0.25)
        pdf.line(ox + d, oy + 2, ox + d, oy + 2 + length)          # horizontal scale
        pdf.line(ox - 2, oy + d, ox - 2 - length, oy + d)          # vertical scale
        if whole and mm:
            pdf.drawCentredString(ox + d, oy + 2 + length + 1.5, f"{mm // 2:+d}")
            pdf.drawRightString(ox - 2 - length - 1.5, oy + d - 1.5, f"{mm // 2:+d}")

    # Which scale is which axis has to be on the sheet. Working it out from
    # the geometry is possible and takes a moment nobody has at a print
    # counter, and guessing the sign doubles the error being corrected.
    pdf.setFont("Helvetica-Bold", 5.5)
    pdf.drawString(
        ox + _mm_pt(4), oy + 14,
        "Lay over a label sheet against a light; read the die-cut corner off "
        "these scales.",
    )
    pdf.setFont("Helvetica", 5.5)
    pdf.drawString(
        ox + _mm_pt(4), oy + 7,
        "TOP scale = \"nudge right\" mm.   LEFT scale = \"nudge up\" mm.   "
        "Type both in exactly as read — no sign to flip.",
    )


def render_calibration(stock) -> bytes:
    """An outline at every label position and nothing else.

    Print on plain paper, hold it against a label sheet, and you can see
    whether the geometry and the printer's feed agree — for the price of a
    sheet of copy paper rather than a sheet of labels. The ruler in the bottom
    margin catches the other classic failure: a print dialog left on "fit to
    page", which shrinks everything a few percent and is invisible until you
    measure it.
    """
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors

    page_w, page_h = _pt(stock.page_width_in), _pt(stock.page_height_in)
    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(page_w, page_h))
    pdf.setTitle(f"Calibration — {stock.name}")

    pdf.setStrokeColor(colors.HexColor("#1558a0"))
    pdf.setFillColor(colors.HexColor("#1558a0"))
    pdf.setLineWidth(0.4)
    pdf.setFont("Helvetica", 6)
    for index in range(stock.labels_per_sheet):
        slot = slot_for(stock, index)
        pdf.rect(slot.x, slot.y, _pt(stock.label_width_in), _pt(stock.label_height_in))
        # Numbered in place, so this doubles as the map for reading a marker
        # sticker: hold it against a part-used sheet and label 57 is label 57.
        pdf.drawCentredString(
            slot.x + _pt(stock.label_width_in) / 2,
            slot.y + _pt(stock.label_height_in) / 2 - 2,
            str(index + 1),
        )

    _draw_orientation_banner(pdf, stock)
    _draw_vernier(pdf, stock)

    # Bottom margin is free on every stock we care about (the last row ends
    # above it), so the scale check and the caption go there.
    pdf.setFont("Helvetica", 7)
    pdf.setFillColor(colors.HexColor("#333333"))
    baseline = 18
    pdf.drawString(
        _pt(stock.margin_left_in), baseline + 12,
        f"{stock.name} — rows numbered at left. "
        f"Print at 100% (no 'fit to page'), then measure the bar below.",
    )
    ruler_x = _pt(stock.margin_left_in)
    pdf.setLineWidth(0.8)
    pdf.line(ruler_x, baseline, ruler_x + POINTS_PER_INCH, baseline)
    for tick in (0, POINTS_PER_INCH):
        pdf.line(ruler_x + tick, baseline - 3, ruler_x + tick, baseline + 3)
    pdf.setFont("Helvetica", 6)
    pdf.drawString(ruler_x + POINTS_PER_INCH + 5, baseline - 2, "this must measure exactly 1 inch")

    pdf.showPage()
    pdf.save()
    buf.seek(0)
    return buf.read()
