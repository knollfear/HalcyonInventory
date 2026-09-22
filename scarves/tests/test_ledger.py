"""The one door for a stock movement, and the report that honours a claim."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .. import ledger, production
from ..models import FinishedProduct, InventoryLog, ProductionRun, ProductionRunRow, RawProduct
from .helpers import make_bathable, make_recipe, make_undyed


class LedgerTests(TestCase):
    def test_move_writes_the_count_and_the_row_together(self):
        product = make_bathable(make_recipe("Ledger"), "Ledger Heavenly", on_hand=3)
        log = ledger.move(
            product, 4,
            log_type=InventoryLog.PRODUCTION,
            source=InventoryLog.SOURCE_RECIPE_PAGE,
            notes="one bath",
        )
        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 7)
        self.assertEqual((log.quantity, log.source, log.raw_product_id),
                         (4, InventoryLog.SOURCE_RECIPE_PAGE, product.raw_product_id))

    def test_move_clamps_at_zero_but_records_what_was_reported(self):
        product = make_bathable(make_recipe("Clamp"), "Clamp Heavenly", on_hand=1)
        log = ledger.move(
            product, -3,
            log_type=InventoryLog.SALE, source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
        )
        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 0)
        self.assertEqual(log.quantity, -3)

    def test_a_passthrough_moves_on_the_raw_row_and_mirrors_down(self):
        product = make_undyed("Undyed Noble", on_hand=10)
        ledger.move(
            product, -2,
            log_type=InventoryLog.SALE, source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
        )
        raw = RawProduct.objects.get(pk=product.raw_product_id)
        product.refresh_from_db()
        self.assertEqual((raw.number_on_hand, product.number_on_hand), (8, 8))

    def test_count_is_absolute_and_says_what_it_changed(self):
        product = make_bathable(make_recipe("Count"), "Count Heavenly", on_hand=5)
        log = ledger.count(product, 2, source=InventoryLog.SOURCE_SUNDAY_CLOSE)
        product.refresh_from_db()
        self.assertEqual((product.number_on_hand, log.quantity), (2, -3))

    def test_a_count_that_matches_writes_nothing(self):
        product = make_bathable(make_recipe("Same"), "Same Heavenly", on_hand=5)
        self.assertIsNone(ledger.count(product, 5, source=InventoryLog.SOURCE_RESTOCK))
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_every_stock_writer_goes_through_the_ledger(self):
        """A `+=` on `number_on_hand` outside the ledger is a lock somebody skipped."""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        offenders = []
        pattern = re.compile(r"\.number_on_hand\s*(\+=|-=|=\s*max\(|=\s*\w+\.number_on_hand\s*[+-])")
        for path in list(root.glob("*.py")) + list(root.glob("views/*.py")) \
                + list(root.glob("management/commands/*.py")):
            if path.name in ("ledger.py", "production.py", "producedsince.py"):
                continue   # the ledger, and the two places raw blanks move
            for number, line in enumerate(path.read_text().splitlines(), 1):
                # `raw`/`shelf` are RawProduct rows: blanks are the other pile,
                # locked where they move and never ledgered (yet).
                if pattern.search(line) and not line.lstrip().startswith(("raw.", "shelf.")):
                    offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [])


class ReportHonoursTheClaimTests(TestCase):
    """A bath recorded after the fact ticks the sheet before it takes any blanks."""

    def setUp(self):
        self.product = make_bathable(make_recipe("Claimed"), "Claimed Heavenly", on_hand=0, bath=4)
        self.raw = self.product.raw_product
        RawProduct.objects.filter(pk=self.raw.pk).update(number_on_hand=20)
        self.run = ProductionRun.objects.create()
        self.rows = production.open_rows(self.run, [(self.product, 4), (self.product, 4)])
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 12)   # the claim landed at planning

    def test_open_rows_are_accepted_before_anything_is_booked_direct(self):
        outcome = production.report(
            self.product, 12,
            source=InventoryLog.SOURCE_RECIPE_PAGE, notes="3 dye baths × 4",
        )
        self.raw.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual([row.pk for row in outcome.rows], [row.pk for row in self.rows])
        self.assertEqual(outcome.direct, 4)
        self.assertEqual(outcome.units, 12)
        self.assertEqual(self.product.number_on_hand, 12)
        # Only the unplanned bath took blanks now; the sheet's two were paid for.
        self.assertEqual(self.raw.number_on_hand, 8)
        self.assertEqual(
            ProductionRunRow.objects.filter(run=self.run, accepted_at__isnull=False).count(), 2
        )
        sources = sorted(InventoryLog.objects.values_list("source", flat=True))
        self.assertEqual(sources, sorted([
            InventoryLog.SOURCE_PRODUCTION_SHEET, InventoryLog.SOURCE_PRODUCTION_SHEET,
            InventoryLog.SOURCE_RECIPE_PAGE,
        ]))
        self.assertEqual(outcome.sheets, [self.run])

    def test_a_report_smaller_than_the_next_row_leaves_the_row_alone(self):
        outcome = production.report(
            self.product, 3, source=InventoryLog.SOURCE_PRODUCTION_NEEDED, notes="",
        )
        self.raw.refresh_from_db()
        self.assertEqual((outcome.rows, outcome.direct), ([], 3))
        self.assertEqual(self.raw.number_on_hand, 9)
        self.assertEqual(
            ProductionRunRow.objects.filter(run=self.run, accepted_at__isnull=True).count(), 2
        )

    def test_the_recipe_page_ticks_the_sheet_and_says_so(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        response = self.client.post(
            reverse("record_recipe_production", args=[self.product.recipe_id]),
            {f"baths_{self.product.pk}": "1"},
            follow=True,
        )
        self.raw.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 12)   # not taken twice
        self.assertEqual(self.product.number_on_hand, 4)
        self.assertEqual(
            ProductionRunRow.objects.filter(run=self.run, accepted_at__isnull=False).count(), 1
        )
        self.assertContains(response, "ticked off there")

    def test_the_production_needed_button_does_the_same(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.client.post(reverse("record_dye_bath", args=[self.product.pk]), {})
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 12)
        self.assertEqual(
            ProductionRunRow.objects.filter(run=self.run, accepted_at__isnull=False).count(), 1
        )
