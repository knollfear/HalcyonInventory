"""Reading a marked production sheet back off a photograph.

The reasoning behind these is in `docs/claude/production.md`.
"""
import pathlib
import re
from datetime import date, datetime, time, timedelta
from django.contrib.auth.models import User
from django.utils import timezone
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from .. import (
    closing, colorbands, crew, fancy, nav, photowalk, production, restock,
    sales, seasonreport, seasons, sheetscan, skus, slowsellers, timesheets,
    weather,
)
from .. import labels as labelmod
from ..models import (
    UNCATEGORIZED_BRAND,
    BoothPhoto,
    CatalogGroup,
    CloseRun,
    CloseRunRow,
    DisplayFixture,
    DisplayPosition,
    Dye,
    DyeBrand,
    DayWeather,
    Employee,
    Faire,
    FaireDay,
    LabelStock,
    FinishedProduct,
    FinishedProductImage,
    InventoryLog,
    ProductImageUpload,
    ProductionRun,
    ProductionRunRow,
    RUN_ADJECTIVES,
    RUN_ANIMALS,
    new_run_token,
    normalize_token,
    RawProduct,
    RawProductCategory,
    Recipe,
    RecipeDye,
    RestockCheck,
    RestockPass,
    Sale,
    SaleLine,
    TimeEntry,
    UnmatchedSale,
    sync_display_slots,
)
from .helpers import (
    _pdf_text,
    _pdf_text_items,
    make_bathable,
    make_product,
    make_recipe,
    make_stock,
)


class RealSheetPhotoTests(TestCase):
    """The scanner against an actual photograph of an actual marked sheet.

    Kept as a file because the thing under test is optics, and a synthetic
    image cannot stand in for it: every generated QR tried here decoded on the
    first pass, where the real one needed an enlargement. Focus, curl, glare
    and the angle a page was lying at are not reproducible from a matrix.

    `scarves/testdata/marked_sheet_run5.jpg` — iPhone 16 Pro, 3024×4032,
    twelve rows all filled in. Its token was rotated in the admin before the
    file was committed, because this repository is public and a sheet's code
    opens a page that moves stock with no login.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = (
            pathlib.Path(__file__).resolve().parent.parent
            / "testdata" / "marked_sheet_run5.jpg"
        )
        cls.photo = path.read_bytes()

    def setUp(self):
        self.scan = sheetscan.read_sheet(self.photo)

    def test_it_names_the_sheet(self):
        """The QR did **not** decode at native resolution — it sits on the
        lifted corner of the page. Recovering it is the whole reason the
        passes are pooled instead of stopping at the first that finds a row:
        pass one reads rows and no QR, and a photo that names no sheet leaves
        somebody typing a code off paper."""
        self.assertEqual(self.scan.qr_token, "18-tranquil-bobcat")

    def test_it_reads_the_rows(self):
        """Eleven of twelve, measured. Asserted as a floor rather than an
        equality so a better scanner does not fail its own test — what must
        not happen is going backwards."""
        self.assertGreaterEqual(len(self.scan.marks), 11)
        self.assertGreaterEqual(len(self.scan.filled), 9)

    def test_the_first_row_is_read(self):
        """The sharp regression pin. `RECTAN-GOLDEN#1` decodes on a pass whose
        geometry puts its box outside the frame, so claiming a code the moment
        it decoded retired it before it produced a mark and the enlargement
        that would have read it never got its turn."""
        self.assertIn("RECTAN-GOLDEN#1", {m.code for m in self.scan.marks})

    def test_the_marks_come_back_in_page_order(self):
        """Pooling makes pixel coordinates ambiguous: a row found at 2x
        reports a `top` twice the size of the same row at 1x, which sorted row
        one into the middle of the page."""
        numbers = [int(m.code.rsplit("#", 1)[1]) for m in self.scan.marks]

        self.assertEqual(numbers, sorted(numbers))

    def test_a_photo_this_size_is_not_blamed_for_the_rows_it_missed(self):
        """2.74 pixels per module against the two zbar needs. A photo that
        reads badly at this size was soft or badly lit and is worth retaking,
        which is a different instruction from "that image can never work"."""
        self.assertFalse(self.scan.too_small_for_rows)
        self.assertFalse(self.scan.error)
class ScreenshotSheetPhotoTests(TestCase):
    """The other failure: a picture too small to hold a row barcode.

    `screenshot_sheet_run5.jpg` is the same sheet as the camera file, taken as
    a screenshot and exported through Preview — 1308px wide, where a 7.7 mil
    module lands on 1.18 pixels against the two zbar needs. Thirteen decode
    attempts read zero rows, 4x upscaling with unsharp masking included:
    interpolation cannot invent a sample that was never taken.

    It is a fixture because mistaking it for a camera photograph produced a
    confident wrong conclusion about the whole pipeline, which stood until the
    original turned up. The EXIF is the tell — no Make, no Model.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.photo = (
            pathlib.Path(__file__).resolve().parent.parent
            / "testdata" / "screenshot_sheet_run5.jpg"
        ).read_bytes()

    def setUp(self):
        self.scan = sheetscan.read_sheet(self.photo)

    def test_it_still_names_the_sheet(self):
        """Which is the whole job of the upload page. The boxes are the bonus,
        and this photo cannot give them — but it lands you on the right run
        with them ready to tap, which is the flow minus the shortcut."""
        self.assertEqual(self.scan.qr_token, "18-tranquil-bobcat")
        self.assertTrue(self.scan.named_but_unread)

    def test_it_reads_no_rows_and_says_why(self):
        """"Couldn't read that photo" and "that photo is too small to hold a
        row barcode" send somebody to do completely different things — retake
        it, or stop retaking it."""
        self.assertEqual(self.scan.marks, [])
        self.assertTrue(self.scan.too_small_for_rows)
        self.assertFalse(self.scan.error)
