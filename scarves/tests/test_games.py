"""The public games — the matching board and the name quiz.
"""
import random
import re
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
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
    make_product,
    make_recipe,
)


class GamePoolTests(TestCase):
    def test_recipe_with_two_products_yields_one_pair(self):
        """The dedupe guarantee. An infinity and a rectangle from the same dye
        bath photograph near-identically; dealt as two pairs the board would be
        unwinnable by sight."""
        from ..views import _deal_board, _recipe_game_pool

        recipe = make_recipe("Stormy Sea")
        make_product(recipe, "Stormy Sea Infinity")
        make_product(recipe, "Stormy Sea Rectangle")

        self.assertEqual(len(_recipe_game_pool()), 1)

        cards, pairs = _deal_board(4)
        self.assertEqual(pairs, 1)
        self.assertEqual(len(cards), 2)

    def test_recipes_without_images_are_excluded(self):
        from ..views import _recipe_game_pool

        make_product(make_recipe("Photographed"), "A", with_image=True)
        make_product(make_recipe("Unphotographed"), "B", with_image=False)

        names = {r.name for r in _recipe_game_pool()}
        self.assertEqual(names, {"Photographed"})

    def test_inactive_recipes_and_products_are_excluded(self):
        from ..views import _recipe_game_pool

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
        from ..views import _deal_board

        cards, pairs = _deal_board(6)
        self.assertEqual(pairs, 6)
        self.assertEqual(len(cards), 12)

    def test_every_pair_id_appears_exactly_twice(self):
        from ..views import _deal_board

        cards, _ = _deal_board(6)
        counts = {}
        for card in cards:
            counts[card["pair_id"]] = counts.get(card["pair_id"], 0) + 1
        self.assertTrue(all(n == 2 for n in counts.values()), counts)

    def test_each_pair_is_one_photo_and_one_name(self):
        from ..views import _deal_board

        cards, _ = _deal_board(6)
        by_pair = {}
        for card in cards:
            by_pair.setdefault(card["pair_id"], []).append(card["kind"])
        for pair_id, kinds in by_pair.items():
            self.assertEqual(sorted(kinds), ["name", "photo"], f"pair {pair_id}")

    def test_small_pool_degrades_instead_of_erroring(self):
        from ..views import _deal_board

        # Deactivating is how a pool actually shrinks — recipes can't be deleted
        # while products reference them (FinishedProduct.recipe is PROTECT).
        Recipe.objects.exclude(name="Recipe 0").update(is_active=False)
        cards, pairs = _deal_board(8)
        self.assertEqual(pairs, 1)
        self.assertEqual(len(cards), 2)

    def test_empty_pool_deals_nothing(self):
        from ..views import _deal_board

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

    def test_urls_are_https_behind_a_tls_terminating_proxy(self):
        """Railway forwards over plain HTTP with X-Forwarded-Proto: https. If
        Django doesn't trust that header it emits http:// absolute URLs, which
        an https:// page blocks as mixed active content — breaking the buttons
        in the fragment. Invisible on localhost, fatal in prod."""
        response = self.client.get(
            reverse("game_board"), HTTP_X_FORWARDED_PROTO="https"
        )
        self.assertTrue(
            response.context["board_url"].startswith("https://"),
            response.context["board_url"],
        )
        for card in response.context["cards"]:
            if card["kind"] == "photo":
                self.assertFalse(card["image_url"].startswith("http://"))

    def test_family_mode_is_off_by_default(self):
        response = self.client.get(reverse("game_board"))
        self.assertEqual(response.context["family_qs"], "")
        response = self.client.get(reverse("game_board"), {"family": "1"})
        self.assertEqual(response.context["family_qs"], "&family=1")
class QuizPoolTests(TestCase):
    def test_one_product_per_recipe(self):
        """Same dedupe guarantee as the matching board, and it matters more
        here: two names from one dye bath under one photo is a question with two
        right answers."""
        from ..views import _quiz_product_pool

        recipe = make_recipe("Stormy Sea")
        make_product(recipe, "Stormy Sea Infinity")
        make_product(recipe, "Stormy Sea Rectangle")

        self.assertEqual(len(_quiz_product_pool()), 1)

    def test_unphotographed_and_inactive_are_excluded(self):
        from ..views import _quiz_product_pool

        make_product(make_recipe("A"), "Photographed")
        make_product(make_recipe("B"), "Unphotographed", with_image=False)
        make_product(make_recipe("C"), "Discontinued", active=False)
        make_product(make_recipe("D", active=False), "Retired Recipe")

        names = {p.name for p in _quiz_product_pool()}
        self.assertEqual(names, {"Photographed"})
