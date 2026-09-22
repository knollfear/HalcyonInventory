"""Reference sheets: the printed catalogue, per category and by colour."""
from io import BytesIO

from django.http import HttpResponse
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.shortcuts import render

from ..sitemap import page_meta
from ..models import FinishedProduct, RawProductCategory
from .. import colorbands
from ..models import Recipe


@page_meta(
    title="Reference Sheets",
    description="Pick a category to generate a printable barcode/SKU reference "
                "sheet (PDF) — one portrait page per recipe with photos and "
                "per-item barcode cards.",
    category="Reference Sheets",
)
def reference_sheet_index(request):
    """Category picker for the reference-sheet PDFs.

    Public, like the sheets themselves: the contents are product photos,
    names and barcodes — the same things printed on the stall table.

    Carries the same kind of at-a-glance counts as `raw_inventory_index`, so
    the page says what you'd get before you wait on a PDF build: one page per
    recipe, one barcode card per item, and how many items would print without a
    photo — the sheet's whole point is matching a photo to a barcode.
    """
    printable = Q(
        raw_products__is_active=True,
        raw_products__finished_products__is_active=True,
        raw_products__finished_products__recipe__is_active=True,
    )
    categories = list(
        RawProductCategory.objects.annotate(
            recipe_count=Count(
                "raw_products__finished_products__recipe",
                filter=printable,
                distinct=True,
            ),
            item_count=Count(
                "raw_products__finished_products",
                filter=printable,
                distinct=True,
            ),
        )
        .filter(recipe_count__gt=0)
        .order_by("name")
    )

    # Only an uploaded file can be embedded in the PDF, so an external
    # image_url doesn't count as having a photo (see _select_recipe_photos).
    photoless = dict(
        FinishedProduct.objects.dyed()
        .filter(raw_product__is_active=True)
        .exclude(images__image__gt="")
        .values("raw_product__category")
        .annotate(n=Count("pk", distinct=True))
        .values_list("raw_product__category", "n")
    )
    for category in categories:
        category.photoless = photoless.get(category.pk, 0)
        # The same category, two orderings. Counted here rather than left to
        # the click because the by-colour sheet is longer than the by-name one
        # (a colorway prints once per section it claims) and skips whatever
        # nobody has classified — both are things to know before printing.
        category.band_pages, category.unclassified = _by_color_counts(category)

    return render(request, "scarves/reference_sheet_index.html", {"categories": categories})


def _image_flowable(fpi, max_w, max_h):
    """A ReportLab Image of a FinishedProductImage's uploaded file, scaled to
    fit max_w x max_h (preserving aspect), or None if there's no usable file.

    The source is downscaled with PIL first. Uploads are already capped at
    IMAGE_MAX_EDGE, so for anything shot since that change this is a no-op —
    but it stays as the backstop that made the PDF viable in the first place:
    at full phone resolution reportlab's base85 encode was slow enough to
    threaten the gunicorn timeout, and it bloated the file. Externally-sourced
    images still arrive at whatever size they like."""
    from reportlab.platypus import Image as RLImage
    from PIL import Image as PILImage

    if not fpi or not fpi.image:
        return None
    try:
        with fpi.image.open("rb") as f:
            raw = f.read()
        im = PILImage.open(BytesIO(raw))
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail((1400, 1400), PILImage.LANCZOS)
        out = BytesIO()
        im.save(out, format="JPEG", quality=80)
        out.seek(0)
        iw, ih = im.size
    except Exception:
        return None
    if not iw or not ih:
        return None
    ratio = min(max_w / iw, max_h / ih)
    return RLImage(out, width=iw * ratio, height=ih * ratio)


