"""What a session was worth: the freeze at accept, and `private/dye-statements/`.

Three failures are pinned above the arithmetic, because each one is a money
error the moment a statement becomes a bill for the dyeing:

- a figure derived on read, so a reprice next spring rewrites what a past
  session earned;
- a bath whose entry was taken back still being paid for;
- a bath from before the figures were kept being valued at zero rather than
  reported as unpriced.
"""
import dataclasses
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .. import dyebill, producedsince, production, sales
from ..models import (
    Faire,
    FaireDay,
    FinishedProduct,
    ProductionRun,
    ProductionRunRow,
    RawProduct,
    RawProductCategory,
    Sale,
    SaleLine,
)
from .helpers import make_recipe


class FreezeAtAcceptTests(TestCase):
    """The figures are written when the bath is accepted, and never again."""

    def setUp(self):
        category = RawProductCategory.objects.create(name="Silk")
        self.blank = RawProduct.objects.create(
            name="Half Circle Veil", category=category, price="24.99",
            number_per_dye_bath=5, number_on_hand=100,
        )
        self.product = FinishedProduct.objects.create(
            name="Ember Half Circle", raw_product=self.blank,
            recipe=make_recipe("Ember"), price="95.00",
        )
        self.run = ProductionRun.objects.create()

    def _row(self):
        # One tuple is one bath, and the number is the units it makes — not a
        # bath count. `open_rows` is the only door that claims the blanks.
        return production.open_rows(
            self.run, [(self.product, self.product.bath_size)]
        )[0]

    def test_a_bath_records_what_it_cost_and_what_it_made(self):
        row = self._row()
        production.apply_row(row)
        row.refresh_from_db()

        self.assertEqual(row.unit_blank_cost, Decimal("24.99"))
        self.assertEqual(row.output_retail, Decimal("475.00"))  # 5 × $95

    def test_a_later_reprice_does_not_rewrite_a_closed_session(self):
        """The whole reason the figures are stored. A bill is against the
        prices at the time."""
        row = self._row()
        production.apply_row(row)

        self.blank.price = Decimal("40.00")
        self.blank.save()
        self.product.price = Decimal("120.00")
        self.product.save()

        statement = dyebill.statements()[0]
        self.assertEqual(statement.cost, Decimal("124.95"))   # 5 × $24.99
        self.assertEqual(statement.retail, Decimal("475.00"))

    def test_a_short_bath_still_consumed_its_blanks(self):
        row = self._row()
        production.apply_row(row, yielded=3)

        statement = dyebill.statements()[0]
        self.assertEqual(statement.cost, Decimal("124.95"))   # five blanks
        self.assertEqual(statement.retail, Decimal("285.00"))  # three scarves
        self.assertEqual(statement.lost, 2)

    def test_a_lost_bath_costs_its_blanks_and_makes_nothing(self):
        row = self._row()
        production.apply_row(row, yielded=0)

        statement = dyebill.statements()[0]
        self.assertEqual(statement.cost, Decimal("124.95"))
        self.assertEqual(statement.retail, Decimal("0.00"))

    def test_a_cancelled_bath_is_not_a_session_at_all(self):
        production.cancel_row(self._row())

        self.assertEqual(dyebill.statements(), [])