class SheetPhotoRescueTests(TestCase):
    """A photo too small for the rows can still name the sheet.

    Measured on a real 1308px-wide picture of run 5: a 7.7 mil Code128 module
    landed on 1.18 pixels where zbar needs about two, and **not one of
    thirteen decode attempts read a single row barcode** — including 4x
    upscaling with unsharp masking. Interpolation cannot invent a sample that
    was never taken. The QR came back from the same image once it was scaled
    up and sharpened, which is the half that matters: the photo's job on the
    upload page is to say which sheet this is.
    """

    def _qr_image(self, url, module_px):
        """The same QR the sheet prints, drawn at `module_px` per module.

        Drawn from the encoder's own matrix rather than rendered through
        reportlab, because `renderPM` has no raster backend installed here —
        and drawing it directly is what lets the test say *exactly* how many
        pixels a module got, which is the whole variable under test.
        """
        from PIL import Image

        from reportlab.graphics.barcode import qr as qr_module

        matrix = qr_module.QrCodeWidget(url, barLevel="M").qr
        # Without this the matrix is empty and `getModuleCount()` is 0, so the
        # "QR" is a blank square that decodes to nothing — a test that would
        # have passed for the wrong reason had it been asserting a failure.
        matrix.make()
        count = matrix.getModuleCount()
        quiet = 4                                   # the spec's quiet zone
        side = (count + quiet * 2) * module_px
        image = Image.new("L", (side, side), 255)
        for row in range(count):
            for col in range(count):
                if not matrix.isDark(row, col):
                    continue
                x = (col + quiet) * module_px
                y = (row + quiet) * module_px
                for dx in range(module_px):
                    for dy in range(module_px):
                        image.putpixel((x + dx, y + dy), 0)
        return image

    def _on_a_page(self, qr_image, width):
        """The QR pasted into the corner of a white page `width` px across.

        A bare QR filling the frame is not what a photo of a sheet looks
        like — the symbol is a small part of a mostly-white page, which is
        exactly the condition that makes it hard to decode. Saved as JPEG at
        the quality a phone uses, so the artefacts are there too.
        """
        from io import BytesIO

        from PIL import Image

        page = Image.new("L", (width, int(width * 11 / 8.5)), 255)
        page.paste(qr_image, (int(width * 0.72), int(width * 0.03)))
        out = BytesIO()
        page.save(out, format="JPEG", quality=85)
        return out.getvalue()

    def test_a_small_photo_still_names_the_sheet(self):
        url = "https://production.halcyonsilks.com/scarves/secret/production/18-tranquil-bobcat/"
        # Two pixels a module: the sheet's QR in a photo of this size, and
        # under what the first decode pass can manage on its own.
        page = self._on_a_page(self._qr_image(url, 2), width=1300)

        scan = sheetscan.read_sheet(page)

        self.assertEqual(scan.qr_token, "18-tranquil-bobcat")

    def test_and_says_the_rows_were_never_readable_rather_than_unread(self):
        """"Couldn't read that photo" and "that photo is too small to hold a
        row barcode" send somebody to do completely different things — retake
        it, or stop retaking it and tap the boxes."""
        url = "https://example.test/scarves/secret/production/18-tranquil-bobcat/"
        page = self._on_a_page(self._qr_image(url, 2), width=1300)

        scan = sheetscan.read_sheet(page)

        self.assertTrue(scan.too_small_for_rows)
        self.assertTrue(scan.named_but_unread)
        self.assertFalse(scan.error)

    def test_a_big_photo_that_read_nothing_is_not_blamed_on_its_size(self):
        """That one was soft or badly lit and is worth retaking."""
        from io import BytesIO

        from PIL import Image

        blank = BytesIO()
        Image.new("L", (3000, 3800), 255).save(blank, format="JPEG")

        scan = sheetscan.read_sheet(blank.getvalue())

        self.assertFalse(scan.too_small_for_rows)
        self.assertFalse(scan.named_but_unread)

    def test_the_key_carries_what_a_database_row_would_have(self):
        """There is no row and no admin: the bucket is browsable, so a pointer
        would be a second place for the answer to live — and the one that goes
        stale the moment the lifecycle rule deletes the object under it."""
        from datetime import datetime

        scan = sheetscan.ScanResult(qr_token="18-tranquil-bobcat", width=3024)
        scan.marks = [
            sheetscan.Mark(code="A#1", state=sheetscan.FILLED, score=1.0, top=1),
            sheetscan.Mark(code="B#2", state=sheetscan.UNSURE, score=0.3, top=2),
        ]

        key = sheetscan.photo_key(scan, datetime(2026, 9, 9, 22, 1, 34))

        self.assertTrue(key.startswith(sheetscan.PHOTO_PREFIX))
        self.assertIn("20260909T220134", key)
        self.assertIn("18-tranquil-bobcat", key)
        self.assertIn("-w3024-r2-f1-u1", key)

    def test_a_photo_that_named_no_sheet_still_gets_a_usable_key(self):
        """`r0` read nothing and `unnamed` decoded no QR — the two photos most
        worth looking at, findable in a listing without opening any of them."""
        from datetime import datetime

        key = sheetscan.photo_key(
            sheetscan.ScanResult(width=1308), datetime(2026, 9, 9, 22, 1, 34)
        )

        self.assertIn("unnamed", key)
        self.assertIn("-w1308-r0-f0-u0", key)

    def test_a_hostile_token_cannot_shape_the_key(self):
        """The token comes off a QR in a photograph, so it is somebody else's
        string until proven otherwise — and it is about to become part of an
        object path."""
        from datetime import datetime

        key = sheetscan.photo_key(
            sheetscan.ScanResult(qr_token="../../etc/passwd", width=10),
            datetime(2026, 9, 9, 22, 1, 34),
        )

        self.assertNotIn("..", key)
        self.assertEqual(key.count("/"), sheetscan.PHOTO_PREFIX.count("/"))

    def test_the_enlargement_is_skipped_when_it_would_not_fit(self):
        """A photo that failed at 48MP did not fail for want of pixels — it
        was soft, or moving, or badly lit — so doubling it would cost hundreds
        of megabytes to learn nothing."""
        from PIL import Image

        huge = Image.new("L", (8000, int(sheetscan.UPSCALE_MAX_PIXELS / 4 / 8000) + 50), 255)
        ordinary = Image.new("L", (3024, 4032), 255)

        self.assertEqual(len(list(sheetscan._passes(huge))), 1)
        self.assertEqual(len(list(sheetscan._passes(ordinary))), 3)

    def test_each_pass_carries_the_scale_it_is_drawn_at(self):
        """Pooling makes every pixel coordinate ambiguous otherwise: a row
        found at 2x reports a `top` twice the size of the same row found at
        1x, and sorting pooled marks on that puts row one mid-page."""
        from PIL import Image

        passes = list(sheetscan._passes(Image.new("L", (1000, 1200), 255)))

        for image, at_scale in passes:
            self.assertEqual(image.width, 1000 * at_scale)