def _select_recipe_photos(items, cap=4):
    """One uploaded photo per item first (item order), then fill remaining
    slots with each item's additional photos, up to `cap`. Only counts images
    that have an uploaded file (external image_url can't be embedded)."""
    per_item = [[img for img in fp.images.all() if img.image] for fp in items]
    selected = []
    for imgs in per_item:            # first photo of each item
        if len(selected) >= cap:
            break
        if imgs:
            selected.append(imgs[0])
    idx = 1                          # then fill from additional photos
    while len(selected) < cap:
        added = False
        for imgs in per_item:
            if len(selected) >= cap:
                break
            if len(imgs) > idx:
                selected.append(imgs[idx])
                added = True
        if not added:
            break
        idx += 1
    return selected[:cap]


def _photo_gallery(photos, usable_width, area_h):
    """Stack photos vertically, each spanning the full page width, sharing the
    vertical space `area_h`. Full-width best fits landscape photos on a portrait
    page (least whitespace). Returns a flowable (Table) or None."""
    from reportlab.platypus import Table, TableStyle, Spacer

    n = len(photos)
    if n == 0 or area_h <= 0:
        return None

    per_h = area_h / n
    rows = [[_image_flowable(p, usable_width, per_h) or Spacer(1, 1)] for p in photos]
    t = Table(rows, colWidths=[usable_width], rowHeights=[per_h] * n)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _barcode_card(fp, card_w, name_style, sku_style):
    """A bordered card: item (raw-product) name, Code128 barcode, and SKU."""
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import Table, TableStyle, Paragraph, Spacer
    from reportlab.graphics.barcode import code128

    inner = [Paragraph(fp.raw_product.name, name_style), Spacer(1, 0.06 * inch)]
    if fp.sku:
        inner.append(code128.Code128(fp.sku, barHeight=0.45 * inch, barWidth=0.62))
        inner.append(Spacer(1, 0.03 * inch))
        inner.append(Paragraph(fp.sku, sku_style))
    else:
        inner.append(Paragraph(f"${fp.price} (no SKU)", sku_style))

    card = Table([[inner]], colWidths=[card_w])
    card.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#9cbce0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return card


def _barcode_grid(items, usable_width, name_style, sku_style):
    """2-across grid of barcode cards for all items sharing the recipe."""
    from reportlab.lib.units import inch
    from reportlab.platypus import Table, TableStyle

    gap = 0.25 * inch
    card_w = (usable_width - gap) / 2
    cards = [_barcode_card(fp, card_w, name_style, sku_style) for fp in items]
    rows = []
    for i in range(0, len(cards), 2):
        pair = cards[i:i + 2]
        if len(pair) == 1:
            pair.append("")
        rows.append(pair)
    grid = Table(rows, colWidths=[card_w, card_w], hAlign="CENTER")
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return grid


