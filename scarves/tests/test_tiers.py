"""Tiers and the ladder they plan by — `scarves/tiers.py`.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .. import production, tiers
from ..models import FinishedProduct, RawProductCategory
from .helpers import make_bathable, make_recipe


def tiered(name, tier, on_hand=0, bath=5):
    """A yarn colorway at a tier, with the par that tier writes."""
    product = make_bathable(make_recipe(name), name, on_hand=on_hand, bath=bath)
    yarn, _ = RawProductCategory.objects.get_or_create(name="Yarn")
    product.raw_product.category = yarn
    product.raw_product.save(update_fields=["category"])
    if tier:
        tiers.set_tier(product, tier)
    product.refresh_from_db()
    return product


def suggested(limit, **kwargs):
    """`{product name: baths}` from a suggestion by tier."""
    counts = {}
    for bath in production.suggest(limit, order=production.ORDER_TIER,
                                   **kwargs).baths:
        counts[bath.product.name] = counts.get(bath.product.name, 0) + 1
    return counts


class SettingATierTests(TestCase):
    """A tier is a person's statement, and it writes the par it means."""

    def test_a_tier_writes_display_plus_its_baths(self):
        high = tiered("Cabernet", FinishedProduct.TIER_HIGH)
        low = tiered("Soft Tan", FinishedProduct.TIER_LOW)

        self.assertEqual(high.par, 2 + 3 * 5)
        self.assertEqual(low.par, 2 + 1 * 5)

    def test_discontinued_is_par_zero_and_keeps_the_tier(self):
        """Par 0 is the switch every page already honours; two switches for
        one meaning eventually disagree."""
        product = tiered("Violet", FinishedProduct.TIER_MEDIUM)

        tiers.set_tier(product, tiers.DISCONTINUED)
        product.refresh_from_db()

        self.assertEqual(product.par, 0)
        self.assertEqual(product.tier, FinishedProduct.TIER_MEDIUM)
        self.assertEqual(tiers.current(product), tiers.DISCONTINUED)

    def test_bringing_one_back_restores_its_par(self):
        product = tiered("Violet", FinishedProduct.TIER_MEDIUM)
        tiers.set_tier(product, tiers.DISCONTINUED)

        tiers.set_tier(product, FinishedProduct.TIER_MEDIUM)
        product.refresh_from_db()

        self.assertEqual(product.par, 12)

    def test_nothing_changes_when_nothing_changed(self):
        product = tiered("Teal", FinishedProduct.TIER_LOW)

        self.assertFalse(tiers.set_tier(product, FinishedProduct.TIER_LOW))
        self.assertFalse(tiers.set_tier(product, "nonsense"))


class TheLadderTests(TestCase):
    """Every High's first bath, every Medium's, every Low's; then the second
    round of High and Medium; then a third High; then well stocked."""

    def setUp(self):
        self.high = tiered("Cabernet", FinishedProduct.TIER_HIGH)
        self.medium = tiered("Teal", FinishedProduct.TIER_MEDIUM)
        self.low = tiered("Soft Tan", FinishedProduct.TIER_LOW)

    def test_the_first_round_is_one_of_each(self):
        self.assertEqual(
            suggested(3), {"Cabernet": 1, "Teal": 1, "Soft Tan": 1}
        )

    def test_the_second_round_is_high_and_medium(self):
        self.assertEqual(
            suggested(5), {"Cabernet": 2, "Teal": 2, "Soft Tan": 1}
        )

    def test_a_third_high_closes_the_ladder(self):
        self.assertEqual(
            suggested(6), {"Cabernet": 3, "Teal": 2, "Soft Tan": 1}
        )

    def test_a_climbed_ladder_asks_for_nothing_more(self):
        """However many baths the session has room for."""
        self.assertEqual(
            suggested(30), {"Cabernet": 3, "Teal": 2, "Soft Tan": 1}
        )

    def test_a_low_at_nothing_gets_one_bath_not_two(self):
        """Par 7 less a bath of 5 is still two short, and rounding that up
        is the second Low bath the ladder exists not to suggest."""
        self.assertEqual(suggested(30)["Soft Tan"], 1)

    def test_baths_of_one_colorway_print_together(self):
        """Chosen by rung, printed by pot."""
        names = [b.product.name for b in production.suggest(
            6, order=production.ORDER_TIER).baths]

        self.assertEqual(names, ["Cabernet"] * 3 + ["Teal"] * 2 + ["Soft Tan"])

    def test_stock_on_hand_counts_toward_the_rungs(self):
        """A rung's bath fires when its bag is empty. A High at 9 is past
        the display and into its second bath (7), so what it still wants is
        its third — after every Medium's second."""
        FinishedProduct.objects.filter(pk=self.high.pk).update(number_on_hand=9)

        self.assertEqual(suggested(3), {"Teal": 2, "Soft Tan": 1})
        self.assertEqual(
            suggested(4), {"Cabernet": 1, "Teal": 2, "Soft Tan": 1}
        )

    def test_a_low_with_one_behind_the_display_is_left_alone(self):
        """Its bag is not empty yet; at two it is, and it gets its bath."""
        FinishedProduct.objects.filter(pk=self.low.pk).update(number_on_hand=3)
        self.assertNotIn("Soft Tan", suggested(30))

        FinishedProduct.objects.filter(pk=self.low.pk).update(number_on_hand=2)
        self.assertEqual(suggested(30)["Soft Tan"], 1)

    def test_an_untiered_shortage_comes_after_the_lows_not_off_the_list(self):
        untiered = tiered("Mystery", "", on_hand=0)
        FinishedProduct.objects.filter(pk=untiered.pk).update(par=7)

        self.assertNotIn("Mystery", suggested(3))
        self.assertEqual(suggested(4)["Mystery"], 1)

    def test_discontinued_never_suggests(self):
        tiers.set_tier(self.low, tiers.DISCONTINUED)

        self.assertNotIn("Soft Tan", suggested(30))

    def test_each_row_says_its_rung(self):
        products = production.candidates(order=production.ORDER_TIER)
        labels = {p.name: p.ladder_label for p in products}

        self.assertEqual(labels["Cabernet"], "High · has 0 of 3 baths")
        self.assertEqual(labels["Soft Tan"], "Low · has 0 of 1 bath")