class SheetMapNoiseTests(TestCase):
    """The map draws only sheets that differ from "all 80 used".

    A real run — 2435 labels over 31 sheets — drew 29 identical full grids,
    which buries the two that carry information: the part-used sheet you start
    on and the one you finish on.
    """

    def setUp(self):
        self.stock = make_stock()   # 4 × 20 = 80

    def test_a_long_run_draws_only_the_unfinished_sheet(self):
        plan = labelmod.plan_sheets(self.stock, 2435, start_at=0)
        self.assertEqual(plan.sheet_count, 31)
        self.assertEqual([s["number"] for s in plan.sheets], [31])
        self.assertEqual(plan.full_sheets, 30)
        self.assertEqual((plan.full_from, plan.full_to), (1, 30))

    def test_a_partial_start_keeps_the_first_sheet_too(self):
        # 44 already peeled + 2435 printed ends at index 2478, on sheet 31.
        plan = labelmod.plan_sheets(self.stock, 2435, start_at=44)
        self.assertEqual([s["number"] for s in plan.sheets], [1, 31])
        self.assertEqual(plan.full_sheets, 29)
        self.assertEqual((plan.full_from, plan.full_to), (2, 30))

    def test_a_single_sheet_is_always_drawn(self):
        plan = labelmod.plan_sheets(self.stock, 12, start_at=0)
        self.assertEqual([s["number"] for s in plan.sheets], [1])
        self.assertEqual(plan.full_sheets, 0)

    def test_an_exactly_full_single_sheet_is_still_drawn(self):
        """One sheet, no marker — but with nothing else on screen, hiding the
        only diagram would say less than showing it."""
        plan = labelmod.plan_sheets(self.stock, 80, start_at=0)
        self.assertEqual(plan.sheet_count, 1)
        self.assertEqual(plan.full_sheets, 1)
        self.assertEqual(plan.sheets, [])

    def test_the_marker_sheet_is_never_hidden(self):
        """It's the one that says where to start next time."""
        plan = labelmod.plan_sheets(self.stock, 159, start_at=0)
        self.assertEqual(plan.marker_index, 159)
        drawn = [s["number"] for s in plan.sheets]
        self.assertIn(2, drawn)
        marker_cells = [
            c for s in plan.sheets for c in s["cells"] if c["state"] == "marker"
        ]
        self.assertEqual(len(marker_cells), 1)

    def test_drawn_cells_are_only_built_for_drawn_sheets(self):
        """31 sheets × 80 cells is 2480 dicts nobody looks at."""
        plan = labelmod.plan_sheets(self.stock, 2435, start_at=0)
        self.assertEqual(sum(len(s["cells"]) for s in plan.sheets), 80)

    def test_the_page_says_how_many_it_left_out(self):
        user = User.objects.create_superuser("noise", "n@example.test", "pw")
        self.client.force_login(user)
        recipe = make_recipe("Noise Recipe")
        product = make_product(recipe, "Noise Product", with_image=False)
        product.number_on_hand = 500
        product.save()

        response = self.client.get(reverse("label_index"), {
            "dataset": "inventory", "extra": "0",
            "stock": str(LabelStock.objects.first().pk), "start_at": "1",
        })
        self.assertContains(response, "used end to end")
        self.assertContains(response, "Only the part-used sheets are drawn")
