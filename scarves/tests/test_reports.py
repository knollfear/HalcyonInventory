"""The pages that read and never write, plus the calendar under them.

The reasoning behind these is in `docs/claude/reports.md`.
"""
from collections import Counter
import json
import pathlib
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from .. import (
    closing, colorbands, crew, fancy, nav, photowalk, production, restock,
    sales, seasonreport, seasons, sheetscan, skus, slowsellers, timesheets,
    weather,
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
    make_bathable,
    make_product,
    make_recipe,
)


class ClearColorwayAttributionTests(TestCase):
    """Removing a colorway claim that was never true, without losing the sale.

    A Square misconfiguration credited one colorway with sales belonging to
    forty others. The line carries four true things — units, money, date,
    blank — and one false one, so the repair removes the false one and keeps
    the rest. Deleting would throw away all five, and would not put a single
    unit back on a shelf.
    """

    def setUp(self):
        self.blank = make_bathable(
            make_recipe("Amethyst"), "Sash Belt", on_hand=0, par=8, bath=4
        )
        self.other = make_bathable(
            make_recipe("Russet"), "Sash Belt", on_hand=14, par=8, bath=4
        )
        self.day = timezone.now()
        self.line = self._line(self.blank, 55, "Amethyst")
        self.flat = self._line(self.other, 26, "Regular Price", link=False)

    def _line(self, product, units, colorway, link=True):
        sale = Sale.objects.create(
            order_id=f"o-{colorway}-{units}", sold_at=self.day,
            source=Sale.SOURCE_SQUARE_API,
        )
        return SaleLine.objects.create(
            sale=sale, line_key=f"k-{colorway}", sold_at=self.day,
            item_name="Sash Belt", price_point=colorway, quantity=units,
            gross_cents=183000, net_cents=183000,
            finished_product=product if link else None,
            raw_product=product.raw_product, source=Sale.SOURCE_SQUARE_API,
        )

    def _run(self, *args):
        # **`localdate`, not `.date()`.** `self.day` is UTC-aware, so
        # `.date()` is the UTC date — while the command filters
        # `sold_at__date`, which Django evaluates in `TIME_ZONE`. The two
        # agree for twenty hours a day and disagree for the four after 8pm in
        # New York, which is when the window asked for tomorrow and matched
        # nothing. A date an operator types is a local date, so the command
        # is right and this was building its window in the wrong zone.
        day = str(timezone.localdate(self.day))
        out = StringIO()
        call_command("clear_colorway_attribution", "--blank", "Sash Belt",
                     "--from", day, "--to", day,
                     *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_writes_nothing(self):
        out = self._run()

        self.line.refresh_from_db()
        self.assertEqual(self.line.price_point, "Amethyst")
        self.assertIn("Dry run", out)

    def test_it_reports_what_it_would_clear(self):
        out = self._run()

        self.assertIn("Amethyst", out)
        self.assertIn("55", out)

    def test_applying_removes_the_colorway_and_the_link(self):
        self._run("--apply")

        self.line.refresh_from_db()
        self.assertEqual(self.line.price_point, "")
        self.assertIsNone(self.line.finished_product)

    def test_the_sale_itself_survives(self):
        """Units, money, date and blank are all true and all stay."""
        self._run("--apply")

        self.line.refresh_from_db()
        self.assertEqual(self.line.quantity, 55)
        self.assertEqual(self.line.gross_cents, 183000)
        self.assertEqual(self.line.raw_product, self.blank.raw_product)
        self.assertEqual(self.line.item_name, "Sash Belt")

    def test_what_square_claimed_is_recorded_not_erased(self):
        """A repair that leaves no trace of what it repaired is the silent
        kind."""
        self._run("--apply")

        self.line.refresh_from_db()
        self.assertIn("Amethyst", self.line.notes)

    def test_a_line_already_carrying_no_colorway_is_left_alone(self):
        self._run("--apply")

        self.flat.refresh_from_db()
        self.assertEqual(self.flat.price_point, "Regular Price")
        self.assertEqual(self.flat.notes, "")

    def test_no_stock_movement_is_touched(self):
        """Those decrements really happened. The count is wrong in a way only
        a physical count can settle."""
        InventoryLog.objects.create(
            finished_product=self.blank, raw_product=self.blank.raw_product,
            log_type=InventoryLog.SALE, source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-55, notes="sale",
        )

        self._run("--apply")

        self.assertEqual(InventoryLog.objects.count(), 1)
        self.blank.refresh_from_db()
        self.assertEqual(self.blank.number_on_hand, 0)

    def test_the_wrongly_credited_colorway_stops_being_credited(self):
        rng = slowsellers.season_range({"range": "all", "from": "2000-01-01"})
        before = slowsellers.sold_by_recipe(rng).get(self.blank.recipe_id, 0)

        self._run("--apply")

        after = slowsellers.sold_by_recipe(rng).get(self.blank.recipe_id, 0)
        self.assertEqual(before, 55)
        self.assertEqual(after, 0)

    def test_the_units_still_count_as_that_blank(self):
        """They are real sales of a real blank — only the colour is unknown."""
        self._run("--apply")

        rng = slowsellers.season_range({"range": "all", "from": "2000-01-01"})
        named = dict(slowsellers.unattributed(rng))
        self.assertEqual(named.get("Sash Belt"), 81)

    def test_one_colorway_can_be_scoped_so_the_good_lines_survive(self):
        """A partial misconfiguration still lets some lines through, and
        those few are the only good colorway data of the weekend."""
        good = self._line(self.other, 1, "Russet")

        self._run("--colorway", "Amethyst", "--apply")

        self.line.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(self.line.price_point, "")
        self.assertEqual(good.price_point, "Russet")
        self.assertIsNotNone(good.finished_product)

    def test_without_the_scope_it_clears_every_colorway_on_the_blank(self):
        """Stated so the wholesale behaviour is a choice rather than a
        surprise — the dry run is where you find out which you want."""
        good = self._line(self.other, 1, "Russet")

        self._run("--apply")

        good.refresh_from_db()
        self.assertEqual(good.price_point, "")

    def test_a_small_neutralize_is_refused(self):
        """Below the floor it destroys more real colorway data than it
        removes doubtful rows."""
        SaleLine.objects.all().delete()
        self._line(self.other, 6, "Russet")

        with self.assertRaises(CommandError) as caught:
            self._run("--apply")

        self.assertIn("floor", str(caught.exception))

    def test_the_floor_can_be_overridden_deliberately(self):
        SaleLine.objects.all().delete()
        line = self._line(self.other, 6, "Russet")

        self._run("--floor", "1", "--apply")

        line.refresh_from_db()
        self.assertEqual(line.price_point, "")

    def test_it_names_the_weekends_it_reaches(self):
        """Running --year after the catalogue is fixed would sweep up the
        weeks that came back correctly; this is where that shows."""
        faire = Faire.objects.create(slug="labor-day-run", year=self.day.year)
        FaireDay.objects.create(faire=faire, date=self.day.date(), weekend=2)

        out = self._run()

        self.assertIn("weekend", out)
        self.assertIn("2", out)

    def test_a_bad_window_is_refused_rather_than_guessed(self):
        with self.assertRaises(CommandError):
            call_command("clear_colorway_attribution", "--blank", "Sash Belt",
                         "--from", "2026-08-29", stdout=StringIO())
class SlowSellersTests(TestCase):
    """The bottom of the list, where a zero means two opposite things.

    A colorway that sold nothing either sat on the display and nobody wanted
    it, or was never out there to be wanted. Those argue for opposite
    decisions, so the page never reports a zero without the stock beside it.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.url = reverse("slow_sellers")
        self.dud = make_bathable(
            make_recipe("Wasteland"), "Sash Belt", on_hand=12, par=8, bath=4
        )
        self.never = make_bathable(
            make_recipe("Aegean"), "Half Circle Veil", on_hand=0, par=8, bath=4
        )
        self.mover = make_bathable(
            make_recipe("Ember"), "Rectangle Veil", on_hand=4, par=8, bath=4
        )
        self.day = timezone.now()

    def _sell(self, product, units, colorway=None):
        sale = Sale.objects.create(
            order_id=f"o-{product.pk}-{units}-{timezone.now().timestamp()}",
            sold_at=self.day, source=Sale.SOURCE_SQUARE_API,
        )
        return SaleLine.objects.create(
            sale=sale, line_key=f"k{product.pk}-{units}", sold_at=self.day,
            item_name=product.raw_product.name,
            price_point=colorway or product.recipe.name,
            quantity=units, finished_product=product,
            raw_product=product.raw_product, source=Sale.SOURCE_SQUARE_API,
        )

    def _rows(self, **params):
        params.setdefault("range", "all")
        # This class is about the per-blank view, which is now the opt-in.
        params.setdefault("group", "product")
        return self.client.get(self.url, params).context["rows"]

    def test_a_colorway_that_sold_nothing_is_listed(self):
        names = [r.product.recipe.name for r in self._rows()]

        self.assertIn("Wasteland", names)

    def test_something_that_sold_well_is_not(self):
        self._sell(self.mover, 9)

        names = [r.product.recipe.name for r in self._rows()]

        self.assertNotIn("Ember", names)

    def test_the_max_is_settable_because_three_is_not_worth_a_bath(self):
        self._sell(self.mover, 3)

        self.assertNotIn("Ember", [r.product.recipe.name for r in self._rows(max="1")])
        self.assertIn("Ember", [r.product.recipe.name for r in self._rows(max="3")])

    def test_a_zero_with_stock_is_told_apart_from_a_zero_without(self):
        """The distinction the page exists for: one says stop dyeing it, the
        other says nobody could have bought it."""
        rows = {r.product.recipe.name: r for r in self._rows()}

        self.assertFalse(rows["Wasteland"].never_out)
        self.assertTrue(rows["Aegean"].never_out)

    def test_the_stock_is_on_the_row(self):
        rows = {r.product.recipe.name: r for r in self._rows()}

        self.assertEqual(rows["Wasteland"].on_hand, 12)

    def test_the_counts_split_the_two_kinds_of_zero(self):
        tally = self.client.get(
            self.url, {"range": "all", "group": "product"}).context["tally"]

        self.assertEqual(tally["zero"], 3)
        self.assertEqual(tally["zero_with_stock"], 2)
        self.assertEqual(tally["never_out"], 1)

    def test_the_never_out_pill_narrows_to_them(self):
        rows = self._rows(never="1")

        self.assertEqual([r.product.recipe.name for r in rows], ["Aegean"])

    def test_biggest_pile_of_unsold_stock_leads(self):
        """Sorted by what the answer costs, not alphabetically."""
        rows = [r for r in self._rows() if r.units == 0]

        self.assertEqual(rows[0].product.recipe.name, "Wasteland")

    def test_a_colourless_sale_is_named_rather_than_ignored(self):
        """Those units are real and belong to some colorway; nothing can say
        which, so the page says so instead of accusing every colorway."""
        self._sell(self.dud, 26, colorway="Regular Price")

        response = self.client.get(self.url, {"range": "all"})

        self.assertContains(response, "Some sales carry no colorway")
        self.assertContains(response, "Sash Belt")

    def test_an_undyed_passthrough_is_not_a_colorway(self):
        """It has no recipe, and its shortfall is a reorder decision."""
        make_bathable(None, "Undyed Skein", on_hand=3, par=8, bath=4)

        names = [r.product.raw_product.name for r in self._rows()]

        self.assertNotIn("Undyed Skein", names)

    def test_it_needs_a_login(self):
        self.client.logout()

        self.assertEqual(self.client.get(self.url).status_code, 302)
class GroupingByColorwayTests(TestCase):
    """Pooled across every blank, because that is how a recipe is retired.

    A colorway that sells nowhere is a recipe to stop dyeing. A colorway that
    sells on one blank and not another is a fact about the blank — nobody
    stops dyeing a colour for one yarn while the others move.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.url = reverse("slow_sellers")
        # One colour on four yarns, none of it selling: the retirement case.
        self.dead = make_recipe("Wasteland")
        self.dead_products = [
            self._on(self.dead, blank, on_hand=6)
            for blank in ("Heavenly", "Homespun", "Artisan", "Noble")
        ]
        # One colour that works on one yarn and not another.
        self.mixed = make_recipe("Aegean")
        self.mixed_good = self._on(self.mixed, "Heavenly", on_hand=3)
        self.mixed_bad = self._on(self.mixed, "Artisan", on_hand=9)

    def _on(self, recipe, blank, on_hand):
        p = make_bathable(recipe, blank, on_hand=on_hand, par=8, bath=4)
        return p

    def _sell(self, product, units):
        sale = Sale.objects.create(
            order_id=f"o{product.pk}", sold_at=timezone.now(),
            source=Sale.SOURCE_SQUARE_API,
        )
        SaleLine.objects.create(
            sale=sale, line_key=f"k{product.pk}", sold_at=timezone.now(),
            item_name=product.raw_product.name, price_point=product.recipe.name,
            quantity=units, finished_product=product,
            raw_product=product.raw_product, source=Sale.SOURCE_SQUARE_API,
        )

    def _names(self, **params):
        params.setdefault("range", "all")
        return [r.name for r in self.client.get(self.url, params).context["rows"]]

    def test_a_colour_that_sells_nowhere_is_listed(self):
        self.assertIn("Wasteland", self._names())

    def test_a_colour_selling_on_one_blank_is_not(self):
        """The whole point of pooling. Its Artisan row sold nothing, but the
        colour is not the problem — retiring it would be wrong."""
        self._sell(self.mixed_good, 20)

        self.assertNotIn("Aegean", self._names())

    def test_and_it_would_be_listed_without_pooling(self):
        """Guards the test above: the same data does surface per product, so
        pooling is what changes the answer rather than the fixture."""
        self._sell(self.mixed_good, 20)

        per_product = [
            r.product.recipe.name
            for r in self.client.get(
                self.url, {"range": "all", "group": "product"}).context["rows"]
        ]

        self.assertIn("Aegean", per_product)

    def test_it_pools_before_the_threshold_not_after(self):
        """One unit on each of four blanks is four, not four rows of one."""
        for product in self.dead_products:
            self._sell(product, 1)

        self.assertNotIn("Wasteland", self._names(max="1"))
        self.assertIn("Wasteland", self._names(max="4"))

    def test_it_counts_the_blanks_and_which_had_stock(self):
        """'Sold none across four boards' is a stronger argument than the
        same zero on one empty peg."""
        rows = {r.name: r for r in self.client.get(
            self.url, {"range": "all"}).context["rows"]}

        self.assertEqual(rows["Wasteland"].products, 4)
        self.assertEqual(rows["Wasteland"].stocked, 4)
        self.assertEqual(rows["Wasteland"].on_hand, 24)

    def test_a_colour_with_nothing_anywhere_reads_as_never_out(self):
        for product in self.dead_products:
            FinishedProduct.objects.filter(pk=product.pk).update(number_on_hand=0)

        rows = {r.name: r for r in self.client.get(
            self.url, {"range": "all"}).context["rows"]}

        self.assertTrue(rows["Wasteland"].never_out)

    def test_the_toggle_carries_the_rest_of_the_reading(self):
        response = self.client.get(self.url, {"range": "all", "max": "3"})
        body = response.content.decode()

        self.assertIn("max=3", body)
        self.assertIn("group=product", body)

    def test_the_header_counts_the_rows_on_screen(self):
        response = self.client.get(self.url, {"range": "all"})

        self.assertEqual(response.context["tally"]["listed"],
                         len(response.context["rows"]))
class MiscodedColorwayIsCalledOutTests(TestCase):
    """Sales that all landed on one colorway — the confidently-wrong case.

    Missing sales leave a gap and colourless sales announce themselves, but a
    miscoded variation looks like complete data: one runaway hit and forty
    duds, both false. The duds are exactly what this page reports.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.url = reverse("slow_sellers")
        self.blank = None
        self.products = []
        for i in range(12):
            p = make_bathable(
                make_recipe(f"Colour {i}"), "Sash Belt", on_hand=2, par=8, bath=4
            ) if i == 0 else FinishedProduct.objects.create(
                name=f"Sash Belt - Colour {i}", raw_product=self.blank,
                recipe=make_recipe(f"Colour {i}"), price=10, par=8,
                number_on_hand=2,
            )
            if i == 0:
                self.blank = p.raw_product
            self.products.append(p)

    def _sell(self, product, units, colorway):
        sale = Sale.objects.create(
            order_id=f"o{product.pk}-{colorway}", sold_at=timezone.now(),
            source=Sale.SOURCE_SQUARE_API,
        )
        SaleLine.objects.create(
            sale=sale, line_key=f"k{product.pk}", sold_at=timezone.now(),
            item_name="Sash Belt", price_point=colorway, quantity=units,
            finished_product=product, raw_product=self.blank,
            source=Sale.SOURCE_SQUARE_API,
        )

    def test_a_blank_with_everything_on_one_colorway_is_flagged(self):
        self._sell(self.products[0], 55, "Colour 0")
        self._sell(self.products[1], 4, "Colour 1")

        response = self.client.get(self.url, {"range": "all"})

        self.assertContains(response, "Worth a look before believing the zeros")
        self.assertContains(response, "Colour 0")

    def test_an_evenly_spread_blank_is_not_flagged(self):
        for p in self.products[:6]:
            self._sell(p, 10, p.recipe.name)

        response = self.client.get(self.url, {"range": "all"})

        self.assertNotContains(response, "Worth a look before believing the zeros")

    def test_it_is_detected_rather_than_named(self):
        """No blank is hardcoded, so the next one to break is caught without
        anybody remembering to add it."""
        import scarves.slowsellers as mod

        self.assertNotIn("sash", mod.__file__.lower().split("/")[-1])
        source = pathlib.Path(mod.__file__).read_text().lower()
        self.assertNotIn('"sash belt"', source)
        self.assertNotIn("'sash belt'", source)
class SalesReportTests(TestCase):
    """`private/sales/` — what sold, per finished product, over a range.

    The report reads the inventory log as a dataset. Everything worth pinning
    here is a way it could be quietly wrong: a boundary that drops the last
    day, a row count passed off as a sale count, or a stock adjustment
    counted as if the till had seen it.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("rep", "r@example.test", "pw")
        self.client.login(username="rep", password="pw")
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_product(self.recipe, "Stormy Sea Silk Scarf")
        self.other = make_product(make_recipe("Aegean"), "Aegean Silk Scarf")
        self.today = timezone.localdate()

    def _sale(self, product, qty, when=None, ref="ORDER-1",
              log_type=InventoryLog.SALE):
        """A log row on a given local day. `created_at` is auto_now_add, so
        the date can only be set afterwards."""
        log = InventoryLog.objects.create(
            finished_product=product,
            log_type=log_type,
            source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-qty if log_type == InventoryLog.SALE else qty,
            sale_reference=ref,
        )
        if when is not None:
            InventoryLog.objects.filter(pk=log.pk).update(created_at=when)
        return log

    def _at(self, day, hour=12):
        return timezone.make_aware(
            datetime.combine(day, time(hour, 0)),
            timezone.get_current_timezone(),
        )

    def _rows(self, **params):
        response = self.client.get(reverse("sales_report"), params)
        self.assertEqual(response.status_code, 200)
        return {r["product"].pk: r for r in response.context["rows"]}

    def test_units_come_back_positive(self):
        """Sales are stored negative. A ranking of top sellers headed by -12
        is the sort of thing nobody reports and everybody works around."""
        self._sale(self.product, 3, self._at(self.today))
        rows = self._rows(range="today")
        self.assertEqual(rows[self.product.pk]["units"], 3)

    def test_yesterday_does_not_leak_into_today(self):
        self._sale(self.product, 3, self._at(self.today))
        self._sale(self.other, 5, self._at(self.today - timedelta(days=1)))

        today = self._rows(range="today")
        self.assertEqual(set(today), {self.product.pk})

        yesterday = self._rows(range="yesterday")
        self.assertEqual(set(yesterday), {self.other.pk})

    def test_a_custom_range_includes_the_whole_of_its_last_day(self):
        """The boundary bug worth a test of its own. `created_at <= the end
        date` compares a timestamp against midnight, so a range ending today
        would take nothing sold after midnight — which is all of it, silently,
        with the page still reading as a complete answer."""
        day = self.today - timedelta(days=2)
        self._sale(self.product, 4, self._at(day, hour=23))

        rows = self._rows(**{
            "from": (day - timedelta(days=1)).isoformat(),
            "to": day.isoformat(),
        })
        self.assertEqual(rows[self.product.pk]["units"], 4)

    def test_transactions_counts_orders_not_rows(self):
        """Twelve sold in one sale and twelve sold in eleven are the same
        number of units and different facts about a colorway."""
        self._sale(self.product, 2, self._at(self.today), ref="A")
        self._sale(self.product, 1, self._at(self.today), ref="B")
        self._sale(self.product, 1, self._at(self.today), ref="B")

        row = self._rows(range="today")[self.product.pk]
        self.assertEqual(row["units"], 4)
        self.assertEqual(row["transactions"], 2)

    def test_rows_with_no_order_reference_count_one_each(self):
        """Nothing says two referenceless rows were the same sale, and
        assuming so deflates the count."""
        self._sale(self.product, 1, self._at(self.today), ref="")
        self._sale(self.product, 1, self._at(self.today), ref="")

        row = self._rows(range="today")[self.product.pk]
        self.assertEqual(row["transactions"], 2)

    def test_an_adjustment_is_not_a_sale(self):
        """The Sunday close writes adjustments, and some of them really are
        unregistered sales — but the app cannot tell which, and a guess in
        the same column as a till receipt is worse than a gap."""
        self._sale(self.other, 6, self._at(self.today),
                   log_type=InventoryLog.ADJUSTMENT)
        self.assertEqual(self._rows(range="today"), {})

    def test_days_separates_a_rush_from_a_following(self):
        for offset in (0, 1, 1, 2):
            self._sale(
                self.product, 1,
                self._at(self.today - timedelta(days=offset)),
                ref=f"O{offset}-{offset}",
            )
        row = self._rows(range="7")[self.product.pk]
        self.assertEqual(row["units"], 4)
        self.assertEqual(row["days"], 3)

    def test_shortfall_is_clamped_and_par_is_shown_beside_stock(self):
        """`on hand / par` is one cell because the gap is the reading. A
        product above par is not short by a negative number — overshoot is
        bath-size rounding and means nothing here."""
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            number_on_hand=1, par=8
        )
        FinishedProduct.objects.filter(pk=self.other.pk).update(
            number_on_hand=10, par=8
        )
        self._sale(self.product, 1, self._at(self.today), ref="A")
        self._sale(self.other, 1, self._at(self.today), ref="B")

        rows = self._rows(range="today")
        self.assertEqual(rows[self.product.pk]["short"], 7)
        self.assertEqual(rows[self.other.pk]["short"], 0)

    def test_sorting_runs_on_a_derived_column(self):
        """Shortfall and value are computed here rather than queried, and a
        column you can see but can't sort by is a question the page can
        obviously answer and won't."""
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            number_on_hand=0, par=10
        )
        FinishedProduct.objects.filter(pk=self.other.pk).update(
            number_on_hand=9, par=10
        )
        self._sale(self.product, 1, self._at(self.today), ref="A")
        self._sale(self.other, 9, self._at(self.today), ref="B")

        response = self.client.get(
            reverse("sales_report"), {"range": "today", "sort": "short", "dir": "desc"}
        )
        order = [r["product"].pk for r in response.context["rows"]]
        self.assertEqual(order[0], self.product.pk)

        response = self.client.get(
            reverse("sales_report"), {"range": "today", "sort": "units", "dir": "desc"}
        )
        order = [r["product"].pk for r in response.context["rows"]]
        self.assertEqual(order[0], self.other.pk)

    def test_a_range_pill_keeps_the_sort_and_the_filter(self):
        """The useful readings are combinations. A control that resets the
        others means the combination can only be reached by starting again."""
        response = self.client.get(
            reverse("sales_report"),
            {"range": "today", "sort": "value", "dir": "asc", "q": "stormy"},
        )
        hrefs = {r["key"]: r["href"] for r in response.context["ranges"]}
        self.assertIn("sort=value", hrefs["yesterday"])
        self.assertIn("dir=asc", hrefs["yesterday"])
        self.assertIn("q=stormy", hrefs["yesterday"])

    def test_a_custom_range_survives_being_narrowed(self):
        """The filter form carries the dates, so typing a colour name does
        not silently drop the range back to today."""
        start = (self.today - timedelta(days=4)).isoformat()
        response = self.client.get(
            reverse("sales_report"), {"from": start, "to": self.today.isoformat()}
        )
        carried = dict(response.context["filter_hidden"])
        self.assertEqual(carried["from"], start)
        self.assertEqual(carried["range"], "custom")

    def test_the_filters_narrow_to_one_style(self):
        self._sale(self.product, 1, self._at(self.today), ref="A")
        self._sale(self.other, 1, self._at(self.today), ref="B")

        rows = self._rows(range="today", blank=self.product.raw_product_id)
        self.assertEqual(set(rows), {self.product.pk})

        rows = self._rows(range="today", q="aegean")
        self.assertEqual(set(rows), {self.other.pk})

    def test_a_stale_link_degrades_to_no_filter(self):
        """There is no 404 in this app, so bad links are ordinary. A page
        that 500s instead of answering is the expensive version."""
        self._sale(self.product, 1, self._at(self.today), ref="A")
        rows = self._rows(range="today", blank="nonsense", sort="wat", dir="sideways")
        self.assertIn(self.product.pk, rows)

    def test_the_source_breakdown_names_how_the_sales_arrived(self):
        """A run of imported rows where webhook ones normally sit is a dead
        integration, and it reads as ordinary sales in every other column."""
        self._sale(self.product, 2, self._at(self.today), ref="A")
        log = self._sale(self.other, 1, self._at(self.today), ref="B")
        InventoryLog.objects.filter(pk=log.pk).update(
            source=InventoryLog.SOURCE_SQUARE_IMPORT
        )

        response = self.client.get(reverse("sales_report"), {"range": "today"})
        by_source = {s["source"]: s["units"] for s in response.context["sources"]}
        self.assertEqual(by_source[InventoryLog.SOURCE_SQUARE_WEBHOOK], 2)
        self.assertEqual(by_source[InventoryLog.SOURCE_SQUARE_IMPORT], 1)

    def test_backwards_dates_are_read_the_way_round_they_were_meant(self):
        day = self.today - timedelta(days=3)
        self._sale(self.product, 2, self._at(day))
        rows = self._rows(**{
            "from": self.today.isoformat(),
            "to": day.isoformat(),
        })
        self.assertEqual(rows[self.product.pk]["units"], 2)

    def test_an_unreadable_date_is_refused_rather_than_guessed(self):
        """A range built from half a date is a total for days nobody asked
        about, and it looks exactly like a real one. So an unparseable date
        contributes nothing — and what the page fell back to is said in the
        heading, which is what stops the answer being mistaken for the
        question."""
        rng = sales.resolve_range({"from": "not-a-date", "to": ""})
        self.assertEqual(rng.key, "today")
        self.assertEqual(rng.label, "Today")

        # With the other half readable the range stays open-ended rather than
        # inventing a start, and says so.
        end = self.today - timedelta(days=1)
        rng = sales.resolve_range(
            {"range": "custom", "from": "13/02/26", "to": end.isoformat()}
        )
        self.assertIsNone(rng.start)
        self.assertEqual(rng.end, end)
        self.assertTrue(rng.label.startswith("Up to "))
