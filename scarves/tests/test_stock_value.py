"""The shelves priced: `scarves/stockvalue.py` and `private/stock-value/`.

Two failures are worth pinning above the arithmetic, because both are silent
and both would make the headline figure wrong in a direction somebody would
believe: a passthrough's one physical pile counted on both of its rows, and
yarn claimed by an open sheet dropped out of a valuation because
`number_on_hand` means *unclaimed* everywhere else in the app.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .. import production, stockvalue
from ..models import (
    FinishedProduct,
    ProductionRun,
    RawProduct,
    RawProductCategory,
)
from .helpers import make_recipe


def _section(found, key):
    return next(s for s in found if s.key == key)


class StockValueTests(TestCase):
    def setUp(self):
        self.silk = RawProductCategory.objects.create(name="Silk")
        self.yarn = RawProductCategory.objects.create(name="Yarn")
        self.blank = RawProduct.objects.create(
            name="Infinity Scarf",
            category=self.silk,
            price="6.00",
            suggested_price="30.00",
            number_per_dye_bath=5,
            number_on_hand=52,
        )
        self.recipe = make_recipe("Rainbow")

    def test_undyed_stock_is_valued_at_cost_and_at_what_it_will_sell_for(self):
        rows = _section(stockvalue.sections(), "undyed").rows

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.units, 52)
        self.assertEqual(row.cost, Decimal("312.00"))
        self.assertEqual(row.retail, Decimal("1560.00"))
        self.assertEqual(row.retail_basis, stockvalue.BASIS_SET)

    def test_baths_are_the_naive_division(self):
        row = _section(stockvalue.sections(), "undyed").rows[0]

        self.assertEqual(row.baths, 10)  # 52 // 5, the remainder is not a bath

    def test_a_blank_no_bath_can_produce_has_no_bath_count(self):
        """A dash, not a zero. Fancy blanks and passthrough blanks are real
        stock and a bath is not a thing that happens to them."""
        self.blank.made_in_a_dye_bath = False
        self.blank.save()

        self.assertIsNone(_section(stockvalue.sections(), "undyed").rows[0].baths)

    def test_claimed_yarn_is_still_owned(self):
        """`number_on_hand` means unclaimed — right for ordering, wrong for a
        valuation. A planned bath has not spent anything."""
        product = FinishedProduct.objects.create(
            name="Infinity Rainbow", raw_product=self.blank,
            recipe=self.recipe, price="30.00",
        )
        # `open_rows` is the only door that claims the blanks, which is the
        # whole reason this test exists.
        production.open_rows(ProductionRun.objects.create(), [(product, 5)])
        self.blank.refresh_from_db()
        self.assertEqual(self.blank.number_on_hand, 47)  # the claim came off

        row = _section(stockvalue.sections(), "undyed").rows[0]
        self.assertEqual(row.units, 52)
        self.assertEqual(row.claimed, 5)

    def test_dyed_stock_is_valued_off_the_finished_price(self):
        FinishedProduct.objects.create(
            name="Infinity Rainbow", raw_product=self.blank,
            recipe=self.recipe, price="34.00", number_on_hand=3,
        )
        section = _section(stockvalue.sections(), "dyed")

        self.assertEqual(section.units, 3)
        self.assertEqual(section.retail, Decimal("102.00"))
        self.assertEqual(section.cost, Decimal("18.00"))  # the blank's cost

    def test_a_passthrough_pile_is_valued_once(self):
        """One pile, two rows. The raw row holds the count and the finished
        row mirrors it, so valuing both would double the shelf."""
        skein = RawProduct.objects.create(
            name="Heavenly Undyed", category=self.yarn,
            price="4.00", number_on_hand=10,
        )
        FinishedProduct.objects.create(
            name="Heavenly Undyed", raw_product=skein, recipe=None, price="18.00",
        )

        found = stockvalue.sections()
        self.assertEqual(
            [r.name for r in _section(found, "undyed").rows], ["Infinity Scarf"]
        )
        bought = _section(found, "passthrough")
        self.assertEqual(bought.units, 10)
        self.assertEqual(bought.cost, Decimal("40.00"))
        self.assertEqual(bought.retail, Decimal("180.00"))

    def test_an_unpriced_blank_gets_no_retail_rather_than_a_zero(self):
        self.blank.suggested_price = None
        self.blank.save()

        row = _section(stockvalue.sections(), "undyed").rows[0]
        self.assertIsNone(row.retail)
        self.assertEqual(row.unpriced_units, 52)

    def test_an_unpriced_blank_falls_back_to_its_own_colorways(self):
        self.blank.suggested_price = None
        self.blank.save()
        FinishedProduct.objects.create(
            name="Infinity Rainbow", raw_product=self.blank,
            recipe=self.recipe, price="40.00", number_on_hand=1,
        )

        row = _section(stockvalue.sections(), "undyed").rows[0]
        self.assertEqual(row.retail, Decimal("2080.00"))
        self.assertEqual(row.retail_basis, stockvalue.BASIS_CATALOGUE)

    def test_a_retired_blank_is_not_on_the_balance(self):
        self.blank.is_active = False
        self.blank.save()

        self.assertEqual(_section(stockvalue.sections(), "undyed").rows, [])

    def test_totals_add_the_sections_up(self):
        FinishedProduct.objects.create(
            name="Infinity Rainbow", raw_product=self.blank,
            recipe=self.recipe, price="34.00", number_on_hand=3,
        )
        found = stockvalue.sections()
        totals = stockvalue.totals(found)

        self.assertEqual(totals.units, 55)
        self.assertEqual(totals.cost, Decimal("330.00"))
        self.assertEqual(totals.retail, Decimal("1662.00"))
        self.assertEqual(totals.margin, Decimal("1332.00"))


class StockValuePageTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        category = RawProductCategory.objects.create(name="Silk")
        RawProduct.objects.create(
            name="Infinity Scarf", category=category, price="6.00",
            suggested_price="30.00", number_per_dye_bath=5, number_on_hand=52,
        )

    def test_the_page_prints_the_balance(self):
        response = self.client.get(reverse("stock_value"))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Infinity Scarf", body)
        self.assertIn("1560", body)   # at the asking price
        self.assertIn("312", body)    # at cost