class FancySplitTests(TestCase):
    """A bath finished partly fancy sells its output at two prices."""

    def setUp(self):
        category = RawProductCategory.objects.create(name="Silk")
        self.blank = RawProduct.objects.create(
            name="Half Circle Veil", category=category, price="24.99",
            number_per_dye_bath=5, number_on_hand=100,
        )
        self.fancy_blank = RawProduct.objects.create(
            name="Fancy Half Circle Veil", category=category, price="0",
            fancying_cost="25.00", made_in_a_dye_bath=False,
            number_per_dye_bath=5,
        )
        self.blank.fancy_counterpart = self.fancy_blank
        self.blank.save()
        recipe = make_recipe("Ember")
        self.product = FinishedProduct.objects.create(
            name="Ember Half Circle", raw_product=self.blank,
            recipe=recipe, price="95.00",
        )
        self.fancy = FinishedProduct.objects.create(
            name="Ember Fancy Half Circle", raw_product=self.fancy_blank,
            recipe=recipe, price="125.00",
        )

    def test_the_output_is_priced_at_both_prices(self):
        row = production.open_rows(
            ProductionRun.objects.create(),
            [(self.product, self.product.bath_size)],
        )[0]
        production.apply_row(row, fancy=2)
        row.refresh_from_db()

        # 3 plain at $95 + 2 fancy at $125
        self.assertEqual(row.output_retail, Decimal("535.00"))
        # The blanks are untouched by the split: a fancy veil is a plain scarf
        # somebody worked on, so the bath ate five plain veils.
        self.assertEqual(row.unit_blank_cost, Decimal("24.99"))


class StatementTests(TestCase):
    def setUp(self):
        category = RawProductCategory.objects.create(name="Yarn")
        self.blank = RawProduct.objects.create(
            name="Heavenly", category=category, price="12.89",
            number_per_dye_bath=5, number_on_hand=200,
        )
        self.product = FinishedProduct.objects.create(
            name="Heavenly Rainbow", raw_product=self.blank,
            recipe=make_recipe("Rainbow"), price="39.00",
        )

    def _closed_run(self, baths=2, **kwargs):
        run = ProductionRun.objects.create()
        plan = [(self.product, self.product.bath_size)] * baths
        for row in production.open_rows(run, plan):
            production.apply_row(row, **kwargs)
        return run

    def test_a_session_reports_its_baths_units_and_money(self):
        self._closed_run(baths=2)
        statement = dyebill.statements()[0]

        self.assertEqual(statement.baths, 2)
        self.assertEqual(statement.yielded, 10)
        self.assertEqual(statement.cost, Decimal("128.90"))
        self.assertEqual(statement.retail, Decimal("390.00"))
        self.assertEqual(statement.value_added, Decimal("261.10"))

    def test_a_taken_back_bath_is_off_the_bill(self):
        """The question is never whether an entry was written, it is whether
        one still stands. Paying for a retracted bath is the money version of
        the double-count `produced_since` exists to undo."""
        run = self._closed_run(baths=2)
        first = run.rows.order_by("pk").first()
        producedsince.retract(first.applied_log)

        statement = dyebill.statements()[0]
        self.assertEqual(statement.baths, 1)
        self.assertEqual(statement.retracted, 1)
        self.assertEqual(statement.cost, Decimal("64.45"))
        self.assertEqual(statement.retail, Decimal("195.00"))

    def test_a_bath_from_before_the_freeze_is_estimated_at_todays_prices(self):
        """Left blank, a page of real sessions reads as though it earned
        nothing. Valued, it has to be tellable from a record."""
        run = self._closed_run(baths=1)
        run.rows.update(unit_blank_cost=None, output_retail=None)

        statement = dyebill.statements()[0]
        self.assertEqual(statement.baths, 1)
        self.assertEqual(statement.unpriced, 1)
        self.assertTrue(statement.is_estimated)
        # Nothing frozen, everything estimated, and the two add up to the
        # figure the page prints.
        self.assertEqual(statement.frozen_cost, Decimal("0.00"))
        self.assertEqual(statement.estimated_cost, Decimal("64.45"))
        self.assertEqual(statement.estimated_retail, Decimal("195.00"))
        self.assertEqual(statement.cost, Decimal("64.45"))
        self.assertEqual(statement.retail, Decimal("195.00"))
        self.assertEqual(
            dyebill.totals([statement]).estimated_retail, Decimal("195.00")
        )

    def test_an_estimate_is_never_written_back(self):
        """The null columns stay null. A guess that is stored is
        indistinguishable from a record the moment after it is written."""
        run = self._closed_run(baths=1)
        run.rows.update(unit_blank_cost=None, output_retail=None)

        dyebill.statements()[0].retail

        row = run.rows.get()
        self.assertIsNone(row.unit_blank_cost)
        self.assertIsNone(row.output_retail)

    def test_an_estimate_moves_with_the_price_list_and_a_record_does_not(self):
        priced = self._closed_run(baths=1)
        stale = self._closed_run(baths=1)
        stale.rows.update(unit_blank_cost=None, output_retail=None)

        self.product.price = Decimal("50.00")
        self.product.save()

        by_run = {s.run.pk: s for s in dyebill.statements()}
        self.assertEqual(by_run[priced.pk].retail, Decimal("195.00"))
        self.assertEqual(by_run[stale.pk].retail, Decimal("250.00"))

    def test_a_pending_sheet_is_not_a_statement(self):
        production.open_rows(
            ProductionRun.objects.create(),
            [(self.product, self.product.bath_size)] * 3,
        )

        self.assertEqual(dyebill.statements(), [])

    def test_sessions_come_back_newest_first(self):
        older = self._closed_run(baths=1)
        newer = self._closed_run(baths=1)

        self.assertEqual(
            [s.run.pk for s in dyebill.statements()], [newer.pk, older.pk]
        )

    def test_totals_add_the_sessions_up(self):
        self._closed_run(baths=1)
        self._closed_run(baths=2)
        totals = dyebill.totals(dyebill.statements())

        self.assertEqual(totals.runs, 2)
        self.assertEqual(totals.baths, 3)
        self.assertEqual(totals.yielded, 15)
        self.assertEqual(totals.cost, Decimal("193.35"))
        self.assertEqual(totals.retail, Decimal("585.00"))
        self.assertEqual(totals.value_added, Decimal("391.65"))