class QuizDealTests(TestCase):
    def setUp(self):
        for i in range(12):
            make_product(make_recipe(f"Recipe {i}"), f"Product {i}")

    def test_deals_the_requested_number_of_questions(self):
        from ..views import _deal_quiz

        self.assertEqual(len(_deal_quiz(10)), 10)

    def test_every_question_has_exactly_one_right_answer(self):
        from ..views import _deal_quiz

        for question in _deal_quiz(10):
            correct = [o for o in question["options"] if o["correct"]]
            self.assertEqual(len(correct), 1, question["options"])
            self.assertEqual(correct[0]["name"], question["answer"])

    def test_options_are_four_distinct_names(self):
        """A repeated name would be a second right answer or a wasted slot."""
        from ..views import _deal_quiz, QUIZ_CHOICES

        for question in _deal_quiz(10):
            names = [o["name"] for o in question["options"]]
            self.assertEqual(len(names), QUIZ_CHOICES)
            self.assertEqual(len(set(names)), QUIZ_CHOICES, names)

    def test_no_product_is_asked_about_twice(self):
        from ..views import _deal_quiz

        answers = [q["answer"] for q in _deal_quiz(10)]
        self.assertEqual(len(set(answers)), len(answers))

    def test_the_answer_is_not_always_in_the_same_slot(self):
        """A stable position would make the quiz winnable without looking — and
        a forgotten shuffle looks fine in any single hand-played round."""
        from ..views import _deal_quiz

        slots = set()
        for question in _deal_quiz(12, rng=random.Random(0)):
            slots.add(next(
                i for i, o in enumerate(question["options"]) if o["correct"]
            ))
        self.assertGreater(len(slots), 1, slots)

    def test_distractors_never_share_the_answers_recipe(self):
        from ..views import _deal_quiz, _quiz_product_pool

        # Two products off one recipe: only one may ever reach the pool, so the
        # other can never turn up as a distractor beside it.
        recipe = make_recipe("Twinned")
        make_product(recipe, "Twinned Infinity")
        make_product(recipe, "Twinned Rectangle")

        pool_names = {p.name for p in _quiz_product_pool()}
        for question in _deal_quiz(12):
            for option in question["options"]:
                self.assertIn(option["name"], pool_names)
        self.assertLessEqual(
            len({"Twinned Infinity", "Twinned Rectangle"} & pool_names), 1
        )

    def test_small_pool_degrades_instead_of_erroring(self):
        from ..views import _deal_quiz

        FinishedProduct.objects.exclude(name="Product 0").update(is_active=False)
        questions = _deal_quiz(10)
        self.assertEqual(len(questions), 1)
        self.assertEqual(len(questions[0]["options"]), 1)

    def test_empty_pool_deals_nothing(self):
        from ..views import _deal_quiz

        FinishedProductImage.objects.all().delete()
        self.assertEqual(_deal_quiz(10), [])

    def test_family_mode_draws_distractors_from_one_color_family(self):
        """The assertion that catches an inverted distance comparison: wrong
        answers from across the color wheel still play fine, they're just too
        easy to be worth asking.

        Three dyes apiece, because that's what a real scarf is — a flow across
        several distinct colors, not one blended shade.
        """
        from ..views import _deal_quiz, _quiz_product_pool

        FinishedProduct.objects.update(is_active=False)
        blues = [
            ("#0a1f6b", "#1b3f9b", "#3f6fd0"),
            ("#12276f", "#2450a5", "#4c7cd8"),
            ("#1b2f78", "#2d5bb0", "#5a88e0"),
            ("#0e2270", "#1f47a0", "#4573cc"),
        ]
        oranges = [
            ("#e8720c", "#f59b3c", "#c25a05"),
            ("#f07d18", "#ffab4e", "#cc6408"),
            ("#d96a05", "#eb9333", "#b85502"),
            ("#e97a10", "#fba044", "#c85f06"),
        ]
        for i, hexes in enumerate(blues):
            make_product(make_recipe(f"Blue {i}", hexes=hexes), f"Blue Scarf {i}")
        for i, hexes in enumerate(oranges):
            make_product(make_recipe(f"Orange {i}", hexes=hexes), f"Orange Scarf {i}")

        pool = _quiz_product_pool()
        for seed in range(8):
            for question in _deal_quiz(4, pool=pool, rng=random.Random(seed), family=True):
                families = {o["name"].split()[0] for o in question["options"]}
                self.assertEqual(
                    len(families), 1, f"seed {seed} mixed families: {families}"
                )