class FaireCalendarTests(TestCase):
    """The season rule, which everything season-over-season stands on.

    Getting it wrong is invisible: the pages still render nine weekends, they
    are simply the wrong nine, and every comparison quietly comes from a week
    nobody traded. So the rule is pinned against the four seasons whose week-1
    date is independently on record.
    """

    def test_labor_day_is_the_first_monday_in_september(self):
        self.assertEqual(seasons.labor_day(2026), date(2026, 9, 7))
        self.assertEqual(seasons.labor_day(2025), date(2025, 9, 1))
        self.assertEqual(seasons.labor_day(2021), date(2021, 9, 6))

    def test_week_one_reproduces_every_recorded_season(self):
        recorded = {
            2017: date(2017, 8, 26),
            2018: date(2018, 8, 25),
            2019: date(2019, 8, 24),
            2021: date(2021, 8, 28),
        }
        for year, saturday in recorded.items():
            with self.subTest(year=year):
                self.assertEqual(seasons.week_one_saturday(year), saturday)
                self.assertEqual(saturday.weekday(), 5)

    def test_a_run_is_nineteen_days_over_nine_weekends(self):
        days = seasons.labor_day_run(2026)
        self.assertEqual(len(days), seasons.SEASON_DAYS)
        self.assertEqual(len(days), 19)
        self.assertEqual(max(weekend for _d, weekend, _m in days), 9)

    def test_labor_day_monday_lands_in_weekend_two(self):
        """The one asymmetry in the run, and the reason weekly totals mislead."""
        for year in (2021, 2022, 2023, 2024, 2025, 2026):
            with self.subTest(year=year):
                mondays = [
                    (when, weekend)
                    for when, weekend, monday in seasons.labor_day_run(year)
                    if monday
                ]
                self.assertEqual(len(mondays), 1)
                when, weekend = mondays[0]
                self.assertEqual(when, seasons.labor_day(year))
                self.assertEqual(weekend, seasons.LABOR_DAY_WEEKEND)
                self.assertEqual(when.weekday(), 0)

    def test_weekend_two_has_three_days_and_every_other_has_two(self):
        counts = Counter(weekend for _d, weekend, _m in seasons.labor_day_run(2026))
        self.assertEqual(counts[seasons.LABOR_DAY_WEEKEND], 3)
        for weekend in range(1, 10):
            if weekend != seasons.LABOR_DAY_WEEKEND:
                self.assertEqual(counts[weekend], 2)

    def test_days_are_only_saturdays_sundays_and_the_one_monday(self):
        for when, _weekend, monday in seasons.labor_day_run(2026):
            self.assertIn(when.weekday(), {5, 6, 0})
            self.assertEqual(when.weekday() == 0, monday)

    def test_a_midweek_day_is_not_in_the_season(self):
        """A range test would put a Wednesday in September inside the run."""
        self.assertEqual(seasons.labor_day_season_for(date(2026, 9, 7)), 2026)
        self.assertIsNone(seasons.labor_day_season_for(date(2026, 9, 9)))
        self.assertIsNone(seasons.labor_day_season_for(date(2026, 1, 5)))

    def test_manual_dates_group_into_weekends_by_the_gap(self):
        """Sat-Sun is one weekend; Sat-Sun-Mon is too; a six-day gap is not."""
        numbered = seasons.group_into_weekends([
            date(2027, 5, 1), date(2027, 5, 2),
            date(2027, 5, 8), date(2027, 5, 9), date(2027, 5, 10),
        ])
        self.assertEqual([w for _d, w in numbered], [1, 1, 2, 2, 2])

    def test_a_manual_rule_generates_nothing_rather_than_an_empty_season(self):
        with self.assertRaises(ValueError):
            seasons.days_for(seasons.MANUAL, 2027)
