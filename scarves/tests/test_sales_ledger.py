"""Importing what the till took, through either door.

The reasoning behind these is in `docs/claude/sales-ledger.md`.
"""
import csv
from collections import Counter
import os
import re
import shutil
import tempfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from django.test import TestCase, override_settings
from .. import (
    closing, colorbands, crew, fancy, nav, photowalk, production, restock,
    sales, salesimport, seasonreport, seasons, sheetscan, skus, slowsellers,
    timesheets, weather,
)
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
    make_recipe,
)


SALES_CSV_HEADER = (
    "Date,Time,Time Zone,Category,Item,Qty,Price Point Name,SKU,Gross Sales,"
    "Discounts,Net Sales,Tax,Transaction ID,Device Name,Event Type,Location,"
    "Customer Name,Channel,Token,Card Brand\n"
)
def sales_csv(rows, path):
    """Write a Square-shaped itemised export. `rows` are dicts of overrides."""
    default = {
        "Date": "2026-09-05", "Time": "13:14:15",
        "Time Zone": "Eastern Time (US & Canada)",
        "Category": "Silk Scarves", "Item": "Rectangle Veil", "Qty": "1.0",
        "Price Point Name": "Regular Price", "SKU": "",
        "Gross Sales": "$95.00", "Discounts": "$0.00", "Net Sales": "$95.00",
        "Tax": "$5.04", "Transaction ID": "TXN1", "Device Name": "Terminal 1",
        "Event Type": "Payment", "Location": "Michael Knoll", "Customer Name": "",
        "Channel": "Michael Knoll", "Token": "TOKEN-A", "Card Brand": "Visa",
    }
    order = SALES_CSV_HEADER.strip().split(",")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(SALES_CSV_HEADER)
        for row in rows:
            merged = {**default, **row}
            handle.write(",".join(str(merged[key]) for key in order) + "\n")
    return path
