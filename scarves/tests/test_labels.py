"""Barcode labels, the sheet plan, and how a SKU is assigned.

The reasoning behind these is in `docs/claude/labels.md`.
"""
from collections import Counter
import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock
from django.contrib.auth.models import User
from django.core.management import call_command
from django.utils import timezone
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from .. import (
    closing, colorbands, crew, fancy, nav, photowalk, production, restock,
    sales, seasonreport, seasons, sheetscan, skus, slowsellers, timesheets,
    weather,
)
from ..forms import (
    CrewHandbookForm,
    HoursForm,
    LabelRunForm,
    PickedBathsField,
    ProductionSheetForm,
    QuickRecipeRowForm,
    RecipeDyesForm,
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
    _pdf_streams,
    _pdf_text,
    _pdf_text_items,
    make_product,
    make_recipe,
    make_stock,
)


def _pdf_lines(pdf_bytes, x_max=None):
    """Both endpoints of every stroked line segment, in page coordinates.

    reportlab emits `canvas.line()` as `n x1 y1 m x2 y2 l S` on one line —
    enough to assert where the registration ticks landed without pulling in a
    PDF library.
    """
    content = _pdf_streams(pdf_bytes)
    pts = []
    for x1, y1, x2, y2 in re.findall(
        r"([\d.]+) ([\d.]+) m ([\d.]+) ([\d.]+) l S", content
    ):
        pts.append((float(x1), float(y1)))
        pts.append((float(x2), float(y2)))
    if x_max is not None:
        pts = [p for p in pts if p[0] <= x_max]
    return pts
class LabelStockGeometryTests(TestCase):
    """The eight numbers are transcribed by hand off a vendor page, and a
    transposed digit prints one ruined sheet before anyone notices. These are
    the checks that make that a test failure instead."""

    def test_seeded_stock_fits_its_sheet(self):
        """Every stock shipped in a migration closes to the page size."""
        for stock in LabelStock.objects.all():
            with self.subTest(stock=stock.name):
                over_x, over_y = stock.overflow_in()
                self.assertLessEqual(over_x, Decimal("0.01"), "runs off the right edge")
                self.assertLessEqual(over_y, Decimal("0.01"), "runs off the bottom")

    def test_the_migration_seeded_the_stock_we_buy(self):
        stock = LabelStock.objects.get(name__startswith="Avery 5167")
        self.assertEqual(stock.labels_per_sheet, 80)
        self.assertEqual((stock.columns, stock.rows), (4, 20))
        self.assertTrue(stock.purchase_url, "the page offers a 'buy more' link")

    def test_bad_geometry_is_refused(self):
        from django.core.exceptions import ValidationError

        stock = LabelStock(
            name="Transposed pitch",
            page_width_in=Decimal("8.5"), page_height_in=Decimal("11"),
            label_width_in=Decimal("1.75"), label_height_in=Decimal("0.5"),
            columns=4, rows=20,
            margin_left_in=Decimal("0.32812"), margin_top_in=Decimal("0.5"),
            pitch_x_in=Decimal("3.20125"),  # digits swapped
            pitch_y_in=Decimal("0.5"),
        )
        with self.assertRaises(ValidationError):
            stock.full_clean()

    def test_every_label_lands_inside_the_page(self):
        stock = make_stock()
        page_w = labelmod._pt(stock.page_width_in)
        page_h = labelmod._pt(stock.page_height_in)
        label_w = labelmod._pt(stock.label_width_in)
        label_h = labelmod._pt(stock.label_height_in)

        for index in range(stock.labels_per_sheet):
            slot = labelmod.slot_for(stock, index)
            with self.subTest(index=index):
                self.assertGreaterEqual(slot.x, 0)
                self.assertGreaterEqual(slot.y, 0)
                self.assertLessEqual(slot.x + label_w, page_w + 0.5)
                self.assertLessEqual(slot.y + label_h, page_h + 0.5)

    def test_labels_do_not_overlap(self):
        stock = make_stock()
        label_w = labelmod._pt(stock.label_width_in)
        label_h = labelmod._pt(stock.label_height_in)

        boxes = [labelmod.slot_for(stock, i) for i in range(stock.labels_per_sheet)]
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                overlap = (
                    a.x < b.x + label_w and b.x < a.x + label_w
                    and a.y < b.y + label_h and b.y < a.y + label_h
                )
                self.assertFalse(overlap, f"{a} overlaps {b}")

    def test_labels_run_left_to_right_then_down(self):
        stock = make_stock()
        first, second, fifth = (labelmod.slot_for(stock, i) for i in (0, 1, 4))
        self.assertGreater(second.x, first.x, "label 2 is to the right of label 1")
        self.assertAlmostEqual(second.y, first.y, places=6, msg="still row 1")
        self.assertLess(fifth.y, first.y, "label 5 has dropped to row 2")
        self.assertAlmostEqual(fifth.x, first.x, places=6)

    def test_offsets_move_every_label_together(self):
        plain = make_stock()
        nudged = make_stock(name="Nudged", x_offset_mm=Decimal("2"), y_offset_mm=Decimal("-1"))
        for index in (0, 7, 79):
            a, b = labelmod.slot_for(plain, index), labelmod.slot_for(nudged, index)
            self.assertAlmostEqual(b.x - a.x, labelmod._mm_pt(2), places=6)
            self.assertAlmostEqual(b.y - a.y, labelmod._mm_pt(-1), places=6)
class LabelMarkerTests(TestCase):
    """Resuming a part-used sheet is the whole reason the marker exists.

    A weekly run leaves a part-used sheet nearly every time. If the next run
    can't find where to start, that sheet gets binned — and 19 wasted rows
    reads as waste no matter what it cost. The marker puts the answer on the
    paper, so it survives a cleared cache and a different laptop.
    """

    def setUp(self):
        self.stock = make_stock()

    def test_marker_goes_straight_after_the_last_label(self):
        self.assertEqual(labelmod.marker_index_for(self.stock, 63, start_at=0), 63)

    def test_marker_says_the_label_after_itself(self):
        plan = labelmod.plan_sheets(self.stock, 63, start_at=0)
        self.assertEqual(plan.marker_index, 63)
        self.assertEqual(plan.resume_at, 65, "64 is the marker; the next run starts at 65")
        self.assertEqual(plan.free_after, 16)

    def test_resuming_where_the_marker_said_reuses_the_sheet(self):
        """Print 63, resume at 65, and the second run continues the same sheet
        rather than starting a fresh one."""
        first = labelmod.plan_sheets(self.stock, 63, start_at=0)
        second = labelmod.plan_sheets(self.stock, 10, start_at=first.resume_at - 1)
        self.assertEqual(second.sheets[0]["number"], 1)
        self.assertEqual(
            [c["state"] for c in second.sheets[0]["cells"]][:64],
            ["used"] * 64,
            "everything up to and including the old marker is already peeled",
        )
        self.assertEqual(second.sheets[0]["cells"][64]["state"], "printing")

    def test_no_marker_when_the_run_ends_the_sheet_exactly(self):
        """There'd be nowhere to put it but a fresh sheet, and a fresh sheet
        needs no marker."""
        plan = labelmod.plan_sheets(self.stock, 80, start_at=0)
        self.assertIsNone(plan.marker_index)
        self.assertTrue(plan.finishes_sheet)
        self.assertEqual(plan.resume_at, 1)
        self.assertEqual(plan.sheet_count, 1, "the marker must not spill a second sheet")

    def test_marker_can_start_the_next_sheet(self):
        """81 labels fill sheet one and put one on sheet two; the marker
        follows it there."""
        plan = labelmod.plan_sheets(self.stock, 81, start_at=0)
        self.assertEqual(plan.sheet_count, 2)
        self.assertEqual(plan.marker_index, 81)
        self.assertEqual(plan.resume_at, 3)

    def test_partial_sheet_start_marks_earlier_labels_used(self):
        plan = labelmod.plan_sheets(self.stock, 4, start_at=64)
        states = [c["state"] for c in plan.sheets[0]["cells"]]
        self.assertEqual(states[:64], ["used"] * 64)
        self.assertEqual(states[64:68], ["printing"] * 4)
        self.assertEqual(states[68], "marker")
