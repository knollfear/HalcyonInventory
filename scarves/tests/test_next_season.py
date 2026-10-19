"""Weekends of cover — `scarves/nextseason.py` and `private/next-season/`.
"""
from datetime import date, datetime, time
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .. import nextseason, tiers
from ..models import (
    FaireDay, FinishedProduct, RawProduct, RawProductCategory, Sale, SaleLine,
)
from .helpers import make_recipe

FRIDAY_BEFORE_WEEKEND_9 = date(2026, 10, 23)
AFTER_THE_CLOSE = date(2026, 10, 26)


class Season:
    """A yarn base with colours and two seasons of sales, built per test."""

    def __init__(self, per_day=None):
        for year in ("2025", "2026"):
            call_command("generate_faire", "--year", year, stdout=StringIO())
        yarn, _ = RawProductCategory.objects.get_or_create(name="Yarn")
        self.raw = RawProduct.objects.create(
            name="Homespun", category=yarn, price="6.70", number_per_dye_bath=4,
            number_on_hand=0,
        )
        self.colours = {}
        self.n = 0
        for name, (tier, rate) in (per_day or {
            "Cabernet": ("high", 4), "Teal": ("medium", 1),
        }).items():
            self.add(name, tier, rate)

    def add(self, name, tier, rate, on_hand=0):
        product = FinishedProduct.objects.create(
            name=f"Homespun - {name}", raw_product=self.raw,
            recipe=make_recipe(name), price="25.00", number_on_hand=on_hand,
            par=8, tier=tier,
        )
        self.colours[name] = product
        if rate:
            # Every traded day before weekend 9 of 2026, and all of 2025.
            for day in FaireDay.objects.filter(traded=True):
                if day.date >= date(2026, 10, 24):
                    continue
                self.sell(product, day.date, rate)
        return product

    def sell(self, product, day, units):
        self.n += 1
        when = timezone.make_aware(datetime.combine(day, time(12)))
        sale = Sale.objects.create(order_id=f"o{self.n}", sold_at=when,
                                   source=Sale.SOURCE_SQUARE_API)
        SaleLine.objects.create(
            sale=sale, line_key=f"k{self.n}", sold_at=when,
            item_name=self.raw.name, price_point=product.recipe.name,
            quantity=units, finished_product=product, raw_product=self.raw,
            source=Sale.SOURCE_SQUARE_API,
        )

    def stock(self, name, on_hand):
        FinishedProduct.objects.filter(pk=self.colours[name].pk).update(number_on_hand=on_hand)

    def plan(self, today, **kwargs):
        hz, rows = nextseason.plan(today=today, blanks=[self.raw], **kwargs)
        return hz, {c.product.name.split(" - ")[1]: c for c in rows[0].colours}, rows[0]


class WhatItLooksAheadOverTests(TestCase):

    def setUp(self):
        Season()

    def test_while_the_season_runs_it_looks_at_the_weekends_left(self):
        """Which is how it gets checked: it says what weekend 9 will run out
        of, and then weekend 9 happens."""
        hz = nextseason.horizon(FRIDAY_BEFORE_WEEKEND_9)

        self.assertTrue(hz.current)
        self.assertEqual(hz.weekends, [(9, 2)])

    def test_after_the_close_it_looks_at_all_of_next_season(self):
        hz = nextseason.horizon(AFTER_THE_CLOSE)

        self.assertFalse(hz.current)
        self.assertEqual(hz.year, 2027)
        self.assertEqual(len(hz.weekends), 9)
        self.assertEqual(dict(hz.weekends)[2], 3, "Labor Day weekend trades three days")