class ImportSalesHistoryTests(TestCase):
    """The reporting ledger's importer.

    Everything here is about the ledger being *complete* and *re-runnable*.
    Both failures it guards are silent: a dropped line makes a season look
    quieter than it was, and a double-counted one makes it look busier, and
    neither shows up as an error anywhere.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sales.csv")
        category = RawProductCategory.objects.create(name="Silk Scarves")
        self.blank = RawProduct.objects.create(
            name="Rectangle Veil", category=category, price=Decimal("21.50"),
        )
        recipe = make_recipe("Drucilla")
        self.product = FinishedProduct.objects.create(
            name="Rectangle Veil — Drucilla", raw_product=self.blank,
            recipe=recipe, price=Decimal("95.00"),
        )

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_it_imports_lines_and_records_squares_own_time(self):
        sales_csv([{}], self.path)
        call_command("import_sales_history", self.path, stdout=StringIO())

        line = SaleLine.objects.get()
        self.assertEqual(line.item_name, "Rectangle Veil")
        self.assertEqual(line.net_cents, 9500)
        self.assertEqual(line.tax_cents, 504)
        self.assertEqual(line.source, Sale.SOURCE_SQUARE_CSV)
        local = timezone.localtime(line.sold_at)
        self.assertEqual(local.date(), date(2026, 9, 5))
        self.assertEqual((local.hour, local.minute), (13, 14))

    def test_it_moves_no_stock_and_writes_no_inventory_log(self):
        """The whole point of the second ledger. `import_square_sales` is the
        one that moves stock; running this one must never look like that."""
        self.product.number_on_hand = 7
        self.product.save()
        sales_csv([{"SKU": self.product.sku, "Qty": "3.0"}], self.path)

        call_command("import_sales_history", self.path, stdout=StringIO())

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 7)
        self.assertFalse(InventoryLog.objects.exists())

    def test_re_importing_the_same_file_writes_nothing_new(self):
        sales_csv([{}, {"Item": "Infinity", "Token": "TOKEN-B"}], self.path)
        call_command("import_sales_history", self.path, stdout=StringIO())
        self.assertEqual(SaleLine.objects.count(), 2)

        out = StringIO()
        call_command("import_sales_history", self.path, stdout=out)

        self.assertEqual(SaleLine.objects.count(), 2)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertIn("0 new lines", out.getvalue())

    def test_a_repeated_token_is_three_sales_not_one(self):
        """Square's `Token` names the product, not the line — the same token
        comes back on every sale of a triangle fringe. Keying on it would
        collapse three sales into one and lose the revenue silently."""
        sales_csv([
            {"Transaction ID": "T1", "Item": "Triangle Fringe", "Token": "SAME",
             "Net Sales": "$50.00"},
            {"Transaction ID": "T2", "Item": "Triangle Fringe", "Token": "SAME",
             "Net Sales": "$50.00"},
            {"Transaction ID": "T3", "Item": "Triangle Fringe", "Token": "SAME",
             "Net Sales": "$50.00"},
        ], self.path)

        call_command("import_sales_history", self.path, stdout=StringIO())

        self.assertEqual(Sale.objects.count(), 3)
        self.assertEqual(SaleLine.objects.count(), 3)
        self.assertEqual(
            sum(line.net_cents for line in SaleLine.objects.all()), 15000
        )

    def test_two_identical_lines_in_one_order_both_survive(self):
        """Square usually aggregates them, but nothing promises it always will."""
        sales_csv([
            {"Transaction ID": "T1", "Net Sales": "$95.00"},
            {"Transaction ID": "T1", "Net Sales": "$95.00"},
        ], self.path)

        call_command("import_sales_history", self.path, stdout=StringIO())

        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(SaleLine.objects.count(), 2)

    def test_a_line_with_no_sku_is_kept_in_full(self):
        """Most historical lines have none. Dropping them would lose the
        seasons this ledger exists to read."""
        sales_csv([{"SKU": "", "Item": "Half Circle Veil", "Price Point Name": "Sea Smoke"}], self.path)

        call_command("import_sales_history", self.path, stdout=StringIO())

        line = SaleLine.objects.get()
        self.assertEqual(line.sku, "")
        self.assertEqual(line.price_point, "Sea Smoke")
        self.assertEqual(line.net_cents, 9500)
        self.assertIsNone(line.finished_product)

    def test_it_matches_by_sku_and_falls_back_to_the_blank(self):
        sales_csv([
            {"Transaction ID": "T1", "SKU": self.product.sku},
            {"Transaction ID": "T2", "SKU": "", "Item": "Rectangle Veil",
             "Price Point Name": "Some Colorway"},
            {"Transaction ID": "T3", "SKU": "", "Item": "Wax Hands",
             "Category": "Wax"},
        ], self.path)

        call_command("import_sales_history", self.path, stdout=StringIO())

        matched = SaleLine.objects.get(sale__order_id="T1")
        self.assertEqual(matched.finished_product, self.product)
        self.assertEqual(matched.raw_product, self.blank)

        by_blank = SaleLine.objects.get(sale__order_id="T2")
        self.assertIsNone(by_blank.finished_product)
        self.assertEqual(by_blank.raw_product, self.blank)

        unmatched = SaleLine.objects.get(sale__order_id="T3")
        self.assertIsNone(unmatched.raw_product)
        self.assertEqual(unmatched.item_name, "Wax Hands")
        self.assertEqual(unmatched.category, "Wax")

    def test_category_survives_so_a_retired_line_can_be_excluded(self):
        """Wax was on this till through 2024 and is gone. A season total that
        cannot name its categories reads that as a decline."""
        sales_csv([
            {"Transaction ID": "T1", "Category": "Silk Scarves", "Net Sales": "$95.00"},
            {"Transaction ID": "T2", "Category": "Wax", "Item": "Wax Hands", "Net Sales": "$10.00"},
        ], self.path)

        call_command("import_sales_history", self.path, stdout=StringIO())

        silk = SaleLine.objects.filter(category="Silk Scarves")
        self.assertEqual(sum(l.net_cents for l in silk), 9500)
        self.assertEqual(SaleLine.objects.exclude(category="Wax").count(), 1)

    def test_a_discount_is_read_as_a_negative(self):
        sales_csv([{
            "Gross Sales": "$100.00", "Discounts": "-$12.50", "Net Sales": "$87.50",
        }], self.path)

        call_command("import_sales_history", self.path, stdout=StringIO())

        line = SaleLine.objects.get()
        self.assertEqual(line.gross_cents, 10000)
        self.assertEqual(line.discount_cents, -1250)
        self.assertEqual(line.net_cents, 8750)
        self.assertEqual(line.gross_cents + line.discount_cents, line.net_cents)

    def test_a_refund_is_marked_as_one(self):
        sales_csv([{
            "Event Type": "Refund", "Gross Sales": "-$95.00",
            "Net Sales": "-$95.00", "Tax": "-$5.04",
        }], self.path)

        call_command("import_sales_history", self.path, stdout=StringIO())

        line = SaleLine.objects.get()
        self.assertEqual(line.event_type, SaleLine.REFUND)
        self.assertEqual(line.net_cents, -9500)

    def test_an_unknown_time_zone_stops_the_run(self):
        """A wrong zone shifts every hour-of-day figure and looks correct."""
        sales_csv([{"Time Zone": "Middle-earth Standard Time"}], self.path)

        with self.assertRaises(CommandError) as caught:
            call_command("import_sales_history", self.path, stdout=StringIO())

        self.assertIn("Middle-earth", str(caught.exception))
        self.assertFalse(SaleLine.objects.exists())

    def test_dry_run_writes_nothing_but_still_reconciles(self):
        sales_csv([{}, {"Transaction ID": "T2", "Net Sales": "$50.00"}], self.path)
        out = StringIO()

        call_command("import_sales_history", self.path, "--dry-run", stdout=out)

        self.assertFalse(Sale.objects.exists())
        self.assertFalse(SaleLine.objects.exists())
        self.assertIn("$145.00", out.getvalue())
        self.assertIn("DRY RUN", out.getvalue())

    def test_it_says_which_season_the_lines_landed_in(self):
        call_command("generate_faire", "--year", "2026", stdout=StringIO())
        sales_csv([
            {"Transaction ID": "T1", "Date": "2026-09-05"},
            {"Transaction ID": "T2", "Date": "2026-12-25"},
        ], self.path)
        out = StringIO()

        call_command("import_sales_history", self.path, stdout=out)

        report = out.getvalue()
        self.assertIn("2026: 1 lines", report)
        self.assertIn("outside any faire", report)
        self.assertEqual(SaleLine.objects.count(), 2)

    def test_the_source_is_recorded_and_selectable(self):
        sales_csv([{}], self.path)
        call_command(
            "import_sales_history", self.path, "--source", Sale.SOURCE_SQUARE_API,
            stdout=StringIO(),
        )
        self.assertEqual(Sale.objects.get().source, Sale.SOURCE_SQUARE_API)
        self.assertEqual(SaleLine.objects.get().source, Sale.SOURCE_SQUARE_API)
def square_order(order_id, closed_at, items, refunds=None):
    return {
        "id": order_id,
        "closed_at": closed_at,
        "location_id": "L1",
        "tenders": [{"card_details": {"card": {"card_brand": "VISA"}}}],
        "line_items": items,
        "refunds": refunds or [],
    }
def square_item(name, variation_name="", variation_id="", quantity="1",
                gross=9500, discount=0, tax=504):
    return {
        "uid": f"u-{name}-{variation_name}",
        "name": name,
        "variation_name": variation_name,
        "catalog_object_id": variation_id,
        "quantity": quantity,
        "gross_sales_money": {"amount": gross},
        "total_discount_money": {"amount": discount},
        "total_tax_money": {"amount": tax},
    }
class FakeResult:
    def __init__(self, body):
        self.body = body
        self.errors = None

    def is_error(self):
        return False
class SquareOrdersImportTests(TestCase):
    """The API door. It must agree with the CSV door and not double-count it."""

    def setUp(self):
        call_command("generate_faire", "--year", "2021", stdout=StringIO())
        category = RawProductCategory.objects.create(name="Silk")
        self.blank = RawProduct.objects.create(
            name="Rectangle Veil", category=category, price=Decimal("21.50"),
        )
        recipe = make_recipe("Drucilla")
        self.product = FinishedProduct.objects.create(
            name="Rectangle Veil — Drucilla", raw_product=self.blank,
            recipe=recipe, price=Decimal("95.00"),
            square_variation_id="VAR-DRUCILLA",
        )
        self.day = "2021-08-28T17:30:00Z"

    def _client(self, orders, catalog=None):
        client = mock.MagicMock()
        client.orders.search_orders.return_value = FakeResult({"orders": orders})
        client.catalog.list_catalog.return_value = FakeResult(
            {"objects": catalog if catalog is not None else []}
        )
        # Without this the MagicMock answers `is_error()` with a truthy mock
        # and the deleted-object lookup reads it as Square refusing.
        client.catalog.batch_retrieve_catalog_objects.return_value = FakeResult(
            {"objects": [], "related_objects": []}
        )
        return client

    def _run(self, orders, catalog=None, **kwargs):
        client = self._client(orders, catalog)
        with mock.patch(
            "scarves.management.commands.import_square_orders.Command._client",
            return_value=client,
        ):
            out = StringIO()
            call_command("import_square_orders", "--year", "2021", stdout=out, **kwargs)
        return out.getvalue(), client

    @override_settings(SQUARE_ACCESS_TOKEN="t", SQUARE_LOCATION_ID="L1")
    def test_a_line_matches_on_squares_own_variation_id(self):
        """The reason this door exists — no SKU and no name guessing."""
        self._run([square_order("O1", self.day, [
            square_item("Rectangle Veil", "Drucilla", "VAR-DRUCILLA"),
        ])])
        line = SaleLine.objects.get()
        self.assertEqual(line.finished_product, self.product)
        self.assertEqual(line.raw_product, self.blank)
        self.assertEqual(line.source, Sale.SOURCE_SQUARE_API)

    @override_settings(SQUARE_ACCESS_TOKEN="t", SQUARE_LOCATION_ID="L1")
    def test_category_comes_from_squares_catalogue(self):
        catalog = [
            {"type": "CATEGORY", "id": "C1", "category_data": {"name": "Wax"}},
            {"type": "ITEM", "id": "I1", "item_data": {
                "category_id": "C1",
                "variations": [{"id": "VAR-WAX"}],
            }},
        ]
        self._run([square_order("O1", self.day, [
            square_item("Wax Hand", "", "VAR-WAX"),
        ])], catalog=catalog)
        self.assertEqual(SaleLine.objects.get().category, "Wax")

    @override_settings(SQUARE_ACCESS_TOKEN="t", SQUARE_LOCATION_ID="L1")
    def test_a_retired_item_gets_its_category_from_this_apps_catalogue(self):
        """Square only describes what is still in its catalogue; a line from
        five years ago usually is not."""
        self._run([square_order("O1", self.day, [
            square_item("Rectangle Veil", "Drucilla", "VAR-DRUCILLA"),
        ])])
        self.assertEqual(SaleLine.objects.get().category, "Silk")

    @override_settings(SQUARE_ACCESS_TOKEN="t", SQUARE_LOCATION_ID="L1")
    def test_money_splits_into_gross_discount_and_net(self):
        self._run([square_order("O1", self.day, [
            square_item("Rectangle Veil", "Drucilla", "VAR-DRUCILLA",
                        gross=10000, discount=1250),
        ])])
        line = SaleLine.objects.get()
        self.assertEqual(line.gross_cents, 10000)
        self.assertEqual(line.discount_cents, -1250)
        self.assertEqual(line.net_cents, 8750)

    @override_settings(SQUARE_ACCESS_TOKEN="t", SQUARE_LOCATION_ID="L1")
    def test_a_refund_becomes_its_own_negative_line(self):
        self._run([square_order("O1", self.day,
                                [square_item("Rectangle Veil", "Drucilla", "VAR-DRUCILLA")],
                                refunds=[{"id": "R1", "reason": "Returned",
                                          "amount_money": {"amount": 9500}}])])
        refund = SaleLine.objects.get(event_type=SaleLine.REFUND)
        self.assertEqual(refund.net_cents, -9500)
        self.assertEqual(SaleLine.objects.count(), 2)

    @override_settings(SQUARE_ACCESS_TOKEN="t", SQUARE_LOCATION_ID="L1")
    def test_re_running_writes_nothing_new(self):
        orders = [square_order("O1", self.day, [
            square_item("Rectangle Veil", "Drucilla", "VAR-DRUCILLA"),
        ])]
        self._run(orders)
        report, _ = self._run(orders)
        self.assertEqual(SaleLine.objects.count(), 1)
        self.assertIn("0 new lines", report)

    @override_settings(SQUARE_ACCESS_TOKEN="t", SQUARE_LOCATION_ID="L1")
    def test_it_will_not_load_an_order_another_door_already_supplied(self):
        """An export aggregates identical items and the API does not, so
        merging the two line by line would double-count the order."""
        when = timezone.make_aware(datetime(2021, 8, 28, 13, 30))
        sale = Sale.objects.create(order_id="O1", sold_at=when,
                                   source=Sale.SOURCE_SQUARE_CSV)
        SaleLine.objects.create(
            sale=sale, line_key="Rectangle Veil|Drucilla|1", sold_at=when,
            item_name="Rectangle Veil", price_point="Drucilla",
            quantity=Decimal(1), gross_cents=9500, net_cents=9500,
            source=Sale.SOURCE_SQUARE_CSV,
        )
        report, _ = self._run([square_order("O1", self.day, [
            square_item("Rectangle Veil", "Drucilla", "VAR-DRUCILLA"),
        ])])
        self.assertEqual(SaleLine.objects.count(), 1)
        self.assertIn("already on file from", report)

    @override_settings(SQUARE_ACCESS_TOKEN="t", SQUARE_LOCATION_ID="L1")
    def test_it_moves_no_stock(self):
        self.product.number_on_hand = 5
        self.product.save()
        self._run([square_order("O1", self.day, [
            square_item("Rectangle Veil", "Drucilla", "VAR-DRUCILLA", quantity="3"),
        ])])
        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 5)
        self.assertFalse(InventoryLog.objects.exists())

    @override_settings(SQUARE_ACCESS_TOKEN="t", SQUARE_LOCATION_ID="L1")
    def test_dry_run_writes_nothing(self):
        report, _ = self._run([square_order("O1", self.day, [
            square_item("Rectangle Veil", "Drucilla", "VAR-DRUCILLA"),
        ])], dry_run=True)
        self.assertFalse(SaleLine.objects.exists())
        self.assertIn("DRY RUN", report)

    @override_settings(SQUARE_ACCESS_TOKEN="t", SQUARE_LOCATION_ID="L1")
    def test_it_asks_square_for_the_faires_own_dates(self):
        _report, client = self._run([])
        body = client.orders.search_orders.call_args.kwargs["body"]
        window = body["query"]["filter"]["date_time_filter"]["closed_at"]
        self.assertTrue(window["start_at"].startswith("2021-08-28"))
        self.assertIn("COMPLETED", body["query"]["filter"]["state_filter"]["states"])

    @override_settings(SQUARE_ACCESS_TOKEN="", SQUARE_LOCATION_ID="L1")
    def test_missing_credentials_stop_the_run(self):
        with self.assertRaises(CommandError):
            call_command("import_square_orders", "--year", "2021", stdout=StringIO())

    @override_settings(SQUARE_ACCESS_TOKEN="t", SQUARE_LOCATION_ID="L1")
    def test_a_season_with_no_calendar_says_so(self):
        with self.assertRaises(CommandError) as caught:
            call_command("import_square_orders", "--year", "2019", stdout=StringIO())
        self.assertIn("generate_faire", str(caught.exception))
class DeletedCatalogCategoryTests(TestCase):
    """A season five years old is mostly things Square no longer lists.

    `ListCatalog` returns the living catalogue only, so the four base yarns —
    deleted from Square since — came back with no category at all, which put
    roughly $40k of a $48k yarn year in a bucket labelled "(uncategorised)".
    Asking about specific objects with `include_deleted_objects` is a
    different question, and the one that gets an answer.
    """

    def setUp(self):
        call_command("generate_faire", "--year", "2021", stdout=StringIO())
        self.day = "2021-08-28T17:30:00Z"

    def _client(self, live_catalog, deleted):
        client = mock.MagicMock()
        client.orders.search_orders.return_value = FakeResult({
            "orders": [square_order("O1", self.day, [
                square_item("Homespun", "Wasteland", "VAR-GONE"),
            ])],
        })
        client.catalog.list_catalog.return_value = FakeResult({"objects": live_catalog})
        client.catalog.batch_retrieve_catalog_objects.return_value = FakeResult(deleted)
        return client

    @override_settings(SQUARE_ACCESS_TOKEN="t", SQUARE_LOCATION_ID="L1")
    def test_a_deleted_variation_still_gets_its_category(self):
        live = [{"type": "CATEGORY", "id": "CAT-YARN", "category_data": {"name": "Yarn"}}]
        deleted = {
            "objects": [{
                "type": "ITEM_VARIATION", "id": "VAR-GONE", "is_deleted": True,
                "item_variation_data": {"item_id": "ITEM-GONE"},
            }],
            "related_objects": [{
                "type": "ITEM", "id": "ITEM-GONE",
                "item_data": {
                    "name": "Homespun - Single & Stunning",
                    "categories": [{"id": "CAT-YARN"}],
                },
            }],
        }
        client = self._client(live, deleted)
        with mock.patch(
            "scarves.management.commands.import_square_orders.Command._client",
            return_value=client,
        ):
            out = StringIO()
            call_command("import_square_orders", "--year", "2021", stdout=out)

        self.assertEqual(SaleLine.objects.get().category, "Yarn")
        self.assertIn("since deleted", out.getvalue())
        asked = client.catalog.batch_retrieve_catalog_objects.call_args.kwargs["body"]
        self.assertTrue(asked["include_deleted_objects"])
        self.assertTrue(asked["include_related_objects"])

    @override_settings(SQUARE_ACCESS_TOKEN="t", SQUARE_LOCATION_ID="L1")
    def test_the_variation_id_is_kept_on_the_line(self):
        """The only durable handle on a line — names get edited and catalogue
        objects get deleted, so this is what lets a category be resolved again
        without re-fetching every order."""
        client = self._client([], {"objects": [], "related_objects": []})
        with mock.patch(
            "scarves.management.commands.import_square_orders.Command._client",
            return_value=client,
        ):
            call_command("import_square_orders", "--year", "2021", stdout=StringIO())
        self.assertEqual(SaleLine.objects.get().square_variation_id, "VAR-GONE")

    def test_every_shape_square_names_a_category_in_is_read(self):
        """`category_id` is the old field and null on anything recent;
        `reporting_category` is what the dashboard's own reports use."""
        from scarves.squareorders import category_name as _category_name
        names = {"C1": "Yarn"}
        self.assertEqual(_category_name({"category_id": "C1"}, names), "Yarn")
        self.assertEqual(_category_name({"reporting_category": {"id": "C1"}}, names), "Yarn")
        self.assertEqual(_category_name({"categories": [{"id": "C1"}]}, names), "Yarn")
        self.assertEqual(_category_name({}, names), "")
        # A category deleted along with its item has no name left; grouping the
        # lines under its id beats dropping them into "(uncategorised)".
        self.assertEqual(_category_name({"categories": [{"id": "C9"}]}, names), "C9")