class GenerateFaireTests(TestCase):
    def test_it_writes_a_whole_season(self):
        call_command("generate_faire", "--year", "2026", stdout=StringIO())
        faire = Faire.objects.get(slug=seasons.DEFAULT_FAIRE_SLUG, year=2026)
        self.assertEqual(faire.days.count(), 19)
        self.assertEqual(faire.trading_days, 19)
        self.assertEqual(faire.days.first().date, date(2026, 8, 29))

    def test_2020_is_skipped_because_there_was_no_faire(self):
        out = StringIO()
        call_command("generate_faire", "--range", "2019-2021", stdout=out)
        self.assertFalse(Faire.objects.filter(year=2020).exists())
        self.assertTrue(Faire.objects.filter(year=2019).exists())
        self.assertTrue(Faire.objects.filter(year=2021).exists())
        self.assertIn("2020", out.getvalue())

    def test_rerunning_changes_nothing_and_keeps_a_struck_day_struck(self):
        """Regenerating must never quietly re-open a washed-out day."""
        call_command("generate_faire", "--year", "2023", stdout=StringIO())
        washed = FaireDay.objects.get(date=date(2023, 9, 23))
        washed.traded = False
        washed.note = "Rained out"
        washed.save()

        call_command("generate_faire", "--year", "2023", stdout=StringIO())

        self.assertEqual(FaireDay.objects.filter(faire__year=2023).count(), 19)
        washed.refresh_from_db()
        self.assertFalse(washed.traded)
        self.assertEqual(washed.note, "Rained out")
        self.assertEqual(Faire.objects.get(year=2023).trading_days, 18)

    def test_dry_run_writes_nothing(self):
        call_command("generate_faire", "--year", "2026", "--dry-run", stdout=StringIO())
        self.assertFalse(Faire.objects.exists())
        self.assertFalse(FaireDay.objects.exists())

    def test_a_second_faire_is_a_new_slug_and_leaves_the_first_alone(self):
        call_command("generate_faire", "--year", "2027", stdout=StringIO())
        call_command(
            "generate_faire", "--faire", "madison-hill",
            "--dates", "2027-05-01,2027-05-02,2027-05-08,2027-05-09",
            stdout=StringIO(),
        )
        other = Faire.objects.get(slug="madison-hill", year=2027)
        self.assertEqual(other.rule, seasons.MANUAL)
        self.assertEqual(other.days.count(), 4)
        self.assertEqual([d.weekend for d in other.days.all()], [1, 1, 2, 2])
        self.assertEqual(Faire.objects.get(slug=seasons.DEFAULT_FAIRE_SLUG, year=2027).days.count(), 19)

    def test_a_year_needs_saying(self):
        with self.assertRaises(CommandError):
            call_command("generate_faire", stdout=StringIO())
