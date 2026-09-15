"""What a session was worth: the freeze at accept, and `private/dye-statements/`.

Three failures are pinned above the arithmetic, because each one is a money
error the moment a statement becomes a bill for the dyeing:

- a figure derived on read, so a reprice next spring rewrites what a past
  session earned;
- a bath whose entry was taken back still being paid for;
- a bath from before the figures were kept being valued at zero rather than
  reported as unpriced.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .. import dyebill, producedsince, production
from ..models import (
    FinishedProduct,
    ProductionRun,
    RawProduct,
    RawProductCategory,
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

    def test_a_bath_from_before_the_freeze_is_counted_and_not_valued(self):
        run = self._closed_run(baths=1)
        run.rows.update(unit_blank_cost=None, output_retail=None)

        statement = dyebill.statements()[0]
        self.assertEqual(statement.baths, 1)
        self.assertEqual(statement.unpriced, 1)
        self.assertEqual(statement.cost, Decimal("0.00"))
        self.assertEqual(dyebill.totals([statement]).unpriced, 1)

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
