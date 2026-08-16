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
prints its own one-inch ruler — the scaled-print failure is otherwise
undetectable until the labels are already paid for.

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

    @property
    def total(self) -> int:
        return sum(r.quantity for r in self.rows)

    def flat(self):
        """One entry per physical sticker, in sheet order."""
        for row in self.rows:
            for _ in range(row.quantity):
                yield row.product


def _finish(products_and_counts, extra):
    """Shared tail of both datasets: drop what can't be printed, apply the
    extras, sort by SKU.

    Sorting by SKU is what groups a bath's 3–5 identical items into one
    contiguous clump on the sheet, matching the pile they're going onto.
    """
    rows, skipped = [], []
    for product, base in products_and_counts:
        if not product.sku:
            skipped.append(product)
            continue
        rows.append(LabelRow(product=product, base=base, quantity=base + extra))
    rows = [r for r in rows if r.quantity > 0]
    rows.sort(key=lambda r: r.product.sku)
    return rows, sorted(skipped, key=lambda p: p.name)


def inventory_run(extra=0, category=None, raw_product=None, include_zero=False):
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
    if raw_product:
        qs = qs.filter(raw_product=raw_product)
    if not include_zero:
        qs = qs.filter(number_on_hand__gt=0)

    rows, skipped = _finish(((p, p.number_on_hand) for p in qs), extra)
    return LabelRun(rows=rows, skipped_no_sku=skipped, ambiguous_month_logs=0)


def produced_since(cutoff, extra=0):
    """One sticker per unit produced at or after `cutoff`.

    Sums production log quantities rather than reading `number_on_hand`, so
    what already sold still gets a sticker — the items exist, they just aren't
    on the shelf by the time anyone prints. Negative quantities (corrections)
    net out, and a product netting zero or less drops off entirely.

    Back-dated entries take care of themselves: a kanban backfill carries
    `created_at` set to the date on the card, not the day it was typed, so a
    2024 card entered this morning correctly does not ask for stickers.
    """
    cutoff_dt = _as_datetime(cutoff)
    totals = (
        InventoryLog.objects.filter(
            log_type=InventoryLog.PRODUCTION,
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

    rows, skipped = _finish(((p, max(counts[p.pk], 0)) for p in products), extra)
    return LabelRun(
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
    it. The page reports the count instead of silently dropping them, which is
    the same bargain the rest of the app makes with these rows: say only what
    is actually known.

    Realistic cutoffs are recent and old backfills are irrelevant, so this is
    almost always zero — it just shouldn't be *silently* nonzero.
    """
    month_start = cutoff_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start == cutoff_dt:
        return 0
    return InventoryLog.objects.filter(
        log_type=InventoryLog.PRODUCTION,
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
    """
    sheets: list          # per sheet: {"number": n, "cells": [...]}
    sheet_count: int
    resume_at: int        # 1-indexed label the next run should start on
    marker_index: int     # 0-indexed absolute position of the marker, or None
    free_after: int       # unused labels left on the final sheet
    finishes_sheet: bool


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


def plan_sheets(stock, count, start_at=0) -> SheetPlan:
    """Lay the run out over sheets, including the marker sticker."""
    per_sheet = stock.labels_per_sheet
    if count <= 0:
        return SheetPlan([], 0, start_at + 1, None, per_sheet - start_at, False)

    marker = marker_index_for(stock, count, start_at)
    last_index = (marker if marker is not None else start_at + count - 1)
    sheet_count = last_index // per_sheet + 1

    printing = set(range(start_at, start_at + count))
    sheets = []
    for page in range(sheet_count):
        cells = []
        for within in range(per_sheet):
            index = page * per_sheet + within
            if index in printing:
                state = "printing"
            elif index == marker:
                state = "marker"
            elif index < start_at:
                state = "used"
            else:
                state = "free"
            cells.append({"number": within + 1, "state": state})
        sheets.append({"number": page + 1, "cells": cells})

    return SheetPlan(
        sheets=sheets,
        sheet_count=sheet_count,
        resume_at=1 if marker is None else marker % per_sheet + 2,
        marker_index=marker,
        free_after=0 if marker is None else per_sheet - (marker % per_sheet) - 1,
        finishes_sheet=marker is None,
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
    """
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

    products = list(run.flat())
    positions = slots(stock, len(products), start_at)

    # (slot, draw) pairs in page order, marker last.
    work = [(slot, lambda p, s, fp=fp: _draw_label(p, fp, s, stock))
            for fp, slot in zip(products, positions)]

    marker = marker_index_for(stock, len(products), start_at)
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

    if work:
        _draw_scale_check(pdf, stock)
        pdf.showPage()
    pdf.save()
    buf.seek(0)
    return buf.read()


def _draw_label(pdf, product, slot, stock):
    barcode, _ = barcode_for(product.sku, stock)
    pad = _pt(PAD_IN)
    label_w = _pt(stock.label_width_in)

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
    pdf.drawCentredString(page_w / 2, y - 4, "TOP OF SHEET")
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

    pdf.setFont("Helvetica", 5.5)
    pdf.drawString(
        ox + _mm_pt(4), oy + 10,
        "lay this over a label sheet against a light; read the die-cut corner "
        "off these scales and enter those two numbers as the nudge",
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