def sale_line(day, cents, category="Silk Scarves", item="Rectangle Veil",
              units=1, hour=13, order=None, blank=None):
    """One line on a given local date, for the season-report tests."""
    when = timezone.make_aware(datetime.combine(day, time(hour, 30)))
    order = order or f"T{day.isoformat()}-{cents}-{item}-{hour}"
    sale, _ = Sale.objects.get_or_create(
        order_id=order,
        defaults={"sold_at": when, "source": Sale.SOURCE_SQUARE_CSV},
    )
    return SaleLine.objects.create(
        sale=sale, line_key=f"{item}|x|{SaleLine.objects.count()}",
        sold_at=when, category=category, item_name=item,
        quantity=Decimal(units), gross_cents=cents, net_cents=cents,
        source=Sale.SOURCE_SQUARE_CSV, raw_product=blank,
    )
class SeasonReportTests(TestCase):
    """Season against season on the weekend axis.

    The failures guarded here are the ones that read as facts: a weekend-2
    total compared against two-day weekends, and a projection drawn over a
    weekend that already happened and simply was not imported.
    """

    def setUp(self):
        call_command("generate_faire", "--range", "2021-2022", stdout=StringIO())
        self.y2021 = Faire.objects.get(year=2021)
        self.y2022 = Faire.objects.get(year=2022)

    def _fill(self, faire, per_weekend, category="Silk Scarves", blank=None,
              item="Rectangle Veil"):
        """One line on the first day of each weekend, worth `per_weekend` cents."""
        for number, cents in per_weekend.items():
            day = faire.days.filter(weekend=number).first()
            sale_line(day.date, cents, category=category, item=item, blank=blank,
                      order=f"{faire.year}-w{number}-{category}-{item}")

    def test_totals_land_in_the_weekend_the_day_belongs_to(self):
        self._fill(self.y2021, {1: 100_000, 2: 250_000, 9: 400_000})
        seasons = seasonreport.build("labor-day-run", today=date(2026, 12, 31))
        y2021 = next(s for s in seasons if s.year == 2021)
        values = {w.number: w.value for w in y2021.weekends}
        self.assertEqual(values[1], 100_000)
        self.assertEqual(values[2], 250_000)
        self.assertEqual(values[9], 400_000)
        self.assertEqual(y2021.total, 750_000)

    def test_weekend_two_is_divided_by_three_days_not_two(self):
        """The whole reason per-day exists. Weekend 2 carries Labor Day."""
        self._fill(self.y2021, {2: 300_000, 3: 300_000})
        seasons = seasonreport.build("labor-day-run", today=date(2026, 12, 31))
        y2021 = next(s for s in seasons if s.year == 2021)
        by_number = {w.number: w for w in y2021.weekends}

        self.assertEqual(by_number[2].traded_days, 3)
        self.assertEqual(by_number[3].traded_days, 2)
        self.assertEqual(by_number[2].per_day, Decimal(100_000))
        self.assertEqual(by_number[3].per_day, Decimal(150_000))
        # Identical weekly totals, and weekend 2 is the weaker weekend.
        self.assertLess(by_number[2].per_day, by_number[3].per_day)

    def test_a_struck_day_leaves_the_weekend_with_one_denominator(self):
        washed = self.y2021.days.filter(weekend=5)
        washed.update(traded=False)
        self._fill(self.y2021, {5: 0})
        seasons = seasonreport.build("labor-day-run", today=date(2026, 12, 31))
        y2021 = next(s for s in seasons if s.year == 2021)
        by_number = {w.number: w for w in y2021.weekends}
        self.assertEqual(by_number[5].traded_days, 0)
        self.assertEqual(by_number[5].per_day, Decimal(0))

    def test_a_weekend_still_ahead_is_projected_and_a_past_gap_is_not(self):
        """The distinction the module exists to keep: to-come versus missing."""
        complete = {n: 100_000 for n in range(1, 10)}
        self._fill(self.y2021, complete)
        # 2022 has weekends 1 and 2 recorded; 3 happened and was never
        # imported; the rest are treated as still ahead.
        self._fill(self.y2022, {1: 100_000, 2: 100_000})
        seasons = seasonreport.build(
            "labor-day-run", today=date(2022, 9, 12),
        )
        y2022 = next(s for s in seasons if s.year == 2022)
        y2021 = next(s for s in seasons if s.year == 2021)

        self.assertEqual(y2022.gaps, [3])
        self.assertNotIn(3, y2022.to_come)

        total = seasonreport.project(y2022, [y2021])
        self.assertIsNotNone(total)
        by_number = {w.number: w for w in y2022.weekends}
        self.assertFalse(by_number[3].projected, "a past gap must never be projected")
        self.assertTrue(by_number[9].projected)

    def test_nothing_is_projected_without_a_complete_season_behind_it(self):
        self._fill(self.y2021, {1: 100_000})
        self._fill(self.y2022, {1: 100_000})
        seasons = seasonreport.build("labor-day-run", today=date(2022, 9, 1))
        y2022 = next(s for s in seasons if s.year == 2022)
        y2021 = next(s for s in seasons if s.year == 2021)
        self.assertIsNone(seasonreport.project(y2022, [y2021]))

    def test_a_category_filter_changes_the_total(self):
        """Wax was on this till and is gone; a total must say what it counts."""
        self._fill(self.y2021, {1: 100_000}, category="Silk Scarves")
        self._fill(self.y2021, {1: 40_000}, category="Wax")

        everything = seasonreport.build("labor-day-run", today=date(2026, 12, 31))
        self.assertEqual(next(s for s in everything if s.year == 2021).total, 140_000)

        silk = seasonreport.build(
            "labor-day-run", categories=["Silk Scarves"], today=date(2026, 12, 31),
        )
        self.assertEqual(next(s for s in silk if s.year == 2021).total, 100_000)

    # ---------------------------------------------------------- the style

    def _blank(self, name):
        category, _ = RawProductCategory.objects.get_or_create(name="Silk")
        return RawProduct.objects.create(
            name=name, category=category, price="5.00",
        )

    def _blanks(self):
        return self._blank("Rectangle Veil"), self._blank("Sash Belt")

    def test_a_style_filter_narrows_the_total_and_the_units(self):
        """The whole ask: one style, year on year, in dollars or units."""
        veil, belt = self._blanks()
        self._fill(self.y2021, {1: 100_000}, blank=veil)
        self._fill(self.y2021, {1: 40_000}, blank=belt, item="Sash Belt")

        everything = seasonreport.build("labor-day-run", today=date(2026, 12, 31))
        self.assertEqual(next(s for s in everything if s.year == 2021).total, 140_000)

        veils = seasonreport.build(
            "labor-day-run", blank=veil.pk, today=date(2026, 12, 31),
        )
        y2021 = next(s for s in veils if s.year == 2021)
        self.assertEqual(y2021.total, 100_000)
        self.assertEqual(y2021.units, Decimal(1))

    def test_a_weekend_the_style_sold_none_of_is_a_zero_not_a_gap(self):
        """The failure the filter would otherwise introduce.

        Unfiltered, a weekend with no lines can only be one nobody imported.
        Filtered, it is almost always one where that style did not sell — and
        rendering the second as the first invents missing imports out of quiet
        weekends and, worse, drops them out of the denominator below.
        """
        veil, belt = self._blanks()
        self._fill(self.y2021, {1: 100_000, 2: 100_000}, blank=veil)
        # Weekend 3 traded and was imported; it just sold no veils.
        self._fill(self.y2021, {3: 40_000}, blank=belt, item="Sash Belt")

        veils = seasonreport.build(
            "labor-day-run", blank=veil.pk, today=date(2026, 12, 31),
        )
        y2021 = next(s for s in veils if s.year == 2021)
        by_number = {w.number: w for w in y2021.weekends}

        self.assertEqual(by_number[3].value, 0)
        self.assertTrue(by_number[3].has_data, "the weekend was imported")
        self.assertTrue(by_number[3].sold_nothing)
        self.assertFalse(by_number[3].is_gap)
        self.assertNotIn(3, y2021.gaps)

    def test_a_quiet_weekend_stays_in_the_denominator(self):
        """A zero weekend counts its trading days, or per-day reads high."""
        veil, belt = self._blanks()
        self._fill(self.y2021, {1: 100_000}, blank=veil)
        self._fill(self.y2021, {3: 40_000}, blank=belt, item="Sash Belt")

        veils = seasonreport.build(
            "labor-day-run", blank=veil.pk, today=date(2026, 12, 31),
        )
        y2021 = next(s for s in veils if s.year == 2021)
        # Weekend 1 and weekend 3 both traded two days, and both were
        # imported — so the veil's takings are spread over four days, not two.
        self.assertEqual(y2021.traded_days, 4)
        self.assertEqual(y2021.per_day, Decimal(25_000))

    def test_a_style_that_did_not_exist_yet_reads_zero_not_missing(self):
        """The yarn case: real zeros, and they must not read as lost data."""
        veil, belt = self._blanks()
        yarn = self._blank("Heavenly")
        self._fill(self.y2021, {n: 100_000 for n in range(1, 10)}, blank=veil)
        self._fill(self.y2022, {n: 100_000 for n in range(1, 10)}, blank=yarn,
                   item="Heavenly")

        yarns = seasonreport.build(
            "labor-day-run", blank=yarn.pk, today=date(2026, 12, 31),
        )
        y2021 = next(s for s in yarns if s.year == 2021)
        self.assertEqual(y2021.total, 0)
        self.assertEqual(y2021.gaps, [], "a season that sold none is not a gap")
        self.assertTrue(y2021.has_any_data, "its weekends were imported")

    def test_a_category_absent_from_a_season_is_not_a_missing_import(self):
        """The same fault, reachable through the pills that were already here.

        The wax hands left the till after 2024, so counting only them makes a
        later season carry no lines at all — which is a discontinued product
        line, not an export nobody loaded.
        """
        self._fill(self.y2021, {n: 100_000 for n in range(1, 10)}, category="Wax")
        self._fill(self.y2022, {n: 100_000 for n in range(1, 10)},
                   category="Silk Scarves")

        wax = seasonreport.build(
            "labor-day-run", categories=["Wax"], today=date(2026, 12, 31),
        )
        y2022 = next(s for s in wax if s.year == 2022)
        self.assertEqual(y2022.gaps, [])
        self.assertEqual(y2022.total, 0)

    def test_nothing_is_projected_for_a_style_that_banked_nothing(self):
        """A dashed tail along zero reads as a forecast. There isn't one."""
        veil, belt = self._blanks()
        self._fill(self.y2021, {n: 100_000 for n in range(1, 10)}, blank=veil)
        self._fill(self.y2022, {1: 100_000, 2: 100_000}, blank=belt,
                   item="Sash Belt")

        veils = seasonreport.build(
            "labor-day-run", blank=veil.pk, today=date(2022, 9, 12),
        )
        y2021 = next(s for s in veils if s.year == 2021)
        y2022 = next(s for s in veils if s.year == 2022)
        self.assertIsNone(seasonreport.project(y2022, [y2021]))

    def test_only_blanks_with_sales_are_offered(self):
        veil, belt = self._blanks()
        self._blank("Never Sold")
        self._fill(self.y2021, {1: 100_000}, blank=veil)

        offered = [b.name for b in seasonreport.blanks_on_file()]
        self.assertEqual(offered, ["Rectangle Veil"])

    def test_a_season_with_no_lines_is_absent_rather_than_zero(self):
        self._fill(self.y2021, {1: 100_000})
        seasons = seasonreport.build("labor-day-run", today=date(2026, 12, 31))
        y2022 = next(s for s in seasons if s.year == 2022)
        self.assertFalse(y2022.has_any_data)
        self.assertEqual(
            seasonreport.chart(seasons, 2021, seasonreport.MODE_CUMULATIVE,
                               seasonreport.METRIC_NET).count("2022"), 0,
            "a season with nothing imported must not be drawn as a flat zero",
        )

    def test_the_chart_breaks_the_line_over_a_gap(self):
        self._fill(self.y2021, {1: 100_000, 3: 100_000})
        seasons = seasonreport.build("labor-day-run", today=date(2026, 12, 31))
        svg = seasonreport.chart(seasons, 2021, seasonreport.MODE_WEEKEND,
                                 seasonreport.METRIC_NET)
        self.assertGreaterEqual(
            svg.count('class="line focus"'), 2,
            "a weekend with nothing known is a break, not a zero joined through",
        )
class SeasonReportPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("staff", password="pw")
        call_command("generate_faire", "--range", "2021-2022", stdout=StringIO())
        for faire in Faire.objects.all():
            for number in range(1, 10):
                day = faire.days.filter(weekend=number).first()
                sale_line(day.date, 100_000 + number * 1000,
                          order=f"{faire.year}-{number}")
        self.url = reverse("season_report")

    def test_it_is_private(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_it_renders_with_a_chart_and_a_table(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Weekend by weekend", body)
        self.assertIn("<svg", body)
        self.assertIn("2021", body)
        self.assertIn("2022", body)

    def _veil(self):
        category, _ = RawProductCategory.objects.get_or_create(name="Silk")
        veil = RawProduct.objects.create(
            name="Rectangle Veil", category=category, price="5.00",
        )
        SaleLine.objects.filter(item_name="Rectangle Veil").update(raw_product=veil)
        return veil

    def test_the_style_filter_narrows_the_page(self):
        veil = self._veil()
        self.client.force_login(self.user)
        response = self.client.get(self.url, {"blank": veil.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["blank"], veil)
        self.assertIn("Rectangle Veil", response.content.decode())

    def test_an_unknown_style_falls_back_rather_than_erroring(self):
        """A filter is navigation; a stale link shows more, never breaks."""
        self.client.force_login(self.user)
        response = self.client.get(self.url, {"blank": "99999"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["blank"])

    def test_every_control_carries_the_style(self):
        veil = self._veil()
        self.client.force_login(self.user)
        response = self.client.get(self.url, {"blank": veil.pk, "metric": "units"})
        for group in ("mode_links", "metric_links", "year_links",
                      "category_links", "palette_links"):
            for link in response.context[group]:
                self.assertIn(f"blank={veil.pk}", link["href"], group)
        # And the style form carries what the links carry, or choosing a
        # style would silently reset the metric somebody had just picked.
        carried = dict(response.context["blank_hidden"])
        self.assertEqual(carried.get("metric"), "units")

    def test_every_control_keeps_the_rest_of_the_state(self):
        self.client.force_login(self.user)
        response = self.client.get(
            self.url, {"metric": "units", "year": "2021", "cat": "Silk Scarves"},
        )
        self.assertEqual(response.status_code, 200)
        for link in response.context["mode_links"]:
            self.assertIn("metric=units", link["href"])
            self.assertIn("year=2021", link["href"])
            self.assertIn("cat=Silk+Scarves", link["href"])

    def test_an_unknown_focus_year_falls_back_rather_than_erroring(self):
        """A filter is navigation; a stale link should show more, not break."""
        self.client.force_login(self.user)
        response = self.client.get(self.url, {"year": "1999"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["focus_year"], 2022)

    def test_an_unreadable_mode_falls_back_to_the_default(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url, {"mode": "sideways"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["mode"], seasonreport.DEFAULT_MODE)

    def test_per_day_shows_different_numbers_from_per_weekend(self):
        self.client.force_login(self.user)
        weekly = self.client.get(self.url, {"mode": "weekend"}).content.decode()
        per_day = self.client.get(self.url, {"mode": "day"}).content.decode()
        self.assertNotEqual(weekly, per_day)

    def test_it_says_so_when_no_sales_have_been_imported(self):
        SaleLine.objects.all().delete()
        Sale.objects.all().delete()
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Nothing to chart", response.content.decode())
WEATHER_PAYLOAD = json.dumps({
    "daily": {
        "time": ["2021-08-28", "2021-08-29", "2021-09-04"],
        "temperature_2m_max": [85.3, 82.1, None],
        "temperature_2m_min": [73.9, 72.7, None],
        "temperature_2m_mean": [78.4, 76.8, None],
        "precipitation_sum": [0.071, 0.4, None],
    },
    "hourly": {
        "time": (
            [f"2021-08-28T{h:02d}:00" for h in range(24)]
            + [f"2021-08-29T{h:02d}:00" for h in range(24)]
        ),
        # 100% cloud overnight, 20% during opening hours — so a whole-day mean
        # and an open-hours mean disagree, which is the point of the split.
        "cloud_cover": (
            [100 if not 10 <= h <= 19 else 20 for h in range(24)]
            + [50] * 24
        ),
        "relative_humidity_2m": [60] * 24 + [70] * 24,
    },
})
class WeatherFetchTests(TestCase):
    """Reading the archive, and the three reasons a day can have no weather."""

    def test_cloud_and_humidity_average_over_opening_hours_only(self):
        """Fog at four in the morning is not weather anybody stood in."""
        readings = weather.fetch(
            39.0, -76.6, date(2021, 8, 28), date(2021, 9, 4), "America/New_York",
            opener=lambda url: WEATHER_PAYLOAD,
        )
        first = readings[date(2021, 8, 28)]
        self.assertEqual(first["cloud_pct"], Decimal("20.0"))
        self.assertEqual(first["humidity_pct"], Decimal("60.0"))
        self.assertEqual(first["high_f"], Decimal("85.3"))
        self.assertEqual(first["low_f"], Decimal("73.9"))

    def test_a_day_the_archive_has_no_answer_for_is_absent_not_null(self):
        """The caller has to tell 'not yet' from 'calm and dry'."""
        readings = weather.fetch(
            39.0, -76.6, date(2021, 8, 28), date(2021, 9, 4), "America/New_York",
            opener=lambda url: WEATHER_PAYLOAD,
        )
        self.assertIn(date(2021, 8, 28), readings)
        self.assertNotIn(date(2021, 9, 4), readings)

    def test_an_error_from_the_archive_is_raised_not_swallowed(self):
        payload = json.dumps({"error": True, "reason": "start_date is out of range"})
        with self.assertRaises(weather.WeatherUnavailable) as caught:
            weather.fetch(39.0, -76.6, date(2021, 8, 28), date(2021, 8, 29),
                          "America/New_York", opener=lambda url: payload)
        self.assertIn("out of range", str(caught.exception))

    def test_rain_worth_noticing(self):
        readings = weather.fetch(
            39.0, -76.6, date(2021, 8, 28), date(2021, 9, 4), "America/New_York",
            opener=lambda url: WEATHER_PAYLOAD,
        )
        call_command("generate_faire", "--year", "2021", stdout=StringIO())
        day = FaireDay.objects.get(date=date(2021, 8, 28))
        light = DayWeather.objects.create(day=day, **readings[date(2021, 8, 28)])
        self.assertFalse(light.was_wet)

        wet_day = FaireDay.objects.get(date=date(2021, 8, 29))
        wet = DayWeather.objects.create(day=wet_day, **readings[date(2021, 8, 29)])
        self.assertTrue(wet.was_wet)
class FetchWeatherCommandTests(TestCase):
    def setUp(self):
        call_command("generate_faire", "--year", "2021", stdout=StringIO())
        self.faire = Faire.objects.get(year=2021)

    def test_it_needs_coordinates_and_remembers_them(self):
        with self.assertRaises(CommandError) as caught:
            call_command("fetch_weather", "--year", "2021", stdout=StringIO())
        self.assertIn("coordinates", str(caught.exception))

        with mock.patch("scarves.weather._open", return_value=WEATHER_PAYLOAD):
            call_command("fetch_weather", "--year", "2021",
                         "--lat", "39.0068", "--lon", "-76.6", stdout=StringIO())
        self.faire.refresh_from_db()
        self.assertEqual(float(self.faire.latitude), 39.0068)
        self.assertEqual(float(self.faire.longitude), -76.6)

    def test_it_stores_what_it_got_and_names_what_it_did_not(self):
        out = StringIO()
        with mock.patch("scarves.weather._open", return_value=WEATHER_PAYLOAD):
            call_command("fetch_weather", "--year", "2021",
                         "--lat", "39.0068", "--lon", "-76.6", stdout=out)
        self.assertEqual(DayWeather.objects.count(), 2)
        report = out.getvalue()
        self.assertIn("no reading for yet", report)
        self.assertIn("come back later", report)

    def test_re_running_fetches_nothing_already_on_file(self):
        with mock.patch("scarves.weather._open", return_value=WEATHER_PAYLOAD) as opener:
            call_command("fetch_weather", "--year", "2021",
                         "--lat", "39.0068", "--lon", "-76.6", stdout=StringIO())
            first_calls = opener.call_count
            call_command("fetch_weather", "--year", "2021", stdout=StringIO())
            self.assertEqual(opener.call_count, first_calls + 1,
                             "one call for the days still missing, not a re-fetch of the lot")
        self.assertEqual(DayWeather.objects.count(), 2)

    def test_days_still_ahead_are_never_requested(self):
        """Asking the archive for the future is what earns a 400."""
        call_command("generate_faire", "--year", "2099", stdout=StringIO())
        out = StringIO()
        with mock.patch("scarves.weather._open") as opener:
            call_command("fetch_weather", "--year", "2099",
                         "--lat", "39.0", "--lon", "-76.6", stdout=out)
        opener.assert_not_called()
        self.assertIn("still ahead", out.getvalue())
        self.assertEqual(DayWeather.objects.count(), 0)

    def test_dry_run_writes_nothing(self):
        with mock.patch("scarves.weather._open", return_value=WEATHER_PAYLOAD):
            call_command("fetch_weather", "--year", "2021", "--lat", "39.0",
                         "--lon", "-76.6", "--dry-run", stdout=StringIO())
        self.assertEqual(DayWeather.objects.count(), 0)
class SeasonWeatherRowTests(TestCase):
    def test_a_weekend_with_no_reading_is_absent_not_cold_and_dry(self):
        call_command("generate_faire", "--year", "2021", stdout=StringIO())
        day = FaireDay.objects.get(date=date(2021, 8, 28))
        DayWeather.objects.create(day=day, mean_f=Decimal("78.4"),
                                  precipitation_in=Decimal("0.400"))

        seasons = seasonreport.build("labor-day-run", today=date(2026, 12, 31))
        weekends = {w.number: w for w in seasons[0].weekends}
        self.assertTrue(weekends[1].has_weather)
        self.assertEqual(weekends[1].mean_f, Decimal("78.4"))
        self.assertTrue(weekends[1].was_wet)
        self.assertFalse(weekends[2].has_weather)
        self.assertIsNone(weekends[2].mean_f)
class ProjectionBasisTests(TestCase):
    """A projected weekend is not a counted one, and the page must not blur it."""

    def setUp(self):
        call_command("generate_faire", "--range", "2021-2022", stdout=StringIO())

    def test_a_projection_never_lands_in_the_so_far_total(self):
        """Otherwise the figure captioned 'so far' equals the one captioned
        'on course for', and the page agrees with itself about a number
        nobody measured."""
        for number in range(1, 10):
            day = Faire.objects.get(year=2021).days.filter(weekend=number).first()
            sale_line(day.date, 100_000, order=f"a{number}")
        opener = Faire.objects.get(year=2022).days.filter(weekend=1).first()
        sale_line(opener.date, 50_000, order="b1")

        seasons = seasonreport.build("labor-day-run", today=date(2022, 8, 29))
        y2021 = next(s for s in seasons if s.year == 2021)
        y2022 = next(s for s in seasons if s.year == 2022)
        projected = seasonreport.project(y2022, [y2021])

        self.assertEqual(y2022.total, 50_000, "only what was counted")
        self.assertGreater(projected, y2022.total)
        self.assertEqual(y2022.traded_days, 2, "not the whole nineteen")
        self.assertEqual(y2022.per_day, Decimal(25_000))
        self.assertEqual(y2022.projection_weekends, 1)
class CategoryPillTests(TestCase):
    """`SaleLine` has a default ordering, and Django puts ordering columns
    into a `SELECT DISTINCT` — so the category filter once rendered one pill
    per line instead of one per category. Iterating is the only check that
    catches it: `.count()` wraps the query and reports the right number."""

    def setUp(self):
        call_command("generate_faire", "--year", "2021", stdout=StringIO())
        day = FaireDay.objects.filter(faire__year=2021).first().date
        for index in range(6):
            sale_line(day, 1000 + index,
                      category="Wax" if index % 2 else "Silk",
                      item=f"Thing {index}", hour=10 + index,
                      order=f"O{index}")

    def test_one_pill_per_category_not_one_per_line(self):
        self.assertEqual(SaleLine.objects.count(), 6)
        self.assertEqual(seasonreport.categories_on_file(), ["Silk", "Wax"])

    def test_the_page_renders_one_pill_per_category(self):
        user = User.objects.create_user("staff2", password="pw")
        self.client.force_login(user)
        response = self.client.get(reverse("season_report"))
        self.assertEqual(len(response.context["category_links"]), 2)
        body = response.content.decode()
        self.assertEqual(body.count("cat=Silk&"), body.count("cat=Silk&"))
        self.assertLess(body.count('class="pill'), 30)
class SeasonPaletteTests(TestCase):
    """The palette is a mode: it follows you round the page.

    Cosmetic, so an unknown value falls back rather than erroring — but it has
    to ride in the query string like every other piece of this page's state,
    or a reading sent to somebody arrives looking like a different reading.
    """

    def setUp(self):
        self.user = User.objects.create_user("staff3", password="pw")
        call_command("generate_faire", "--year", "2021", stdout=StringIO())
        day = FaireDay.objects.filter(faire__year=2021, weekend=1).first().date
        sale_line(day, 100_000, order="P1")
        self.client.force_login(self.user)
        self.url = reverse("season_report")

    def test_it_defaults_to_the_house_palette(self):
        response = self.client.get(self.url)
        self.assertEqual(response.context["palette"], seasonreport.DEFAULT_PALETTE)
        self.assertIn('data-palette="silk"', response.content.decode())

    def test_a_chosen_palette_reaches_the_body(self):
        response = self.client.get(self.url, {"palette": "eyebleed"})
        self.assertIn('data-palette="eyebleed"', response.content.decode())

    def test_every_other_control_carries_it(self):
        response = self.client.get(self.url, {"palette": "autumn", "metric": "units"})
        for group in ("mode_links", "metric_links", "year_links", "category_links"):
            for link in response.context[group]:
                self.assertIn("palette=autumn", link["href"], group)

    def test_an_unknown_palette_falls_back_rather_than_erroring(self):
        response = self.client.get(self.url, {"palette": "chartreuse"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["palette"], seasonreport.DEFAULT_PALETTE)

    def test_every_palette_has_readable_text(self):
        """The bug the eye-bleed palette made obvious and all four had.

        `--focus` is a 3px stroke on a chart and a bright colour is right
        there. The same colour as table text, a stat figure, or a pill
        background behind white is unreadable — 3.2:1 for the house colours,
        1.18:1 for the acid green, which is invisible rather than loud.
        """
        for entry in seasonreport.PALETTES:
            with self.subTest(palette=entry["key"]):
                ratio = seasonreport.contrast_ratio(
                    entry["focus_ink"], seasonreport.PAGE_GROUND,
                )
                self.assertGreaterEqual(
                    ratio, seasonreport.MIN_TEXT_CONTRAST,
                    f"{entry['key']} ink {entry['focus_ink']} is {ratio:.2f}:1 "
                    "on the page — text has to clear 4.5",
                )

    def test_the_mark_and_the_ink_are_not_the_same_colour(self):
        """If they ever converge, one of the two jobs is being done wrong."""
        for entry in seasonreport.PALETTES:
            with self.subTest(palette=entry["key"]):
                self.assertNotEqual(entry["focus"], entry["focus_ink"])

    def test_the_prior_season_ramp_reads_light_to_dark(self):
        """Seasons are ordered, so recency has to be legible without a legend."""
        for entry in seasonreport.PALETTES:
            with self.subTest(palette=entry["key"]):
                steps = [
                    seasonreport.contrast_ratio(entry[key], seasonreport.PAGE_GROUND)
                    for key in ("s1", "s2", "s3")
                ]
                self.assertEqual(steps, sorted(steps),
                                 "the ramp must darken from oldest to newest")

    def test_the_chart_names_classes_not_colours(self):
        """Which is why swapping palettes needs no redraw — and why the SVG
        carries no hex at all."""
        response = self.client.get(self.url, {"palette": "autumn"})
        svg = response.context["chart"]
        self.assertIn('class="line', svg)
        self.assertNotIn("#", svg)