class RowBreakTests(TestCase):
    """The whole-catalogue export starts each blank on a fresh row.

    So a 31-sheet stack can be split by blank without a seam landing
    mid-row. Deliberately off everywhere else: the padding is ~20 labels
    either way, which rounds to nothing across 31 sheets and is a third of a
    3-sheet weekly run.
    """

    def setUp(self):
        self.stock = make_stock()      # 4 columns
        self.recipe = make_recipe("Dawn")
        category, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.category = category
        # SKUs come out BLANK-DAWN, so the prefix is the blank.
        self.blanks = {}
        for blank, on_hand in (("Alpha Scarf", 6), ("Beta Scarf", 3)):
            raw = RawProduct.objects.create(
                name=blank, category=category, price="5.00"
            )
            product = FinishedProduct.objects.create(
                name=f"{blank} Dawn", raw_product=raw, recipe=self.recipe,
                price="20.00", number_on_hand=on_hand,
            )
            self.blanks[blank] = product

    def test_a_new_blank_starts_on_a_fresh_row(self):
        run = labelmod.inventory_run()
        sequence = run.sequence(columns=4)

        # 6 of ALPHAS fills a row and two of the next; the remaining two
        # positions are padded so BETASC starts clean.
        skus = [p.sku if p else None for p in sequence]
        self.assertEqual(skus[:6], ["ALPHAS-DAWN"] * 6)
        self.assertEqual(skus[6:8], [None, None])
        self.assertEqual(skus[8:], ["BETASC-DAWN"] * 3)
        self.assertEqual(len(sequence) % 4, 3 % 4)

    def test_stickers_printed_is_unchanged_by_padding(self):
        run = labelmod.inventory_run()
        self.assertEqual(run.total, 9)
        self.assertEqual(len(run.flat(columns=4)), 9)
        self.assertEqual(len(run.sequence(columns=4)), 11)

    def test_no_padding_when_a_group_already_ends_on_a_row_boundary(self):
        self.blanks["Alpha Scarf"].number_on_hand = 8
        self.blanks["Alpha Scarf"].save()
        run = labelmod.inventory_run()
        self.assertEqual(len(run.sequence(columns=4)), 11)
        self.assertNotIn(None, run.sequence(columns=4))

    def test_a_filtered_run_does_not_pad(self):
        """Same ~20 labels, but a third of a short run instead of nothing."""
        run = labelmod.inventory_run(category=self.category)
        self.assertFalse(run.row_break_on_group)
        self.assertNotIn(None, run.sequence(columns=4))

        picked = [self.blanks["Alpha Scarf"].raw_product]
        run = labelmod.inventory_run(raw_products=picked)
        self.assertFalse(run.row_break_on_group)

    def test_the_weekly_run_does_not_pad(self):
        """~20 products over ~80 labels — breaking per group would waste a
        quarter of it."""
        InventoryLog.objects.create(
            finished_product=self.blanks["Alpha Scarf"],
            log_type=InventoryLog.PRODUCTION, quantity=6,
        )
        run = labelmod.produced_since(timezone.localdate() - timedelta(days=1))
        self.assertFalse(run.row_break_on_group)
        self.assertNotIn(None, run.sequence(columns=4))

    def test_hand_picked_runs_do_not_pad(self):
        run = labelmod.specific_items([(self.blanks["Alpha Scarf"], 6),
                                       (self.blanks["Beta Scarf"], 3)])
        self.assertFalse(run.row_break_on_group)
        self.assertNotIn(None, run.sequence(columns=4))

    def test_padding_consumes_a_sheet_position_but_prints_nothing(self):
        run = labelmod.inventory_run()
        pdf = labelmod.render_run(run, self.stock, start_at=0)
        text = _pdf_text(pdf)
        self.assertEqual(text.count("ALPHAS-DAWN"), 6)
        self.assertEqual(text.count("BETASC-DAWN"), 3)

        # Position 8 is the first of the second blank, i.e. row 3 column 1.
        items = [i for i in _pdf_text_items(pdf) if i[2] == "BETASC-DAWN"]
        first_beta_x = min(i[0] for i in items)
        alphas = [i for i in _pdf_text_items(pdf) if i[2] == "ALPHAS-DAWN"]
        self.assertAlmostEqual(first_beta_x, min(i[0] for i in alphas), delta=0.5,
                               msg="the new blank starts in column 1")

    def test_the_page_reports_the_padding(self):
        user = User.objects.create_superuser("pad", "p@example.test", "pw")
        self.client.force_login(user)
        response = self.client.get(reverse("label_index"), {
            "dataset": "inventory", "extra": "0",
            "stock": str(LabelStock.objects.first().pk), "start_at": "1",
        })
        self.assertEqual(response.context["padding"], 2)
        self.assertContains(response, "skipped so each blank starts its own row")