@page_meta(
    title="Reference Sheet PDF",
    description="Generates a printable PDF reference sheet for one category: "
                "one portrait page per recipe, with product photos and a "
                "Code128 barcode card for every item sharing that recipe.",
    category="Reference Sheets",
    note="Returns a PDF.",
    # Reached from the "Reference Sheets" picker, same reasoning as
    # raw_inventory_view: a route needing a category id is a dead card here.
    show_in_index=False,
)
def reference_sheet_pdf(request, category_id):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, portrait
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, KeepInFrame

    category = get_object_or_404(RawProductCategory, pk=category_id)

    recipes = (
        Recipe.objects.filter(
            finished_products__raw_product__category=category,
            finished_products__is_active=True,
            is_active=True,
        )
        .distinct()
        .order_by("name")
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["h1"], fontSize=18, leading=22, spaceBefore=0, spaceAfter=0)
    sub_style = ParagraphStyle("sub", parent=styles["Normal"], fontSize=10, textColor=colors.HexColor("#555555"), spaceBefore=0, spaceAfter=0)
    name_style = ParagraphStyle("cardname", parent=styles["Normal"], fontSize=10, leading=12, fontName="Helvetica-Bold", alignment=1)
    sku_style = ParagraphStyle("cardsku", parent=styles["Normal"], fontSize=8, leading=10, alignment=1)

    page_w, page_h = portrait(letter)
    margin = 0.5 * inch
    usable_width = page_w - 2 * margin
    usable_height = page_h - 2 * margin
    top_gap = 0.15 * inch
    mid_gap = 0.2 * inch
    safety = 0.1 * inch  # keeps the flowed block just under the frame height

    story = []
    first = True
    for recipe in recipes:
        items = list(
            FinishedProduct.objects.filter(
                recipe=recipe,
                raw_product__category=category,
                is_active=True,
            )
            .select_related("raw_product")
            .prefetch_related("images")
            .order_by("raw_product__name")
        )
        if not items:
            continue
        if not first:
            story.append(PageBreak())
        first = False

        # Measure the fixed parts (title + barcodes) so the photos can take all
        # the remaining height on THIS page — everything stays on one page so a
        # recipe's barcodes are always printed with its photos.
        title_p = Paragraph(recipe.name, title_style)
        sub_p = Paragraph(f"{category.name} · {len(items)} item(s)", sub_style)
        _, th = title_p.wrap(usable_width, usable_height)
        _, sh = sub_p.wrap(usable_width, usable_height)
        bc_grid = _barcode_grid(items, usable_width, name_style, sku_style)
        _, bc_h = bc_grid.wrap(usable_width, usable_height)

        photo_area = (
            usable_height - th - sh - top_gap - mid_gap - bc_h - safety
        )
        gallery = None
        if photo_area > 1.2 * inch:
            gallery = _photo_gallery(
                _select_recipe_photos(items, cap=4), usable_width, photo_area
            )

        block = [title_p, sub_p, Spacer(1, top_gap)]
        if gallery is not None:
            block += [gallery, Spacer(1, mid_gap)]
        else:
            # No photos to show: push the barcodes toward the bottom anyway.
            block.append(Spacer(1, max(photo_area + mid_gap, 0)))
        block.append(bc_grid)

        # Force the whole recipe (photos + every barcode row) onto one page;
        # shrink slightly rather than split if it's ever a hair too tall.
        story.append(KeepInFrame(usable_width, usable_height, block, mode="shrink"))

    if not story:
        story = [Paragraph(f"{category.name} — no active items with recipes.", styles["h1"])]

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=portrait(letter),
        topMargin=margin,
        bottomMargin=margin,
        leftMargin=margin,
        rightMargin=margin,
    )
    doc.build(story)
    buf.seek(0)
    return HttpResponse(buf, content_type="application/pdf")


# ---------------------------------------------------------------------------
# The same category, ordered by the rainbow instead of by colorway.
#
# The sheet above answers "what does this colorway look like?". This one
# answers the question a customer actually asks — "what have you got in red?"
# — off the same category and the same picker, because Yarn and Silk is how
# the stall is laid out and a sheet is printed per table, not per style.
# ---------------------------------------------------------------------------


def _by_color_pages(category):
    """`(slug, label, color, recipe, items)` for one category, rainbow order.

    One entry is one page, and it holds the same thing a page of the by-name
    sheet holds: a colorway, its photos, and a barcode for every style dyed in
    it. What changes is the order and the repetition.

    A colorway claiming red and blue yields two entries, which is the entire
    point: the dyes aren't blended, so a red-and-blue scarf is genuinely in
    both sections, and printing it once leaves it missing from one of them
    (same reasoning as `colorbands`).

    Unconfirmed recipes are left out rather than guessed at. `colorbands` gets
    roughly 85% of these right, and the 15% is silent — you look under orange,
    the scarf isn't there, and nothing tells you it was filed under red.
    """
    items_by_recipe = {}
    for fp in (
        FinishedProduct.objects.dyed().filter(
            raw_product__category=category,
            raw_product__is_active=True,
            recipe__bands_confirmed_at__isnull=False,
        )
        .select_related("recipe", "raw_product")
        .prefetch_related("images")
        .order_by("raw_product__name")
    ):
        items_by_recipe.setdefault(fp.recipe, []).append(fp)

    recipes = sorted(items_by_recipe, key=lambda r: r.name)
    return [
        (slug, label, color, recipe, items_by_recipe[recipe])
        for slug, label, color in colorbands.BANDS
        for recipe in recipes
        if slug in (recipe.color_bands or [])
    ]