class ImportWindowTests(TestCase):
    """`--current` and `--since`, which are what make this runnable on a
    schedule without anybody deciding a year or a date."""

    def setUp(self):
        call_command("generate_faire", "--range", "2021-2026", stdout=StringIO())

    def _windows(self, **options):
        from scarves.management.commands.import_square_orders import Command
        merged = {"faire": "labor-day-run", "years": None, "range": None,
                  "start": None, "end": None, "current": False, "since": None}
        merged.update(options)
        command = Command()
        command.stdout = StringIO()
        return command._windows(merged)

    def test_current_picks_the_season_that_has_begun(self):
        windows = self._windows(current=True)
        self.assertEqual(len(windows), 1)
        label, start, _end = windows[0]
        self.assertIn("2026", label)
        self.assertEqual(start, date(2026, 8, 29))

    def test_a_window_never_runs_past_today(self):
        """Asking an API for the future buys nothing and can be an error."""
        _label, _start, end = self._windows(current=True)[0]
        self.assertLessEqual(end, timezone.localdate())

    def test_since_narrows_the_window_without_leaving_the_season(self):
        _label, start, _end = self._windows(current=True, since=1)[0]
        self.assertGreaterEqual(start, timezone.localdate() - timedelta(days=1))
        # and it never reaches back before the season opened
        _label, wide, _end = self._windows(current=True, since=9999)[0]
        self.assertEqual(wide, date(2026, 8, 29))

    def test_a_season_that_has_not_started_is_skipped_not_fetched(self):
        call_command("generate_faire", "--year", "2030", stdout=StringIO())
        with self.assertRaises(CommandError):
            self._windows(years=[2030])

    def test_between_seasons_it_answers_with_the_last_one(self):
        """Running this in February should top up what the season finished
        with, not refuse because nothing is on today."""
        Faire.objects.filter(year=2026).delete()
        label, _start, _end = self._windows(current=True)[0]
        self.assertIn("2025", label)

    def test_no_arguments_means_the_season_running_now(self):
        """The bare command has to mean something useful — a scheduled
        `--year 2026` keeps exiting 0 in 2027 while covering nothing."""
        label, start, _end = self._windows()[0]
        self.assertIn("2026", label)
        self.assertEqual(start, date(2026, 8, 29))