class ByTierOnTheNeededPageTests(TestCase):

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.url = reverse("production_needed")
        tiered("Soft Tan", FinishedProduct.TIER_LOW)
        tiered("Cabernet", FinishedProduct.TIER_HIGH)

    def test_the_pill_orders_by_the_ladder(self):
        groups = self.client.get(self.url, {"sort": "tier"}).context["groups"]

        self.assertEqual([g["recipe_name"] for g in groups][0], "Cabernet")

    def test_the_rung_prints_on_the_row(self):
        response = self.client.get(self.url, {"sort": "tier"})

        self.assertContains(response, "High · has 0 of 3 baths")

    def test_well_stocked_is_said(self):
        FinishedProduct.objects.update(number_on_hand=17)

        response = self.client.get(self.url, {"sort": "tier"})

        self.assertTrue(response.context["well_stocked"])
        self.assertContains(response, "Well stocked")

    def test_the_rung_prints_under_every_pill(self):
        response = self.client.get(self.url)

        self.assertContains(response, "Low · has 0 of 1 bath")


class TierPageTests(TestCase):

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.product = tiered("Cabernet", "")

    def test_the_picker_lists_yarn_bases(self):
        response = self.client.get(reverse("tiers_index"))

        self.assertContains(response, self.product.raw_product.name)

    def test_saving_a_tier_writes_tier_and_par(self):
        url = reverse("tiers_blank", args=[self.product.raw_product_id])

        self.client.post(url, {f"tier-{self.product.pk}": "high"})
        self.product.refresh_from_db()

        self.assertEqual(self.product.tier, "high")
        self.assertEqual(self.product.par, 17)

    def test_the_suggestion_is_printed_never_applied(self):
        url = reverse("tiers_blank", args=[self.product.raw_product_id])

        self.client.get(url)
        self.product.refresh_from_db()

        self.assertEqual(self.product.tier, "")

    def test_silk_is_not_offered(self):
        """The ladder's numbers were worked out on yarn, and a tier writes par."""
        silk = make_bathable(make_recipe("Golden Hour"), "Veil", bath=4)

        response = self.client.get(
            reverse("tiers_blank", args=[silk.raw_product_id])
        )

        self.assertEqual(response.status_code, 404)


class ByTierOnTheSheetTests(TestCase):

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.url = reverse("production_sheet_index")
        self.high = tiered("Cabernet", FinishedProduct.TIER_HIGH)

    def test_the_suggestion_says_its_rung(self):
        response = self.client.get(self.url, {"baths": 20, "order": "tier"})

        self.assertContains(response, "High · has 0 of 3 baths")

    def test_the_rung_survives_an_edit(self):
        """A label that vanished on the first edit would read as the app
        changing its mind."""
        response = self.client.get(
            self.url, {"items": f"{self.high.pk}:1"}
        )

        self.assertContains(response, "High · has 0 of 3 baths")

    def test_a_climbed_ladder_says_well_stocked(self):
        FinishedProduct.objects.update(number_on_hand=17)

        response = self.client.get(self.url, {"baths": 20, "order": "tier"})

        self.assertContains(response, "Well stocked")