class EstimateTests(TestCase):
    """The planning-stage figure: what a session will be worth before it runs."""

    def setUp(self):
        category = RawProductCategory.objects.create(name="Silk")
        self.blank = RawProduct.objects.create(
            name="Half Circle Veil", category=category, price="24.99",
            number_per_dye_bath=5, number_on_hand=100,
        )
        self.product = FinishedProduct.objects.create(
            name="Ember Half Circle", raw_product=self.blank,
            recipe=make_recipe("Ember"), price="95.00",
        )

    def test_a_planned_list_is_priced_at_todays_prices(self):
        baths = production.baths_from_picks([(self.product, 3)])
        estimate = dyebill.estimate(baths)

        self.assertEqual(estimate.baths, 3)
        self.assertEqual(estimate.units, 15)
        self.assertEqual(estimate.cost, Decimal("374.85"))
        self.assertEqual(estimate.retail, Decimal("1425.00"))
        self.assertEqual(estimate.value_added, Decimal("1050.15"))

    def test_an_empty_list_is_worth_nothing_and_says_so(self):
        estimate = dyebill.estimate([])

        self.assertEqual(estimate.baths, 0)
        self.assertEqual(estimate.cost, Decimal("0.00"))

    def test_the_planner_prints_it(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        response = self.client.get(
            reverse("production_sheet_index"),
            {"items": f"{self.product.pk}:2"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("249.90", body)   # blanks for two baths
        self.assertIn("950.00", body)   # at the asking price


class StatementPageTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        category = RawProductCategory.objects.create(name="Yarn")
        blank = RawProduct.objects.create(
            name="Heavenly", category=category, price="12.89",
            number_per_dye_bath=5, number_on_hand=50,
        )
        product = FinishedProduct.objects.create(
            name="Heavenly Rainbow", raw_product=blank,
            recipe=make_recipe("Rainbow"), price="39.00",
        )
        run = ProductionRun.objects.create()
        for row in production.open_rows(run, [(product, product.bath_size)]):
            production.apply_row(row)

    def test_the_page_prints_a_statement(self):
        response = self.client.get(reverse("dye_statements"))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("64.45", body)    # the blanks
        self.assertIn("195.00", body)   # at the asking price

    def test_it_names_no_rate_for_the_dyeing(self):
        """Nothing has been agreed, and a number in a column reads as a
        decision. The page says the rate is absent rather than guessing one."""
        body = self.client.get(reverse("dye_statements")).content.decode()

        self.assertIn("no rate here", body)


class WindowTests(TestCase):
    """The date filter, and what it is drawn on.

    The window selects **baths** by `accepted_at`, not whole runs by their
    last accept. Getting that backwards would pile a sheet worked over ten
    days onto one afternoon, which is the failure `import_square_sales`
    already has at the other end of the shop.
    """

    def setUp(self):
        category = RawProductCategory.objects.create(name="Yarn")
        self.blank = RawProduct.objects.create(
            name="Heavenly", category=category, price="10.00",
            number_per_dye_bath=5, number_on_hand=200,
        )
        self.product = FinishedProduct.objects.create(
            name="Heavenly Rainbow", raw_product=self.blank,
            recipe=make_recipe("Rainbow"), price="30.00",
        )
        self.run = ProductionRun.objects.create()

    def _bath_on(self, day):
        row = production.open_rows(
            self.run, [(self.product, self.product.bath_size)]
        )[0]
        production.apply_row(row)
        when = timezone.make_aware(
            datetime.combine(day, time(14, 0)), timezone.get_current_timezone()
        )
        ProductionRunRow.objects.filter(pk=row.pk).update(accepted_at=when)
        return row

    def test_a_bath_outside_the_window_is_not_on_the_page(self):
        self._bath_on(date(2026, 9, 1))
        rng = sales.resolve_range({"from": "2026-09-10", "to": "2026-09-20"})

        self.assertEqual(dyebill.statements(rng), [])

    def test_the_window_cuts_a_session_at_the_bath(self):
        """One sheet, two baths, one on each side of the boundary. A range is
        answered with the baths inside it and not with the whole sheet."""
        self._bath_on(date(2026, 9, 1))
        self._bath_on(date(2026, 9, 15))

        self.assertEqual(dyebill.statements()[0].baths, 2)

        rng = sales.resolve_range({"from": "2026-09-10", "to": "2026-09-20"})
        inside = dyebill.statements(rng)
        self.assertEqual(len(inside), 1)
        self.assertEqual(inside[0].baths, 1)
        self.assertEqual(dyebill.totals(inside).yielded, 5)

    def test_the_last_day_of_the_window_is_whole(self):
        """Half-open on the datetime, so a bath accepted at two in the
        afternoon on the closing day is inside it."""
        self._bath_on(date(2026, 9, 20))
        rng = sales.resolve_range({"from": "2026-09-20", "to": "2026-09-20"})

        self.assertEqual(dyebill.totals(dyebill.statements(rng)).baths, 1)

    def test_a_bare_visit_still_means_every_session_on_file(self):
        """The one place this page differs from `private/sales/`, which
        defaults to today. Changing it would silently re-answer a bookmark."""
        rng = dyebill.resolve_range({})

        self.assertTrue(rng.is_all_time)

    def test_the_range_keys_are_the_sales_report_s(self):
        """A window picked on one report page means the same window here."""
        rng = dyebill.resolve_range({"range": "30"})

        self.assertEqual(rng.key, "30")
        self.assertEqual(rng.start, timezone.localdate() - timedelta(days=29))


class AlongsideTests(TestCase):
    """What sold over the same dates, printed beside what was made.

    Two rules are pinned here rather than left to the template, because both
    are about what the page is allowed to claim: the money on the two sides
    is never differenced, and the units that could not be attributed to a
    colorway are named rather than quietly dropped.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.category = RawProductCategory.objects.create(name="Yarn")
        self.blank = RawProduct.objects.create(
            name="Heavenly", category=self.category, price="10.00",
            number_per_dye_bath=5, number_on_hand=200,
        )
        self.product = FinishedProduct.objects.create(
            name="Heavenly Rainbow", raw_product=self.blank,
            recipe=make_recipe("Rainbow"), price="30.00",
        )
        self.day = timezone.now()

    def _sell(self, units, product=None, blank=None, cents=0):
        sale = Sale.objects.create(
            order_id=f"o-{Sale.objects.count()}", sold_at=self.day,
            source=Sale.SOURCE_SQUARE_API,
        )
        return SaleLine.objects.create(
            sale=sale, line_key=f"k-{SaleLine.objects.count()}",
            sold_at=self.day,
            item_name=(blank or self.blank).name,
            price_point="Rainbow" if product else "Regular Price",
            quantity=units, gross_cents=cents, net_cents=cents,
            finished_product=product,
            raw_product=blank or self.blank,
            source=Sale.SOURCE_SQUARE_API,
        )

    def _dye(self, baths=1):
        run = ProductionRun.objects.create()
        for row in production.open_rows(
            run, [(self.product, self.product.bath_size)] * baths
        ):
            production.apply_row(row)

    def test_it_counts_what_sold_with_a_colorway_on_it(self):
        self._sell(3, product=self.product, cents=9000)
        counter = dyebill.sold(sales.resolve_range({"range": "all"}))

        self.assertEqual(counter.units, 3)
        self.assertEqual(counter.net, Decimal("90.00"))

    def test_a_flat_price_button_is_carried_separately_not_folded_in(self):
        """Those units are dyed stock and nothing says which colorway, so they
        are named beside the count rather than added to it — the same call
        `slowsellers.unattributed` makes."""
        self._sell(3, product=self.product, cents=9000)
        self._sell(4)                      # no colorway rang up
        counter = dyebill.sold(sales.resolve_range({"range": "all"}))

        self.assertEqual(counter.units, 3)
        self.assertEqual(counter.colourless, 4)

    def test_a_notion_never_saw_a_pot_and_is_not_a_dyed_sale(self):
        """`made_in_a_dye_bath` is the obvious test and the wrong one: it is
        true for notions and for undyed yarn, neither of which came out of a
        bath. Having a colorway on file is the test that means what is
        wanted."""
        notions = RawProductCategory.objects.create(name="Notions")
        bowl = RawProduct.objects.create(
            name="Yarn Bowl", category=notions, price="30.00",
            number_on_hand=10,
        )
        FinishedProduct.objects.create(
            name="Yarn Bowl", raw_product=bowl, recipe=None, price="30.00",
        )
        self._sell(6, blank=bowl, cents=18000)
        counter = dyebill.sold(sales.resolve_range({"range": "all"}))

        self.assertEqual(counter.units, 0)
        self.assertEqual(counter.colourless, 0)

    def test_the_difference_is_units_and_says_which_way_the_shelves_moved(self):
        self._dye(baths=2)                              # 10 units made
        self._sell(14, product=self.product, cents=42000)

        rng = sales.resolve_range({"range": "all"})
        made = dyebill.totals(dyebill.statements(rng))
        alongside = dyebill.alongside(made, dyebill.sold(rng))

        self.assertEqual(alongside.change, -4)
        self.assertEqual(alongside.size, 4)
        self.assertEqual(alongside.direction, "lighter")

    def test_level_is_its_own_answer_rather_than_a_zero(self):
        self._dye(baths=1)
        self._sell(5, product=self.product, cents=15000)

        rng = sales.resolve_range({"range": "all"})
        made = dyebill.totals(dyebill.statements(rng))

        self.assertEqual(
            dyebill.alongside(made, dyebill.sold(rng)).direction, "level"
        )

    def test_the_page_prints_both_sides(self):
        self._dye(baths=1)
        self._sell(8, product=self.product, cents=24000)
        body = self.client.get(reverse("dye_statements")).content.decode()

        self.assertIn("Out of the dye room", body)
        self.assertIn("Over the counter", body)
        self.assertIn("lighter", body)

    def test_the_difference_is_never_worded_as_a_result(self):
        """The sentence says which way the shelves moved. It does not say
        that either side won, fell short or failed to keep up — the figure is
        a fact about stock, and the people it is about read this page."""
        self._dye(baths=1)
        self._sell(40, product=self.product, cents=120000)
        body = self.client.get(reverse("dye_statements")).content.decode()

        said = re.search(r'<p class="shelves">(.*?)</p>', body, re.S).group(1)
        for word in ("winning", "ahead", "behind", "keeping up", "kept up",
                     "shortfall", "failed", "only"):
            self.assertNotIn(word, said.lower(), f"{word!r} is a verdict")

    def test_the_two_money_figures_are_never_differenced(self):
        """Output is at the asking price and the till is net of whatever it
        actually went out at, so a difference between them reads as a margin
        and is not one. The comparison carries units and nothing else, which
        is what makes that impossible to render by accident."""
        self._dye(baths=1)
        self._sell(8, product=self.product, cents=20000)

        rng = sales.resolve_range({"range": "all"})
        alongside = dyebill.alongside(
            dyebill.totals(dyebill.statements(rng)), dyebill.sold(rng)
        )

        self.assertEqual(
            {f.name for f in dataclasses.fields(alongside)}, {"made", "sold"}
        )

    def test_a_window_with_no_session_says_so_without_claiming_the_book_is_empty(self):
        self._dye(baths=1)
        body = self.client.get(
            reverse("dye_statements"), {"from": "2020-01-01", "to": "2020-01-31"}
        ).content.decode()

        self.assertIn("No bath was accepted between these dates", body)


class MissingExportTests(TestCase):
    """A trading day nobody imported is not a day nothing sold.

    The two are the same zero in the sold column and read oppositely, and the
    direction the mistake runs matters: an unloaded export makes the shelves
    read fuller than they were, on the one figure on this page that is about
    somebody's work.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.faire = Faire.objects.create(
            name="Renaissance Faire", slug="ren", year=2026,
        )
        for day in (date(2026, 8, 29), date(2026, 8, 30)):
            FaireDay.objects.create(
                faire=self.faire, date=day, weekend=1, traded=True,
            )
        self.rng = sales.resolve_range({"from": "2026-08-29", "to": "2026-08-30"})

    def test_a_traded_day_with_nothing_on_file_is_counted_as_a_gap(self):
        counter = dyebill.sold(self.rng)

        self.assertEqual(counter.trading_days, 2)
        self.assertEqual(counter.missing_days, 2)

    def test_a_window_with_no_faire_days_has_no_gaps_to_report(self):
        """Off season there is nothing to import, so a zero is just a zero."""
        counter = dyebill.sold(
            sales.resolve_range({"from": "2026-02-01", "to": "2026-02-28"})
        )

        self.assertEqual(counter.trading_days, 0)
        self.assertEqual(counter.missing_days, 0)

    def test_a_day_that_was_struck_is_not_a_missing_export(self):
        """`traded` is the denominator everywhere else and it is here too — a
        washed-out day has no export to be waiting for."""
        FaireDay.objects.filter(date=date(2026, 8, 30)).update(traded=False)

        self.assertEqual(dyebill.sold(self.rng).missing_days, 1)

    def test_the_page_says_so_rather_than_printing_a_bare_zero(self):
        body = self.client.get(
            reverse("dye_statements"), {"from": "2026-08-29", "to": "2026-08-30"}
        ).content.decode()

        self.assertIn("sales on file at all", body)
        self.assertIn("reads low", body)
        # And it says how many, so the reading can be weighed rather than
        # just doubted.
        self.assertIn("<b>2</b>", body)


class EmptyWindowTests(TestCase):
    """A window with nothing on either side is not a reading of level."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))

    def test_it_draws_no_conclusion_about_shelves_that_did_not_move(self):
        body = self.client.get(
            reverse("dye_statements"), {"from": "2020-01-01", "to": "2020-01-31"}
        ).content.decode()

        self.assertNotIn("dyed shelves", body)
        self.assertNotIn("came out of the pots", body)
        self.assertIn("No bath was accepted between these dates", body)