class WorkIsSeparableFromItsTriggerTests(TestCase):
    """Turning an order into ledger lines must not need a management command.

    The webhook already holds a retrieved order, and the moment it wants to
    write the ledger it either calls this or copies it — and a copy is how two
    totals for one weekend appear with nothing to say which is right. So the
    conversion lives in `squareorders`, which knows nothing about why it is
    being called, and swapping the trigger is a change to the caller only.
    """

    def test_an_order_becomes_lines_with_no_command_involved(self):
        from scarves import squareorders
        order = square_order("O1", "2021-08-28T17:30:00Z", [
            square_item("Rectangle Veil", "Drucilla", "VAR-1", gross=9500),
        ])
        lines = squareorders.lines_from_order(order, {"VAR-1": "Silk Scarves"}, Counter())
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["item_name"], "Rectangle Veil")
        self.assertEqual(line["price_point"], "Drucilla")
        self.assertEqual(line["square_variation_id"], "VAR-1")
        self.assertEqual(line["category"], "Silk Scarves")
        self.assertEqual(line["net_cents"], 9500)

    def test_those_lines_go_straight_into_the_ledger(self):
        """The whole path, with nothing from `management` imported."""
        from scarves import salesimport, squareorders
        order = square_order("O-DIRECT", "2021-08-28T17:30:00Z", [
            square_item("Shawl", "Lost Woods", "VAR-2", gross=8000),
        ])
        lines = squareorders.lines_from_order(order, {}, Counter())
        matcher = salesimport.Matcher()
        for line in lines:
            matcher.attach(line)
        result = salesimport.write(lines, Sale.SOURCE_SQUARE_API)

        self.assertEqual(result.written, 1)
        self.assertEqual(SaleLine.objects.get().item_name, "Shawl")

    def test_the_modules_import_nothing_from_management(self):
        """Including the exception. A module that raises `CommandError` is
        telling its caller what kind of program it is."""
        import ast, inspect
        from scarves import salesimport, squareorders
        for module in (squareorders, salesimport):
            with self.subTest(module=module.__name__):
                tree = ast.parse(inspect.getsource(module))
                imported = [
                    node.module or ""
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                ] + [
                    alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Import)
                    for alias in node.names
                ]
                for name in imported:
                    self.assertNotIn("management", name)

    def test_a_square_failure_is_not_a_command_error(self):
        from scarves import squareorders
        self.assertFalse(issubclass(squareorders.SquareUnavailable, CommandError))


