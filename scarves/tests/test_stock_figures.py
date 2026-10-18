"""On hand, par and sold this season travel together.

Any page that prints one of the three prints all three: none of them can be
judged alone — two on hand is plenty at par two and nothing at par twelve,
and a par means little without what sold against it. These pin the places
that used to print one or two, so a later edit that drops a column fails
here rather than in front of somebody deciding what to dye.

Sold is the till's season (`production.sold_per_blank()` for a colorway,
`production.sold_per_raw_blank()` for a blank). With no faire on file the
season falls back to today, so a sale rung up now counts.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .. import production, productline, slowsellers
from ..models import (
    InventoryLog,
    LabelStock,
    RawProduct,
    Sale,
    SaleLine,
    Supplier,
)
from .helpers import make_bathable, make_recipe


def ring(raw, units, product=None):
    """A till line on `raw`, with or without a colorway."""
    now = timezone.now()
    sale = Sale.objects.create(
        order_id=f"o-{raw.pk}-{units}-{now.timestamp()}-{product and product.pk}",
        sold_at=now, source=Sale.SOURCE_SQUARE_API,
    )
    return SaleLine.objects.create(
        sale=sale, line_key=f"k{raw.pk}-{units}-{product and product.pk}",
        sold_at=now, item_name=raw.name,
        price_point=product.recipe.name if product else "",
        quantity=units, finished_product=product, raw_product=raw,
        source=Sale.SOURCE_SQUARE_API,
    )


class SoldByRawBlankTests(TestCase):
    def test_a_flat_price_ring_counts_toward_its_blank(self):
        """The line with no colorway is the reason this is not the sum of
        the colorways' figures — a blank rung up flat would read as dead."""
        product = make_bathable(make_recipe("Ember"), "Sash Ember", on_hand=2)
        raw = product.raw_product
        ring(raw, 3, product)
        ring(raw, 4)

        self.assertEqual(production.sold_per_raw_blank()[raw.pk], 7)
        self.assertEqual(production.sold_per_blank()[product.pk], 3)

    def test_the_product_line_picker_reads_the_same_figure(self):
        product = make_bathable(make_recipe("Ember"), "Sash Ember", on_hand=2)
        ring(product.raw_product, 4)
        rng = slowsellers.season_range({})

        row = next(r for r in productline.blanks(rng)
                   if r["blank"] == product.raw_product)

        self.assertEqual(row["sold"], slowsellers.sold_by_raw_blank(rng)[product.raw_product.pk])


class ColorwayPagesTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_bathable(self.recipe, "Stormy Veil", on_hand=5, par=9)
        ring(self.product.raw_product, 6, self.product)

    def test_the_recipe_page_prints_sold_outside_par_mode(self):
        response = self.client.get(reverse("recipe_detail", args=[self.recipe.pk]))

        self.assertContains(response, "Sold this season")
        (product,) = response.context["products"]
        self.assertEqual(product.sold_this_season, 6)
        self.assertEqual(response.context["sold_season"], 6)
        self.assertEqual(response.context["par_total"], 9)

    def test_the_recipe_figures_follow_the_chip_swap(self):
        response = self.client.get(
            reverse("recipe_history", args=[self.recipe.pk]),
            {"product": self.product.pk},
        )

        self.assertEqual(response.context["sold_season"], 6)
        self.assertContains(response, "sold this season")

    def test_produced_since_prints_par_and_sold_beside_on_hand(self):
        InventoryLog.objects.create(
            finished_product=self.product, raw_product=self.product.raw_product,
            log_type=InventoryLog.PRODUCTION, quantity=4,
            source=InventoryLog.SOURCE_RECIPE_PAGE,
        )

        response = self.client.get(reverse("produced_since"))

        self.assertContains(response, "Sold this season")
        (log,) = response.context["record"].groups[0].rows
        self.assertEqual(log.sold_this_season, 6)

    def test_bulk_inventory_rows_carry_par_and_sold(self):
        response = self.client.get(
            reverse("bulk_inventory_update"),
            {"raw_ids": str(self.product.raw_product_id)},
        )

        (row,) = response.context["groups"][0]["rows"]
        self.assertEqual(row["sold"], 6)
        self.assertContains(response, "Sold this season")

    def test_the_on_hand_label_run_prints_par_and_sold(self):
        self.product.sku = "STRM-1"
        self.product.save()
        stock = LabelStock.objects.get(name__startswith="Avery 5167")
        params = {"dataset": "inventory", "extra": "0",
                  "stock": str(stock.pk), "start_at": "1"}

        response = self.client.get(reverse("label_index"), params)

        self.assertTrue(response.context["show_stock"])
        (row,) = response.context["run"].rows
        self.assertEqual(row.sold_this_season, 6)
        self.assertContains(response, "Sold this season")

    def test_slow_sellers_pools_par_with_on_hand(self):
        other = make_bathable(make_recipe("Other"), "Other Veil", on_hand=0, par=3)
        response = self.client.get(
            reverse("slow_sellers"), {"range": "all", "max": "100"}
        )

        rows = {r.recipe.pk: r for r in response.context["rows"]}
        self.assertEqual(rows[self.recipe.pk].par, 9)
        self.assertEqual(rows[other.recipe.pk].par, 3)

    def test_the_sales_report_offers_the_season(self):
        response = self.client.get(reverse("sales_report"), {"range": "season"})

        keys = [r["key"] for r in response.context["ranges"]]
        self.assertIn("season", keys)
        # With no faire on file the season falls back to the default window,
        # which is what every other page's season does too.
        self.assertEqual(response.status_code, 200)


class RawBlankPagesTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.product = make_bathable(make_recipe("Ember"), "Sash Ember", on_hand=2)
        self.raw = self.product.raw_product
        RawProduct.objects.filter(pk=self.raw.pk).update(par_level=30)
        ring(self.raw, 3, self.product)
        ring(self.raw, 4)

    def test_raw_inventory_prints_sold_beside_on_hand_and_par(self):
        response = self.client.get(
            reverse("raw_inventory", args=[self.raw.category_id])
        )

        self.assertContains(response, "Sold this season")
        (blank,) = [p for p in response.context["products"] if p.pk == self.raw.pk]
        self.assertEqual(blank.sold_this_season, 7)

    def test_the_raw_inventory_picker_totals_par_and_sold(self):
        response = self.client.get(reverse("raw_inventory_index"))

        (card,) = [c for c in response.context["categories"]
                   if c.pk == self.raw.category_id]
        self.assertEqual(card.par_total, 30)
        self.assertEqual(card.sold_this_season, 7)

    def test_a_supplier_page_prints_sold(self):
        supplier = Supplier.objects.create(name="Wool2dye4")
        RawProduct.objects.filter(pk=self.raw.pk).update(supplier=supplier)

        response = self.client.get(reverse("supplier_detail", args=[supplier.pk]))

        (blank,) = response.context["blanks"]
        self.assertEqual(blank.sold_this_season, 7)
        self.assertContains(response, "Sold this season")

    def test_the_bulk_picker_prints_par_and_sold(self):
        response = self.client.get(reverse("bulk_inventory_update"))

        self.assertContains(response, "par&nbsp;30")
        self.assertContains(response, "sold&nbsp;7&nbsp;this&nbsp;season")

    def test_the_product_line_picker_prints_par(self):
        response = self.client.get(reverse("product_line_index"), {"range": "all"})

        (row,) = [r for r in response.context["blanks"] if r["blank"] == self.raw]
        self.assertEqual(row["par"], 8)
        self.assertContains(response, "Undyed blanks / par")
