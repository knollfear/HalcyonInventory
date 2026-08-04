"""
Tests for the public matching game.

Two things here are worth more than they look:

* the dedupe guarantee — an infinity and a rectangle from the same dye bath are
  the same recipe and must never both appear, or the board can't be won by sight;
* the CORS preflight — htmx's `HX-Request` header makes the browser preflight,
  so getting `Access-Control-Allow-Headers` wrong breaks every embed while the
  Django page itself keeps working perfectly.
"""
import random

from django.test import TestCase
from django.urls import reverse

from .colorutils import delta_e, hex_to_lab, hex_to_rgb, pick_color_cluster, recipe_color
from .models import (
    Dye,
    DyeBrand,
    FinishedProduct,
    FinishedProductImage,
    RawProduct,
    RawProductCategory,
    Recipe,
    RecipeDye,
)


def make_recipe(name, hexes=("#3355cc",), active=True):
    brand, _ = DyeBrand.objects.get_or_create(name="TestBrand")
    recipe = Recipe.objects.create(name=name, is_active=active)
    for i, hex_color in enumerate(hexes, start=1):
        dye, _ = Dye.objects.get_or_create(
            name=f"{name}-dye-{i}", brand=brand, defaults={"hex_color": hex_color}
        )
        dye.hex_color = hex_color
        dye.save()
        RecipeDye.objects.create(recipe=recipe, dye=dye, order=i)
    return recipe


def make_product(recipe, name, with_image=True, active=True):
    category, _ = RawProductCategory.objects.get_or_create(name="Silk")
    raw, _ = RawProduct.objects.get_or_create(
        name=f"raw-{name}", category=category, defaults={"price": "5.00"}
    )
    product = FinishedProduct.objects.create(
        name=name, raw_product=raw, recipe=recipe, price="30.00", is_active=active
    )
    if with_image:
        FinishedProductImage.objects.create(
            finished_product=product,
            image_url=f"https://example.test/{name}.jpg",
        )
    return product


class GamePoolTests(TestCase):
    def test_recipe_with_two_products_yields_one_pair(self):
        """The dedupe guarantee. An infinity and a rectangle from the same dye
        bath photograph near-identically; dealt as two pairs the board would be
        unwinnable by sight."""
        from .views import _deal_board, _recipe_game_pool

        recipe = make_recipe("Stormy Sea")
        make_product(recipe, "Stormy Sea Infinity")
        make_product(recipe, "Stormy Sea Rectangle")

        self.assertEqual(len(_recipe_game_pool()), 1)

        cards, pairs = _deal_board(4)
        self.assertEqual(pairs, 1)
        self.assertEqual(len(cards), 2)

    def test_recipes_without_images_are_excluded(self):
        from .views import _recipe_game_pool

        make_product(make_recipe("Photographed"), "A", with_image=True)
        make_product(make_recipe("Unphotographed"), "B", with_image=False)

        names = {r.name for r in _recipe_game_pool()}
        self.assertEqual(names, {"Photographed"})

    def test_inactive_recipes_and_products_are_excluded(self):
        from .views import _recipe_game_pool

        make_product(make_recipe("Retired", active=False), "C")
        make_product(make_recipe("Discontinued Product"), "D", active=False)
        make_product(make_recipe("Current"), "E")

        names = {r.name for r in _recipe_game_pool()}
        self.assertEqual(names, {"Current"})


class DealTests(TestCase):
    def setUp(self):
        for i in range(8):
            make_product(make_recipe(f"Recipe {i}"), f"Product {i}")

    def test_deal_returns_two_cards_per_pair(self):
        from .views import _deal_board

        cards, pairs = _deal_board(6)
        self.assertEqual(pairs, 6)
        self.assertEqual(len(cards), 12)

    def test_every_pair_id_appears_exactly_twice(self):
        from .views import _deal_board

        cards, _ = _deal_board(6)
        counts = {}
        for card in cards:
            counts[card["pair_id"]] = counts.get(card["pair_id"], 0) + 1
        self.assertTrue(all(n == 2 for n in counts.values()), counts)

    def test_each_pair_is_one_photo_and_one_name(self):
        from .views import _deal_board

        cards, _ = _deal_board(6)
        by_pair = {}
        for card in cards:
            by_pair.setdefault(card["pair_id"], []).append(card["kind"])
        for pair_id, kinds in by_pair.items():
            self.assertEqual(sorted(kinds), ["name", "photo"], f"pair {pair_id}")

    def test_small_pool_degrades_instead_of_erroring(self):
        from .views import _deal_board

        # Deactivating is how a pool actually shrinks — recipes can't be deleted
        # while products reference them (FinishedProduct.recipe is PROTECT).
        Recipe.objects.exclude(name="Recipe 0").update(is_active=False)
        cards, pairs = _deal_board(8)
        self.assertEqual(pairs, 1)
        self.assertEqual(len(cards), 2)

    def test_empty_pool_deals_nothing(self):
        from .views import _deal_board

        FinishedProductImage.objects.all().delete()
        cards, pairs = _deal_board(6)
        self.assertEqual((cards, pairs), ([], 0))