class ItemNameToBlankTests(TestCase):
    """Which blank an item name reaches.

    The failure being pinned is silent in both directions. A blank that never
    matches makes a season read as one in which that style sold nothing —
    `raw_product` is null, so the style filter returns zero rather than an
    error. A blank that matches the *wrong* item is worse, because the total
    still adds up and only the attribution is wrong.
    """

    def setUp(self):
        self.category = RawProductCategory.objects.create(name="Yarn")

    def _blank(self, name):
        return RawProduct.objects.create(
            name=name, category=self.category, price=Decimal("9.14"),
        )

    def test_an_item_name_matches_the_blank_it_opens(self):
        """Square rang the yarn up as `Noble` for the whole of 2025 and this
        app calls it `Noble - Diamond Extra`. Keyed on six characters the two
        were `NOBLE` and `NOBLED`, and 179 lines matched nothing."""
        noble = self._blank("Noble - Diamond Extra")
        self.assertEqual(salesimport.BlankIndex([noble]).get("Noble"), noble)

    def test_the_other_three_yarns_still_match(self):
        """They matched before only by luck of spelling — each survives a
        six-character truncation with its first word intact. The rule that
        rescues Noble has to keep them."""
        blanks = [self._blank(name) for name in (
            "Heavenly - Angel",
            "Homespun - Single & Stunning",
            "Artisan - Ethereal Fingering",
            "Hearth - (Yarn Base)",
        )]
        index = salesimport.BlankIndex(blanks)
        for blank, item in zip(blanks, ("Heavenly", "Homespun", "Artisan", "Hearth")):
            with self.subTest(item=item):
                self.assertEqual(index.get(item), blank)
                self.assertEqual(index.get(blank.name), blank)

    def test_an_item_name_that_opens_two_blanks_matches_neither(self):
        """`Fancy` heads both. Filing a season's fancy work under whichever
        row happened to be created first is worse than leaving it unmatched,
        where the reconciliation print names it."""
        veil = self._blank("Fancy Veil")
        half = self._blank("Fancy Half Circle Veil")
        index = salesimport.BlankIndex([veil, half])
        self.assertIsNone(index.get("Fancy"))
        self.assertEqual(index.get("Fancy Veil"), veil)
        self.assertEqual(index.get("Fancy Half Circle"), half)

    def test_a_whole_name_beats_another_blanks_opening(self):
        shawl = self._blank("Shawl")
        longer = self._blank("Shawl Extra Long")
        index = salesimport.BlankIndex([shawl, longer])
        self.assertEqual(index.get("Shawl"), shawl)

    def test_a_longer_item_name_does_not_reach_a_shorter_blank(self):
        """The prefix runs one way. `Large Satin` is not a Large Hand-woven
        Rayon, and a rule loose enough to say it was would put one style's
        revenue on another."""
        rayon = self._blank("Large Hand-woven Rayon")
        index = salesimport.BlankIndex([rayon])
        self.assertIsNone(index.get("Large Satin"))