def _by_color_counts(category):
    """What the by-colour link on the picker promises: pages, and what's
    missing. Two different silences, kept apart — an unconfirmed colorway is
    work someone still has to do, while a confirmed one claiming no band is a
    decision already taken, and merging them would keep re-raising the settled
    one."""
    recipes = (
        Recipe.objects.filter(
            finished_products__raw_product__category=category,
            finished_products__is_active=True,
            finished_products__raw_product__is_active=True,
            is_active=True,
        )
        .distinct()
        .values_list("color_bands", "bands_confirmed_at")
    )
    pages = sum(len(bands or []) for bands, confirmed in recipes if confirmed)
    unclassified = sum(1 for _, confirmed in recipes if not confirmed)
    return pages, unclassified


def _band_tab_painter(page_bands):
    """Paint each page's colour as a tab on the right edge, thumb-index style.

    The band is named on the page too, but the tab is what makes a printed
    stack usable: fan it and the sections are visible from the edge, which is
    how someone standing at the stall finds red without reading anything. Its
    slot is fixed per band, so the gaps in a stack tell you which sections that
    category doesn't have.

    Keyed on page number, which holds because the story puts exactly one entry
    on each page (`KeepInFrame` shrinks rather than splitting).
    """
    from reportlab.lib import colors
    from reportlab.lib.units import inch

    slots = len(colorbands.BANDS)

    def paint(canvas, doc):
        index = canvas.getPageNumber() - 1
        if index >= len(page_bands):
            return
        slug, label, color = page_bands[index]
        slot = colorbands.BAND_SLUGS.index(slug)

        page_w, page_h = doc.pagesize
        top = page_h - doc.topMargin
        height = (page_h - doc.topMargin - doc.bottomMargin) / slots
        y = top - (slot + 1) * height
        width = 0.28 * inch
        x = page_w - doc.rightMargin + 0.10 * inch

        canvas.saveState()
        if slug == colorbands.RAINBOW:
            # **No single colour can say rainbow**, and picking one would make
            # the tab a lie in the one place a tab is read without reading:
            # fanned, from the edge. So the section draws the spectrum in
            # stripes, which is recognisable at a glance and survives a
            # photocopy as a banded block rather than as a flat grey the same
            # as its neighbours.
            stripes = colorbands.CHROMATIC
            band_h = height / len(stripes)
            for i, stripe in enumerate(stripes):
                canvas.setFillColor(colors.HexColor(colorbands.BAND_COLORS[stripe]))
                canvas.rect(x, y + i * band_h, width, band_h + 0.5, stroke=0, fill=1)
            # The label needs a plate to sit on: rotated text over eight
            # colours is unreadable in either ink, and this is the tab whose
            # name matters most because it is the one nobody expects.
            plate_h = min(height * 0.5, 0.9 * inch)
            canvas.setFillColor(colors.white)
            canvas.rect(x, y + (height - plate_h) / 2, width, plate_h, stroke=0, fill=1)
            canvas.setFillColor(colors.black)
        else:
            canvas.setFillColor(colors.HexColor(color))
            canvas.rect(x, y, width, height, stroke=0, fill=1)
            # Yellow, orange, pink and grey are too light to carry white text;
            # the rest are too dark to carry black. Cheap luminance rather
            # than a lookup nobody would remember to update when a band colour
            # changes — the label has to survive a black-and-white photocopy.
            r, g, b = colors.HexColor(color).rgb()
            canvas.setFillColor(
                colors.black if (0.299 * r + 0.587 * g + 0.114 * b) > 0.5 else colors.white
            )
        canvas.setFont("Helvetica-Bold", 8)
        canvas.translate(x + width / 2, y + height / 2)
        canvas.rotate(90)
        canvas.drawCentredString(0, -3, label.upper())
        canvas.restoreState()

    return paint