class BoardViewTests(TestCase):
    def setUp(self):
        for i in range(8):
            make_product(make_recipe(f"Recipe {i}"), f"Product {i}")

    def test_page_and_board_are_public(self):
        """The regression that would silently break every embed: an auth
        redirect on either endpoint."""
        self.assertEqual(self.client.get(reverse("game_page")).status_code, 200)
        self.assertEqual(self.client.get(reverse("game_board")).status_code, 200)

    def test_board_sends_cors_headers(self):
        response = self.client.get(reverse("game_board"))
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")

    def test_preflight_allows_the_htmx_request_header(self):
        """htmx sends `HX-Request`, which is a custom header, so the browser
        preflights. Allow-Origin alone is not enough and the failure is silent."""
        response = self.client.options(reverse("game_board"))
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertIn("HX-Request", response["Access-Control-Allow-Headers"])
        self.assertIn("GET", response["Access-Control-Allow-Methods"])

    def test_pairs_param_controls_board_size(self):
        response = self.client.get(reverse("game_board"), {"pairs": 4})
        self.assertEqual(response.context["pairs"], 4)
        self.assertEqual(len(response.context["cards"]), 8)

    def test_bogus_pairs_param_falls_back_to_default(self):
        for bad in ("99", "-1", "abc", ""):
            response = self.client.get(reverse("game_board"), {"pairs": bad})
            self.assertEqual(response.context["requested_pairs"], 6, bad)

    def test_image_urls_are_absolute(self):
        """Relative URLs would resolve against the *host* site when embedded,
        and 404 there while working fine on the Django page."""
        response = self.client.get(reverse("game_board"))
        for card in response.context["cards"]:
            if card["kind"] == "photo":
                self.assertRegex(card["image_url"], r"^https?://")
        self.assertRegex(response.context["board_url"], r"^https?://")

    def test_family_mode_is_off_by_default(self):
        response = self.client.get(reverse("game_board"))
        self.assertEqual(response.context["family_qs"], "")
        response = self.client.get(reverse("game_board"), {"family": "1"})
        self.assertEqual(response.context["family_qs"], "&family=1")


class ColorUtilsTests(TestCase):
    def test_hex_parsing_is_forgiving(self):
        self.assertEqual(hex_to_rgb("#1a2b3c"), (26, 43, 60))
        self.assertEqual(hex_to_rgb("1a2b3c"), (26, 43, 60))
        self.assertEqual(hex_to_rgb("#abc"), (170, 187, 204))
        for bad in ("", None, "nope", "#12"):
            self.assertIsNone(hex_to_rgb(bad))

    def test_delta_e_ranks_similar_closer_than_dissimilar(self):
        navy = hex_to_lab("#001a4d")
        midnight = hex_to_lab("#002060")
        orange = hex_to_lab("#e8720c")
        self.assertLess(delta_e(navy, midnight), delta_e(navy, orange))

    def test_recipe_color_is_weighted_by_ratio(self):
        """A recipe that is 90% blue should read as blue, not as a 50/50 muddle."""
        recipe = make_recipe("Mostly Blue", hexes=("#0000ff", "#ff0000"))
        rds = list(recipe.recipe_dyes.order_by("order"))
        rds[0].ratio = 90
        rds[0].save()
        rds[1].ratio = 10
        rds[1].save()

        weighted = recipe_color(Recipe.objects.get(pk=recipe.pk))
        pure_blue = hex_to_lab("#0000ff")
        even_split = hex_to_lab("#7f007f")
        self.assertLess(delta_e(weighted, pure_blue), delta_e(weighted, even_split))

    def test_recipe_with_no_dyes_has_no_color(self):
        recipe = make_recipe("Colorless", hexes=())
        self.assertIsNone(recipe_color(recipe))

    def test_cluster_picks_near_neighbours_not_far_ones(self):
        """The assertion that catches an inverted distance comparison — a bug
        that still yields a perfectly playable board, just a pointless one, so
        it would never be spotted by hand."""
        blues = ["#0a1f6b", "#12276f", "#1b2f78"]
        oranges = ["#e8720c", "#f07d18", "#d96a05"]
        for i, hex_color in enumerate(blues):
            make_recipe(f"Blue {i}", hexes=(hex_color,))
        for i, hex_color in enumerate(oranges):
            make_recipe(f"Orange {i}", hexes=(hex_color,))

        pool = list(Recipe.objects.prefetch_related("recipe_dyes__dye"))

        # Seeded so a pass/fail is reproducible, looped so it isn't a fluke of
        # whichever family the seed happened to land in.
        for seed in range(12):
            picked = pick_color_cluster(pool, 3, rng=random.Random(seed))
            families = {r.name.split()[0] for r in picked}
            self.assertEqual(len(picked), 3)
            self.assertEqual(len(families), 1, f"seed {seed} mixed families: {families}")

    def test_cluster_handles_pool_smaller_than_board(self):
        make_recipe("Only One")
        pool = list(Recipe.objects.prefetch_related("recipe_dyes__dye"))
        self.assertEqual(len(pick_color_cluster(pool, 6)), 1)
        self.assertEqual(pick_color_cluster([], 6), [])