class CoverTests(TestCase):

    def setUp(self):
        self.season = Season()

    def test_the_display_is_a_floor(self):
        """Two on the pegs is not cover: a bare peg is a lost sale."""
        self.season.stock("Teal", 2)

        _, colours, _ = self.season.plan(AFTER_THE_CLOSE)

        self.assertEqual(colours["Teal"].available, 0)
        self.assertEqual(colours["Teal"].covered_through, 0)

    def test_more_stock_covers_further(self):
        self.season.stock("Cabernet", 10)
        _, low, _ = self.season.plan(AFTER_THE_CLOSE)
        self.season.stock("Cabernet", 40)
        _, high, _ = self.season.plan(AFTER_THE_CLOSE)

        self.assertGreater(high["Cabernet"].covered_through,
                           low["Cabernet"].covered_through)

    def test_a_slow_colour_with_the_same_stock_lasts_longer(self):
        """25 of a one-a-weekend colour is not three weekends."""
        self.season.stock("Cabernet", 20)
        self.season.stock("Teal", 20)

        _, colours, _ = self.season.plan(AFTER_THE_CLOSE)

        self.assertGreater(colours["Teal"].covered_through,
                           colours["Cabernet"].covered_through)

    def test_asking_for_more_confidence_never_covers_further(self):
        self.season.stock("Cabernet", 26)

        _, at90, _ = self.season.plan(AFTER_THE_CLOSE, confidence=90)
        _, at95, _ = self.season.plan(AFTER_THE_CLOSE, confidence=95)

        self.assertLessEqual(at95["Cabernet"].covered_through,
                             at90["Cabernet"].covered_through)

    def test_growth_applies_to_next_season_only(self):
        _, flat, _ = self.season.plan(FRIDAY_BEFORE_WEEKEND_9, growth={self.season.raw.pk: 100})
        _, plain, _ = self.season.plan(FRIDAY_BEFORE_WEEKEND_9)

        self.assertAlmostEqual(flat["Cabernet"].expected[0], plain["Cabernet"].expected[0])

    def test_the_baths_it_asks_for_reach_the_target(self):
        _, colours, _ = self.season.plan(AFTER_THE_CLOSE, through=1)
        baths = colours["Cabernet"].baths
        self.assertGreater(baths, 0)

        self.season.stock("Cabernet", tiers.TIER_DISPLAY + baths * 4)
        _, after, _ = self.season.plan(AFTER_THE_CLOSE, through=1)

        self.assertGreaterEqual(after["Cabernet"].covered_through, 1)
        self.assertEqual(after["Cabernet"].baths, 0)

    def test_yarn_is_bought_in_whole_packs(self):
        _, _, row = self.season.plan(AFTER_THE_CLOSE)

        self.assertEqual(row.packs, -(-row.to_buy // nextseason.YARN_PACK))
        self.assertAlmostEqual(row.cost, row.packs * nextseason.YARN_PACK * 6.70)


class NewColoursUseTheirTierTests(TestCase):

    def test_a_colour_with_no_history_is_planned_like_its_tier(self):
        """Tier a new colour Medium and it is planned like a Medium."""
        season = Season()
        season.add("Fresh", "medium", 0)

        _, colours, _ = season.plan(AFTER_THE_CLOSE)

        self.assertTrue(colours["Fresh"].from_tier)
        self.assertAlmostEqual(colours["Fresh"].share, colours["Teal"].share)

    def test_the_shares_still_add_up_to_the_base(self):
        season = Season()
        season.add("Fresh", "high", 0)

        _, colours, _ = season.plan(AFTER_THE_CLOSE)

        self.assertAlmostEqual(sum(c.share for c in colours.values()), 1.0)

    def test_a_discontinued_colour_is_not_planned(self):
        season = Season()
        tiers.set_tier(season.colours["Teal"], tiers.DISCONTINUED)

        _, colours, _ = season.plan(AFTER_THE_CLOSE)

        self.assertNotIn("Teal", colours)


class PoissonTests(TestCase):

    def test_the_cdf(self):
        self.assertAlmostEqual(nextseason.poisson_cdf(0, 1.0), 0.36788, places=4)
        self.assertEqual(nextseason.poisson_cdf(-1, 3.0), 0.0)
        self.assertEqual(nextseason.poisson_cdf(5, 0), 1.0)


class PagesTests(TestCase):

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.season = Season()

    def test_the_index_lists_the_base(self):
        response = self.client.get(reverse("next_season_index"))

        self.assertContains(response, "Homespun")

    def test_the_base_lists_its_colours_and_keeps_the_settings(self):
        url = reverse("next_season_blank", args=[self.season.raw.pk])

        response = self.client.get(url, {"confidence": 95})

        self.assertContains(response, "Cabernet")
        self.assertEqual(response.context["confidence"], 95)

    def test_silk_is_not_offered(self):
        silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        raw = RawProduct.objects.create(name="Shawl", category=silk, price="15")

        response = self.client.get(reverse("next_season_blank", args=[raw.pk]))

        self.assertEqual(response.status_code, 404)