@page_meta(
    title="Reference Sheet by Colour (PDF)",
    description="The same category sheet ordered by the rainbow: a page per "
                "colorway per section it claims, with a colour tab on the "
                "edge, so a red scarf can be found by being red.",
    category="Reference Sheets",
    note="Returns a PDF. Only colorways whose sections have been confirmed are printed.",
    # Same picker as the by-name sheet — this is the other button on the card,
    # not a second directory. The URL keeps the category ahead of the ordering
    # for that reason, which also puts it under the existing picker for
    # PickerPageConventionTests.
    show_in_index=False,
)
def reference_sheet_by_color_pdf(request, category_id):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, portrait
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, KeepInFrame

    category = get_object_or_404(RawProductCategory, pk=category_id)
    pages = _by_color_pages(category)

    styles = getSampleStyleSheet()
    kicker_style = ParagraphStyle("kicker", parent=styles["Normal"], fontSize=11, leading=13, fontName="Helvetica-Bold", spaceBefore=0, spaceAfter=0)
    title_style = ParagraphStyle("title", parent=styles["h1"], fontSize=18, leading=22, spaceBefore=0, spaceAfter=0)
    sub_style = ParagraphStyle("sub", parent=styles["Normal"], fontSize=10, textColor=colors.HexColor("#555555"), spaceBefore=0, spaceAfter=0)
    name_style = ParagraphStyle("cardname", parent=styles["Normal"], fontSize=10, leading=12, fontName="Helvetica-Bold", alignment=1)
    sku_style = ParagraphStyle("cardsku", parent=styles["Normal"], fontSize=8, leading=10, alignment=1)

    page_w, page_h = portrait(letter)
    margin = 0.5 * inch
    usable_width = page_w - 2 * margin
    usable_height = page_h - 2 * margin
    top_gap = 0.15 * inch
    mid_gap = 0.2 * inch
    safety = 0.1 * inch

    story = []
    for position, (slug, label, color, recipe, items) in enumerate(pages):
        if position:
            story.append(PageBreak())

        kicker_p = Paragraph(
            f'<font color="{color}">{label.upper()}</font>', kicker_style
        )
        title_p = Paragraph(recipe.name, title_style)
        sub_p = Paragraph(
            f"{category.name} · {len(items)} item(s)", sub_style
        )
        _, kh = kicker_p.wrap(usable_width, usable_height)
        _, th = title_p.wrap(usable_width, usable_height)
        _, sh = sub_p.wrap(usable_width, usable_height)
        bc_grid = _barcode_grid(items, usable_width, name_style, sku_style)
        _, bc_h = bc_grid.wrap(usable_width, usable_height)

        photo_area = (
            usable_height - kh - th - sh - top_gap - mid_gap - bc_h - safety
        )
        gallery = None
        if photo_area > 1.2 * inch:
            gallery = _photo_gallery(
                _select_recipe_photos(items, cap=4), usable_width, photo_area
            )

        block = [kicker_p, title_p, sub_p, Spacer(1, top_gap)]
        if gallery is not None:
            block += [gallery, Spacer(1, mid_gap)]
        else:
            block.append(Spacer(1, max(photo_area + mid_gap, 0)))
        block.append(bc_grid)

        story.append(KeepInFrame(usable_width, usable_height, block, mode="shrink"))

    if not story:
        story = [Paragraph(
            f"{category.name} — no colorways with confirmed colour sections yet.",
            styles["h1"],
        )]

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=portrait(letter),
        topMargin=margin,
        bottomMargin=margin,
        leftMargin=margin,
        rightMargin=margin,
    )
    painter = _band_tab_painter([p[:3] for p in pages])
    doc.build(story, onFirstPage=painter, onLaterPages=painter)
    buf.seek(0)
    return HttpResponse(buf, content_type="application/pdf")
