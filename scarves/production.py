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
from io import BytesIO
from math import ceil

from django.db.models import F, Value
from django.db.models.functions import Greatest

from .models import FinishedProduct

#: Page furniture, in points (72 to the inch). Plain paper, so unlike the
#: label stock none of this has to line up with anything physical.
PAGE_MARGIN = 40
HEADER_HEIGHT = 96
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


def candidates(category=None, include_overshoot=False):
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
        )
        .select_related("raw_product", "raw_product__category", "recipe")
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

    return sorted(qs, key=_urgency)


def _urgency(product):
    """Empty shelf first, then biggest shortfall, then a stable name.

    Out of stock leads because it is the only state a customer can see: a
    colorway at zero is missing from the table, where one at half par is just
    a shorter stack.
    """
    return (
        product.number_on_hand > 0,
        -product.shortage,
        product.name,
    )


def plan_baths(limit, category=None, include_overshoot=False):
    """The next `limit` baths, grouped so consecutive rows share a dye pot.

    Baths of the same recipe sit together because that is how the work is
    actually cheaper: one mix, one pot, one temperature, several loads. The
    order *between* recipes is urgency; the order within one is just the
    products that need it.

    A recipe can be cut in half by the limit, and that is fine — the sheet
    was asked for a number of baths and it delivers exactly that number.
    """
    by_recipe = {}
    for product in candidates(category, include_overshoot):
        needed = ceil(product.shortage / product.bath_size)
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


def apply_row(row):
    """Move the stock one finished bath represents, once.

    Returns the `InventoryLog` written, or the existing one if this row has
    already been applied. **Applying twice is the failure this guards**: the
    return URL is a piece of paper that can be scanned again, the submit
    button can be double-tapped, and a crew member who remembers one more
    bath will re-open the same page and submit again. All three are normal,
    and all three used to be how a bath gets counted into stock twice — the
    same shape as the Square webhook and redelivered orders.

    Un-ticking is deliberately not the inverse of this. Once stock has moved
    the correction is an inventory adjustment with a reason attached, not a
    checkbox quietly going out again on a page with no login.
    """
    from .models import InventoryLog

    if row.applied_log_id is not None:
        return row.applied_log

    product = row.finished_product
    raw = product.raw_product

    raw.number_on_hand = max(raw.number_on_hand - row.quantity, 0)
    raw.save(update_fields=["number_on_hand"])

    product.number_on_hand += row.quantity
    product.save(update_fields=["number_on_hand"])

    log = InventoryLog.objects.create(
        finished_product=product,
        raw_product=raw,
        log_type=InventoryLog.PRODUCTION,
        quantity=row.quantity,
        notes=f"Dye bath reported from production sheet run {row.run_id}.",
    )
    row.applied_log = log
    row.save(update_fields=["applied_log"])
    return log


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def render_sheet(run, return_url) -> bytes:
    """The sheet, as PDF bytes.

    Every page carries the QR and the run number, not just the first. Sheets
    get split, stapled, and left on benches, and a page that can't say which
    run it belongs to is a page whose ticks can't be reported.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    page_w, page_h = letter
    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=letter)
    pdf.setTitle(f"Production sheet — run {run.pk}")

    rows = list(run.rows.select_related(
        "finished_product__recipe", "finished_product__raw_product"
    ))
    per_page = int((page_h - PAGE_MARGIN * 2 - HEADER_HEIGHT) // ROW_HEIGHT)
    per_page = max(per_page, 1)

    for start in range(0, max(len(rows), 1), per_page):
        page_rows = rows[start:start + per_page]
        _draw_header(pdf, run, return_url, page_w, page_h,
                     page_no=start // per_page + 1,
                     page_count=max(ceil(len(rows) / per_page), 1))
        y = page_h - PAGE_MARGIN - HEADER_HEIGHT
        for index, row in enumerate(page_rows):
            _draw_row(pdf, row, start + index + 1, y, page_w)
            y -= ROW_HEIGHT
        pdf.showPage()

    pdf.save()
    buf.seek(0)
    return buf.read()


def _draw_header(pdf, run, return_url, page_w, page_h, page_no, page_count):
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode import qr
    from reportlab.graphics.shapes import Drawing

    top = page_h - PAGE_MARGIN

    pdf.setFont("Helvetica-Bold", 17)
    pdf.drawString(PAGE_MARGIN, top - 14, f"Production sheet — run {run.pk}")

    total = run.rows.count()
    pdf.setFont("Helvetica", 10)
    pdf.drawString(
        PAGE_MARGIN, top - 30,
        f"{total} bath{'' if total == 1 else 's'}"
        f" · printed {run.created_at:%d %b %Y}"
        f" · page {page_no} of {page_count}",
    )
    pdf.drawString(
        PAGE_MARGIN, top - 44,
        "Fill in the box for every bath you finish. Leave the rest blank.",
    )
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(
        PAGE_MARGIN, top - 58,
        "Scan the code when you're done and tap the ones you filled in.",
    )

    # The URL in plain text under the code, because the QR is the convenience
    # and the paper is the record. A cracked camera or a dead phone shouldn't
    # be the reason a session goes unreported.
    widget = qr.QrCodeWidget(return_url, barLevel="M")
    bounds = widget.getBounds()
    drawing = Drawing(QR_SIZE, QR_SIZE, transform=[
        QR_SIZE / (bounds[2] - bounds[0]), 0, 0,
        QR_SIZE / (bounds[3] - bounds[1]), 0, 0,
    ])
    drawing.add(widget)
    renderPDF.draw(drawing, pdf, page_w - PAGE_MARGIN - QR_SIZE, top - QR_SIZE)

    pdf.setFont("Helvetica", 6.5)
    pdf.drawRightString(page_w - PAGE_MARGIN, top - QR_SIZE - 9, return_url)


def _draw_row(pdf, row, number, y, page_w):
    from reportlab.graphics.barcode import code128

    product = row.finished_product
    baseline = y - ROW_HEIGHT + 12

    # The box. Heavy stroke so a photo of it has something unambiguous to
    # measure against, and empty inside so "filled in" is the only ink there.
    pdf.setLineWidth(1.6)
    pdf.rect(BOX_LEFT, baseline - 4, BOX_SIZE, BOX_SIZE)
    pdf.setLineWidth(1)

    barcode_x = BOX_LEFT + BOX_SIZE + BOX_TO_BARCODE
    if product.sku:
        symbol = code128.Code128(
            product.sku, barHeight=BARCODE_HEIGHT, barWidth=1.0,
            humanReadable=False,
        )
        scale = BARCODE_WIDTH / symbol.width
        symbol = code128.Code128(
            product.sku, barHeight=BARCODE_HEIGHT, barWidth=scale,
            humanReadable=False,
        )
        symbol.drawOn(pdf, barcode_x, baseline + 2)

    text_x = barcode_x + BARCODE_WIDTH + 14

    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(text_x, baseline + 14, f"{row.quantity} × {product.recipe.name}")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(
        text_x, baseline + 2,
        f"{product.raw_product.name} · {product.sku or 'no SKU'} · "
        f"{product.number_on_hand} on hand, par {product.par}",
    )

    pdf.setFont("Helvetica", 8)
    pdf.drawRightString(page_w - PAGE_MARGIN, baseline + 2, f"#{number}")

    pdf.setStrokeGray(0.8)
    pdf.line(PAGE_MARGIN, baseline - 12, page_w - PAGE_MARGIN, baseline - 12)
    pdf.setStrokeGray(0)