def sheet_photo(lines, filled=(), ink=(190, 30, 40), token=None, scale=5.0,
                partial=()):
    """A synthetic photograph of a printed production sheet.

    Draws the same geometry `production._draw_row` does — the box from
    `BOX_LEFT`/`BOX_BASELINE_OFFSET`, the bars from the very symbol
    `barcode_symbol()` returns — so a test that reads it back is exercising
    the real agreement between what the PDF prints and what the scanner
    looks for. `ink` is a colour rather than "black" on purpose: the whole
    question about pen colour is answered by passing a different one.
    """
    from string import ascii_lowercase, ascii_uppercase

    from io import BytesIO

    from PIL import Image, ImageDraw
    from reportlab.lib.pagesizes import letter

    page_w, page_h = letter
    image = Image.new("RGB", (int(page_w * scale), int(page_h * scale)), "white")
    draw = ImageDraw.Draw(image)

    def px(x, y):
        """PDF point to image pixel — y flips, and rounds to a whole pixel.

        Rounding here rather than letting PIL do it per edge is what makes
        adjacent bars tile exactly. Left to floats, a module a few pixels
        wide picks up half a pixel of error per edge, which is a large
        fraction of a bar and enough to stop the symbol decoding.
        """
        return round(x * scale), round((page_h - y) * scale)

    if token:
        widget = __import__(
            "reportlab.graphics.barcode.qr", fromlist=["qr"]
        ).QrCodeWidget(f"https://x.test/scarves/secret/production/{token}/")
        widget.qr.make()
        count = widget.qr.getModuleCount()
        module = production.QR_SIZE / count
        origin_x = page_w - production.PAGE_MARGIN - production.QR_SIZE
        origin_y = page_h - production.PAGE_MARGIN
        for r in range(count):
            for c in range(count):
                if not widget.qr.isDark(r, c):
                    continue
                a = px(origin_x + c * module, origin_y - r * module)
                b = px(origin_x + (c + 1) * module, origin_y - (r + 1) * module)
                draw.rectangle([a[0], a[1], b[0] - 1, b[1] - 1], fill="black")

    y = page_h - production.PAGE_MARGIN - production.HEADER_HEIGHT
    for index, line in enumerate(lines):
        baseline = y - production.ROW_HEIGHT + 12

        box_x = production.BOX_LEFT
        box_y = baseline + production.BOX_BASELINE_OFFSET
        top_left = px(box_x, box_y + production.BOX_SIZE)
        bottom_right = px(box_x + production.BOX_SIZE, box_y)
        draw.rectangle(
            [top_left[0], top_left[1], bottom_right[0], bottom_right[1]],
            outline="black", width=max(int(1.6 * scale), 1),
        )
        if index in filled or index in partial:
            # A partial mark is a corner smudge — enough ink to notice, not
            # enough to be an answer.
            pad = (production.BOX_SIZE * (0.34 if index in partial else 0.12)) * scale
            draw.rectangle(
                [top_left[0] + pad, top_left[1] + pad,
                 bottom_right[0] - pad, bottom_right[1] - pad],
                fill=ink,
            )

        symbol = production.barcode_symbol(production.line_code(line))
        symbol.validated = symbol.validate()
        symbol.encoded = symbol.encode()
        left = (production.BOX_LEFT + production.BOX_SIZE
                + production.BOX_TO_BARCODE + symbol.lquiet)
        bar_top = baseline + production.BARCODE_BASELINE_OFFSET + production.BARCODE_HEIGHT
        bar_bottom = baseline + production.BARCODE_BASELINE_OFFSET
        for char in symbol.decompose():
            if char in ascii_lowercase:
                left += (ord(char) - ord("a") + 1) * symbol.barWidth
            elif char in ascii_uppercase:
                width = (ord(char) - ord("A") + 1) * symbol.barWidth
                a = px(left, bar_top)
                b = px(left + width, bar_bottom)
                draw.rectangle([a[0], a[1], b[0] - 1, b[1]], fill="black")
                left += width
        y -= production.ROW_HEIGHT

    out = BytesIO()
    image.save(out, "PNG")
    return out.getvalue()