class RelinkByItemTests(TestCase):
    """Re-running the item tier over lines that imported without a blank."""

    def setUp(self):
        category = RawProductCategory.objects.create(name="Yarn")
        self.noble = RawProduct.objects.create(
            name="Noble - Diamond Extra", category=category, price=Decimal("9.14"),
        )
        self.sale = Sale.objects.create(
            order_id="T1", sold_at=timezone.now(), source=Sale.SOURCE_SQUARE_API,
        )

    def _line(self, key, item_name, **kwargs):
        return SaleLine.objects.create(
            sale=self.sale, line_key=key, sold_at=self.sale.sold_at,
            item_name=item_name, price_point="Regular", quantity=Decimal("1"),
            gross_cents=5400, net_cents=5400, source=Sale.SOURCE_SQUARE_API,
            **kwargs,
        )

    def test_it_attaches_the_blank_and_no_colorway(self):
        """2025 sold yarn with no colorway on the line at all, so a product
        link here would be inventing one."""
        line = self._line("a", "Noble")
        call_command("relink_sale_lines", "--by-item", stdout=StringIO())

        line.refresh_from_db()
        self.assertEqual(line.raw_product, self.noble)
        self.assertIsNone(line.finished_product)

    def test_it_leaves_a_line_that_already_has_a_blank_alone(self):
        """A line filed by its SKU or by Square's variation id is better
        evidence than an item name, and re-matching it would let a renamed
        item move last season's revenue onto another style."""
        other = RawProduct.objects.create(
            name="Homespun - Single & Stunning",
            category=self.noble.category, price=Decimal("6.71"),
        )
        line = self._line("a", "Noble", raw_product=other)
        call_command("relink_sale_lines", "--by-item", stdout=StringIO())

        line.refresh_from_db()
        self.assertEqual(line.raw_product, other)

    def test_it_names_what_it_could_not_place(self):
        self._line("a", "Wax Hand")
        out = StringIO()
        call_command("relink_sale_lines", "--by-item", stdout=out)
        self.assertIn("Wax Hand", out.getvalue())

    def test_a_dry_run_writes_nothing(self):
        line = self._line("a", "Noble")
        out = StringIO()
        call_command("relink_sale_lines", "--by-item", "--dry-run", stdout=out)

        line.refresh_from_db()
        self.assertIsNone(line.raw_product)
        self.assertIn("DRY RUN", out.getvalue())

    def test_it_moves_no_stock_and_writes_no_inventory_log(self):
        self._line("a", "Noble")
        before = self.noble.number_on_hand
        call_command("relink_sale_lines", "--by-item", stdout=StringIO())

        self.noble.refresh_from_db()
        self.assertEqual(self.noble.number_on_hand, before)
        self.assertEqual(InventoryLog.objects.count(), 0)