class LabelRunSelectionTests(TestCase):
    """Which products get stickers, and how many."""

    def setUp(self):
        self.recipe = make_recipe("Label Recipe")
        self.a = make_product(self.recipe, "Label A", with_image=False)
        self.b = make_product(self.recipe, "Label B", with_image=False)
        self.a.sku, self.a.number_on_hand = "AAA-ONE", 3
        self.a.save()
        self.b.sku, self.b.number_on_hand = "BBB-TWO", 0
        self.b.save()

    def _log(self, product, qty, when, precision=InventoryLog.EXACT):
        log = InventoryLog.objects.create(
            finished_product=product,
            log_type=InventoryLog.PRODUCTION,
            quantity=qty,
            date_precision=precision,
        )
        InventoryLog.objects.filter(pk=log.pk).update(created_at=when)
        return log

    def test_extra_is_added_per_product(self):
        """The user's arithmetic: base 2 with +2 prints 4, with +1 prints 3."""
        self.a.number_on_hand = 2
        self.a.save()
        for extra, expected in ((0, 2), (1, 3), (2, 4)):
            run = labelmod.inventory_run(extra=extra)
            row = next(r for r in run.rows if r.product.sku == "AAA-ONE")
            self.assertEqual(row.quantity, expected, f"+{extra}")
            self.assertEqual(row.base, 2, "the underlying count is reported unchanged")

    def test_zero_on_hand_is_skipped_by_default(self):
        run = labelmod.inventory_run(extra=2)
        self.assertNotIn("BBB-TWO", [r.product.sku for r in run.rows])

    def test_zero_on_hand_can_be_included(self):
        run = labelmod.inventory_run(extra=2, include_zero=True)
        row = next(r for r in run.rows if r.product.sku == "BBB-TWO")
        self.assertEqual(row.quantity, 2, "just the extras")

    def test_products_without_a_sku_are_reported_not_dropped(self):
        """A silently missing sticker is a scarf that won't scan at the till."""
        self.b.number_on_hand = 5
        self.b.save()
        # save() fills a blank SKU now, so an empty one only arrives via a
        # queryset write — a row created before generation moved to create.
        FinishedProduct.objects.filter(pk=self.b.pk).update(sku="")
        run = labelmod.inventory_run()
        self.assertEqual([p.name for p in run.skipped_no_sku], ["Label B"])
        self.assertNotIn("", [r.product.sku for r in run.rows])

    def test_rows_are_sorted_by_sku(self):
        """A dye bath yields 3–5 of one SKU, so sorting by SKU is what puts
        each bath's stickers together on the sheet."""
        self.b.number_on_hand = 4
        self.b.save()
        run = labelmod.inventory_run()
        self.assertEqual([r.product.sku for r in run.rows], ["AAA-ONE", "BBB-TWO"])
        self.assertEqual(
            [p.sku for p in run.flat()],
            ["AAA-ONE"] * 3 + ["BBB-TWO"] * 4,
            "all copies of a SKU are contiguous",
        )

    def test_produced_since_counts_production_logs(self):
        now = timezone.now()
        self._log(self.a, 4, now - timedelta(days=2))
        self._log(self.a, 3, now - timedelta(days=30))
        run = labelmod.produced_since((now - timedelta(days=7)).date())
        row = next(r for r in run.rows if r.product.sku == "AAA-ONE")
        self.assertEqual(row.quantity, 4, "only the recent bath")

    def test_produced_since_ignores_sales(self):
        """Stickers are for what was made, not for what's left on the shelf."""
        now = timezone.now()
        self._log(self.a, 5, now - timedelta(days=1))
        InventoryLog.objects.create(
            finished_product=self.a, log_type=InventoryLog.SALE, quantity=-3,
        )
        run = labelmod.produced_since((now - timedelta(days=7)).date())
        row = next(r for r in run.rows if r.product.sku == "AAA-ONE")
        self.assertEqual(row.quantity, 5)

    def test_backdated_cards_do_not_ask_for_stickers(self):
        """A 2024 kanban card entered today carries created_at of 2024, so it
        correctly falls outside a recent cutoff — those scarves are long gone."""
        self._log(self.a, 6, timezone.make_aware(datetime(2024, 9, 1)),
                  precision=InventoryLog.MONTH)
        run = labelmod.produced_since(timezone.localdate() - timedelta(days=7))
        self.assertEqual(run.rows, [])

    def test_month_precision_rows_near_the_cutoff_are_counted_not_dropped(self):
        """A month-only row is stored on the 1st as sort padding, so a
        mid-month cutoff excludes it even though its real date is unknown.
        The page has to say so rather than quietly losing production."""
        today = timezone.localdate()
        first = today.replace(day=1)
        self._log(self.a, 4, timezone.make_aware(datetime(first.year, first.month, 1)),
                  precision=InventoryLog.MONTH)

        cutoff = first + timedelta(days=14)
        run = labelmod.produced_since(cutoff)
        self.assertEqual(run.ambiguous_month_logs, 1)

        from_the_first = labelmod.produced_since(first)
        self.assertEqual(from_the_first.ambiguous_month_logs, 0,
                         "no ambiguity when the cutoff is the 1st")
        self.assertEqual(from_the_first.total, 4)
class LabelDensityTests(TestCase):
    """A barcode too dense to scan is a silent failure — it looks like a
    sticker and fails at the till with a queue behind you."""

    def setUp(self):
        self.stock = make_stock()
        self.recipe = make_recipe("Density Recipe")

    def test_the_stock_we_buy_prints_a_full_length_sku_readably(self):
        """SKUs are SLUG6-SLUG6, so 13 characters is the normal maximum."""
        _, mil = labelmod.barcode_for("SILKSC-STORMY", self.stock)
        self.assertGreater(mil, labelmod.MIN_MODULE_MIL)
        self.assertGreater(mil, 7.0, "comfortably scannable, not just legal")

    def test_shorter_skus_get_fatter_bars(self):
        """Sizing per label rather than per run is what buys this."""
        _, short = labelmod.barcode_for("SILK-TEAL", self.stock)
        _, long = labelmod.barcode_for("SILKSC-STORMY", self.stock)
        self.assertGreater(short, long)

    def test_a_narrow_stock_is_reported_as_a_problem(self):
        """The 1in stock we nearly bought, with a normal SKU."""
        narrow = make_stock(
            name="1in", label_width_in=Decimal("1.0"), columns=7,
            margin_left_in=Decimal("0.45"), pitch_x_in=Decimal("1.1"),
        )
        product = make_product(self.recipe, "Dense", with_image=False)
        product.sku, product.number_on_hand = "SILKSC-STORMY", 1
        product.save()

        run = labelmod.inventory_run()
        problems = labelmod.density_problems(run, narrow)
        self.assertEqual([sku for sku, _ in problems], ["SILKSC-STORMY"])
        self.assertEqual(labelmod.density_problems(run, self.stock), [],
                         "the stock we actually buy is fine")