class SheetScanTests(TestCase):
    """Reading tick boxes off a photo of a sheet.

    The barcode does the hard part: every row prints one a fixed distance
    from its box, so a decoded symbol gives the row's identity *and* the
    position and scale of everything beside it. Finding a box is then
    arithmetic rather than checkbox recognition.

    Nothing here applies anything — the scan fills the form in and a person
    submits it, which is what makes it safe to be approximate.
    """

    def setUp(self):
        self.run = ProductionRun.objects.create()
        self.rows = []
        for i, (recipe_name, product_name) in enumerate([
            ("Stormy Sea", "Silk Infinity"),
            ("Aegean", "Silk Rectangle"),
            ("Ember", "Wool Wrap"),
            ("Moss", "Silk Square"),
        ], start=1):
            recipe = make_recipe(recipe_name, hexes=())
            product = make_bathable(recipe, product_name, on_hand=0, par=8, bath=4)
            self.rows.append(ProductionRunRow.objects.create(
                run=self.run, finished_product=product, order=i, quantity=4,
            ))
        # One bath each, so a line is a row here — which is what makes the
        # grouping test below the interesting one.
        self.lines = production.lines_for_run(self.run)
        self.codes = [production.line_code(line) for line in self.lines]

    def _read(self, **kwargs):
        return sheetscan.read_sheet(sheet_photo(self.lines, **kwargs))

    # --- the reading ------------------------------------------------------

    def test_every_row_is_found(self):
        scan = self._read()

        self.assertEqual(scan.error, "")
        self.assertEqual(len(scan.marks), len(self.lines))

    def test_a_filled_box_reads_filled_and_a_blank_one_blank(self):
        scan = self._read(filled=(0, 2))

        states = {m.code: m.state for m in scan.marks}
        self.assertEqual(states[self.codes[0]], sheetscan.FILLED)
        self.assertEqual(states[self.codes[1]], sheetscan.EMPTY)
        self.assertEqual(states[self.codes[2]], sheetscan.FILLED)
        self.assertEqual(states[self.codes[3]], sheetscan.EMPTY)

    def test_a_red_pen_works(self):
        """Luminance, not blackness — red sits far nearer black than paper."""
        scan = self._read(filled=(0,), ink=(200, 25, 35))

        self.assertEqual(len(scan.filled), 1)

    def test_a_pencil_works(self):
        scan = self._read(filled=(0,), ink=(105, 105, 108))

        self.assertEqual(len(scan.filled), 1)

    def test_a_blue_pen_works(self):
        scan = self._read(filled=(0,), ink=(25, 45, 160))

        self.assertEqual(len(scan.filled), 1)

    def test_a_yellow_highlighter_does_not(self):
        """It is about as bright as the page. This is why the sheet says any
        pen but yellow, and it has to fail as 'empty' rather than 'maybe'."""
        scan = self._read(filled=(0,), ink=(255, 246, 90))

        self.assertEqual(scan.filled, [])

    def test_a_smudge_is_reported_unsure_rather_than_guessed(self):
        """There is somewhere for an ambiguous mark to go, and it is a
        person's eyes."""
        scan = self._read(partial=(1,))

        self.assertEqual([m.code for m in scan.unsure], [self.codes[1]])

    # --- scale and framing -------------------------------------------------

    def test_it_does_not_care_how_close_the_phone_was(self):
        """Scale comes off each barcode's own width, so a tighter or wider
        frame is the same reading."""
        for scale in (4.0, 7.0):
            scan = sheetscan.read_sheet(
                sheet_photo(self.lines, filled=(0, 2), scale=scale),
            )
            self.assertEqual(len(scan.filled), 2, f"at scale {scale}")

    def test_rows_not_on_this_run_are_reported(self):
        """Expected to be empty forever; if it isn't, the photo is of some
        other sheet and the matched marks would land here unremarked."""
        scan = sheetscan.read_sheet(sheet_photo(self.lines, filled=(0,)))

        self.assertEqual(sheetscan.strays(self.run, scan), [])

    def test_junk_is_an_error_not_a_crash(self):
        scan = sheetscan.read_sheet(b"not a photo")

        self.assertTrue(scan.error)
        self.assertEqual(scan.marks, [])

    # --- the wrong sheet ---------------------------------------------------

    def test_the_qr_confirms_which_sheet_this_is(self):
        scan = sheetscan.read_sheet(
            sheet_photo(self.lines, filled=(0,), token=self.run.token))

        self.assertEqual(scan.qr_token, self.run.token)
        self.assertEqual(len(scan.filled), 1)

    def test_a_photo_of_another_sheet_is_refused(self):
        """Two sheets printed days apart share most of their rows, so marks
        off the wrong one land on rows that look right."""
        scan = sheetscan.read_sheet(
            sheet_photo(self.lines, filled=(0, 1, 2), token="SOMEOTHERTOKEN"))

        self.assertEqual(scan.qr_token, "SOMEOTHERTOKEN")

    # --- turning marks into rows -------------------------------------------

    def test_marks_become_rows_to_tick(self):
        scan = self._read(filled=(0, 2))

        ticked = sheetscan.rows_to_tick(self.run, scan)

        self.assertEqual(set(ticked), {self.rows[0].pk, self.rows[2].pk})

    def test_repeated_baths_of_one_colorway_are_one_mark(self):
        """**The inversion of what this used to test, and the reason the
        sheet changed.**

        `plan_baths` clumps repeated baths of a colorway together, so a sheet
        is *expected* to contain several of one SKU. That used to mean
        several boxes, and the barcode carried the row's position because a
        decoder returns one result per distinct symbol and SKU-only codes
        would have collapsed them.

        They are one line now: one box, one mark, one answer about one pile.
        The position stays in the code as a check on being pointed at the
        right sheet, but it no longer has anything to disambiguate.
        """
        extra = ProductionRunRow.objects.create(
            run=self.run, finished_product=self.rows[0].finished_product,
            order=9, quantity=4,
        )
        lines = production.lines_for_run(self.run)
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0].rows, [self.rows[0], extra])
        self.assertEqual(lines[0].quantity, 8)

        scan = sheetscan.read_sheet(sheet_photo(lines, filled=(0,)))

        self.assertEqual(len(scan.filled), 1)
        # One key back, and it banks both baths when it is submitted.
        self.assertEqual(sheetscan.rows_to_tick(self.run, scan), [lines[0].key])

    def test_re_reading_the_same_photo_ticks_nothing_new(self):
        """Somebody re-uploads the original picture after it has been acted
        on. The marks for rows already recorded must not go looking for
        another row to land on."""
        scan = self._read(filled=(0, 1))
        for key in sheetscan.rows_to_tick(self.run, scan):
            production.apply_row(ProductionRunRow.objects.get(pk=key))

        again = self._read(filled=(0, 1))

        self.assertEqual(sheetscan.rows_to_tick(self.run, again), [])

    def test_an_already_recorded_row_is_not_offered_again(self):
        production.apply_row(self.rows[0])
        scan = self._read(filled=(0,))

        self.assertEqual(sheetscan.rows_to_tick(self.run, scan), [])