class QuizViewTests(TestCase):
    def setUp(self):
        for i in range(12):
            make_product(make_recipe(f"Recipe {i}"), f"Product {i}")

    def test_page_and_board_are_public(self):
        """The regression that would silently break every embed: an auth
        redirect on either endpoint."""
        self.assertEqual(self.client.get(reverse("quiz_page")).status_code, 200)
        self.assertEqual(self.client.get(reverse("quiz_board")).status_code, 200)

    def test_board_sends_cors_headers(self):
        response = self.client.get(reverse("quiz_board"))
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")

    def test_preflight_allows_the_htmx_request_header(self):
        response = self.client.options(reverse("quiz_board"))
        self.assertEqual(response.status_code, 204)
        self.assertIn("HX-Request", response["Access-Control-Allow-Headers"])

    def test_questions_param_controls_length(self):
        response = self.client.get(reverse("quiz_board"), {"questions": 5})
        self.assertEqual(response.context["asked"], 5)

    def test_bogus_questions_param_falls_back_to_default(self):
        for bad in ("99", "-1", "abc", ""):
            response = self.client.get(reverse("quiz_board"), {"questions": bad})
            self.assertEqual(response.context["requested_questions"], 10, bad)

    def test_image_urls_are_absolute(self):
        """Relative URLs would resolve against the *host* site when embedded,
        and 404 there while working fine on the Django page."""
        response = self.client.get(reverse("quiz_board"))
        for question in response.context["questions"]:
            self.assertRegex(question["image_url"], r"^https?://")
        self.assertRegex(response.context["board_url"], r"^https?://")

    def test_urls_are_https_behind_a_tls_terminating_proxy(self):
        response = self.client.get(
            reverse("quiz_board"), HTTP_X_FORWARDED_PROTO="https"
        )
        self.assertTrue(response.context["board_url"].startswith("https://"))

    def test_thin_pool_shows_the_empty_state_instead_of_a_giveaway(self):
        from ..views import QUIZ_MIN_POOL

        keep = [f"Product {i}" for i in range(QUIZ_MIN_POOL - 1)]
        FinishedProduct.objects.exclude(name__in=keep).update(is_active=False)
        response = self.client.get(reverse("quiz_board"))
        self.assertTrue(response.context["too_few"])
        self.assertContains(response, "Not enough photographed products")

    def test_scoring_constants_reach_the_template(self):
        """The JS reads these from the render; a rename in views.py that missed
        the template would silently score every answer as NaN."""
        from ..views import QUIZ_POINTS_CORRECT, QUIZ_SPEED_BONUS, QUIZ_SPEED_WINDOW

        response = self.client.get(reverse("quiz_board"))
        body = response.content.decode()
        self.assertIn(f"var POINTS = {QUIZ_POINTS_CORRECT};", body)
        self.assertIn(f"var BONUS = {QUIZ_SPEED_BONUS};", body)
        self.assertIn(f"var WINDOW = {QUIZ_SPEED_WINDOW};", body)

    def test_alt_text_does_not_leak_the_answer(self):
        """The image's stored alt text is usually the product name, which would
        read the answer straight out to a screen reader."""
        FinishedProductImage.objects.update(alt_text="Product 3")
        response = self.client.get(reverse("quiz_board"))
        body = response.content.decode()
        for question in response.context["questions"]:
            self.assertNotIn(f'alt="{question["answer"]}"', body)

    def test_family_mode_is_off_by_default(self):
        response = self.client.get(reverse("quiz_board"))
        self.assertEqual(response.context["family_qs"], "")
        response = self.client.get(reverse("quiz_board"), {"family": "1"})
        self.assertEqual(response.context["family_qs"], "&family=1")