class LabelViewTests(TestCase):
    """The page and the two PDFs."""

    def setUp(self):
        self.user = User.objects.create_superuser("labels", "l@example.test", "pw")
        self.client.force_login(self.user)
        self.stock = LabelStock.objects.get(name__startswith="Avery 5167")
        self.recipe = make_recipe("View Recipe")
        self.product = make_product(self.recipe, "View Product", with_image=False)
        self.product.sku, self.product.number_on_hand = "VIEW-ONE", 3
        self.product.save()

    def _params(self, **overrides):
        params = {
            "dataset": "inventory", "extra": "0",
            "stock": str(self.stock.pk), "start_at": "1",
        }
        params.update(overrides)
        return params

    def test_empty_page_renders_the_form(self):
        response = self.client.get(reverse("label_index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Start at label")

    def test_preview_reports_counts_before_anything_prints(self):
        response = self.client.get(reverse("label_index"), self._params(extra="2"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "VIEW-ONE")
        self.assertContains(response, "5 labels")   # 3 on hand + 2 extra

    def test_preview_shows_what_the_marker_will_say(self):
        response = self.client.get(reverse("label_index"), self._params())
        self.assertContains(response, "START AT 5")  # 3 labels, marker at 4

    def test_pdf_renders(self):
        response = self.client.get(reverse("label_pdf"), self._params())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_pdf_refuses_an_unreadable_run_instead_of_printing_it(self):
        narrow = make_stock(
            name="Too narrow", label_width_in=Decimal("1.0"), columns=7,
            margin_left_in=Decimal("0.45"), pitch_x_in=Decimal("1.1"),
        )
        self.product.sku = "SILKSC-STORMY"
        self.product.save()
        response = self.client.get(
            reverse("label_pdf"), self._params(stock=str(narrow.pk)), follow=True
        )
        self.assertContains(response, "too long for")
        self.assertNotEqual(response["Content-Type"], "application/pdf")

    def test_pdf_redirects_when_there_is_nothing_to_print(self):
        self.product.number_on_hand = 0
        self.product.save()
        response = self.client.get(reverse("label_pdf"), self._params(), follow=True)
        self.assertContains(response, "Nothing to print")

    def test_start_at_beyond_the_sheet_is_rejected(self):
        form = LabelRunForm(self._params(start_at="200"))
        self.assertFalse(form.is_valid())
        self.assertIn("start_at", form.errors)

    def test_since_dataset_needs_a_date(self):
        form = LabelRunForm(self._params(dataset="since", since=""))
        self.assertFalse(form.is_valid())
        self.assertIn("since", form.errors)

    def test_calibration_sheet_renders(self):
        response = self.client.get(
            reverse("label_calibration_pdf"), {"stock": self.stock.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))
class ContinuousRollTests(TestCase):
    """A thermal label printer is the same table with a 1 × 1 grid.

    Worth pinning because the sheet-oriented behaviours are actively wrong on
    a roll: a marker sticker would print after every run, cost a label, and be
    read by nobody.
    """

    def setUp(self):
        self.roll = make_stock(
            name="Rollo 2.25 x 1.25 roll",
            page_width_in=Decimal("2.25"), page_height_in=Decimal("1.25"),
            label_width_in=Decimal("2.25"), label_height_in=Decimal("1.25"),
            columns=1, rows=1,
            margin_left_in=Decimal("0"), margin_top_in=Decimal("0"),
            pitch_x_in=Decimal("2.25"), pitch_y_in=Decimal("1.25"),
        )
        recipe = make_recipe("Roll Recipe")
        self.product = make_product(recipe, "Roll Product", with_image=False)
        self.product.sku, self.product.number_on_hand = "ROLL-ONE", 5
        self.product.save()

    def test_a_one_by_one_stock_is_a_roll(self):
        self.assertTrue(self.roll.is_continuous)
        self.assertFalse(make_stock(name="Sheet").is_continuous)

    def test_no_marker_is_printed_on_a_roll(self):
        plan = labelmod.plan_sheets(self.roll, 5)
        self.assertIsNone(plan.marker_index)
        self.assertEqual(plan.sheet_count, 5, "one label per page")

    def test_one_label_per_page_at_the_media_size(self):
        run = labelmod.inventory_run()
        pdf = labelmod.render_run(run, self.roll)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(pdf.count(b"/Type /Page\n"), 5, "five labels, five pages")

    def test_a_roll_has_room_for_a_full_length_sku(self):
        _, mil = labelmod.barcode_for("SILKSC-STORMY", self.roll)
        self.assertGreater(mil, labelmod.MIN_MODULE_MIL)

    def test_geometry_still_has_to_close(self):
        self.assertEqual(self.roll.overflow_in(), (Decimal("0"), Decimal("0")))
class PrintShopTests(TestCase):
    """Printing at a shop rather than on a printer you own.

    Two assumptions break: you can't calibrate the machine beforehand, and you
    have no computer with you when the first sheet comes out wrong. So the
    offset has to be adjustable from the URL, and every sheet has to carry its
    own proof it wasn't scaled.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("shop", "s@example.test", "pw")
        self.client.force_login(self.user)
        self.stock = LabelStock.objects.get(name__startswith="Avery 5167")
        recipe = make_recipe("Shop Recipe")
        self.product = make_product(recipe, "Shop Product", with_image=False)
        self.product.sku, self.product.number_on_hand = "SHOP-ONE", 3
        self.product.save()

    def _params(self, **overrides):
        params = {
            "dataset": "inventory", "extra": "0",
            "stock": str(self.stock.pk), "start_at": "1",
        }
        params.update(overrides)
        return params

    def test_offset_can_be_overridden_from_the_query_string(self):
        form = LabelRunForm(self._params(x_offset_mm="1.5", y_offset_mm="-2"))
        self.assertTrue(form.is_valid(), form.errors)

        from scarves.views import _label_stock_from
        stock = _label_stock_from(form)
        self.assertEqual(stock.x_offset_mm, Decimal("1.5"))
        self.assertEqual(stock.y_offset_mm, Decimal("-2"))

    def test_an_override_is_never_written_back_to_the_stock(self):
        """A correction for one shop's machine on one day is not a property
        of the paper."""
        form = LabelRunForm(self._params(x_offset_mm="3"))
        self.assertTrue(form.is_valid(), form.errors)

        from scarves.views import _label_stock_from
        _label_stock_from(form)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.x_offset_mm, Decimal("0"))

    def test_a_blank_override_keeps_the_saved_offset(self):
        LabelStock.objects.filter(pk=self.stock.pk).update(x_offset_mm=Decimal("1"))
        form = LabelRunForm(self._params(x_offset_mm="", y_offset_mm=""))
        self.assertTrue(form.is_valid(), form.errors)

        from scarves.views import _label_stock_from
        self.assertEqual(_label_stock_from(form).x_offset_mm, Decimal("1"))

    def test_the_override_actually_moves_the_labels(self):
        plain = self.client.get(reverse("label_pdf"), self._params())
        nudged = self.client.get(reverse("label_pdf"), self._params(x_offset_mm="2"))
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(nudged.status_code, 200)
        self.assertNotEqual(plain.content, nudged.content)

    def test_absurd_offsets_are_refused(self):
        self.assertFalse(LabelRunForm(self._params(x_offset_mm="50")).is_valid())

    def test_every_sheet_carries_a_scale_check(self):
        """Catches a print dialog left on 'fit to page' — the failure that
        looks fine at the top of the sheet and cuts through labels at the
        bottom."""
        self.product.number_on_hand = 100   # spills onto a second sheet
        self.product.save()
        pdf = self.client.get(reverse("label_pdf"), self._params()).content
        text = _pdf_text(pdf)
        self.assertEqual(pdf.count(b"/Type /Page\n"), 2)
        self.assertEqual(text.count("must line up with a row of die-cuts"), 2,
                         "one check per sheet, not just the last")

    def test_the_check_spans_the_whole_sheet_not_one_inch(self):
        """The point of using the die-cuts: a 1in reference at 98% scale is
        out by half a millimetre and unreadable, while the same error over the
        full column stack is five and obvious. One tick per row, top to
        bottom, in the left margin."""
        pdf = self.client.get(reverse("label_pdf"), self._params()).content
        ticks = _pdf_lines(pdf, x_max=20)
        ys = sorted({round(y, 1) for _, y in ticks})
        self.assertGreaterEqual(len(ys), self.stock.rows,
                                "a tick for every row of die-cuts")

        expected_top = labelmod.slot_for(self.stock, 0).y + labelmod._pt(
            self.stock.label_height_in)
        expected_bottom = labelmod.slot_for(
            self.stock, (self.stock.rows - 1) * self.stock.columns).y
        self.assertAlmostEqual(max(ys), expected_top, delta=0.5)
        self.assertAlmostEqual(min(ys), expected_bottom, delta=0.5)

    def test_the_scale_check_stays_off_the_labels(self):
        """It's diagnostics, not product — it belongs on the liner."""
        pdf = self.client.get(reverse("label_pdf"), self._params()).content
        first_column_x = labelmod._pt(self.stock.margin_left_in)
        for x, _ in _pdf_lines(pdf, x_max=20):
            self.assertLess(x, first_column_x,
                            "ticks must stay left of the first label column")

    def test_the_scale_check_names_the_offset_in_use(self):
        """So a reprint at a different nudge is tellable from the one before."""
        pdf = self.client.get(reverse("label_pdf"), self._params(x_offset_mm="1.5")).content
        self.assertIn("nudge 1.5/0", _pdf_text(pdf))

    def test_a_roll_gets_no_ruler(self):
        """The page is the label; there's no margin to put one in."""
        roll = make_stock(
            name="Roll", page_width_in=Decimal("2.25"), page_height_in=Decimal("1.25"),
            label_width_in=Decimal("2.25"), label_height_in=Decimal("1.25"),
            columns=1, rows=1, margin_left_in=Decimal("0"), margin_top_in=Decimal("0"),
            pitch_x_in=Decimal("2.25"), pitch_y_in=Decimal("1.25"),
        )
        pdf = self.client.get(reverse("label_pdf"), self._params(stock=str(roll.pk))).content
        self.assertNotIn("must measure exactly 1 inch", _pdf_text(pdf))
class CalibrationSheetTests(TestCase):
    """The plain-paper sheet that makes a first trip to the print shop enough."""

    def setUp(self):
        self.user = User.objects.create_superuser("cal", "c@example.test", "pw")
        self.client.force_login(self.user)
        self.stock = LabelStock.objects.get(name__startswith="Avery 5167")

    def _pdf(self, stock=None):
        return self.client.get(
            reverse("label_calibration_pdf"), {"stock": (stock or self.stock).pk}
        ).content

    def test_it_answers_orientation_before_millimetres(self):
        """Four ways a sheet goes into a bypass tray, three of them wrong. A
        180° error reads as a plausible offset and sends you chasing a nudge
        that never converges, so this has to be settled first."""
        text = _pdf_text(self._pdf())
        self.assertIn("TOP OF PRINT", text)
        self.assertIn("Write TOP and a feed arrow", text)

    def test_the_top_banner_is_actually_at_the_top(self):
        """Stating the top edge somewhere other than the top would be worse
        than not stating it."""
        first_row_top = (
            labelmod.slot_for(self.stock, 0).y
            + labelmod._pt(self.stock.label_height_in)
        )
        page_w = labelmod._pt(self.stock.page_width_in)
        page_h = labelmod._pt(self.stock.page_height_in)

        banner = [i for i in _pdf_text_items(self._pdf()) if "TOP OF PRINT" in i[2]]
        self.assertEqual(len(banner), 1)
        x, y, _ = banner[0]
        self.assertGreater(y, first_row_top, "banner sits in the top margin")
        self.assertLess(y, page_h)
        self.assertAlmostEqual(x, page_w / 2, delta=60, msg="roughly centred")

    def test_it_carries_a_millimetre_vernier(self):
        text = _pdf_text(self._pdf())
        self.assertIn("read the die-cut corner", text)
        for label in ("+1", "-1", "+3", "-3"):
            self.assertIn(label, text)

    def test_it_says_which_scale_is_which_axis(self):
        """Inferring it from the geometry is possible and takes a moment
        nobody has at a print counter; guessing the sign doubles the error."""
        text = _pdf_text(self._pdf())
        self.assertIn('TOP scale = "nudge right" mm', text)
        self.assertIn('LEFT scale = "nudge up" mm', text)
        self.assertIn("no sign to flip", text)

    def test_the_horizontal_scale_is_above_and_the_vertical_one_left(self):
        """What the caption claims has to match where the ink actually is."""
        corner = labelmod.slot_for(self.stock, 0)
        ox = corner.x
        oy = corner.y + labelmod._pt(self.stock.label_height_in)

        numbered = [i for i in _pdf_text_items(self._pdf())
                    if i[2] in ("+1", "-1", "+2", "-2", "+3", "-3")]
        self.assertEqual(len(numbered), 12, "six labels on each of two scales")

        # Tell the scales apart by which axis they hold constant, not by
        # position — the left scale's positive labels also sit above the
        # corner, so a position filter catches both.
        from collections import Counter

        # The horizontal scale is centred on each tick, so all six share one
        # baseline exactly. Group on that; the vertical scale's x values are
        # *not* uniform, because it's right-aligned and Helvetica's "+" is
        # wider than its "-".
        rows = Counter(round(i[1]) for i in numbered)
        row_y, row_n = rows.most_common(1)[0]
        self.assertEqual(row_n, 6, "six labels sharing one baseline")

        across = [i for i in numbered if round(i[1]) == row_y]
        down = [i for i in numbered if round(i[1]) != row_y]
        self.assertEqual(len(down), 6)

        # Swapped, the sheet would be perfectly readable and wrong.
        self.assertGreater(row_y, oy, "the 'nudge right' scale sits above")
        self.assertEqual(len({round(i[0]) for i in across}), 6,
                         "the top scale steps sideways")
        self.assertEqual(len({round(i[1]) for i in down}), 6,
                         "the left scale steps vertically")
        for x, _, _ in down:
            self.assertLess(x, ox, "the 'nudge up' scale sits to the left")

    def test_every_label_position_is_numbered(self):
        """So a marker sticker reading 57 can be found on the sheet."""
        text = _pdf_text(self._pdf())
        for n in ("1", "40", "80"):
            self.assertIn(n, text)

    def test_a_stock_with_no_top_margin_skips_the_banner(self):
        roll = make_stock(
            name="Roll", page_width_in=Decimal("2.25"), page_height_in=Decimal("1.25"),
            label_width_in=Decimal("2.25"), label_height_in=Decimal("1.25"),
            columns=1, rows=1, margin_left_in=Decimal("0"), margin_top_in=Decimal("0"),
            pitch_x_in=Decimal("2.25"), pitch_y_in=Decimal("1.25"),
        )
        self.assertNotIn("TOP OF PRINT", _pdf_text(self._pdf(roll)))
class LabelFormFieldVisibilityTests(TestCase):
    """Controls the chosen dataset doesn't read are hidden client-side.

    A date box that changes nothing is worse than no date box — it looks like
    it's filtering and quietly isn't. The toggling itself is JS, so what's
    checked here is the markup it hangs off: if a field loses its `data-when`
    in a refactor, it silently becomes a dead control again.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("vis", "v@example.test", "pw")
        self.client.force_login(self.user)

    def test_each_dataset_only_field_is_tagged(self):
        html = self.client.get(reverse("label_index")).content.decode()

        for field, dataset in (
            ("since", "since"),
            ("category", "inventory"),
            ("raw_products", "inventory"),
            ("include_zero", "inventory"),
        ):
            with self.subTest(field=field):
                block = re.search(
                    r'<div class="field" data-when="(\w+)">(?:(?!</div>).)*?'
                    r'id_' + field,
                    html, re.S,
                )
                self.assertIsNotNone(block, f"{field} is not tagged data-when")
                self.assertEqual(block.group(1), dataset)

    def test_fields_every_dataset_reads_are_not_tagged(self):
        """stock and start_at apply to any run and must always show."""
        html = self.client.get(reverse("label_index")).content.decode()
        for field in ("stock", "start_at"):
            with self.subTest(field=field):
                block = re.search(
                    r'<div class="field"( data-when="\w+")?>'
                    r'(?:(?!</div>).)*?id_' + field,
                    html, re.S,
                )
                self.assertIsNotNone(block)
                self.assertIsNone(block.group(1), f"{field} must not be hidden")

    def test_hidden_fields_still_submit_and_are_ignored(self):
        """They stay in the DOM, so the view has to tolerate values the
        dataset doesn't use rather than erroring on them."""
        response = self.client.get(reverse("label_index"), {
            "dataset": "since",
            "since": (timezone.localdate() - timedelta(days=7)).isoformat(),
            "extra": "0",
            "stock": str(LabelStock.objects.first().pk),
            "start_at": "1",
            "include_zero": "on",      # inventory-only, sent anyway
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].is_valid(),
                        response.context["form"].errors)
class BlankFilterTests(TestCase):
    """Narrowing a bulk re-label to particular blanks."""

    def setUp(self):
        recipe = make_recipe("Blank Filter Recipe")
        self.products = {}
        for name in ("Scarf", "Skein", "Runner"):
            product = make_product(recipe, f"{name} Product", with_image=False)
            product.raw_product.name = f"raw-{name}"
            product.raw_product.save()
            product.sku, product.number_on_hand = f"{name[:4].upper()}-ONE", 2
            product.save()
            self.products[name] = product

    def test_no_ticks_means_every_blank(self):
        """Not 'none' — a filter that silently prints nothing when you forget
        to tick is a wasted trip to the print shop."""
        self.assertEqual(len(labelmod.inventory_run().rows), 3)
        self.assertEqual(len(labelmod.inventory_run(raw_products=[]).rows), 3)
        self.assertEqual(len(labelmod.inventory_run(raw_products=None).rows), 3)

    def test_several_blanks_can_be_picked_at_once(self):
        picked = [self.products[n].raw_product for n in ("Scarf", "Runner")]
        run = labelmod.inventory_run(raw_products=picked)
        self.assertEqual(
            sorted(r.product.sku for r in run.rows), ["RUNN-ONE", "SCAR-ONE"]
        )

    def test_one_blank_still_works(self):
        run = labelmod.inventory_run(raw_products=[self.products["Skein"].raw_product])
        self.assertEqual([r.product.sku for r in run.rows], ["SKEI-ONE"])

    def test_the_page_renders_them_as_checkboxes(self):
        user = User.objects.create_superuser("blank", "b@example.test", "pw")
        self.client.force_login(user)
        html = self.client.get(reverse("label_index")).content.decode()
        self.assertIn('type="checkbox" name="raw_products"', html)
        self.assertEqual(html.count('name="raw_products"'), 3)

    def test_picking_blanks_through_the_view(self):
        user = User.objects.create_superuser("blank2", "b2@example.test", "pw")
        self.client.force_login(user)
        picked = [self.products[n].raw_product.pk for n in ("Scarf", "Runner")]
        response = self.client.get(reverse("label_index"), {
            "dataset": "inventory", "extra": "0",
            "stock": str(LabelStock.objects.first().pk), "start_at": "1",
            "raw_products": [str(pk) for pk in picked],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sorted(r.product.sku for r in response.context["run"].rows),
            ["RUNN-ONE", "SCAR-ONE"],
        )
class SpecificItemsTests(TestCase):
    """The hand-picked run: exactly these products, exactly these counts.

    Built because deferring it was the reliable way to need it. The shape is
    additive — type three SKUs — rather than unticking 297 rows off a filter.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("pick", "p@example.test", "pw")
        self.client.force_login(self.user)
        recipe = make_recipe("Picked Recipe")
        self.a = make_product(recipe, "Picked A", with_image=False)
        self.b = make_product(recipe, "Picked B", with_image=False)
        self.a.sku, self.a.number_on_hand = "PICK-AAA", 99
        self.a.save()
        self.b.sku, self.b.number_on_hand = "PICK-BBB", 0
        self.b.save()

    def _params(self, items, **overrides):
        params = {
            "dataset": "items", "extra": "0",
            "stock": str(LabelStock.objects.first().pk), "start_at": "1",
            "items": items,
        }
        params.update(overrides)
        return params

    def test_it_prints_exactly_what_was_asked_for(self):
        run = labelmod.specific_items([(self.a, 3), (self.b, 1)])
        self.assertEqual(run.total, 4)
        self.assertEqual([(r.product.sku, r.quantity) for r in run.rows],
                         [("PICK-AAA", 3), ("PICK-BBB", 1)])

    def test_on_hand_is_irrelevant(self):
        """Unlike the bulk runs — you asked for it, so it prints."""
        run = labelmod.specific_items([(self.b, 5)])
        self.assertEqual(run.total, 5)

    def test_extras_do_not_apply(self):
        """The bulk datasets add spares because their counts are derived.
        Here somebody typed the number, so adding to it would surprise."""
        response = self.client.get(
            reverse("label_index"), self._params([f"{self.a.pk}:3"], extra="5")
        )
        self.assertEqual(response.context["run"].total, 3)

    def test_the_same_item_twice_sums(self):
        """Two rows for one SKU is a sum written confusingly."""
        form = LabelRunForm(self._params([f"{self.a.pk}:2", f"{self.a.pk}:3"]))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["items"], [(self.a, 5)])

    def test_a_run_survives_as_a_url(self):
        """The whole point of keeping it in the query string: reopen it."""
        params = self._params([f"{self.a.pk}:2", f"{self.b.pk}:1"])
        response = self.client.get(reverse("label_index"), params)
        self.assertEqual(response.context["run"].total, 3)

        pdf = self.client.get(reverse("label_pdf"), params)
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(_pdf_text(pdf.content).count("PICK-AAA"), 2)
        self.assertEqual(_pdf_text(pdf.content).count("PICK-BBB"), 1)

    def test_picking_nothing_is_an_error_not_an_empty_sheet(self):
        form = LabelRunForm(self._params([]))
        self.assertFalse(form.is_valid())
        self.assertIn("items", form.errors)

    def test_absurd_counts_are_refused(self):
        self.assertFalse(LabelRunForm(self._params([f"{self.a.pk}:400"])).is_valid())
        self.assertFalse(LabelRunForm(self._params([f"{self.a.pk}:0"])).is_valid())

    def test_a_deleted_item_is_reported_not_skipped(self):
        form = LabelRunForm(self._params(["99999:2"]))
        self.assertFalse(form.is_valid())
        self.assertIn("no longer exist", str(form.errors["items"]))

    def test_the_list_survives_an_unrelated_error(self):
        """A bad start row must not wipe a list somebody just built by hand."""
        form = LabelRunForm(self._params([f"{self.a.pk}:2"], start_at="900"))
        self.assertFalse(form.is_valid())
        self.assertEqual(form.items_value, [(self.a, 2)])

    def test_the_page_renders_picked_rows_back(self):
        response = self.client.get(
            reverse("label_index"), self._params([f"{self.a.pk}:4"])
        )
        html = response.content.decode()
        self.assertIn('data-pk="%d"' % self.a.pk, html)
        self.assertIn('value="4"', html)
class LabelItemSearchTests(TestCase):
    """The type-ahead, reusing the upload page's endpoint."""

    def setUp(self):
        self.user = User.objects.create_superuser("srch", "s@example.test", "pw")
        self.client.force_login(self.user)
        recipe = make_recipe("Search Recipe")
        self.withsku = make_product(recipe, "Stormy Silk", with_image=False)
        self.withsku.sku = "SILKSC-STORMY"
        self.withsku.save()
        self.nosku = make_product(recipe, "Stormy Unlabelled", with_image=False)
        # Predates SKU-on-create; only a queryset write can make one now.
        FinishedProduct.objects.filter(pk=self.nosku.pk).update(sku="")

    def test_it_finds_by_name_and_by_sku(self):
        for q in ("Stormy", "SILKSC"):
            response = self.client.get(
                reverse("product_search"), {"q": q, "mode": "labels"}
            )
            self.assertContains(response, "SILKSC-STORMY")

    def test_products_without_a_sku_are_shown_but_unpickable(self):
        """Filtering them out silently means someone searches, doesn't see
        their product, and has no idea why. Shown and disabled says both that
        it exists and what to do about it."""
        response = self.client.get(
            reverse("product_search"), {"q": "Stormy", "mode": "labels"}
        )
        self.assertContains(response, "Stormy Unlabelled")
        self.assertContains(response, "run generate_skus")

        html = response.content.decode()
        block = re.search(r"<button[^>]*>\s*Stormy Unlabelled.*?</button>", html, re.S)
        self.assertIsNotNone(block)
        self.assertIn("disabled", block.group(0))
        self.assertNotIn("data-pk", block.group(0),
                         "a disabled result must carry nothing the adder can use")

    def test_the_upload_picker_still_offers_them(self):
        """That flow assigns a photo and doesn't care about barcodes."""
        response = self.client.get(
            reverse("product_search"), {"q": "Stormy", "upload_id": "1"}
        )
        self.assertContains(response, "Stormy Unlabelled")

    def test_results_carry_what_a_row_needs(self):
        response = self.client.get(
            reverse("product_search"), {"q": "Stormy", "mode": "labels"}
        )
        self.assertContains(response, 'data-pk="%d"' % self.withsku.pk)
        self.assertContains(response, 'data-sku="SILKSC-STORMY"')

    def test_it_needs_a_login(self):
        self.client.logout()
        response = self.client.get(reverse("product_search"), {"q": "x"})
        self.assertEqual(response.status_code, 302)
class GenerateSkusOverwriteTests(TestCase):
    """`--overwrite` got dangerous the day labels became printable.

    A SKU in the database is an edit; a SKU on a sticker stuck to a scarf, and
    in Square's catalogue, is neither. Regenerating orphans both, and the
    symptom is an item scanning to nothing at the till weeks later.
    """

    def setUp(self):
        recipe = make_recipe("SKU Recipe")
        self.existing = make_product(recipe, "Has A Sku", with_image=False)
        self.existing.sku = "PRINTED-CODE"
        self.existing.save()
        self.blank = make_product(recipe, "Needs A Sku", with_image=False)
        FinishedProduct.objects.filter(pk=self.blank.pk).update(sku="")
        self.blank.refresh_from_db()

    def _run(self, **kwargs):
        out = StringIO()
        call_command("generate_skus", stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_a_plain_run_only_fills_blanks(self):
        self._run()
        self.existing.refresh_from_db()
        self.blank.refresh_from_db()
        self.assertEqual(self.existing.sku, "PRINTED-CODE", "never touched")
        self.assertTrue(self.blank.sku)

    def test_overwrite_aborts_unless_confirmed(self):
        with mock.patch("builtins.input", return_value="no"):
            output = self._run(overwrite=True)
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.sku, "PRINTED-CODE")
        self.assertIn("Aborted", output)

    def test_overwrite_says_what_it_will_break(self):
        with mock.patch("builtins.input", return_value="no"):
            output = self._run(overwrite=True)
        self.assertIn("1 SKU(s)", output)
        self.assertIn("scan to nothing", output)

    def test_overwrite_proceeds_once_confirmed(self):
        with mock.patch("builtins.input", return_value="yes"):
            self._run(overwrite=True)
        self.existing.refresh_from_db()
        self.assertNotEqual(self.existing.sku, "PRINTED-CODE")

    def test_noinput_skips_the_prompt(self):
        """For scripts — but it still prints the warning."""
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            output = self._run(overwrite=True, interactive=False)
        self.existing.refresh_from_db()
        self.assertNotEqual(self.existing.sku, "PRINTED-CODE")
        self.assertIn("scan to nothing", output)

    def test_no_prompt_when_nothing_is_at_risk(self):
        FinishedProduct.objects.update(sku="")
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            self._run(overwrite=True)
        self.blank.refresh_from_db()
        self.assertTrue(self.blank.sku)
class SkuOnCreateTests(TestCase):
    """SKUs are assigned when a product is created, not when someone
    remembers to run a command.

    Generation used to live only in `generate_skus`, so anything made through
    the admin, the bulk matrix or a shell had no barcode — and nothing said
    so. It simply wasn't printable and wasn't scannable.
    """

    def setUp(self):
        self.recipe = make_recipe("Sunset Glow")
        self.category, _ = RawProductCategory.objects.get_or_create(name="Silk")

    def _raw(self, name):
        raw, _ = RawProduct.objects.get_or_create(
            name=name, category=self.category, defaults={"price": "5.00"}
        )
        return raw

    def test_a_new_product_gets_a_sku(self):
        fp = FinishedProduct.objects.create(
            name="Sunset Silk", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00",
        )
        self.assertEqual(fp.sku, "SILKSC-SUNSET")

    def test_it_survives_a_reload(self):
        """Set in memory but not persisted would be the subtle version."""
        fp = FinishedProduct.objects.create(
            name="Sunset Silk", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00",
        )
        fp.refresh_from_db()
        self.assertEqual(fp.sku, "SILKSC-SUNSET")

    def test_an_explicit_sku_is_respected(self):
        fp = FinishedProduct.objects.create(
            name="Sunset Silk", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00", sku="HAND-PICKED",
        )
        self.assertEqual(fp.sku, "HAND-PICKED")

    def test_an_existing_sku_is_never_rewritten(self):
        """It's on stickers and in Square; this app can rewrite neither."""
        fp = FinishedProduct.objects.create(
            name="Sunset Silk", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00",
        )
        original = fp.sku
        fp.raw_product.name = "Completely Different Blank"
        fp.raw_product.save()
        fp.price = "35.00"
        fp.save()
        fp.refresh_from_db()
        self.assertEqual(fp.sku, original)

    def test_collisions_get_a_suffix(self):
        first = FinishedProduct.objects.create(
            name="One", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00",
        )
        second = FinishedProduct.objects.create(
            name="Two", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00",
        )
        self.assertEqual(first.sku, "SILKSC-SUNSET")
        self.assertEqual(second.sku, "SILKSC-SUNSET2")

    def test_update_fields_still_persists_a_generated_sku(self):
        """A caller narrowing the write didn't know a SKU was coming; without
        adding it the value is set in memory and silently dropped."""
        fp = FinishedProduct.objects.create(
            name="Sunset Silk", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00",
        )
        FinishedProduct.objects.filter(pk=fp.pk).update(sku="")
        fp.refresh_from_db()
        self.assertEqual(fp.sku, "")

        fp.number_on_hand = 7
        fp.save(update_fields=["number_on_hand"])
        fp.refresh_from_db()
        self.assertEqual(fp.number_on_hand, 7)
        self.assertTrue(fp.sku, "the generated SKU has to reach the database")

    def test_the_command_and_save_agree(self):
        """One definition of a SKU, used by both paths."""
        fp = FinishedProduct.objects.create(
            name="Sunset Silk", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00",
        )
        by_save = fp.sku
        FinishedProduct.objects.filter(pk=fp.pk).update(sku="")
        call_command("generate_skus", stdout=StringIO())
        fp.refresh_from_db()
        self.assertEqual(fp.sku, by_save)
class FixtureSkuTests(TestCase):
    """`loaddata` must not invent SKUs.

    It goes through `save_base(raw=True)` rather than `save()`, so a fixture
    that deliberately carries a blank SKU stays blank. Pinned because the
    alternative — fixtures quietly gaining generated values — would make
    `diff_fixture` round-trips report changes nobody made.
    """

    def test_a_blank_sku_in_a_fixture_stays_blank(self):
        from django.core import serializers

        recipe = make_recipe("Fixture Recipe")
        product = make_product(recipe, "Fixture Product", with_image=False)
        self.assertTrue(product.sku, "created normally, so it has one")

        payload = serializers.serialize("json", [product])
        payload = payload.replace(f'"sku": "{product.sku}"', '"sku": ""')

        for deserialized in serializers.deserialize("json", payload):
            deserialized.save()          # the path loaddata uses

        product.refresh_from_db()
        self.assertEqual(product.sku, "", "loaddata must not generate one")
class LabelsIncludeAddedStockTests(TestCase):
    """Stock counted in gets barcodes too.

    `produced_since` filtered on PRODUCTION, so anything entering through
    `bulk_inventory_update` — a bag found in a cupboard, a display rack folded
    back into inventory, stock that predates this app — got no labels at all.
    Nothing said so. The symptom arrives later and elsewhere: a scarf that
    won't scan at the till, in front of a customer, with the queue waiting.

    The fix errs toward printing, which is the cheap direction. A spare
    sticker sits in a drawer; a missing one costs the sale.
    """

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_product(self.recipe, "Stormy Silk", with_image=False)
        self.cutoff = timezone.localdate() - timedelta(days=7)

    def _log(self, log_type, quantity):
        return InventoryLog.objects.create(
            finished_product=self.product,
            raw_product=self.product.raw_product,
            log_type=log_type,
            quantity=quantity,
        )

    def _quantity(self):
        run = labelmod.produced_since(self.cutoff)
        rows = [r for r in run.rows if r.product.pk == self.product.pk]
        return sum(r.quantity for r in rows)

    def test_a_bulk_adjustment_gets_labels(self):
        self._log(InventoryLog.ADJUSTMENT, 12)

        self.assertEqual(self._quantity(), 12)

    def test_dyeing_and_added_stock_add_up(self):
        self._log(InventoryLog.PRODUCTION, 4)
        self._log(InventoryLog.ADJUSTMENT, 12)

        self.assertEqual(self._quantity(), 16)

    def test_stock_leaving_asks_for_no_labels(self):
        """A barcode answers 'what is this thing in my hand'. Nothing is in
        anyone's hand when the count goes down."""
        self._log(InventoryLog.ADJUSTMENT, -3)

        self.assertEqual(self._quantity(), 0)

    def test_a_downward_correction_does_not_eat_a_bath_s_stickers(self):
        """The scarves from that bath exist and need labelling. A separate
        correction in the same week is about different units."""
        self._log(InventoryLog.PRODUCTION, 4)
        self._log(InventoryLog.ADJUSTMENT, -3)

        self.assertEqual(self._quantity(), 4)

    def test_sales_are_still_ignored(self):
        """Unchanged, and load-bearing: a sold scarf left wearing its sticker,
        so netting sales in would subtract labels already stuck to things."""
        self._log(InventoryLog.PRODUCTION, 5)
        self._log(InventoryLog.SALE, -3)

        self.assertEqual(self._quantity(), 5)

    def test_an_old_adjustment_is_outside_the_cutoff(self):
        log = self._log(InventoryLog.ADJUSTMENT, 9)
        InventoryLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timedelta(days=60)
        )

        self.assertEqual(self._quantity(), 0)
class LabelStyleTests(TestCase):
    """Three flavours of one sticker, off one pipeline.

    The barcode was assumed to be the important half of a label until the
    physical job was looked at: stickers are applied by hand, off a sheet,
    onto a pile of scarves. You cannot apply a sticker you cannot read, so the
    text is what makes the sheet usable and the barcode is what makes the
    scarf usable later. Both matter; they just aren't the same job.

    What the text says follows from what gets confused. Nobody mistakes a
    rectangle for a half-circle, so the blank is visible and `BLANK-DYEBATH`
    spends half a small label saying it. Two reds called Valentine and L Word
    is the confusion a label can fix.
    """

    def setUp(self):
        self.stock = LabelStock.objects.create(
            name="Test 80up", page_width_in=Decimal("8.5"),
            page_height_in=Decimal("11"), label_width_in=Decimal("1.75"),
            label_height_in=Decimal("0.5"), rows=20, columns=4,
            margin_left_in=Decimal("0.3"), margin_top_in=Decimal("0.5"),
            pitch_x_in=Decimal("2.0"), pitch_y_in=Decimal("0.5"),
        )
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_product(self.recipe, "Stormy Silk", with_image=False)
        self.product.number_on_hand = 3
        self.product.save()

    def _run(self, style):
        return labelmod.inventory_run(style=style)

    def test_the_name_style_prints_the_colorway_not_the_product(self):
        """`variation_name` is what Square calls the variation, so the sticker
        says the same string the crew is hunting for in the list — not a
        translation of it."""
        self.assertEqual(self.product.variation_name, "Stormy Sea")

    def test_a_name_run_renders(self):
        pdf = labelmod.render_run(self._run(labelmod.NAME), self.stock)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_the_density_guard_does_not_refuse_a_run_with_no_bars(self):
        """The guard exists because unscannable bars fail silently at the
        till. A sticker reading 'Stormy Sea' has nothing to fail — and letting
        this fire would refuse the very style invented to cope with stock too
        narrow for the SKU."""
        narrow = LabelStock.objects.create(
            name="Too narrow", page_width_in=Decimal("8.5"),
            page_height_in=Decimal("11"), label_width_in=Decimal("0.6"),
            label_height_in=Decimal("0.5"), rows=20, columns=4,
            margin_left_in=Decimal("0.3"), margin_top_in=Decimal("0.5"),
            pitch_x_in=Decimal("0.7"), pitch_y_in=Decimal("0.5"),
        )
        self.assertTrue(
            labelmod.density_problems(self._run(labelmod.BARCODE), narrow),
            "the barcode style should still object to this stock",
        )
        self.assertEqual(
            labelmod.density_problems(self._run(labelmod.NAME), narrow), [],
        )

    def test_a_product_with_no_sku_still_gets_a_name_label(self):
        """A barcode of nothing is unprintable; "Stormy Sea" needs no SKU. The
        two styles are independent runs, so they are free to disagree here."""
        blank_sku = make_product(make_recipe("Nameless"), "Nameless Silk",
                                 with_image=False)
        blank_sku.number_on_hand = 4
        blank_sku.save()
        FinishedProduct.objects.filter(pk=blank_sku.pk).update(sku="")

        barcode_run = self._run(labelmod.BARCODE)
        self.assertIn(blank_sku.pk, [p.pk for p in barcode_run.skipped_no_sku])

        name_run = self._run(labelmod.NAME)
        self.assertEqual(name_run.skipped_no_sku, [])
        self.assertIn(blank_sku.pk,
                      [p.pk for p in name_run.flat(self.stock.columns)])

    def test_the_barcode_style_is_unchanged(self):
        """Today's sheet keeps working exactly as it does — bars with the SKU
        underneath — and stays the default for a form that never mentions
        style."""
        run = self._run(labelmod.BARCODE)
        self.assertTrue(run.needs_barcode)
        self.assertEqual(labelmod.LabelRun([], [], 0).style, labelmod.BARCODE)

    def test_a_long_colorway_is_shortened_rather_than_overflowed(self):
        """Text running past the die-cut lands on the *next* label and
        mislabels a second scarf, so the floor truncates instead."""
        from reportlab.pdfgen import canvas
        import io

        pdf = canvas.Canvas(io.BytesIO())
        size, lines = labelmod._wrap_to_fit(
            pdf, "Extraordinarily Verbose Colorway Name That Will Never Fit",
            max_w=40, max_h=28.8,
            max_pt=labelmod.NAME_MAX_PT, min_pt=labelmod.NAME_MIN_PT,
        )
        self.assertGreaterEqual(size, labelmod.NAME_MIN_PT)
        for line in lines:
            self.assertLessEqual(
                pdf.stringWidth(line, "Helvetica-Bold", size), 40 + 0.01
            )

    def test_the_style_reaches_the_run_from_the_form(self):
        user = User.objects.create_user("labels-staff", password="pw")
        self.client.force_login(user)

        response = self.client.get(reverse("label_index"), {
            "dataset": "inventory", "style": labelmod.NAME,
            "extra": "0", "start_at": "1", "stock": str(self.stock.pk),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["run"].style, labelmod.NAME)
class NameBoxWidthTests(TestCase):
    """Text is measured against the bars, not against the label.

    `barcode.width` is the trap and it is the one CLAUDE.md already warns
    about from the production-sheet side: reportlab pins Code128 quiet zones
    at a quarter inch a side and never scales them, so the drawn object comes
    out *wider than the sticker* — 134.8pt of object on a 126pt label. Text
    fitted to the label got a fifth of an inch a side more room than the bars
    above it used, ran visibly wider than the symbol, and read as overflowing.
    """

    def setUp(self):
        self.stock = LabelStock.objects.create(
            name="1.75x0.5", page_width_in=Decimal("8.5"),
            page_height_in=Decimal("11"), label_width_in=Decimal("1.75"),
            label_height_in=Decimal("0.5"), rows=20, columns=4,
            margin_left_in=Decimal("0.3"), margin_top_in=Decimal("0.5"),
            pitch_x_in=Decimal("2.0"), pitch_y_in=Decimal("0.5"),
        )
        self.product = make_product(make_recipe("Stormy Sea"), "Stormy Silk",
                                    with_image=False)

    def test_the_object_really_is_wider_than_the_label(self):
        """Pinned because everything else here follows from it, and because a
        reportlab change would otherwise silently move the text box."""
        barcode, _ = labelmod.barcode_for(self.product.sku, self.stock)
        label_pt = float(self.stock.label_width_in) * 72

        self.assertGreater(barcode.width, label_pt)
        self.assertEqual(barcode.lquiet, 18.0)
        self.assertEqual(barcode.rquiet, 18.0)

    def test_bars_only_width_strips_the_quiet_zones(self):
        barcode, _ = labelmod.barcode_for(self.product.sku, self.stock)

        self.assertAlmostEqual(
            labelmod.bars_only_width(barcode),
            barcode.width - barcode.lquiet - barcode.rquiet,
        )

    def test_the_text_box_is_the_bars_and_fits_the_label(self):
        box = labelmod.name_box_width(self.product, self.stock)
        barcode, _ = labelmod.barcode_for(self.product.sku, self.stock)
        label_pt = float(self.stock.label_width_in) * 72

        self.assertAlmostEqual(box, labelmod.bars_only_width(barcode))
        self.assertLess(box, label_pt)

    def test_a_product_with_no_sku_falls_back_to_the_padded_label(self):
        """Only reachable in a name-only run, where there are no bars to
        disagree with."""
        FinishedProduct.objects.filter(pk=self.product.pk).update(sku="")
        self.product.refresh_from_db()

        expected = float(self.stock.label_width_in) * 72 - labelmod._pt(labelmod.PAD_IN) * 2
        self.assertAlmostEqual(
            labelmod.name_box_width(self.product, self.stock), expected)

    def test_text_never_exceeds_the_box(self):
        from reportlab.pdfgen import canvas
        import io

        pdf = canvas.Canvas(io.BytesIO())
        box = labelmod.name_box_width(self.product, self.stock)
        for name in ("Stormy Sea", "Valentine", "Chartreuse Neon",
                     "Autumn Harvest Moonrise", "Supercalifragilistic"):
            size, lines = labelmod._wrap_to_fit(
                pdf, name, box, 28.8,
                max_pt=labelmod.NAME_MAX_PT, min_pt=labelmod.NAME_MIN_PT,
            )
            for line in lines:
                self.assertLessEqual(
                    pdf.stringWidth(line, "Helvetica-Bold", size), box + 0.01,
                    f"{name!r} at {size}pt runs past the bars",
                )

    def test_a_tall_block_is_clamped_to_its_band(self):
        """Wrapping to a second line to satisfy the width is what makes a
        block too tall, so a size passing one test can fail the other."""
        from reportlab.pdfgen import canvas
        import io

        pdf = canvas.Canvas(io.BytesIO())
        size, lines = labelmod._wrap_to_fit(
            pdf, "Autumn Harvest Moonrise", 98.8, 12.0,
            max_pt=labelmod.NAME_MAX_PT, min_pt=labelmod.NAME_MIN_PT,
        )

        self.assertLessEqual(size * labelmod.LINE_SPACING * len(lines), 12.0)
class BothSetsTests(TestCase):
    """`both` is two sets of stickers in one file, not two things on a sticker.

    Each label stays exactly what it is. A barcode label keeps the SKU
    underneath — that is what the bars encode, so it is the text that belongs
    beside them; captioning a barcode with the recipe would label it with
    something it doesn't say. A name label carries the recipe, which is what
    gets confused when you are matching a sticker to a scarf by hand.

    Printing them as one job rather than two is the whole feature: the sets
    run continuously, so the name set starts in the gap the barcode set left
    on the last sheet instead of wasting it.
    """

    def setUp(self):
        self.stock = LabelStock.objects.create(
            name="1.75x0.5", page_width_in=Decimal("8.5"),
            page_height_in=Decimal("11"), label_width_in=Decimal("1.75"),
            label_height_in=Decimal("0.5"), rows=20, columns=4,
            margin_left_in=Decimal("0.3"), margin_top_in=Decimal("0.5"),
            pitch_x_in=Decimal("2.0"), pitch_y_in=Decimal("0.5"),
        )
        self.a = make_product(make_recipe("Stormy Sea"), "Stormy Silk",
                              with_image=False)
        self.b = FinishedProduct.objects.create(
            name="Ember Silk", raw_product=self.a.raw_product,
            recipe=make_recipe("Ember"), price="30.00",
        )
        for p, n in ((self.a, 3), (self.b, 2)):
            p.number_on_hand = n
            p.save()

    def _run(self, style):
        return labelmod.inventory_run(style=style)

    def test_it_prints_a_barcode_set_then_a_name_set(self):
        run = self._run(labelmod.NAME_AND_BARCODE)
        self.assertEqual(run.segments, [labelmod.BARCODE, labelmod.NAME])

    def test_every_sticker_is_one_style_or_the_other(self):
        styled = self._run(labelmod.NAME_AND_BARCODE).styled_sequence(
            self.stock.columns)
        used = [st for p, st in styled if p is not None]

        self.assertEqual(set(used), {labelmod.BARCODE, labelmod.NAME})
        # Contiguous: all of one, then all of the other.
        self.assertEqual(used, sorted(used, key=lambda st: st != labelmod.BARCODE))

    def test_it_is_exactly_twice_a_single_set(self):
        single = self._run(labelmod.BARCODE)
        both = self._run(labelmod.NAME_AND_BARCODE)

        self.assertEqual(both.total, single.total * 2)
        self.assertEqual(
            len(both.sequence(self.stock.columns)),
            len(single.sequence(self.stock.columns)) * 2,
        )

    def test_the_two_halves_hold_the_same_products(self):
        styled = self._run(labelmod.NAME_AND_BARCODE).styled_sequence(
            self.stock.columns)
        bars = [p.pk for p, st in styled if p and st == labelmod.BARCODE]
        names = [p.pk for p, st in styled if p and st == labelmod.NAME]

        self.assertEqual(bars, names)

    def test_a_single_style_run_is_one_set(self):
        for style in (labelmod.BARCODE, labelmod.NAME):
            run = self._run(style)
            self.assertEqual(run.segments, [style])
            self.assertEqual(run.total, sum(r.quantity for r in run.rows))

    def test_it_renders(self):
        pdf = labelmod.render_run(self._run(labelmod.NAME_AND_BARCODE), self.stock)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_it_needs_a_sku_and_the_density_guard_applies(self):
        """It prints bars, so both barcode rules still hold."""
        run = self._run(labelmod.NAME_AND_BARCODE)
        self.assertTrue(run.needs_barcode)

        narrow = LabelStock.objects.create(
            name="Too narrow", page_width_in=Decimal("8.5"),
            page_height_in=Decimal("11"), label_width_in=Decimal("0.6"),
            label_height_in=Decimal("0.5"), rows=20, columns=4,
            margin_left_in=Decimal("0.3"), margin_top_in=Decimal("0.5"),
            pitch_x_in=Decimal("0.7"), pitch_y_in=Decimal("0.5"),
        )
        self.assertTrue(labelmod.density_problems(run, narrow))

    def test_the_style_is_offered_on_the_page(self):
        user = User.objects.create_user("both-staff", password="pw")
        self.client.force_login(user)

        html = self.client.get(reverse("label_index")).content.decode()
        self.assertIn(f'value="{labelmod.NAME_AND_BARCODE}"', html)