class RunCodeTests(TestCase):
    """The code on a sheet, which a person reads off paper and types."""

    def test_it_reads_as_words(self):
        token = new_run_token()
        number, adjective, animal = token.split("-")

        self.assertEqual(len(number), 2)
        self.assertTrue(number.isdigit())
        self.assertIn(adjective, RUN_ADJECTIVES)
        self.assertIn(animal, RUN_ANIMALS)

    def test_it_fits_the_column(self):
        longest = max(len(new_run_token()) for _ in range(2000))
        field = ProductionRun._meta.get_field("token")

        self.assertLessEqual(longest, field.max_length)

    def test_typing_it_is_forgiving(self):
        """Punctuation and case are how a phone keyboard differs from a
        printed page, not how one sheet differs from another."""
        token = "42-brisk-wombat"

        for typed in ("42-brisk-wombat", "42 Brisk Wombat", "42BRISKWOMBAT",
                      "  42-BRISK-WOMBAT  "):
            self.assertEqual(normalize_token(typed), normalize_token(token), typed)

    def test_a_different_code_is_still_different(self):
        self.assertNotEqual(
            normalize_token("42-brisk-wombat"), normalize_token("43-brisk-wombat")
        )
class PhotoUploadFlowTests(TestCase):
    """Camera first: photograph any sheet at one page, and the photo says
    which run it is.

    That is what makes the QR do real work — it isn't a second presentation
    of something the address bar already proved, it is the only thing that
    names the sheet.
    """

    def setUp(self):
        self.run = ProductionRun.objects.create()
        # Three colorways rather than three baths of one: the sheet prints a
        # line per colorway now, and a photo that reads some lines and misses
        # others is the failure this class is about.
        self.rows = [
            ProductionRunRow.objects.create(
                run=self.run,
                finished_product=make_bathable(
                    make_recipe(name, hexes=()), blank, on_hand=0, par=8, bath=4
                ),
                order=i,
                quantity=4,
            )
            for i, (name, blank) in enumerate(
                [("Stormy Sea", "Silk Infinity"),
                 ("Aegean", "Silk Rectangle"),
                 ("Ember", "Wool Wrap")],
                start=1,
            )
        ]
        self.product = self.rows[0].finished_product
        self.lines = production.lines_for_run(self.run)
        self.url = reverse("production_upload")

    def _send(self, lines=None, **kwargs):
        from django.core.files.uploadedfile import SimpleUploadedFile
        photo = sheet_photo(lines if lines is not None else self.lines, **kwargs)
        return self.client.post(self.url, {
            "sheet": SimpleUploadedFile("s.png", photo, content_type="image/png"),
        })

    def test_it_serves_an_anonymous_get(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_photo_lands_on_its_own_run_pre_ticked(self):
        response = self._send(filled=(0, 2), token=self.run.token)

        self.assertEqual(response.status_code, 302)
        self.assertIn(self.run.token, response["Location"])
        self.assertIn("done=", response["Location"])

        page = self.client.get(response["Location"])
        self.assertEqual(page.context["prefilled"], {self.rows[0].pk, self.rows[2].pk})

    def test_nothing_is_recorded_by_uploading(self):
        response = self._send(filled=(0, 2), token=self.run.token)
        self.client.get(response["Location"])

        self.assertEqual(InventoryLog.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 0)

    def test_submitting_on_the_run_page_is_what_records(self):
        response = self._send(filled=(0,), token=self.run.token)
        self.client.get(response["Location"])
        self.client.post(
            reverse("production_run", args=[self.run.token]),
            {"done": [str(self.rows[0].pk)]},
        )

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 4)

    def test_no_readable_code_asks_for_it_and_then_hands_over(self):
        """Nearly always a soft photo rather than a wrong sheet, so this is a
        way through rather than an interrogation."""
        self._send(filled=(0,))
        page = self.client.get(self.url)
        self.assertContains(page, "Type the code printed beside it")

        response = self.client.post(self.url, {"sheet_code": self.run.token})

        self.assertEqual(response.status_code, 302)
        page = self.client.get(response["Location"])
        self.assertEqual(page.context["prefilled"], {self.rows[0].pk})

    def test_a_typed_code_is_forgiving(self):
        self._send(filled=(0,))

        response = self.client.post(self.url, {
            "sheet_code": self.run.token.replace("-", " ").upper(),
        })

        self.assertEqual(response.status_code, 302)

    def test_an_unknown_typed_code_says_so(self):
        self._send(filled=(0,))

        self.client.post(self.url, {"sheet_code": "99-wrong-badger"})
        page = self.client.get(self.url)

        self.assertContains(page, "99-wrong-badger")

    def test_an_unreadable_photo_says_so(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.post(self.url, {
            "sheet": SimpleUploadedFile("s.png", b"junk", content_type="image/png"),
        })

        self.assertContains(self.client.get(self.url), "Try again")

    def test_the_run_page_reports_what_the_photo_missed(self):
        """The everyday failure is a soft photo, and it fails partially."""
        response = self._send(lines=self.lines[:2], filled=(0,), token=self.run.token)

        page = self.client.get(response["Location"])

        self.assertEqual(page.context["scan"]["read"], 2)
        self.assertEqual(page.context["scan"]["total"], 3)
        self.assertContains(page, "come out of the photo")

    def test_a_stale_link_degrades_to_an_empty_form(self):
        """Ids that aren't this run's are dropped rather than half-ticking a
        page from some other sheet."""
        other = ProductionRun.objects.create()
        url = reverse("production_run", args=[self.run.token])

        page = self.client.get(f"{url}?done=99999&done=abc&read=3&filled=1")

        self.assertEqual(page.context["prefilled"], set())
        self.assertEqual(other.rows.count(), 0)

    def test_an_already_recorded_row_is_not_re_ticked(self):
        production.apply_row(self.rows[0])
        response = self._send(filled=(0, 1), token=self.run.token)

        page = self.client.get(response["Location"])

        self.assertEqual(page.context["prefilled"], {self.rows[1].pk})

    def test_the_run_page_no_longer_takes_photos(self):
        """Arriving at the run URL first means answering by hand — the user's
        'what not to do' path — so the camera lives on the upload page."""
        page = self.client.get(reverse("production_run", args=[self.run.token]))

        self.assertNotContains(page, 'name="sheet"')
        self.assertContains(page, reverse("production_upload"))
