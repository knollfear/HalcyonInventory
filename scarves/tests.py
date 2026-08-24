"""
Tests for the public games — the matching board and the name quiz.

Two things here are worth more than they look:

* the dedupe guarantee — an infinity and a rectangle from the same dye bath are
  the same recipe and must never both appear, or the board can't be won by sight;
* the CORS preflight — htmx's `HX-Request` header makes the browser preflight,
  so getting `Access-Control-Allow-Headers` wrong breaks every embed while the
  Django page itself keeps working perfectly.
"""
import base64
import csv
import hashlib
import hmac
import json
import os
import random
import re
import shutil
import tempfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import ProtectedError
from django.test import TestCase, override_settings
from django.urls import reverse

from . import closing, colorbands, crew, production, sheetscan, timesheets
from .colorutils import (
    delta_e,
    hex_to_lab,
    hex_to_rgb,
    nearest_by_color,
    palette_distance,
    pick_color_cluster,
    recipe_palette,
)
from .forms import HoursForm, LabelRunForm, QuickRecipeRowForm, RecipeDyesForm
from . import labels as labelmod
from . import views as viewsmod
from .models import (
    UNCATEGORIZED_BRAND,
    BoothPhoto,
    CatalogGroup,
    CloseRun,
    CloseRunRow,
    Dye,
    DyeBrand,
    Employee,
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
    TimeEntry,
    UnmatchedSale,
)
from .views import HOURS_PIN_ATTEMPT_LIMIT, IMAGE_MAX_EDGE


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
        from .views import _quiz_product_pool

        recipe = make_recipe("Stormy Sea")
        make_product(recipe, "Stormy Sea Infinity")
        make_product(recipe, "Stormy Sea Rectangle")

        self.assertEqual(len(_quiz_product_pool()), 1)

    def test_unphotographed_and_inactive_are_excluded(self):
        from .views import _quiz_product_pool

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
        from .views import _deal_quiz

        self.assertEqual(len(_deal_quiz(10)), 10)

    def test_every_question_has_exactly_one_right_answer(self):
        from .views import _deal_quiz

        for question in _deal_quiz(10):
            correct = [o for o in question["options"] if o["correct"]]
            self.assertEqual(len(correct), 1, question["options"])
            self.assertEqual(correct[0]["name"], question["answer"])

    def test_options_are_four_distinct_names(self):
        """A repeated name would be a second right answer or a wasted slot."""
        from .views import _deal_quiz, QUIZ_CHOICES

        for question in _deal_quiz(10):
            names = [o["name"] for o in question["options"]]
            self.assertEqual(len(names), QUIZ_CHOICES)
            self.assertEqual(len(set(names)), QUIZ_CHOICES, names)

    def test_no_product_is_asked_about_twice(self):
        from .views import _deal_quiz

        answers = [q["answer"] for q in _deal_quiz(10)]
        self.assertEqual(len(set(answers)), len(answers))

    def test_the_answer_is_not_always_in_the_same_slot(self):
        """A stable position would make the quiz winnable without looking — and
        a forgotten shuffle looks fine in any single hand-played round."""
        from .views import _deal_quiz

        slots = set()
        for question in _deal_quiz(12, rng=random.Random(0)):
            slots.add(next(
                i for i, o in enumerate(question["options"]) if o["correct"]
            ))
        self.assertGreater(len(slots), 1, slots)

    def test_distractors_never_share_the_answers_recipe(self):
        from .views import _deal_quiz, _quiz_product_pool

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
        from .views import _deal_quiz

        FinishedProduct.objects.exclude(name="Product 0").update(is_active=False)
        questions = _deal_quiz(10)
        self.assertEqual(len(questions), 1)
        self.assertEqual(len(questions[0]["options"]), 1)

    def test_empty_pool_deals_nothing(self):
        from .views import _deal_quiz

        FinishedProductImage.objects.all().delete()
        self.assertEqual(_deal_quiz(10), [])

    def test_family_mode_draws_distractors_from_one_color_family(self):
        """The assertion that catches an inverted distance comparison: wrong
        answers from across the color wheel still play fine, they're just too
        easy to be worth asking.

        Three dyes apiece, because that's what a real scarf is — a flow across
        several distinct colors, not one blended shade.
        """
        from .views import _deal_quiz, _quiz_product_pool

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
        from .views import QUIZ_MIN_POOL

        keep = [f"Product {i}" for i in range(QUIZ_MIN_POOL - 1)]
        FinishedProduct.objects.exclude(name__in=keep).update(is_active=False)
        response = self.client.get(reverse("quiz_board"))
        self.assertTrue(response.context["too_few"])
        self.assertContains(response, "Not enough photographed products")

    def test_scoring_constants_reach_the_template(self):
        """The JS reads these from the render; a rename in views.py that missed
        the template would silently score every answer as NaN."""
        from .views import QUIZ_POINTS_CORRECT, QUIZ_SPEED_BONUS, QUIZ_SPEED_WINDOW

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


class RecipeEditTests(TestCase):
    """Edit mode on the recipe showcase — filling in the dye backlog."""

    def setUp(self):
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)
        # A recipe that already has dyes (the copy source) and one without.
        self.source = make_recipe("blueeyes-mid-navy", hexes=("#2b5fa8", "#1a2340"))
        self.target = make_recipe("Agean Sea", hexes=())
        make_product(self.target, "Half Circle Veil - Agean Sea")

    def test_read_only_by_default(self):
        response = self.client.get(reverse("recipe_showcase"))
        self.assertFalse(response.context["edit_mode"])
        self.assertNotContains(response, "Copy dyes from")

    def test_edit_mode_renders_pickers(self):
        response = self.client.get(reverse("recipe_showcase"), {"edit": "true"})
        self.assertTrue(response.context["edit_mode"])
        self.assertContains(response, "Copy dyes from")

    def test_missing_filter_shows_only_dyeless_recipes(self):
        response = self.client.get(
            reverse("recipe_showcase"), {"edit": "true", "missing": "true"}
        )
        names = [row["recipe"].name for row in response.context["rows"]]
        self.assertIn("Agean Sea", names)
        self.assertNotIn("blueeyes-mid-navy", names)

    def test_copy_source_list_only_offers_recipes_that_have_dyes(self):
        """Offering a dye-less recipe as a copy source is a no-op that looks
        like a bug — and nothing else pins this query down."""
        response = self.client.get(reverse("recipe_row", args=[self.target.pk]))
        offered = {src["pk"] for src in response.context["dye_sources"]}
        self.assertIn(self.source.pk, offered)
        self.assertNotIn(self.target.pk, offered)
        for pk in offered:
            self.assertTrue(
                Recipe.objects.get(pk=pk).recipe_dyes.exists(),
                f"recipe {pk} offered as a copy source but has no dyes",
            )

    def test_recipe_is_not_offered_as_its_own_copy_source(self):
        response = self.client.get(reverse("recipe_row", args=[self.source.pk]))
        html = response.content.decode()
        select = html.split('id="src-')[1].split("</select>")[0]
        self.assertNotIn(f'value="{self.source.pk}"', select)

    def test_copy_prefills_without_saving(self):
        """The whole point of copy-then-adjust: the pickers populate but the
        database must be untouched until Save."""
        response = self.client.get(
            reverse("recipe_row", args=[self.target.pk]), {"source": self.source.pk}
        )
        form = response.context["form"]
        source_dye_ids = [rd.dye_id for rd in self.source.recipe_dyes.all()]
        self.assertEqual(form.initial["dye1"], source_dye_ids[0])
        self.assertEqual(form.initial["dye2"], source_dye_ids[1])
        self.assertEqual(self.target.recipe_dyes.count(), 0)

    def test_save_writes_dyes_in_slot_order(self):
        d1, d2 = [rd.dye for rd in self.source.recipe_dyes.all()]
        response = self.client.post(
            reverse("recipe_dyes_save", args=[self.target.pk]),
            {"dye1": d1.pk, "dye2": "", "dye3": d2.pk, "dye4": "", "dye5": ""},
        )
        self.assertEqual(response.status_code, 200)
        rows = list(self.target.recipe_dyes.order_by("order"))
        # Gaps collapse: slot 3 becomes order 2, so order stays 1..n contiguous.
        self.assertEqual([r.dye_id for r in rows], [d1.pk, d2.pk])
        self.assertEqual([r.order for r in rows], [1, 2])

    def test_save_replaces_rather_than_appends(self):
        d1, d2 = [rd.dye for rd in self.source.recipe_dyes.all()]
        self.client.post(
            reverse("recipe_dyes_save", args=[self.target.pk]), {"dye1": d1.pk}
        )
        self.client.post(
            reverse("recipe_dyes_save", args=[self.target.pk]), {"dye1": d2.pk}
        )
        self.assertEqual([rd.dye_id for rd in self.target.recipe_dyes.all()], [d2.pk])

    def test_saving_all_blank_clears_the_recipe(self):
        d1 = self.source.recipe_dyes.first().dye
        self.client.post(
            reverse("recipe_dyes_save", args=[self.target.pk]), {"dye1": d1.pk}
        )
        self.client.post(reverse("recipe_dyes_save", args=[self.target.pk]), {})
        self.assertEqual(self.target.recipe_dyes.count(), 0)

    def test_duplicate_dye_is_rejected_and_nothing_is_written(self):
        d1 = self.source.recipe_dyes.first().dye
        response = self.client.post(
            reverse("recipe_dyes_save", args=[self.target.pk]),
            {"dye1": d1.pk, "dye2": d1.pk},
        )
        self.assertFalse(response.context["form"].is_valid())
        self.assertEqual(self.target.recipe_dyes.count(), 0)

    def test_out_of_stock_dyes_stay_selectable(self):
        """Recording history must not depend on current stock — otherwise a dye
        going out of stock makes its recipes un-editable."""
        dye = self.source.recipe_dyes.first().dye
        Dye.objects.filter(pk=dye.pk).update(in_stock=False)
        form = RecipeDyesForm()
        self.assertIn(dye, form.fields["dye1"].queryset)

    def test_edit_endpoints_require_login(self):
        self.client.logout()
        for url in [
            reverse("recipe_showcase") + "?edit=true",
            reverse("recipe_row", args=[self.target.pk]),
        ]:
            self.assertEqual(self.client.get(url).status_code, 302, url)
        self.assertEqual(
            self.client.post(reverse("recipe_dyes_save", args=[self.target.pk])).status_code,
            302,
        )


class PageSmokeTests(TestCase):
    """Actually render every GET-able page as a logged-in user.

    A template syntax error took /scarves/images/upload/ down in production and
    nothing caught it: the only check was an anonymous request, which got a 302
    login redirect and never rendered the template at all. Checking status codes
    while logged out proves almost nothing.

    Every @page_meta view is included automatically, so new pages are covered
    the moment they're added.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("smoke", "s@example.test", "pw")
        self.client.force_login(self.user)
        # Enough data that pages have something to render.
        recipe = make_recipe("Smoke Test Recipe")
        make_product(recipe, "Smoke Test Product")

    def test_every_page_meta_view_renders(self):
        from scarves import urls as scarves_urls

        checked = []
        for entry in scarves_urls.urlpatterns:
            callback = getattr(entry, "callback", None)
            if callback is None or not getattr(callback, "page_meta", None):
                continue
            # Views needing URL params can't be reversed without them.
            if getattr(entry.pattern, "converters", None):
                continue
            url = reverse(entry.name)
            with self.subTest(url=url):
                # Any template or view error raises here rather than returning
                # a quiet 500, which is exactly what we want from a smoke test.
                response = self.client.get(url)
                self.assertLess(response.status_code, 500, url)
            checked.append(url)

        # Guard against the loop silently matching nothing.
        self.assertGreater(len(checked), 5, checked)
        self.assertIn(reverse("image_upload"), checked)

    def test_image_upload_renders_for_a_logged_in_user(self):
        """The specific regression: unbalanced {% endif %} in the template."""
        response = self.client.get(reverse("image_upload"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload Product Photos")


def make_jpeg(size=(4032, 3024), exif_orientation=None, color=(180, 90, 60)):
    """Bytes of a JPEG, optionally carrying an EXIF orientation tag."""
    from io import BytesIO
    from PIL import Image

    img = Image.new("RGB", size, color)
    buf = BytesIO()
    if exif_orientation is None:
        img.save(buf, "JPEG", quality=90)
    else:
        exif = img.getexif()
        exif[0x0112] = exif_orientation
        img.save(buf, "JPEG", quality=90, exif=exif)
    return buf.getvalue()


def image_size(data):
    from io import BytesIO
    from PIL import Image

    return Image.open(BytesIO(data)).size


class ShrinkImageTests(TestCase):
    """Downscaling on upload — what keeps a round of the game from costing 40MB."""

    def test_long_edge_is_capped_and_aspect_is_kept(self):
        from .views import IMAGE_MAX_EDGE, _shrink_image

        body, content_type = _shrink_image(make_jpeg((4032, 3024)))
        self.assertEqual(image_size(body), (IMAGE_MAX_EDGE, 900))
        self.assertEqual(content_type, "image/jpeg")

    def test_portrait_is_capped_on_its_own_long_edge(self):
        """The cap is on the longer side, not on width — a portrait photo must
        come back 900x1200, never squashed toward a square."""
        from .views import _shrink_image

        body, _ = _shrink_image(make_jpeg((3024, 4032)))
        self.assertEqual(image_size(body), (900, 1200))

    def test_result_is_dramatically_smaller(self):
        from .views import _shrink_image

        original = make_jpeg((4032, 3024))
        body, _ = _shrink_image(original)
        self.assertLess(len(body), len(original) / 4)

    def test_an_already_small_image_is_left_alone(self):
        """Returning None rather than re-encoding: a second pass over an
        in-bounds photo must not cost it any quality."""
        from .views import _shrink_image

        self.assertIsNone(_shrink_image(make_jpeg((1200, 900))))
        self.assertIsNone(_shrink_image(make_jpeg((800, 600))))

    def test_exif_rotation_is_baked_into_the_pixels(self):
        """Phones store rotation in EXIF instead of rotating the pixels, and
        re-encoding drops the tag. Without transposing first, every portrait
        photo would come out sideways in the games and the PDF — and it would
        look fine right up until it was resized.
        """
        from .views import _shrink_image

        # Orientation 6 = rotate 90°: a 4000x3000 file that displays as 3000x4000.
        body, _ = _shrink_image(make_jpeg((4000, 3000), exif_orientation=6))
        self.assertEqual(image_size(body), (900, 1200))

    def test_a_small_image_needing_rotation_is_still_rewritten(self):
        from .views import _shrink_image

        result = _shrink_image(make_jpeg((800, 600), exif_orientation=6))
        self.assertIsNotNone(result)
        self.assertEqual(image_size(result[0]), (600, 800))

    def test_heic_is_transcoded_to_jpeg(self):
        """iPhones shoot HEIC. Chrome and Firefox won't render it, and without
        pillow-heif PIL can't even open it — so the barcode decode and the
        downscale both fail and a 3MB unviewable file lands in the bucket under
        a .jpg name."""
        from io import BytesIO
        from PIL import Image
        from .views import _shrink_image

        buf = BytesIO()
        Image.new("RGB", (4032, 3024), (200, 80, 40)).save(buf, format="HEIF")

        body, content_type = _shrink_image(buf.getvalue())
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(Image.open(BytesIO(body)).format, "JPEG")
        self.assertEqual(image_size(body), (1200, 900))

    def test_a_small_heic_is_still_transcoded(self):
        """Size isn't the only reason to rewrite. Left alone, a small HEIC sits
        in the bucket under a .jpg key that no browser can open."""
        from io import BytesIO
        from PIL import Image
        from .views import _shrink_image

        buf = BytesIO()
        Image.new("RGB", (800, 600), (30, 60, 120)).save(buf, format="HEIF")

        result = _shrink_image(buf.getvalue())
        self.assertIsNotNone(result)
        self.assertEqual(result[1], "image/jpeg")
        self.assertEqual(image_size(result[0]), (800, 600))

    def test_heic_content_types_are_named_jpg(self):
        """The key's extension has to describe what ends up in the bucket after
        the transcode, not what the phone sent."""
        from .views import _CONTENT_TYPE_EXT

        self.assertEqual(_CONTENT_TYPE_EXT["image/heic"], ".jpg")
        self.assertEqual(_CONTENT_TYPE_EXT["image/heif"], ".jpg")

    def test_png_and_webp_keep_their_format(self):
        """The key's extension and the stored Content-Type were set at presign
        time; changing format here would leave both lying."""
        from io import BytesIO
        from PIL import Image
        from .views import _shrink_image

        for fmt, content_type in (("PNG", "image/png"), ("WEBP", "image/webp")):
            buf = BytesIO()
            Image.new("RGB", (2000, 1500), (20, 120, 90)).save(buf, fmt)
            body, got = _shrink_image(buf.getvalue())
            with self.subTest(fmt=fmt):
                self.assertEqual(got, content_type)
                self.assertEqual(Image.open(BytesIO(body)).format, fmt)
                self.assertEqual(image_size(body), (1200, 900))


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ProcessUploadResizeTests(TestCase):
    """The resize where it actually runs: the upload pipeline."""

    def setUp(self):
        self.user = User.objects.create_superuser("up", "u@example.test", "pw")
        self.client.force_login(self.user)

    def _upload(self, data=None):
        key = default_storage.save(
            "finished_products/test.jpg", ContentFile(data or make_jpeg())
        )
        return ProductImageUpload.objects.create(key=key)

    def test_upload_is_downscaled_in_place(self):
        from .views import IMAGE_MAX_EDGE

        upload = self._upload()
        before = default_storage.size(upload.key)

        response = self.client.post(reverse("process_upload", args=[upload.id]))
        self.assertEqual(response.status_code, 200)

        upload.refresh_from_db()
        with default_storage.open(upload.key, "rb") as fh:
            stored = fh.read()
        self.assertEqual(max(image_size(stored)), IMAGE_MAX_EDGE)
        self.assertLess(len(stored), before / 4)

    def test_the_photo_served_to_the_games_is_the_small_one(self):
        """The whole point: FinishedProductImage must end up pointing at the
        downscaled object, not at a leftover original."""
        from .views import IMAGE_MAX_EDGE

        product = make_product(make_recipe("Barcoded"), "Barcoded Scarf", with_image=False)
        product.sku = "SKU-RESIZE-1"
        product.save()

        upload = self._upload()
        # Stand in for a successful barcode decode, so the matched path runs.
        with mock.patch("pyzbar.pyzbar.decode") as decode:
            decode.return_value = [mock.Mock(data=b"SKU-RESIZE-1")]
            self.client.post(reverse("process_upload", args=[upload.id]))

        upload.refresh_from_db()
        self.assertEqual(upload.status, ProductImageUpload.STATUS_MATCHED)
        fpi = product.images.get()
        self.assertEqual(fpi.image.name, upload.key)
        with fpi.image.open("rb") as fh:
            self.assertEqual(max(image_size(fh.read())), IMAGE_MAX_EDGE)

    def test_barcode_is_decoded_before_the_downscale(self):
        """A Code128 label is a small part of the frame; decoding a 1200px copy
        is exactly what would stop it resolving. The decoder must see the
        original pixels."""
        from .views import IMAGE_MAX_EDGE

        upload = self._upload(make_jpeg((4032, 3024)))
        seen = {}
        with mock.patch("pyzbar.pyzbar.decode") as decode:
            decode.side_effect = lambda img: seen.setdefault("size", img.size) and []
            self.client.post(reverse("process_upload", args=[upload.id]))
        self.assertEqual(seen["size"], (4032, 3024))
        self.assertGreater(max(seen["size"]), IMAGE_MAX_EDGE)

    def test_a_photo_that_will_not_resize_is_kept_not_lost(self):
        upload = self._upload(b"this is not an image at all")
        response = self.client.post(reverse("process_upload", args=[upload.id]))
        self.assertEqual(response.status_code, 200)

        upload.refresh_from_db()
        self.assertTrue(default_storage.exists(upload.key))
        self.assertNotEqual(upload.error, "")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class StoredFileCleanupTests(TestCase):
    """Deleting a photo must take its stored file with it.

    Django stopped deleting files on model delete in 1.3, so without the
    post_delete receivers a removal in the admin drops the row and leaves the
    object in the bucket — paying storage forever with nothing left that knows
    its key. Invisible from the admin, which reports success either way.
    """

    def _image(self):
        product = make_product(make_recipe("Cleanup"), "Cleanup Scarf", with_image=False)
        fpi = FinishedProductImage(finished_product=product)
        fpi.image.save("cleanup.jpg", ContentFile(make_jpeg((40, 30))), save=True)
        return product, fpi

    def test_deleting_an_image_row_deletes_its_file(self):
        _, fpi = self._image()
        name = fpi.image.name
        self.assertTrue(default_storage.exists(name))

        fpi.delete()
        self.assertFalse(default_storage.exists(name))

    def test_bulk_delete_also_deletes_files(self):
        """The admin's checkbox action deletes through the queryset, which never
        calls Model.delete() — only the signal reaches it."""
        _, fpi = self._image()
        name = fpi.image.name

        FinishedProductImage.objects.all().delete()
        self.assertFalse(default_storage.exists(name))

    def test_deleting_the_product_cascades_to_its_files(self):
        product, fpi = self._image()
        name = fpi.image.name

        product.delete()
        self.assertFalse(default_storage.exists(name))

    def test_deleting_an_unfiled_upload_deletes_its_object(self):
        """An upload whose barcode never matched has no FinishedProductImage at
        all, so its tracking row is the only thing pointing at the object."""
        key = default_storage.save("finished_products/orphan.jpg",
                                   ContentFile(make_jpeg((40, 30))))
        upload = ProductImageUpload.objects.create(key=key)

        upload.delete()
        self.assertFalse(default_storage.exists(key))

    def test_a_row_with_no_file_deletes_cleanly(self):
        product = make_product(make_recipe("External"), "External Scarf", with_image=False)
        fpi = FinishedProductImage.objects.create(
            finished_product=product, image_url="https://example.test/x.jpg"
        )
        fpi.delete()  # must not raise
        self.assertEqual(FinishedProductImage.objects.count(), 0)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class PurgeCommandTests(TestCase):
    def setUp(self):
        product = make_product(make_recipe("Purge"), "Purge Scarf", with_image=False)
        self.fpi = FinishedProductImage(finished_product=product)
        self.fpi.image.save("purge.jpg", ContentFile(make_jpeg((40, 30))), save=True)
        self.upload = ProductImageUpload.objects.create(key=self.fpi.image.name)

    def test_dry_run_deletes_nothing(self):
        from django.core.management import call_command
        from io import StringIO

        out = StringIO()
        call_command("purge_product_images", stdout=out)
        self.assertIn("Dry run", out.getvalue())
        self.assertEqual(FinishedProductImage.objects.count(), 1)
        self.assertTrue(default_storage.exists(self.fpi.image.name))

    def test_yes_deletes_rows_and_files(self):
        from django.core.management import call_command
        from io import StringIO

        name = self.fpi.image.name
        call_command("purge_product_images", "--yes", stdout=StringIO())

        self.assertEqual(FinishedProductImage.objects.count(), 0)
        self.assertEqual(ProductImageUpload.objects.count(), 0)
        self.assertFalse(default_storage.exists(name))

    def test_keep_external_leaves_url_only_rows(self):
        from django.core.management import call_command
        from io import StringIO

        product = FinishedProduct.objects.first()
        FinishedProductImage.objects.create(
            finished_product=product, image_url="https://example.test/keep.jpg"
        )
        call_command("purge_product_images", "--yes", "--keep-external", stdout=StringIO())

        remaining = FinishedProductImage.objects.all()
        self.assertEqual([i.image_url for i in remaining], ["https://example.test/keep.jpg"])


class SiteMapTests(TestCase):
    """The /scarves/ directory. Its job is to be clickable."""

    def setUp(self):
        self.user = User.objects.create_superuser("map", "m@example.test", "pw")
        self.client.force_login(self.user)
        RawProductCategory.objects.get_or_create(name="Silk")
        RawProductCategory.objects.get_or_create(name="Yarn")

    def _items(self):
        response = self.client.get(reverse("index"))
        return [i for g in response.context["grouped"] for i in g["items"]]

    def test_the_site_map_requires_login(self):
        """It lists every internal page in the app. A decorator inserted in the
        wrong place once left this view unauthenticated, which nothing caught
        because the page still rendered perfectly."""
        self.client.logout()
        response = self.client.get(reverse("index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_the_games_are_linked(self):
        by_title = {i["title"]: i for i in self._items()}
        self.assertEqual(by_title["Name That Scarf"]["url"], reverse("quiz_page"))
        self.assertEqual(by_title["Scarf Matching Game"]["url"], reverse("game_page"))

    def test_there_are_no_dead_cards(self):
        """Every card on the map goes somewhere. A route needing URL params is
        listed via its picker page instead of as an unclickable card."""
        dead = [i["title"] for i in self._items() if not i["url"]]
        self.assertEqual(dead, [])

    def test_param_routes_are_hidden_in_favour_of_their_pickers(self):
        titles = {i["title"] for i in self._items()}
        self.assertNotIn("Raw Inventory (by category)", titles)
        self.assertNotIn("Reference Sheet PDF", titles)
        # ...but the pickers that reach them are listed.
        self.assertIn("Raw Inventory", titles)
        self.assertIn("Reference Sheets", titles)

    def test_every_rendered_link_actually_resolves(self):
        """A directory full of 404s would be worse than no directory."""
        response = self.client.get(reverse("index"))
        hrefs = set(re.findall(rb'href="(/scarves/[^"]*)"', response.content))
        self.assertGreater(len(hrefs), 8, hrefs)
        for href in hrefs:
            url = href.decode()
            with self.subTest(url=url):
                self.assertLess(self.client.get(url).status_code, 400, url)

    def test_the_route_path_is_clickable_when_there_is_somewhere_to_go(self):
        """A monospace URL is the most link-looking thing on the card, and it
        was the one part that wasn't a link — so that's what got clicked."""
        response = self.client.get(reverse("index")).content.decode()
        self.assertIn(f'<a class="route" href="{reverse("quiz_page")}"', response)


class PickerPageConventionTests(TestCase):
    """Every GET-able page taking a URL param must have a picker at its parent.

    The standing rule: if you add `foo/<int:some_id>/` as a page, add `foo/` as
    a page that lists the choices. Without it the only way in is to already know
    an id, and the site map is left with a card nobody can click — which is
    exactly how the two dead entries got there.

    Only applies to views carrying @page_meta. POST-only actions and HTMX
    fragments take params freely; they aren't pages and were never listed.
    """

    def _parent_route(self, pattern):
        """`raw-inventory/<int:category_id>/` -> `raw-inventory/`."""
        segments = str(pattern).strip("/").split("/")
        kept = []
        for segment in segments:
            if "<" in segment:
                break
            kept.append(segment)
        return "/".join(kept) + "/" if kept else ""

    def test_param_pages_have_a_picker_at_their_parent(self):
        from scarves import urls as scarves_urls

        routes = {
            str(e.pattern): e
            for e in scarves_urls.urlpatterns
            if getattr(e, "callback", None)
        }

        checked = []
        for route, entry in routes.items():
            meta = getattr(entry.callback, "page_meta", None)
            if not meta or not getattr(entry.pattern, "converters", None):
                continue

            parent = self._parent_route(entry.pattern)
            with self.subTest(route=route):
                self.assertIn(
                    parent, routes,
                    f"{route} takes URL params but there is no picker page at "
                    f"/{parent} — add one, or drop @page_meta if it isn't a page.",
                )
                parent_meta = getattr(routes[parent].callback, "page_meta", None)
                self.assertIsNotNone(
                    parent_meta,
                    f"/{parent} exists but has no @page_meta, so the site map "
                    f"still can't offer a way in to {route}.",
                )
                self.assertTrue(parent_meta.get("show_in_index", True), parent)
            checked.append(route)

        # Guard against the loop silently matching nothing.
        self.assertGreaterEqual(len(checked), 2, checked)


class RawInventoryIndexTests(TestCase):
    """The picker that replaced the dead <int:category_id> card."""

    def setUp(self):
        self.user = User.objects.create_superuser("inv", "i@example.test", "pw")
        self.client.force_login(self.user)
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")

    def _raw(self, name, on_hand, par, category=None, active=True):
        return RawProduct.objects.create(
            name=name, category=category or self.silk, price="5.00",
            number_on_hand=on_hand, par_level=par, is_active=active,
        )

    def test_it_requires_login(self):
        self.client.logout()
        self.assertEqual(
            self.client.get(reverse("raw_inventory_index")).status_code, 302
        )

    def test_it_links_to_each_category(self):
        self._raw("Habotai", 10, 20)
        response = self.client.get(reverse("raw_inventory_index"))
        self.assertContains(response, reverse("raw_inventory", args=[self.silk.pk]))
        self.assertContains(response, "Silk")

    def test_it_counts_products_stock_and_shortages(self):
        self._raw("Below", 3, 20)       # short
        self._raw("AlsoBelow", 0, 5)    # short
        self._raw("AtPar", 30, 10)      # fine
        self._raw("NoPar", 7, 0)        # par 0 = no target, never short

        category = self.client.get(reverse("raw_inventory_index")).context["categories"][0]
        self.assertEqual(category.product_count, 4)
        self.assertEqual(category.on_hand, 40)
        self.assertEqual(category.below_par, 2)

    def test_inactive_products_are_ignored(self):
        self._raw("Live", 5, 10)
        self._raw("Retired", 0, 99, active=False)

        category = self.client.get(reverse("raw_inventory_index")).context["categories"][0]
        self.assertEqual(category.product_count, 1)
        self.assertEqual(category.below_par, 1)

    def test_categories_with_no_active_products_are_left_out(self):
        """An empty category is a dead click, which is the thing this page
        exists to stop."""
        self._raw("Live", 5, 10)
        RawProductCategory.objects.create(name="Empty")
        RawProductCategory.objects.create(name="OnlyRetired")
        self._raw("Gone", 1, 2,
                  category=RawProductCategory.objects.get(name="OnlyRetired"),
                  active=False)

        names = [c.name for c in
                 self.client.get(reverse("raw_inventory_index")).context["categories"]]
        self.assertEqual(names, ["Silk"])

    def test_every_listed_category_actually_opens(self):
        self._raw("Habotai", 10, 20)
        self._raw("Wool", 4, 4, category=RawProductCategory.objects.create(name="Yarn"))

        response = self.client.get(reverse("raw_inventory_index"))
        # Built from the reversed picker URL rather than a literal path, so
        # moving the route (private/ vs public/) doesn't quietly turn this
        # into a test that finds nothing and asserts nothing.
        pattern = re.escape(reverse("raw_inventory_index")).encode() + rb"\d+/"
        hrefs = re.findall(rb'href="(' + pattern + rb')"', response.content)
        self.assertEqual(len(hrefs), 2)
        for href in hrefs:
            self.assertEqual(self.client.get(href.decode()).status_code, 200)


class BulkRecipeMatrixTests(TestCase):
    """The grid used to be reachable only by typing ?raw_ids= yourself; a bare
    visit was an error message and nothing else."""

    def setUp(self):
        # These used to run anonymously and pass, which was the tell: the view
        # had no @login_required and was creating recipes for whoever asked.
        self.client.force_login(
            User.objects.create_superuser("matrix", "m@example.test", "pw")
        )
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.yarn = RawProductCategory.objects.create(name="Yarn")
        self.url = reverse("bulk_recipe_matrix_entry")

    def _raw(self, name, category=None, active=True):
        return RawProduct.objects.create(
            name=name, category=category or self.silk, price="5.00",
            suggested_price="30.00", is_active=active,
        )

    def test_a_bare_visit_shows_the_picker(self):
        self._raw("Habotai")
        response = self.client.get(self.url)
        self.assertTrue(response.context["show_picker"])
        self.assertNotIn("columns", response.context)

    def test_raw_products_without_finished_products_are_still_offered(self):
        """The opposite of the bulk-inventory picker: this page is where a raw
        product's first finished products get made."""
        self._raw("Never Dyed")
        self._raw("Retired", active=False)

        names = [rp.name for rp in self.client.get(self.url).context["picker_products"]]
        self.assertEqual(names, ["Never Dyed"])

    def test_the_picker_is_grouped_by_category(self):
        self._raw("Wool", category=self.yarn)
        self._raw("Habotai")

        picked = self.client.get(self.url).context["picker_products"]
        self.assertEqual(
            [(rp.category.name, rp.name) for rp in picked],
            [("Silk", "Habotai"), ("Yarn", "Wool")],
        )

    def test_submitting_the_picker_with_nothing_ticked_is_refused(self):
        self._raw("Habotai")
        response = self.client.get(self.url, {"picked": "1"})
        self.assertTrue(response.context["show_picker"])
        self.assertContains(response, "Pick at least one raw product")

    def test_checkbox_style_raw_ids_build_the_grid(self):
        """The picker posts repeated raw_ids=, not a comma-joined string."""
        a, b = self._raw("Habotai"), self._raw("Wool", category=self.yarn)

        response = self.client.get(self.url, {"raw_ids": [a.id, b.id]})
        self.assertEqual(
            [rp.name for rp, _ in response.context["columns"]], ["Habotai", "Wool"]
        )
        self.assertEqual(response.context["raw_ids_param"], f"{a.id},{b.id}")

    def test_unknown_raw_ids_fall_back_to_the_picker(self):
        self._raw("Habotai")
        response = self.client.get(self.url, {"raw_ids": "9999"})
        self.assertTrue(response.context["show_picker"])

    def test_the_grid_saves_recipes_and_finished_products(self):
        raw = self._raw("Habotai")
        response = self.client.post(
            f"{self.url}?raw_ids={raw.id}",
            {
                "form-TOTAL_FORMS": "10", "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
                "form-0-recipe_name": "Stormy Sea",
                f"form-0-on_hand_{raw.id}": "4",
            },
        )
        self.assertEqual(response.status_code, 302)
        product = FinishedProduct.objects.get(recipe__name="Stormy Sea")
        self.assertEqual(product.name, "Habotai - Stormy Sea")
        self.assertEqual(product.number_on_hand, 4)

    def test_an_invalid_grid_redisplays_its_columns(self):
        """Re-rendering without them dropped every cell — including the ones
        holding the errors."""
        raw = self._raw("Habotai")
        response = self.client.post(
            f"{self.url}?raw_ids={raw.id}",
            {
                "form-TOTAL_FORMS": "10", "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
                "form-0-recipe_name": "Stormy Sea",
                f"form-0-on_hand_{raw.id}": "-3",   # min_value=0
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([rp.name for rp, _ in response.context["columns"]], ["Habotai"])
        self.assertContains(response, f"on_hand_{raw.id}")
        self.assertFalse(FinishedProduct.objects.exists())

    def test_it_links_back_to_the_site_map(self):
        raw = self._raw("Habotai")
        self.assertContains(self.client.get(self.url), reverse("index"))
        self.assertContains(
            self.client.get(self.url, {"raw_ids": str(raw.id)}), reverse("index")
        )


class ReferenceSheetIndexTests(TestCase):
    """The PDF picker. Same card layout as the raw-inventory picker, so it
    carries the same kind of counts."""

    def setUp(self):
        # Deliberately anonymous: these sheets are public, and this is the
        # test that would notice if they stopped being.
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.url = reverse("reference_sheet_index")

    def _item(self, name, category=None, active=True, recipe_active=True, photo=False):
        raw, _ = RawProduct.objects.get_or_create(
            name=f"raw-{name}", category=category or self.silk,
            defaults={"price": "5.00"},
        )
        product = FinishedProduct.objects.create(
            name=name, raw_product=raw,
            recipe=make_recipe(f"{name}-recipe", active=recipe_active),
            price="30.00", is_active=active,
        )
        if photo:
            # Only an uploaded file counts; the path needn't resolve for this.
            FinishedProductImage.objects.create(
                finished_product=product, image="finished_products/x.jpg"
            )
        return product

    def _categories(self):
        return self.client.get(self.url).context["categories"]

    def test_it_counts_pages_and_barcodes(self):
        """One PDF page per recipe, one barcode card per item."""
        shared = make_recipe("Stormy")
        for name in ("Scarf A", "Scarf B"):
            raw = RawProduct.objects.create(
                name=f"raw-{name}", category=self.silk, price="5.00"
            )
            FinishedProduct.objects.create(
                name=name, raw_product=raw, recipe=shared, price="30.00"
            )
        self._item("Sunset Scarf")

        category = self._categories()[0]
        self.assertEqual(category.recipe_count, 2)
        self.assertEqual(category.item_count, 3)

    def test_it_counts_items_that_would_print_without_a_photo(self):
        self._item("Has One", photo=True)
        self._item("Bare", photo=False)
        self._item("Also Bare", photo=False)

        self.assertEqual(self._categories()[0].photoless, 2)

    def test_an_external_image_url_is_not_a_photo(self):
        """The PDF embeds uploaded files only — a URL-only image still prints
        as a barcode with no picture."""
        product = self._item("Linked")
        FinishedProductImage.objects.create(
            finished_product=product, image_url="https://example.test/x.jpg"
        )
        self.assertEqual(self._categories()[0].photoless, 1)

    def test_categories_with_nothing_printable_are_left_out(self):
        self._item("Live")
        self._item("Retired", category=RawProductCategory.objects.create(name="Gone"),
                   active=False)
        self._item("StaleRecipe",
                   category=RawProductCategory.objects.create(name="Stale"),
                   recipe_active=False)
        RawProductCategory.objects.create(name="Empty")

        self.assertEqual([c.name for c in self._categories()], ["Silk"])

    def test_it_links_back_to_the_site_map(self):
        self.assertContains(self.client.get(self.url), reverse("index"))

    def test_every_listed_category_actually_builds_a_pdf(self):
        self._item("Habotai")
        self._item("Wool", category=RawProductCategory.objects.create(name="Yarn"))

        response = self.client.get(self.url)
        pattern = re.escape(reverse("reference_sheet_index")).encode() + rb"\d+/"
        hrefs = re.findall(rb'href="(' + pattern + rb')"', response.content)
        self.assertEqual(len(hrefs), 2)
        for href in hrefs:
            pdf = self.client.get(href.decode())
            self.assertEqual(pdf.status_code, 200)
            self.assertEqual(pdf["Content-Type"], "application/pdf")


class ByColorSheetTests(TestCase):
    """The same category, ordered by the rainbow.

    Two things carry the whole feature and neither is visible in a rendered
    PDF: a colorway claiming two bands has to print twice (or it's missing
    from one of the sections it's genuinely in), and an unconfirmed colorway
    must not print at all (a wrong section is silent — you look under orange,
    it isn't there, and nothing says it was filed under red).
    """

    def setUp(self):
        # Anonymous on purpose, like the by-name sheet: photos, names and
        # barcodes, the same things laid on the stall table.
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.style = RawProduct.objects.create(
            name="Infinity", category=self.silk, price="5.00"
        )
        self.url = reverse("reference_sheet_index")

    def _colorway(self, name, bands, confirmed=True, style=None, active=True):
        recipe = make_recipe(name)
        if confirmed:
            recipe.bands_confirmed_at = timezone.now()
        recipe.color_bands = bands
        recipe.save()
        return FinishedProduct.objects.create(
            name=f"{name} scarf",
            raw_product=style or self.style,
            recipe=recipe,
            price="30.00",
            is_active=active,
        )

    def _pages(self, category=None):
        from .views import _by_color_pages
        return _by_color_pages(category or self.silk)

    def test_a_two_band_colorway_prints_in_both_sections(self):
        self._colorway("Sunset", ["red", "blue"])

        self.assertEqual([slug for slug, _, _, _, _ in self._pages()], ["red", "blue"])

    def test_pages_come_out_in_rainbow_order_not_by_name(self):
        self._colorway("Aardvark", ["blue"])
        self._colorway("Zebra", ["red"])

        self.assertEqual(
            [(slug, recipe.name) for slug, _, _, recipe, _ in self._pages()],
            [("red", "Zebra"), ("blue", "Aardvark")],
        )

    def test_a_page_carries_every_style_dyed_in_that_colorway(self):
        """Same page contents as the by-name sheet — one colorway, and a
        barcode for each style in the category wearing it."""
        belt = RawProduct.objects.create(
            name="Sash Belt", category=self.silk, price="5.00"
        )
        product = self._colorway("Sunset", ["red"])
        FinishedProduct.objects.create(
            name="Sunset belt", raw_product=belt, recipe=product.recipe, price="20.00"
        )

        (_, _, _, _, items), = self._pages()
        self.assertEqual(
            sorted(fp.raw_product.name for fp in items), ["Infinity", "Sash Belt"]
        )

    def test_an_unconfirmed_colorway_does_not_print(self):
        """The bands are there, but nobody has checked them."""
        self._colorway("Guessed", ["red"], confirmed=False)
        self._colorway("Checked", ["red"])

        self.assertEqual(
            [recipe.name for _, _, _, recipe, _ in self._pages()], ["Checked"]
        )

    def test_a_confirmed_colorway_claiming_nothing_prints_nowhere(self):
        """An empty list is a decision, and the decision is 'no section'."""
        self._colorway("Undyed", [])

        self.assertEqual(self._pages(), [])

    def test_only_the_chosen_category_prints(self):
        yarn = RawProductCategory.objects.create(name="Yarn")
        skein = RawProduct.objects.create(name="Halo", category=yarn, price="5.00")
        self._colorway("Shared", ["red"])
        self._colorway("Elsewhere", ["red"], style=skein)

        self.assertEqual(
            [recipe.name for _, _, _, recipe, _ in self._pages()], ["Shared"]
        )

    def test_an_inactive_product_does_not_print(self):
        self._colorway("Retired", ["red"], active=False)

        self.assertEqual(self._pages(), [])

    def test_the_picker_counts_pages_not_colorways(self):
        self._colorway("Sunset", ["red", "orange"])
        self._colorway("Storm", ["blue"])

        category = self.client.get(self.url).context["categories"][0]
        self.assertEqual(category.band_pages, 3)
        self.assertEqual(category.recipe_count, 2)

    def test_the_picker_says_what_the_colour_sheet_will_leave_out(self):
        """Work still to do, versus a decision already taken."""
        self._colorway("Waiting", ["red"], confirmed=False)
        self._colorway("Deliberate", [])

        category = self.client.get(self.url).context["categories"][0]
        self.assertEqual(category.unclassified, 1)
        self.assertEqual(category.band_pages, 0)
        self.assertContains(self.client.get(self.url), "not classified yet")

    def test_the_picker_offers_both_sheets_for_one_category(self):
        self._colorway("Sunset", ["red", "blue"])

        response = self.client.get(self.url)
        self.assertContains(
            response, reverse("reference_sheet_pdf", args=[self.silk.pk])
        )
        self.assertContains(
            response, reverse("reference_sheet_by_color_pdf", args=[self.silk.pk])
        )

    def test_the_colour_sheet_is_not_offered_when_it_would_be_empty(self):
        self._colorway("Waiting", ["red"], confirmed=False)

        response = self.client.get(self.url)
        self.assertContains(
            response, reverse("reference_sheet_pdf", args=[self.silk.pk])
        )
        self.assertNotContains(
            response, reverse("reference_sheet_by_color_pdf", args=[self.silk.pk])
        )

    def test_the_pdf_has_one_page_per_band_claimed(self):
        self._colorway("Sunset", ["red", "blue"])
        self._colorway("Storm", ["blue"])

        pdf = self.client.get(
            reverse("reference_sheet_by_color_pdf", args=[self.silk.pk])
        )
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        # No PDF parser in the deps; the page count is in the trailer.
        self.assertEqual(pdf.content.count(b"/Type /Page\n"), 3)

    def test_a_category_with_nothing_confirmed_still_returns_a_pdf(self):
        """Reachable by URL even when the picker won't link it — it has to say
        why rather than 500."""
        self._colorway("Waiting", ["red"], confirmed=False)

        pdf = self.client.get(
            reverse("reference_sheet_by_color_pdf", args=[self.silk.pk])
        )
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")

    def test_the_tab_slot_is_the_band_not_the_page(self):
        """Fixed slots are what make a gap in a printed stack mean 'this
        category has nothing in green' rather than 'the tabs shifted up'."""
        from .views import _band_tab_painter

        painted = []

        class FakeCanvas:
            def __init__(self, page):
                self._page = page

            def getPageNumber(self):
                return self._page

            def saveState(self): pass
            def restoreState(self): pass
            def setFillColor(self, *a): pass
            def setFont(self, *a): pass
            def translate(self, x, y): painted.append(y)
            def rotate(self, *a): pass
            def drawCentredString(self, *a): pass
            def rect(self, *a, **k): pass

        class FakeDoc:
            pagesize = (612, 792)
            topMargin = bottomMargin = leftMargin = rightMargin = 36

        # Two pages, black then red: black is the last slot, red the first, so
        # the second page's tab must sit *above* the first page's.
        paint = _band_tab_painter([
            ("black", "Black", colorbands.BAND_COLORS["black"]),
            ("red", "Red", colorbands.BAND_COLORS["red"]),
        ])
        paint(FakeCanvas(1), FakeDoc())
        paint(FakeCanvas(2), FakeDoc())

        self.assertGreater(painted[1], painted[0])


class BulkInventoryPickerTests(TestCase):
    """A raw product with no active finished products has no rows to edit, so
    offering it on the picker only leads to an empty form."""

    def setUp(self):
        self.user = User.objects.create_superuser("bulk", "b@example.test", "pw")
        self.client.force_login(self.user)
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.url = reverse("bulk_inventory_update")

    def _raw(self, name, active=True):
        return RawProduct.objects.create(
            name=name, category=self.silk, price="5.00", is_active=active,
        )

    def _finished(self, raw, name, active=True):
        return FinishedProduct.objects.create(
            name=name, raw_product=raw, recipe=make_recipe(f"{name}-recipe"),
            price="30.00", is_active=active,
        )

    def _picker_names(self, response):
        return [rp.name for rp in response.context["picker_products"]]

    def test_only_raw_products_with_finished_products_are_listed(self):
        stocked = self._raw("Habotai")
        self._finished(stocked, "Stormy Habotai")
        self._raw("Never Dyed")
        retired_only = self._raw("Retired Line")
        self._finished(retired_only, "Old Scarf", active=False)

        self.assertEqual(self._picker_names(self.client.get(self.url)), ["Habotai"])

    def test_a_raw_product_appears_once_however_many_finished_products(self):
        stocked = self._raw("Habotai")
        self._finished(stocked, "Stormy Habotai")
        self._finished(stocked, "Sunset Habotai")

        self.assertEqual(self._picker_names(self.client.get(self.url)), ["Habotai"])

    def test_an_empty_raw_id_is_skipped_and_reported(self):
        stocked = self._raw("Habotai")
        self._finished(stocked, "Stormy Habotai")
        empty = self._raw("Never Dyed")

        response = self.client.get(self.url, {"raw_ids": f"{stocked.id},{empty.id}"})
        self.assertEqual(
            [g["raw_product"].name for g in response.context["groups"]], ["Habotai"]
        )
        self.assertContains(response, "Never Dyed")
        # The save-redirect drops it, so the notice doesn't come back every save.
        self.assertEqual(response.context["raw_ids_param"], str(stocked.id))

    def test_all_empty_raw_ids_fall_back_to_the_picker(self):
        empty = self._raw("Never Dyed")

        response = self.client.get(self.url, {"raw_ids": str(empty.id)})
        self.assertTrue(response.context["show_picker"])
        self.assertNotIn("groups", response.context)

    def test_both_views_link_back_to_the_site_map(self):
        stocked = self._raw("Habotai")
        self._finished(stocked, "Stormy Habotai")

        self.assertContains(self.client.get(self.url), reverse("index"))
        self.assertContains(
            self.client.get(self.url, {"raw_ids": str(stocked.id)}), reverse("index")
        )

    def test_it_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)


class TemplateHygieneTests(TestCase):
    def test_no_multiline_hash_comments(self):
        """`{# #}` is single-line only — spread it over two lines and Django
        renders the whole thing as visible text.

        It fails silently and looks exactly like a comment in the editor, which
        is how it reached the public game page and the recipe showcase before
        anyone noticed. `{% comment %}` is the multi-line form.
        """
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent / "templates"
        pattern = re.compile(r"\{#(?:(?!#\}).)*?\n(?:(?!#\}).)*?#\}", re.S)

        offenders = []
        for path in sorted(root.rglob("*.html")):
            text = path.read_text()
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(root)}:{line}")

        self.assertEqual(offenders, [], "use {% comment %} for multi-line comments")


class RecipeDetailTests(TestCase):
    """The recipe page. Its job is to answer "how did this get here?" — so
    the arithmetic over the inventory log is what's worth pinning."""

    def setUp(self):
        self.user = User.objects.create_superuser("detail", "d@example.test", "pw")
        self.client.force_login(self.user)
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_product(self.recipe, "Stormy Infinity")
        self.url = reverse("recipe_detail", args=[self.recipe.pk])

    def _log(self, log_type, quantity, product=None, **kwargs):
        return InventoryLog.objects.create(
            finished_product=product or self.product,
            log_type=log_type, quantity=quantity, **kwargs
        )

    def test_it_requires_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_it_shows_the_recipe_its_dyes_and_its_products(self):
        brand = DyeBrand.objects.create(name="Jacquard")
        dye = Dye.objects.create(name="Teal", brand=brand, hex_color="#008080")
        RecipeDye.objects.create(recipe=self.recipe, dye=dye, order=1)

        response = self.client.get(self.url)
        self.assertContains(response, "Stormy Sea")
        self.assertContains(response, "Teal")
        self.assertContains(response, "#008080")
        self.assertContains(response, "Stormy Infinity")

    def test_totals_separate_production_from_sales(self):
        self._log(InventoryLog.PRODUCTION, 12)
        self._log(InventoryLog.PRODUCTION, 6)
        self._log(InventoryLog.SALE, -5)          # sales are stored negative
        self._log(InventoryLog.ADJUSTMENT, -1)

        context = self.client.get(self.url).context
        self.assertEqual(context["produced"], 18)
        # Shown as a positive count even though the rows are negative.
        self.assertEqual(context["sold"], 5)
        self.assertEqual(context["adjusted"], -1)

    def test_it_covers_every_product_of_the_recipe_not_just_one(self):
        second = make_product(self.recipe, "Stormy Rectangle")
        self._log(InventoryLog.PRODUCTION, 4)
        self._log(InventoryLog.PRODUCTION, 7, product=second)

        context = self.client.get(self.url).context
        self.assertEqual(context["produced"], 11)
        self.assertEqual(len(context["logs"]), 2)

    def test_another_recipes_history_stays_out_of_it(self):
        other = make_recipe("Sunset")
        other_product = make_product(other, "Sunset Infinity")
        InventoryLog.objects.create(
            finished_product=other_product,
            log_type=InventoryLog.PRODUCTION, quantity=99,
        )
        self._log(InventoryLog.PRODUCTION, 3)

        context = self.client.get(self.url).context
        self.assertEqual(context["produced"], 3)
        self.assertNotContains(self.client.get(self.url), "Sunset Infinity")

    def test_history_is_newest_first(self):
        old = self._log(InventoryLog.PRODUCTION, 1)
        new = self._log(InventoryLog.SALE, -1)
        InventoryLog.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=3)
        )
        logs = self.client.get(self.url).context["logs"]
        self.assertEqual([l.pk for l in logs], [new.pk, old.pk])

    def test_a_long_history_is_capped_but_the_totals_are_not(self):
        """The cap is display-only. Totals summing just the visible slice
        would understate a busy recipe exactly when it matters most."""
        from scarves.views import RECIPE_LOG_LIMIT

        InventoryLog.objects.bulk_create([
            InventoryLog(
                finished_product=self.product,
                log_type=InventoryLog.PRODUCTION, quantity=1,
            )
            for _ in range(RECIPE_LOG_LIMIT + 25)
        ])

        context = self.client.get(self.url).context
        self.assertEqual(len(context["logs"]), RECIPE_LOG_LIMIT)
        self.assertTrue(context["truncated"])
        self.assertEqual(context["produced"], RECIPE_LOG_LIMIT + 25)
        self.assertEqual(context["log_count"], RECIPE_LOG_LIMIT + 25)

    def test_a_short_history_is_not_flagged_as_truncated(self):
        self._log(InventoryLog.PRODUCTION, 1)
        self.assertFalse(self.client.get(self.url).context["truncated"])

    def test_a_recipe_with_nothing_yet_still_renders(self):
        bare = make_recipe("Untried", hexes=())  # no dyes, no products
        response = self.client.get(reverse("recipe_detail", args=[bare.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No dyes recorded yet")
        self.assertContains(response, "Nothing is made from this recipe yet")

    def test_retired_products_are_shown_rather_than_hidden(self):
        """Their history is still part of how the recipe got where it is."""
        retired = make_product(self.recipe, "Stormy Scarf", active=False)
        retired.is_active = False
        retired.save()
        response = self.client.get(self.url)
        self.assertContains(response, "Stormy Scarf")
        self.assertContains(response, "retired")

    def test_the_showcase_links_to_every_recipe(self):
        """The showcase is this page's picker; without the link there's no
        way in but guessing an id."""
        response = self.client.get(reverse("recipe_showcase"))
        self.assertContains(response, reverse("recipe_detail", args=[self.recipe.pk]))

    def test_production_needed_links_its_recipe_headings_here(self):
        """Seeing a shortage should be one click from its whole history."""
        self.product.par = 10
        self.product.number_on_hand = 0
        self.product.save()

        response = self.client.get(reverse("production_needed"))
        self.assertContains(response, self.recipe.name)
        self.assertContains(response, reverse("recipe_detail", args=[self.recipe.pk]))

    def test_an_unknown_recipe_is_a_404_not_a_redirect_to_the_home_page(self):
        """The catch-all only fires when no route matched; a real route with
        a bad id must still say so."""
        response = self.client.get(reverse("recipe_detail", args=[999999]))
        self.assertEqual(response.status_code, 404)


class RecordRecipeProductionTests(TestCase):
    """Batch production entry from the recipe page.

    A dye session is one colourway across two or three bases, entered
    afterwards from notes — so the form takes bath counts and writes the
    whole session at once.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("prod", "p@example.test", "pw")
        self.client.force_login(self.user)
        self.recipe = make_recipe("Sage")
        self.category, _ = RawProductCategory.objects.get_or_create(name="Yarn")
        self.url = reverse("record_recipe_production", args=[self.recipe.pk])

    def _base(self, name, per_bath=5, on_hand=100):
        return RawProduct.objects.create(
            name=name, category=self.category, price="5.00",
            number_per_dye_bath=per_bath, number_on_hand=on_hand,
        )

    def _product(self, base, name, on_hand=0, par=8):
        return FinishedProduct.objects.create(
            name=name, raw_product=base, recipe=self.recipe,
            price="30.00", number_on_hand=on_hand, par=par, is_active=True,
        )

    def test_it_requires_login(self):
        self.client.logout()
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_it_refuses_a_get(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_one_bath_adds_the_batch_size_and_draws_down_raw_stock(self):
        base = self._base("Heavenly - Angel", per_bath=5, on_hand=40)
        product = self._product(base, "Heavenly - Angel - Sage")

        self.client.post(self.url, {f"baths_{product.pk}": "1"})

        product.refresh_from_db()
        base.refresh_from_db()
        self.assertEqual(product.number_on_hand, 5)
        self.assertEqual(base.number_on_hand, 35)

    def test_two_baths_are_one_entry_not_two(self):
        """Her real sessions include two baths of one product. That's a
        deliberate quantity, and reads better as a single row."""
        base = self._base("Heavenly - Angel", per_bath=5)
        product = self._product(base, "Heavenly - Angel - Grey")

        self.client.post(self.url, {f"baths_{product.pk}": "2"})

        logs = InventoryLog.objects.filter(finished_product=product)
        self.assertEqual(logs.count(), 1)
        self.assertEqual(logs.first().quantity, 10)
        self.assertEqual(logs.first().log_type, InventoryLog.PRODUCTION)
        self.assertIn("2 dye baths", logs.first().notes)

    def test_a_whole_colourway_across_bases_is_one_submit(self):
        heavenly = self._base("Heavenly - Angel", per_bath=5)
        homespun = self._base("Homespun - Single & Stunning", per_bath=4)
        noble = self._base("Noble - Diamond Extra", per_bath=5)
        a = self._product(heavenly, "Heavenly - Angel - Sage")
        b = self._product(homespun, "Homespun - Single & Stunning - Sage")
        c = self._product(noble, "Noble - Diamond Extra - Sage")

        self.client.post(self.url, {
            f"baths_{a.pk}": "1", f"baths_{b.pk}": "1", f"baths_{c.pk}": "1",
        })

        for product, expected in ((a, 5), (b, 4), (c, 5)):
            product.refresh_from_db()
            self.assertEqual(product.number_on_hand, expected, product.name)
        self.assertEqual(InventoryLog.objects.count(), 3)

    def test_two_products_sharing_one_base_both_draw_it_down(self):
        """The bug this guards: reading the raw product into two stale copies
        and saving both leaves only one deduction applied."""
        shared = self._base("Heavenly - Angel", per_bath=5, on_hand=40)
        a = self._product(shared, "Heavenly - Angel - Sage")
        b = self._product(shared, "Heavenly - Angel - Grey")

        self.client.post(self.url, {f"baths_{a.pk}": "1", f"baths_{b.pk}": "2"})

        shared.refresh_from_db()
        self.assertEqual(shared.number_on_hand, 40 - 5 - 10)

    def test_blank_and_zero_rows_are_left_alone(self):
        base = self._base("Heavenly - Angel")
        touched = self._product(base, "Heavenly - Angel - Sage")
        skipped = self._product(base, "Heavenly - Angel - Grey", on_hand=3)

        self.client.post(self.url, {
            f"baths_{touched.pk}": "1",
            f"baths_{skipped.pk}": "",
        })

        skipped.refresh_from_db()
        self.assertEqual(skipped.number_on_hand, 3)
        self.assertFalse(
            InventoryLog.objects.filter(finished_product=skipped).exists()
        )

    def test_an_empty_submit_records_nothing_and_says_so(self):
        base = self._base("Heavenly - Angel")
        self._product(base, "Heavenly - Angel - Sage")

        response = self.client.post(self.url, {}, follow=True)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.assertContains(response, "nothing was recorded")

    def test_a_non_numeric_entry_records_nothing_at_all(self):
        """Half a dye session in the log is worse than none of it."""
        base = self._base("Heavenly - Angel", on_hand=40)
        good = self._product(base, "Heavenly - Angel - Sage")
        bad = self._product(base, "Heavenly - Angel - Grey")

        response = self.client.post(self.url, {
            f"baths_{good.pk}": "1", f"baths_{bad.pk}": "two",
        }, follow=True)

        good.refresh_from_db()
        base.refresh_from_db()
        self.assertEqual(good.number_on_hand, 0)
        self.assertEqual(base.number_on_hand, 40)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.assertContains(response, "isn&#x27;t a number of baths")

    def test_raw_stock_does_not_go_negative(self):
        base = self._base("Heavenly - Angel", per_bath=5, on_hand=3)
        product = self._product(base, "Heavenly - Angel - Sage")

        self.client.post(self.url, {f"baths_{product.pk}": "2"})

        base.refresh_from_db()
        self.assertEqual(base.number_on_hand, 0)

    def test_a_retired_product_is_not_recordable(self):
        base = self._base("Heavenly - Angel")
        retired = self._product(base, "Heavenly - Angel - Old")
        retired.is_active = False
        retired.save()

        self.client.post(self.url, {f"baths_{retired.pk}": "1"})

        retired.refresh_from_db()
        self.assertEqual(retired.number_on_hand, 0)

    def test_the_recipe_page_offers_the_form(self):
        base = self._base("Heavenly - Angel", per_bath=5)
        product = self._product(base, "Heavenly - Angel - Sage")

        response = self.client.get(reverse("recipe_detail", args=[self.recipe.pk]))
        self.assertContains(response, self.url)
        self.assertContains(response, f'name="baths_{product.pk}"')
        self.assertContains(response, "Record production")

    def test_a_back_dated_session_records_history_without_moving_stock(self):
        """Digitising old paper records must not inflate today's inventory —
        that yarn was counted or sold years ago."""
        base = self._base("Heavenly - Angel", per_bath=5, on_hand=40)
        product = self._product(base, "Heavenly - Angel - Sage", on_hand=2)

        self.client.post(self.url, {
            f"baths_{product.pk}": "2",
            "dyed_on": "2024-06-15",
        })

        product.refresh_from_db()
        base.refresh_from_db()
        self.assertEqual(product.number_on_hand, 2)   # untouched
        self.assertEqual(base.number_on_hand, 40)     # untouched

        log = InventoryLog.objects.get()
        self.assertEqual(log.quantity, 10)
        self.assertEqual(timezone.localtime(log.created_at).date().isoformat(),
                         "2024-06-15")
        self.assertIn("stock left unchanged", log.notes)

    def test_todays_date_behaves_like_a_normal_entry(self):
        """Only the *past* is history. Typing today's date explicitly should
        still move stock, or the everyday path would depend on a blank box."""
        base = self._base("Heavenly - Angel", per_bath=5, on_hand=40)
        product = self._product(base, "Heavenly - Angel - Sage")

        self.client.post(self.url, {
            f"baths_{product.pk}": "1",
            "dyed_on": timezone.localdate().isoformat(),
        })

        product.refresh_from_db()
        base.refresh_from_db()
        self.assertEqual(product.number_on_hand, 5)
        self.assertEqual(base.number_on_hand, 35)

    def test_a_future_date_is_refused(self):
        base = self._base("Heavenly - Angel")
        product = self._product(base, "Heavenly - Angel - Sage")
        ahead = (timezone.localdate() + timedelta(days=1)).isoformat()

        response = self.client.post(self.url, {
            f"baths_{product.pk}": "1", "dyed_on": ahead,
        }, follow=True)

        self.assertEqual(InventoryLog.objects.count(), 0)
        self.assertContains(response, "in the future")

    def test_an_unparseable_date_records_nothing(self):
        base = self._base("Heavenly - Angel")
        product = self._product(base, "Heavenly - Angel - Sage")

        response = self.client.post(self.url, {
            f"baths_{product.pk}": "1", "dyed_on": "last summer",
        }, follow=True)

        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 0)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.assertContains(response, "isn&#x27;t a date I understand")

    def test_back_dated_entries_land_in_the_right_season_not_the_next_day(self):
        """A date carries no time; midnight is the value most likely to slide
        into the adjacent day once a timezone is applied."""
        base = self._base("Heavenly - Angel")
        product = self._product(base, "Heavenly - Angel - Sage")

        self.client.post(self.url, {
            f"baths_{product.pk}": "1", "dyed_on": "2025-01-01",
        })

        local = timezone.localtime(InventoryLog.objects.get().created_at)
        self.assertEqual(local.date().isoformat(), "2025-01-01")
        self.assertEqual(local.hour, 12)

    def test_the_form_offers_back_dating_without_putting_it_in_the_way(self):
        base = self._base("Heavenly - Angel")
        self._product(base, "Heavenly - Angel - Sage")

        response = self.client.get(reverse("recipe_detail", args=[self.recipe.pk]))
        self.assertContains(response, 'name="dyed_on"')
        # Behind a disclosure, so the everyday path is a number and a button.
        self.assertContains(response, "<details")
        self.assertContains(response, f'max="{timezone.localdate():%Y-%m-%d}"')

    def test_the_new_stock_shows_up_in_the_recipes_own_history(self):
        """End to end: record a session, then read it back off the page."""
        base = self._base("Heavenly - Angel", per_bath=5)
        product = self._product(base, "Heavenly - Angel - Sage")

        self.client.post(self.url, {f"baths_{product.pk}": "2"})
        context = self.client.get(
            reverse("recipe_detail", args=[self.recipe.pk])
        ).context
        self.assertEqual(context["produced"], 10)
        self.assertEqual(context["on_hand"], 10)
        self.assertEqual(len(context["logs"]), 1)


class ParseCardDateTests(TestCase):
    """Reading dates off handwritten cards.

    The rule that matters: never invent precision. "9/2024" is a month, and
    saying it was the 1st would be making up a record nobody wrote.
    """

    def _parse(self, text):
        from scarves.views import parse_card_date
        return parse_card_date(text)

    def test_us_order_is_a_day(self):
        for text in ("9/15/2024", "09/15/2024", "9-15-2024", "9.15.2024"):
            with self.subTest(text=text):
                parsed, precision = self._parse(text)
                self.assertEqual(parsed.isoformat(), "2024-09-15")
                self.assertEqual(precision, InventoryLog.DAY)

    def test_iso_order_is_a_day(self):
        parsed, precision = self._parse("2024-09-15")
        self.assertEqual(parsed.isoformat(), "2024-09-15")
        self.assertEqual(precision, InventoryLog.DAY)

    def test_a_two_digit_year_is_this_century(self):
        parsed, _ = self._parse("9/15/24")
        self.assertEqual(parsed.year, 2024)

    def test_month_and_year_stays_a_month(self):
        for text in ("9/2024", "09/2024", "2024-09", "9-24"):
            with self.subTest(text=text):
                parsed, precision = self._parse(text)
                self.assertEqual((parsed.year, parsed.month), (2024, 9))
                self.assertEqual(precision, InventoryLog.MONTH)
                # Stored on the 1st so it sorts — but flagged, so the day is
                # never shown as though it were recorded.
                self.assertEqual(parsed.day, 1)

    def test_four_digits_disambiguates_iso_from_us_order(self):
        self.assertEqual(self._parse("2024-09")[0].month, 9)
        self.assertEqual(self._parse("9-2024")[0].month, 9)

    def test_nonsense_is_refused_rather_than_guessed_at(self):
        for text in ("last summer", "", "9", "1/2/3/4", "sept 2024", "9//"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    self._parse(text)

    def test_an_impossible_date_is_refused(self):
        for text in ("13/40/2024", "2024-02-31"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    self._parse(text)


class CardBackfillTests(TestCase):
    """Typing up the old kanban cards — one card per finished product."""

    def setUp(self):
        self.user = User.objects.create_superuser("card", "c@example.test", "pw")
        self.client.force_login(self.user)
        self.recipe = make_recipe("Sage")
        category, _ = RawProductCategory.objects.get_or_create(name="Yarn")
        self.base = RawProduct.objects.create(
            name="Heavenly - Angel", category=category, price="5.00",
            number_per_dye_bath=5, number_on_hand=40,
        )
        self.product = FinishedProduct.objects.create(
            name="Heavenly - Angel - Sage", raw_product=self.base,
            recipe=self.recipe, price="30.00", number_on_hand=7, par=8,
        )
        self.url = reverse("card_backfill", args=[self.product.pk])

    def test_both_pages_require_login(self):
        self.client.logout()
        for url in (reverse("card_backfill_index"), self.url):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response["Location"])

    def test_the_index_lists_products_and_links_to_their_cards(self):
        response = self.client.get(reverse("card_backfill_index"))
        self.assertContains(response, self.product.name)
        self.assertContains(response, self.url)

    def test_a_typed_entry_records_history_and_leaves_stock_alone(self):
        self.client.post(self.url, {"date_0": "9/15/2024", "baths_0": "2"})

        self.product.refresh_from_db()
        self.base.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 7)   # untouched
        self.assertEqual(self.base.number_on_hand, 40)     # untouched

        log = InventoryLog.objects.get()
        self.assertEqual(log.quantity, 10)
        self.assertEqual(log.date_precision, InventoryLog.DAY)
        self.assertEqual(timezone.localtime(log.created_at).date().isoformat(),
                         "2024-09-15")

    def test_a_month_only_entry_is_stored_as_a_month(self):
        self.client.post(self.url, {"date_0": "9/2024", "baths_0": "1"})

        log = InventoryLog.objects.get()
        self.assertEqual(log.date_precision, InventoryLog.MONTH)
        self.assertEqual(log.when, "Sep 2024")
        # The stored day is padding and must never surface.
        self.assertNotIn("01", log.when)

    def test_a_whole_card_goes_in_at_once(self):
        self.client.post(self.url, {
            "date_0": "3/2024", "baths_0": "1",
            "date_1": "6/12/2024", "baths_1": "2",
            "date_2": "2024-11-03", "baths_2": "1",
        })
        self.assertEqual(InventoryLog.objects.count(), 3)
        self.assertEqual(
            sum(l.quantity for l in InventoryLog.objects.all()), 20
        )

    def test_one_bad_row_stops_the_whole_card(self):
        """Half a transcribed card is worse than none — you can't tell which
        half made it in."""
        response = self.client.post(self.url, {
            "date_0": "9/15/2024", "baths_0": "1",
            "date_1": "sometime", "baths_1": "2",
        }, follow=True)

        self.assertEqual(InventoryLog.objects.count(), 0)
        self.assertContains(response, "can&#x27;t read the date")
        self.assertContains(response, "Nothing was recorded")

    def test_baths_without_a_date_is_refused(self):
        response = self.client.post(self.url, {"baths_0": "2"}, follow=True)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.assertContains(response, "baths but no date")

    def test_a_future_date_is_refused(self):
        ahead = timezone.localdate() + timedelta(days=400)
        response = self.client.post(self.url, {
            "date_0": ahead.strftime("%m/%d/%Y"), "baths_0": "1",
        }, follow=True)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.assertContains(response, "in the future")

    def test_blank_rows_are_ignored(self):
        response = self.client.post(self.url, {
            "date_0": "", "baths_0": "",
            "date_5": "9/2024", "baths_5": "1",
        }, follow=True)
        self.assertEqual(InventoryLog.objects.count(), 1)
        self.assertContains(response, "Added 1 entry")

    def test_an_empty_submit_says_so(self):
        response = self.client.post(self.url, {}, follow=True)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.assertContains(response, "Nothing entered")

    def test_typed_entries_show_up_on_the_card_and_the_recipe(self):
        self.client.post(self.url, {"date_0": "9/2024", "baths_0": "2"})

        card = self.client.get(self.url)
        self.assertContains(card, "Sep 2024")
        self.assertContains(card, "month only")

        recipe = self.client.get(reverse("recipe_detail", args=[self.recipe.pk]))
        self.assertContains(recipe, "Sep 2024")
        # The history counts it, but current stock still doesn't.
        self.assertEqual(recipe.context["produced"], 10)
        self.assertEqual(recipe.context["on_hand"], 7)

    def test_the_index_counts_progress_through_the_stack(self):
        self.assertEqual(
            self.client.get(reverse("card_backfill_index")).context["done"], 0
        )
        self.client.post(self.url, {"date_0": "9/2024", "baths_0": "1"})
        self.assertEqual(
            self.client.get(reverse("card_backfill_index")).context["done"], 1
        )


class LogPrecisionDisplayTests(TestCase):
    """`when` is the only thing templates should print for a log date."""

    def setUp(self):
        recipe = make_recipe("Sage")
        self.product = make_product(recipe, "Heavenly - Angel - Sage")

    def _log(self, precision, when):
        log = InventoryLog.objects.create(
            finished_product=self.product,
            log_type=InventoryLog.PRODUCTION, quantity=5,
            date_precision=precision,
        )
        InventoryLog.objects.filter(pk=log.pk).update(created_at=when)
        return InventoryLog.objects.get(pk=log.pk)

    def test_a_month_only_log_never_shows_a_day(self):
        when = timezone.make_aware(datetime(2024, 9, 1, 12, 0))
        self.assertEqual(self._log(InventoryLog.MONTH, when).when, "Sep 2024")

    def test_a_day_log_shows_the_day_but_not_a_time(self):
        when = timezone.make_aware(datetime(2024, 9, 15, 12, 0))
        self.assertEqual(self._log(InventoryLog.DAY, when).when, "15 Sep 2024")

    def test_a_live_entry_keeps_its_time(self):
        when = timezone.make_aware(datetime(2026, 8, 1, 21, 36))
        self.assertEqual(
            self._log(InventoryLog.EXACT, when).when, "01 Aug 2026, 21:36"
        )

    def test_existing_rows_default_to_exact(self):
        """The migration must not retroactively make old rows look vague."""
        log = InventoryLog.objects.create(
            finished_product=self.product,
            log_type=InventoryLog.PRODUCTION, quantity=5,
        )
        self.assertEqual(log.date_precision, InventoryLog.EXACT)


class RecipeShowcaseFilterTests(TestCase):
    """The filter is client-side, so what's testable server-side is that
    every row carries the haystack the script searches."""

    def setUp(self):
        self.user = User.objects.create_superuser("filt", "f@example.test", "pw")
        self.client.force_login(self.user)

    def test_each_row_carries_a_search_key(self):
        recipe = make_recipe("Burnt Orange")
        make_product(recipe, "Heavenly - Angel - Burnt Orange")

        response = self.client.get(reverse("recipe_showcase"))
        self.assertContains(response, "data-search=")
        self.assertContains(response, "burnt orange")
        # Product names are searchable too — she thinks in bases as well.
        self.assertContains(response, "heavenly - angel - burnt orange")

    def test_a_swapped_row_is_still_searchable(self):
        """A row re-rendered by htmx must keep its key, or it drops out of
        every subsequent search."""
        recipe = make_recipe("Twilight")
        response = self.client.get(reverse("recipe_row", args=[recipe.pk]))
        self.assertContains(response, "data-search=")
        self.assertContains(response, "twilight")

    def test_the_filter_box_is_on_the_page(self):
        response = self.client.get(reverse("recipe_showcase"))
        self.assertContains(response, 'id="recipe-filter"')


class BaseTemplateTests(TestCase):
    """The three-layer template chain: base → base_internal/base_public → page.

    The failure this guards against is silent. A page that overrides
    `{% block style %}` and forgets to open it with `{{ block.super }}`
    still renders, still returns 200, still passes the smoke test — it just
    loses the entire house style and comes out as unstyled HTML. Nothing
    about the page looks wrong until you open it.
    """

    #: Only in base.html's style block, so its presence proves the whole
    #: chain survived — a page that dropped block.super wouldn't have it.
    BASE_MARKER = b"box-sizing: border-box"

    def setUp(self):
        self.user = User.objects.create_superuser("layout", "l@example.test", "pw")
        recipe = make_recipe("Layout Test Recipe")
        make_product(recipe, "Layout Test Product")

    def _page_templates(self):
        """Every full-page template — partials and the bases themselves are
        not pages and are expected to have no doctype of their own."""
        from pathlib import Path

        root = Path(__file__).resolve().parent / "templates" / "scarves"
        return [p for p in sorted(root.glob("*.html")) if not p.name.startswith("base")]

    def test_no_page_template_carries_its_own_doctype(self):
        offenders = [
            p.name
            for p in self._page_templates()
            if "<!doctype" in p.read_text().lower()
        ]
        self.assertEqual(
            offenders, [],
            "these build their own document instead of extending a base — "
            "the point of the base layer is that the shell exists once",
        )

    def test_every_page_template_extends_a_base(self):
        offenders = []
        for path in self._page_templates():
            first = path.read_text().lstrip().splitlines()[0].strip()
            if not first.startswith("{% extends"):
                offenders.append(f"{path.name}: {first!r}")
        self.assertEqual(
            offenders, [],
            "{% extends %} must be the first tag in the file — Django ignores "
            "everything before it",
        )

    def test_page_templates_extend_a_layer_not_the_bare_skeleton(self):
        """base.html has no chrome and no house style; extending it directly
        gets a blank page with a title. Pages pick a side instead."""
        offenders = []
        for path in self._page_templates():
            first = path.read_text().lstrip().splitlines()[0]
            if 'scarves/base.html' in first:
                offenders.append(path.name)
        self.assertEqual(
            offenders, [],
            "extend base_internal.html or base_public.html, not base.html",
        )

    def test_the_shared_style_layer_reaches_every_rendered_page(self):
        """The block.super regression, checked against real responses."""
        from scarves import urls as scarves_urls

        self.client.force_login(self.user)
        checked = []
        for entry in scarves_urls.urlpatterns:
            callback = getattr(entry, "callback", None)
            if callback is None or not getattr(callback, "page_meta", None):
                continue
            if getattr(entry.pattern, "converters", None):
                continue

            url = reverse(entry.name)
            response = self.client.get(url)
            if response.status_code != 200 or b"<html" not in response.content:
                continue  # PDFs and the like aren't HTML pages.
            with self.subTest(url=url):
                self.assertIn(
                    self.BASE_MARKER, response.content,
                    f"{url} lost the shared style layer — its "
                    "{% block style %} is missing {{ block.super }}",
                )
            checked.append(url)

        self.assertGreater(len(checked), 5, checked)

    def test_the_two_layers_keep_their_own_accents(self):
        """Internal and public are meant to look different. If one layer's
        tokens bled into the other, this is where it shows."""
        self.client.force_login(self.user)
        internal = self.client.get(reverse("raw_inventory_index")).content
        self.assertIn(b"--page-bg: #f7f8fa", internal)

        self.client.logout()
        public = self.client.get(reverse("game_page")).content
        self.assertIn(b"--accent: #23466b", public)
        self.assertNotIn(b"--page-bg: #f7f8fa", public)

    def test_internal_pages_offer_a_way_back_to_the_site_map(self):
        """base_internal supplies this, so it holds for pages that never
        wrote the link themselves."""
        self.client.force_login(self.user)
        for name in ("raw_inventory_index", "image_upload", "recipe_showcase"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertContains(response, f'href="{reverse("index")}"')

    def test_the_site_map_does_not_link_to_itself(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("index"))
        self.assertNotContains(response, 'class="back"')


class URLBucketTests(TestCase):
    """`private/` vs `public/` has to mean something, or it's just decoration.

    The first path segment is the app's clearest statement about exposure,
    so it's checked against what the views actually do rather than trusted.
    """

    KNOWN_BUCKETS = ("private/", "public/", "secret/", "webhooks/")

    def setUp(self):
        self.user = User.objects.create_superuser("bucket", "b@example.test", "pw")
        recipe = make_recipe("Bucket Test Recipe")
        make_product(recipe, "Bucket Test Product")

    def _routes(self):
        from scarves import urls as scarves_urls

        return [e for e in scarves_urls.urlpatterns if getattr(e, "callback", None)]

    def test_every_route_declares_a_bucket(self):
        stray = [
            str(e.pattern)
            for e in self._routes()
            if str(e.pattern) and not str(e.pattern).startswith(self.KNOWN_BUCKETS)
        ]
        self.assertEqual(
            stray, [],
            "a new route needs to say who it's for: put it under private/, "
            "public/ or webhooks/",
        )

    def test_private_pages_turn_anonymous_visitors_away(self):
        """Includes the routes that take an id.

        Skipping those is how reference_sheet_pdf stayed open — it served a
        full barcode/SKU sheet to anyone who guessed a category id. The
        placeholder id below is never looked up: @login_required redirects
        before the view body runs, which is the whole point.
        """
        checked = []
        for entry in self._routes():
            if not str(entry.pattern).startswith("private/"):
                continue
            if not getattr(entry.callback, "page_meta", None):
                continue

            converters = getattr(entry.pattern, "converters", None)
            if converters:
                url = reverse(entry.name, args=[1] * len(converters))
            else:
                url = reverse(entry.name)

            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(
                    response.status_code, 302,
                    f"{url} is under private/ but served an anonymous request",
                )
                self.assertIn("/login", response["Location"])
            checked.append(url)

        self.assertGreater(len(checked), 5, checked)

    def test_public_pages_really_are_public(self):
        checked = []
        for entry in self._routes():
            if not str(entry.pattern).startswith("public/"):
                continue
            if not entry.name or getattr(entry.pattern, "converters", None):
                continue
            url = reverse(entry.name)
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(
                    response.status_code, 200,
                    f"{url} is under public/ but did not serve an anonymous "
                    "request — either it needs login (move it to private/) or "
                    "something else is wrong",
                )
            checked.append(url)

        self.assertGreaterEqual(len(checked), 4, checked)

    def test_secret_pages_serve_an_anonymous_visitor(self):
        """secret/ is unlisted, not gated — a login here would be a bug.

        The whole reason the bucket exists is a page nobody logs in to and
        nobody advertises. If one of these starts redirecting, the people it
        was built for are locked out and the only symptom is silence.
        """
        checked = []
        for entry in self._routes():
            if not str(entry.pattern).startswith("secret/"):
                continue
            if not entry.name or getattr(entry.pattern, "converters", None):
                continue
            url = reverse(entry.name)
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(
                    response.status_code, 200,
                    f"{url} is under secret/ but did not serve an anonymous "
                    "request — secret means unlisted, not logged in",
                )
            checked.append(url)

        self.assertGreaterEqual(len(checked), 1, checked)

    def test_the_public_map_names_no_private_page(self):
        """The one thing this page must never do.

        Checked against the private map's own contents rather than a hardcoded
        list, so a new staff page is covered the day it's added. secret/ counts
        as private for this purpose: it is reachable without a login, but a
        customer reading the public map must never be handed the URL.
        """
        public = self.client.get(reverse("public_index"))
        self.assertEqual(public.status_code, 200)

        self.client.force_login(self.user)
        private = self.client.get(reverse("index"))

        leaked = []
        for entry in self._routes():
            if not str(entry.pattern).startswith(("private/", "secret/")):
                continue
            meta = getattr(entry.callback, "page_meta", None)
            if not meta or not meta.get("show_in_index", True):
                continue
            title = meta["title"].encode()
            # Sanity: the title really is the string the private map prints,
            # so a miss below means absence, not a bad needle.
            self.assertIn(title, private.content, meta["title"])
            if title in public.content:
                leaked.append(meta["title"])

        self.assertEqual(
            leaked, [], "private or secret pages named on the public map"
        )

    def test_the_public_map_lists_the_public_pages(self):
        response = self.client.get(reverse("public_index"))
        for name in ("game_page", "quiz_page", "reference_sheet_index"):
            with self.subTest(page=name):
                self.assertContains(response, reverse(name))

    def test_the_private_map_badges_every_card_with_its_bucket(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("index"))
        # Both kinds are present, so neither badge is vacuously passing.
        self.assertContains(response, 'class="badge public"')
        self.assertContains(response, 'class="badge private"')
        self.assertContains(response, 'class="badge secret"')

    def test_the_badge_follows_the_url_not_a_hand_maintained_list(self):
        """Move a view between buckets and the badge must move with it."""
        from scarves.views import _site_map

        by_name = {
            item["name"]: item
            for group in _site_map()["grouped"]
            for item in group["items"]
        }
        self.assertEqual(by_name["game_page"]["bucket"], "public")
        self.assertEqual(by_name["reference_sheet_index"]["bucket"], "public")
        self.assertEqual(by_name["raw_inventory_index"]["bucket"], "private")
        self.assertEqual(by_name["hours_entry"]["bucket"], "secret")

    def test_the_bare_app_root_still_reaches_the_site_map(self):
        """/scarves/ is what people type; it must not dead-end."""
        self.client.force_login(self.user)
        response = self.client.get("/scarves/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("index"))
        self.assertEqual(self.client.get(response["Location"]).status_code, 200)

    def test_the_public_map_offers_a_way_in_for_staff(self):
        anon = self.client.get(reverse("public_index"))
        self.assertContains(anon, "Staff sign in")
        # The link has to land somewhere useful after the login, not on the
        # admin index, which is not where any of this work happens.
        self.assertContains(anon, f'?next={reverse("index")}')

        self.client.force_login(self.user)
        signed_in = self.client.get(reverse("public_index"))
        self.assertNotContains(signed_in, "Staff sign in")
        self.assertContains(signed_in, "Staff site map")


class UnknownRouteTests(TestCase):
    """Unknown URLs land on the public map — it's the de facto home page."""

    def test_an_unknown_path_redirects_to_the_public_map(self):
        for path in ("/nope/", "/scarves/typo/", "/scarves/private/nope/", "/deep/a/b/c"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302, path)
                self.assertEqual(response["Location"], reverse("public_index"))

    def test_the_site_root_goes_to_the_public_map(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("public_index"))

    def test_the_catch_all_does_not_swallow_real_routes(self):
        """The regression this would cause is silent and total: every page
        becomes the home page. Worth pinning explicitly."""
        for name in ("public_index", "game_page", "quiz_page", "reference_sheet_index"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

        # And a private page still redirects to the login, not to the map.
        response = self.client.get(reverse("raw_inventory_index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_a_missing_trailing_slash_reaches_the_real_page(self):
        """APPEND_SLASH is set, but it lives in CommonMiddleware and only fires
        when the resolver *fails* — and the catch-all matches everything, so it
        never failed and the setting was silently dead. /scarves/private/colors
        landed on the home page, which reads as a working page, and the setting
        meant to prevent that had no way to run."""
        for name in ("color_classify", "reference_sheet_index", "game_page"):
            slashed = reverse(name)
            with self.subTest(page=name):
                response = self.client.get(slashed.rstrip("/"))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], slashed)

    def test_appending_a_slash_keeps_the_query_string(self):
        response = self.client.get(reverse("color_classify").rstrip("/"), {"todo": "true"})
        self.assertEqual(response["Location"], reverse("color_classify") + "?todo=true")

    def test_a_slashless_url_that_still_matches_nothing_goes_to_the_map(self):
        """The append only helps when a real route is waiting behind it."""
        for path in ("/deep/a/b/c", "/scarves/private/nope"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response["Location"], reverse("public_index"))

    def test_a_slashless_post_is_not_bounced_to_the_slashed_route(self):
        """A redirect drops the body, which is why Django's own APPEND_SLASH
        leaves POSTs alone. The Square webhook registers both spellings itself
        precisely because of this."""
        response = self.client.post(reverse("color_classify").rstrip("/"))
        self.assertEqual(response["Location"], reverse("public_index"))

    def test_missing_assets_still_fail_as_assets(self):
        """A broken <img> resolving to a page of HTML is a miserable debug."""
        for path in ("/static/nope.css", "/media/nope.jpg"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertNotEqual(
                    response.status_code, 302,
                    f"{path} redirected to a page instead of failing",
                )


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

    def test_scarves_are_matched_on_a_shared_color_not_on_an_average(self):
        """The correction that this whole module now turns on.

        The dyes are not blended into one shade — a red-and-blue scarf shows red
        and blue and flows between them. So it belongs next to a red-and-yellow
        scarf, which visibly shares its red, and *not* next to a solid purple,
        which shares nothing but happens to sit where the average lands.

        Averaging gets this exactly backwards, which is why the assertion is
        written as a comparison: it fails if anyone reintroduces one.
        """
        red_blue = recipe_palette(make_recipe("Red Blue", hexes=("#ff0000", "#0000ff")))
        red_yellow = recipe_palette(make_recipe("Red Yellow", hexes=("#ff0000", "#ffff00")))
        solid_purple = recipe_palette(make_recipe("Solid Purple", hexes=("#7f007f",)))

        self.assertLess(
            palette_distance(red_blue, red_yellow),
            palette_distance(red_blue, solid_purple),
        )

    def test_a_shared_accent_alone_does_not_make_two_scarves_alike(self):
        """`closest_pair` on its own ties every recipe that shares a black
        accent, which is most of them. The spread term breaks those ties on
        whether the rest of the palette lines up."""
        target = make_recipe("Target", hexes=("#0a1f6b", "#111111"))
        also_blue = make_recipe("Also Blue", hexes=("#12276f", "#111111"))
        orange = make_recipe("Orange", hexes=("#e8720c", "#111111"))

        target_palette = recipe_palette(target)
        self.assertEqual(
            palette_distance(target_palette, recipe_palette(also_blue))[0],
            palette_distance(target_palette, recipe_palette(orange))[0],
        )
        picked = nearest_by_color([orange, also_blue], target, 1)
        self.assertEqual([r.name for r in picked], ["Also Blue"])

    def test_a_trace_dye_still_counts_as_a_visible_color(self):
        """Ratio governs how much cloth a dye covers, not whether you can see
        it — the 10% dye still gets its own band, so it stays in the palette at
        full strength."""
        recipe = make_recipe("Mostly Blue", hexes=("#0000ff", "#ff0000"))
        rds = list(recipe.recipe_dyes.order_by("order"))
        rds[0].ratio = 90
        rds[0].save()
        rds[1].ratio = 10
        rds[1].save()

        palette = recipe_palette(Recipe.objects.get(pk=recipe.pk))
        self.assertEqual(len(palette), 2)
        nearest_to_red = min(delta_e(lab, hex_to_lab("#ff0000")) for lab in palette)
        self.assertAlmostEqual(nearest_to_red, 0, places=6)

    def test_recipe_with_no_dyes_has_no_palette(self):
        recipe = make_recipe("Colorless", hexes=())
        self.assertEqual(recipe_palette(recipe), [])
        self.assertIsNone(palette_distance(recipe_palette(recipe), [hex_to_lab("#ff0000")]))

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

    def test_nearest_ranks_by_color_not_by_order(self):
        blues = [make_recipe(f"Blue {i}", hexes=(h,))
                 for i, h in enumerate(("#0a1f6b", "#12276f", "#1b2f78"))]
        oranges = [make_recipe(f"Orange {i}", hexes=(h,))
                   for i, h in enumerate(("#e8720c", "#f07d18", "#d96a05"))]

        target = blues[0]
        candidates = oranges + blues[1:]
        picked = nearest_by_color(candidates, target, 2)
        self.assertEqual({r.name for r in picked}, {"Blue 1", "Blue 2"})

    def test_nearest_falls_back_when_colors_are_missing(self):
        colorless = make_recipe("Colorless", hexes=())
        others = [make_recipe(f"Other {i}") for i in range(3)]

        # An uncolorable target can't be ranked against, so it fills at random
        # rather than returning nothing.
        self.assertEqual(len(nearest_by_color(others, colorless, 2)), 2)
        # An uncolorable candidate is filler, used only once the rest run out.
        picked = nearest_by_color([colorless] + others, others[0], 2)
        self.assertNotIn("Colorless", {r.name for r in picked})
        self.assertEqual(nearest_by_color(others, others[0], 0), [])

    def test_cluster_handles_pool_smaller_than_board(self):
        make_recipe("Only One")
        pool = list(Recipe.objects.prefetch_related("recipe_dyes__dye"))
        self.assertEqual(len(pick_color_cluster(pool, 6)), 1)
        self.assertEqual(pick_color_cluster([], 6), [])


# ---------------------------------------------------------------------------
# Rainbow bands
# ---------------------------------------------------------------------------


def make_band_image(size=(200, 200), patches=(), background=(128, 128, 128)):
    """Bytes of a JPEG: a background with optional coloured patches on it.

    `patches` are (colour, fraction) — each paints a horizontal stripe covering
    that fraction of the *sampled crop*, not of the whole image, so a test can
    say "15% of the visible cloth is blue" and mean it. PHOTO_CROP trims the
    edges the way it does on a real product photo, and a test that ignored it
    would be measuring shares against pixels the classifier never sees.
    """
    from io import BytesIO

    from PIL import Image, ImageDraw

    from .colorbands import PHOTO_CROP

    w, h = size
    left, top, right, bottom = PHOTO_CROP
    crop_top, crop_bottom = int(h * top), int(h * bottom)
    crop_h = crop_bottom - crop_top

    img = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(img)
    y = crop_top
    for colour, fraction in patches:
        band_h = int(crop_h * fraction)
        draw.rectangle([0, y, w, y + band_h], fill=colour)
        y += band_h

    buf = BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


class BandClassifierTests(TestCase):
    """The three axes, tested on the dyes that actually broke a one-axis rule.

    Every hex here is a real dye from stock, not a made-up colour — these are
    the specific cases where hue alone gives a confidently wrong answer.
    """

    def test_hue_alone_would_call_the_blacks_red_and_blue(self):
        from .colorbands import band_for_hex

        # #000000 has hue 0 and #000001 has hue 240. Lightness is what saves it.
        self.assertEqual(band_for_hex("#000000"), "black")     # 639 Jet Black
        self.assertEqual(band_for_hex("#000001"), "black")     # 413 True Black

    def test_greys_are_caught_by_saturation_not_hue(self):
        from .colorbands import band_for_hex

        self.assertEqual(band_for_hex("#708090"), "grey")      # Slate, hue 210
        self.assertEqual(band_for_hex("#877c85"), "grey")      # 638 Silver
        self.assertEqual(band_for_hex("#2a3439"), "grey")      # Gun

    def test_creams_are_caught_by_lightness_not_hue(self):
        from .colorbands import band_for_hex

        self.assertEqual(band_for_hex("#f3ead7"), "grey")      # 488 Ivory, hue 41
        self.assertEqual(band_for_hex("#e9d6ba"), "grey")      # 486 Champagne

    def test_a_pale_pink_is_not_swept_up_as_white(self):
        """The cream rule has to spare saturated tints, or pink loses its palest
        members to grey — `481 Ballerina Pink` is lighter than Ivory."""
        from .colorbands import band_for_hex

        self.assertEqual(band_for_hex("#facbca"), "pink")

    def test_brown_needs_all_three_axes(self):
        from .colorbands import band_for_hex

        # 635 Brown: hue 8.6 says "red", and only dark + dull together say brown.
        self.assertEqual(band_for_hex("#33211e"), "brown")
        # A bright, saturated colour at a similar hue stays red.
        self.assertEqual(band_for_hex("#b72026"), "red")       # 616 Russet

    def test_forest_green_is_green_not_blue(self):
        """Regression: at a 170-degree green/blue line this landed in blue, one
        degree the wrong side. The teals must stay blue all the same."""
        from .colorbands import band_for_hex

        self.assertEqual(band_for_hex("#0b473e"), "green")     # 452 Forest Green, hue 171
        self.assertEqual(band_for_hex("#00536b"), "blue")      # 631 Teal, hue 193
        self.assertEqual(band_for_hex("#009fda"), "blue")      # 624 Turquoise

    def test_the_olive_greens_are_green_not_yellow(self):
        """Regression: at a 70-degree yellow/green line these five landed in
        yellow, `461 Avocado` missing green by five degrees.

        The catalogue has no dye at all between 69.2 and 79.3, so the old line
        classified nothing and never got examined. `445 Fluorescent Lemon` at
        exactly 60.0 is the nearest true yellow and has to stay one.
        """
        from .colorbands import band_for_hex

        self.assertEqual(band_for_hex("#6f752c"), "green")     # 461 Avocado, 64.9
        self.assertEqual(band_for_hex("#b7bb59"), "green")     # 465 Lichen, 62.4
        self.assertEqual(band_for_hex("#d7df23"), "green")     # 628 Chartreuse (Neon)
        self.assertEqual(band_for_hex("#c6d92c"), "green")     # 479 Radioactive, 66.6
        self.assertEqual(band_for_hex("#b7cb48"), "green")     # 448 Chartreuse, 69.2
        self.assertEqual(band_for_hex("#ffff00"), "yellow")    # 445 Fluor. Lemon, 60.0
        self.assertEqual(band_for_hex("#fff200"), "yellow")    # 601 Sun Yellow, 56.9

    def test_the_cream_rule_is_a_different_seventy(self):
        """`band_for_hsl` holds two unrelated 70s. Moving the band boundary to
        61 must not drag the cream cutoff with it, or Ivory turns yellow."""
        from .colorbands import band_for_hex

        self.assertEqual(band_for_hex("#f3ead7"), "grey")      # 488 Ivory, hue 41
        self.assertEqual(band_for_hex("#c2b264"), "yellow")    # 435 Soft Tan, hue 50

    def test_light_reds_read_as_pink(self):
        from .colorbands import band_for_hex

        self.assertEqual(band_for_hex("#f37b70"), "pink")      # 607 Salmon
        self.assertEqual(band_for_hex("#a12033"), "red")       # 440 Oxblood Red

    def test_unparseable_hex_is_none_rather_than_a_guess(self):
        from .colorbands import band_for_hex

        self.assertIsNone(band_for_hex(""))
        self.assertIsNone(band_for_hex("not a colour"))
        self.assertIsNone(band_for_hex(None))

    def test_bands_come_back_in_rainbow_order(self):
        from .colorbands import sort_bands

        self.assertEqual(
            sort_bands(["blue", "red", "grey", "green", "red"]),
            ["red", "green", "blue", "grey"],
        )


class BandsFromDyesTests(TestCase):
    def test_each_dye_contributes_its_band(self):
        from .colorbands import bands_from_dyes

        recipe = make_recipe("Sunset", hexes=("#b72026", "#f78d1e"))
        self.assertEqual(bands_from_dyes(recipe), ["red", "orange"])

    def test_two_dyes_in_one_band_collapse_to_one(self):
        from .colorbands import bands_from_dyes

        recipe = make_recipe("Two Blues", hexes=("#0e2a5e", "#1e3277"))
        self.assertEqual(bands_from_dyes(recipe), ["blue"])

    def test_a_recipe_with_no_dyes_claims_nothing(self):
        """Not 'grey' — an unrecorded recipe is unknown, not colourless."""
        from .colorbands import bands_from_dyes

        self.assertEqual(bands_from_dyes(make_recipe("Blank", hexes=())), [])

    def test_black_grounds_a_colourway_rather_than_claiming_it(self):
        """Black, grey and cream are working dyes, not colorways — they shade
        the colours beside them. Left in, they would have been the biggest
        section on the sheet without one scarf in them anybody calls grey."""
        from .colorbands import bands_from_dyes

        recipe = make_recipe("Turquoise on Black", hexes=("#009fda", "#000000"))
        self.assertEqual(bands_from_dyes(recipe), ["blue"])

    def test_an_all_achromatic_recipe_still_claims_its_section(self):
        """Suppressing grey and black only makes sense when there is something
        to suppress them in favour of. A genuinely grey scarf keeps its
        section — and a black-and-slate one claims both, because the split is
        the point: someone holding a black scarf looks under black."""
        from .colorbands import bands_from_dyes

        recipe = make_recipe("Charcoal", hexes=("#000000", "#708090"))
        self.assertEqual(bands_from_dyes(recipe), ["grey", "black"])

    def test_black_is_its_own_section_not_a_shade_of_grey(self):
        from .colorbands import bands_from_dyes

        recipe = make_recipe("Jet", hexes=("#000000",))
        self.assertEqual(bands_from_dyes(recipe), ["black"])

    def test_a_minor_dye_still_gets_its_band(self):
        """Ratio says how much cloth a dye covers, not whether you can see it.
        Someone hunting for green will still spot the green stripe."""
        from .colorbands import bands_from_dyes

        recipe = make_recipe("Mostly Blue", hexes=("#1e3277", "#00833b"))
        RecipeDye.objects.filter(recipe=recipe).update(ratio=None)
        rd = recipe.recipe_dyes.order_by("order")
        rd.filter(order=1).update(ratio="95.00")
        rd.filter(order=2).update(ratio="5.00")
        self.assertEqual(bands_from_dyes(recipe), ["green", "blue"])


class BandsFromImageTests(TestCase):
    """The photo path, which exists because 22 recipes have photos and no dyes.

    Its accuracy on real cloth is middling by design of the problem, not of the
    code — silk is specular, deep dyes crush toward black in the folds, and the
    scarf shares the frame with a granite counter. What's tested here is the
    behaviour that has to hold regardless: the background must not vote, and a
    near-colourless scarf must not be talked into having colours.
    """

    def test_a_solid_colour_yields_that_band(self):
        from io import BytesIO

        from .colorbands import bands_from_image

        data = make_band_image(background=(30, 60, 160))
        self.assertEqual(bands_from_image(BytesIO(data)), ["blue"])

    def test_two_colours_yield_both_bands(self):
        from io import BytesIO

        from .colorbands import bands_from_image

        data = make_band_image(
            patches=[((30, 60, 160), 0.5)], background=(20, 130, 70)
        )
        self.assertEqual(bands_from_image(BytesIO(data)), ["green", "blue"])

    def test_the_background_cannot_dilute_the_scarf(self):
        """The whole reason shares are measured against chromatic pixels only:
        posterboard, barcode card and granite are all neutral, so a scarf that
        fills a third of the frame still reports its colour at full strength."""
        from io import BytesIO

        from .colorbands import bands_from_image

        data = make_band_image(
            patches=[((30, 60, 160), 0.3)], background=(210, 210, 210)
        )
        self.assertIn("blue", bands_from_image(BytesIO(data)))

    def test_a_genuinely_grey_scarf_reads_as_grey(self):
        from io import BytesIO

        from .colorbands import bands_from_image

        data = make_band_image(background=(130, 130, 132))
        self.assertEqual(bands_from_image(BytesIO(data)), ["grey"])

    def test_a_genuinely_black_scarf_reads_as_black_not_grey(self):
        """The whole reason the band was split: these two used to come back
        with the same answer, and the black scarf was findable only under a
        heading nobody would look for it under."""
        from io import BytesIO

        from .colorbands import bands_from_image

        data = make_band_image(background=(8, 8, 10))
        self.assertEqual(bands_from_image(BytesIO(data)), ["black"])

    def test_a_speck_of_colour_does_not_earn_a_band(self):
        """Regression: dividing by a tiny chromatic mass amplified sensor noise
        into confident bands, and a grey scarf came back claiming orange, blue
        and brown. A band has to cover real area, not just dominate the dregs."""
        from io import BytesIO

        from .colorbands import bands_from_image

        data = make_band_image(
            patches=[((30, 60, 160), 0.01)], background=(130, 130, 132)
        )
        self.assertEqual(bands_from_image(BytesIO(data)), ["grey"])

    def test_a_mostly_grey_scarf_claims_grey_as_well_as_its_colour(self):
        """A muted colourway is both things at once, and which section it
        belongs in is a judgement — so both are offered and a person picks."""
        from io import BytesIO

        from .colorbands import bands_from_image

        data = make_band_image(
            patches=[((30, 60, 160), 0.15)], background=(130, 130, 132)
        )
        self.assertEqual(bands_from_image(BytesIO(data)), ["blue", "grey"])

    def test_an_unreadable_file_leaves_the_row_unsuggested(self):
        from io import BytesIO

        from .colorbands import bands_from_image

        self.assertEqual(bands_from_image(BytesIO(b"not an image")), [])


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ColorClassifyViewTests(TestCase):
    """The page whose job is to make the claim visible.

    The failure this exists to prevent is silent: you look in the orange
    section, the scarf isn't there, and nothing tells you it was filed under
    red. So the tests care most about what separates a confirmed answer from an
    unreviewed guess.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("bands", "b@example.test", "pw")
        self.client.force_login(self.user)
        self.recipe = make_recipe("Sunset Silk", hexes=("#b72026", "#f78d1e"))

    def test_the_page_and_its_row_actions_require_login(self):
        self.client.logout()
        for url in (
            reverse("color_classify"),
            reverse("color_bands_save", args=[self.recipe.pk]),
            reverse("color_suggest_from_photo", args=[self.recipe.pk]),
        ):
            with self.subTest(url=url):
                response = self.client.post(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response["Location"])

    def test_an_unconfirmed_row_shows_the_dye_reading_as_a_guess(self):
        response = self.client.get(reverse("color_classify"))
        self.assertContains(response, 'value="red"')
        self.assertContains(response, "guessed")
        self.assertContains(response, "unconfirmed")
        # Nothing has been written just by looking at the page.
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.color_bands, [])
        self.assertIsNone(self.recipe.bands_confirmed_at)

    def test_confirming_stores_the_bands_in_rainbow_order(self):
        response = self.client.post(
            reverse("color_bands_save", args=[self.recipe.pk]),
            {"bands": ["blue", "red"]},
        )
        self.assertEqual(response.status_code, 200)
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.color_bands, ["red", "blue"])
        self.assertIsNotNone(self.recipe.bands_confirmed_at)

    def test_confirming_nothing_still_counts_as_reviewed(self):
        """"This colourway belongs in no section" is a real answer, and it must
        not leave the row looking untouched forever."""
        self.client.post(reverse("color_bands_save", args=[self.recipe.pk]), {})
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.color_bands, [])
        self.assertTrue(self.recipe.bands_confirmed)

    def test_a_band_that_is_not_a_band_is_dropped(self):
        self.client.post(
            reverse("color_bands_save", args=[self.recipe.pk]),
            {"bands": ["red", "chartreuse", "'; drop table"]},
        )
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.color_bands, ["red"])

    def test_a_confirmed_row_stops_being_offered_a_guess(self):
        self.recipe.color_bands = ["purple"]
        self.recipe.bands_confirmed_at = timezone.now()
        self.recipe.save()

        response = self.client.get(reverse("color_classify"))
        html = response.content.decode()
        row = html[html.index("color-row-%d" % self.recipe.pk):]
        row = row[: row.index("</tr>")]
        # Your decision stands; the dye reading no longer overwrites or marks it.
        self.assertIn('value="purple"\n                 checked', row)
        self.assertNotIn("guessed", row)

    def test_the_todo_filter_shows_only_unconfirmed_recipes(self):
        done = make_recipe("Already Done", hexes=("#1e3277",))
        done.bands_confirmed_at = timezone.now()
        done.save()

        response = self.client.get(reverse("color_classify"), {"todo": "true"})
        self.assertContains(response, "Sunset Silk")
        self.assertNotContains(response, "Already Done")

    def test_counts_track_what_is_left_to_do(self):
        make_recipe("Second", hexes=("#1e3277",))
        response = self.client.get(reverse("color_classify"))
        self.assertEqual(response.context["total_count"], 2)
        self.assertEqual(response.context["todo_count"], 2)

        self.client.post(
            reverse("color_bands_save", args=[self.recipe.pk]), {"bands": ["red"]}
        )
        response = self.client.get(reverse("color_classify"))
        self.assertEqual(response.context["confirmed_count"], 1)
        self.assertEqual(response.context["todo_count"], 1)

    def test_reading_the_photo_suggests_without_saving(self):
        product = make_product(self.recipe, "Sunset Scarf", with_image=False)
        image = FinishedProductImage.objects.create(finished_product=product)
        image.image.save("blue.jpg", ContentFile(make_band_image(
            background=(30, 60, 160))), save=True)

        response = self.client.post(
            reverse("color_suggest_from_photo", args=[self.recipe.pk])
        )
        self.assertContains(response, "not saved yet")
        self.assertContains(response, 'value="blue"')
        # The point of a suggestion: the database is untouched until you confirm.
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.color_bands, [])
        self.assertIsNone(self.recipe.bands_confirmed_at)

    def test_the_photo_adds_to_what_you_already_decided(self):
        """A photo is evidence to add, not a verdict that overrules a band you
        already picked — a scarf can be red in the hand and blue in the shot."""
        self.recipe.color_bands = ["red"]
        self.recipe.bands_confirmed_at = timezone.now()
        self.recipe.save()

        product = make_product(self.recipe, "Sunset Scarf", with_image=False)
        image = FinishedProductImage.objects.create(finished_product=product)
        image.image.save("blue.jpg", ContentFile(make_band_image(
            background=(30, 60, 160))), save=True)

        response = self.client.post(
            reverse("color_suggest_from_photo", args=[self.recipe.pk])
        )
        html = response.content.decode()
        self.assertIn('value="red"\n                 checked', html)
        self.assertIn('value="blue"\n                 checked', html)

    def test_a_recipe_with_no_photo_is_not_offered_the_photo_button(self):
        response = self.client.get(reverse("color_classify"))
        self.assertNotContains(
            response, reverse("color_suggest_from_photo", args=[self.recipe.pk])
        )

    def test_an_external_image_url_is_not_sampled(self):
        """Only an uploaded file can be read; an image_url would mean fetching
        someone else's server, the same limit the reference-sheet PDF has."""
        make_product(self.recipe, "Linked Scarf", with_image=True)
        response = self.client.get(reverse("color_classify"))
        self.assertNotContains(
            response, reverse("color_suggest_from_photo", args=[self.recipe.pk])
        )


class BulkParActionTests(TestCase):
    """Raising par is how you ask for more of a colorway, so the bulk action has
    to reach every finished product in a blank — and stop at the edges of it.
    Anything it touches by accident silently schedules production nobody asked
    for; anything it misses is a par nobody notices is still at the old number.
    """

    def setUp(self):
        self.category = RawProductCategory.objects.create(name="Habotai")
        self.silk = RawProduct.objects.create(
            name="8mm Habotai", category=self.category, price="5.00"
        )
        self.other = RawProduct.objects.create(
            name="Bamboo", category=self.category, price="6.00"
        )
        self.products = [
            FinishedProduct.objects.create(
                name=f"Habotai {n}",
                raw_product=self.silk,
                recipe=make_recipe(f"habotai-{n}"),
                price="30.00",
                par=8,
            )
            for n in ("Red", "Blue")
        ]
        self.retired = FinishedProduct.objects.create(
            name="Habotai Retired",
            raw_product=self.silk,
            recipe=make_recipe("habotai-retired"),
            price="30.00",
            par=8,
            is_active=False,
        )
        self.untouched = FinishedProduct.objects.create(
            name="Bamboo Green",
            raw_product=self.other,
            recipe=make_recipe("bamboo-green"),
            price="30.00",
            par=8,
        )

        User.objects.create_superuser("boss", "boss@example.test", "pw")
        self.client.login(username="boss", password="pw")
        self.url = reverse("admin:scarves_rawproduct_changelist")

    def _post(self, extra=None, raws=None):
        data = {
            "action": "bulk_update_finished_par",
            "_selected_action": [str(rp.pk) for rp in (raws or [self.silk])],
        }
        data.update(extra or {})
        return self.client.post(self.url, data)

    def test_the_confirmation_page_shows_what_is_about_to_change(self):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "8mm Habotai")
        self.assertContains(response, "new_par")
        # Two active products, not the retired third.
        self.assertContains(response, ">2<")

    def test_applying_sets_par_on_every_active_product_in_the_blank(self):
        response = self._post({"apply": "1", "new_par": "20"})
        self.assertEqual(response.status_code, 302)

        for product in self.products:
            product.refresh_from_db()
            self.assertEqual(product.par, 20)

    def test_other_raw_products_are_left_alone(self):
        self._post({"apply": "1", "new_par": "20"})
        self.untouched.refresh_from_db()
        self.assertEqual(self.untouched.par, 8)

    def test_inactive_products_keep_their_par(self):
        """A retired colorway is not in production; giving it a par would put it
        back on the production page."""
        self._post({"apply": "1", "new_par": "20"})
        self.retired.refresh_from_db()
        self.assertEqual(self.retired.par, 8)

    def test_several_raw_products_can_be_set_at_once(self):
        self._post({"apply": "1", "new_par": "12"}, raws=[self.silk, self.other])
        self.untouched.refresh_from_db()
        self.assertEqual(self.untouched.par, 12)
        for product in self.products:
            product.refresh_from_db()
            self.assertEqual(product.par, 12)

    def test_par_of_zero_is_allowed(self):
        """0 means 'stop making this', which is a real thing to want and is
        distinct from leaving par alone."""
        self._post({"apply": "1", "new_par": "0"})
        self.products[0].refresh_from_db()
        self.assertEqual(self.products[0].par, 0)

    def test_a_nonsense_par_changes_nothing(self):
        for bad in ("", "eight", "-3", "4.5"):
            with self.subTest(bad=bad):
                self._post({"apply": "1", "new_par": bad})
                self.products[0].refresh_from_db()
                self.assertEqual(self.products[0].par, 8)

    def test_stock_on_hand_is_not_touched(self):
        self.products[0].number_on_hand = 3
        self.products[0].save()

        self._post({"apply": "1", "new_par": "20"})
        self.products[0].refresh_from_db()
        self.assertEqual(self.products[0].number_on_hand, 3)
        self.assertEqual(self.products[0].shortage, 17)


class FinishedParDefaultTests(TestCase):
    """Par used to be 8 for everything, because 8 was the field default and
    nothing ever overrode it. It belongs to the blank: a silk scarf and a
    bamboo shawl don't sell at the same rate, so they shouldn't ask production
    for the same number.
    """

    def setUp(self):
        self.category = RawProductCategory.objects.create(name="Silk")
        self.raw = RawProduct.objects.create(
            name="8mm Habotai",
            category=self.category,
            price="5.00",
            finished_par_default=15,
        )
        User.objects.create_user("staff", "s@example.test", "pw")
        self.client.login(username="staff", password="pw")

    def _post_matrix(self, recipe_name, on_hand):
        url = reverse("bulk_recipe_matrix_entry")
        return self.client.post(
            f"{url}?raw_ids={self.raw.id}",
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-recipe_name": recipe_name,
                f"form-0-on_hand_{self.raw.id}": str(on_hand),
            },
        )

    def test_a_new_finished_product_takes_the_blanks_par(self):
        self._post_matrix("Sunrise", 3)
        fp = FinishedProduct.objects.get(recipe__name="Sunrise")
        self.assertEqual(fp.par, 15)

    def test_the_default_default_is_still_eight(self):
        """Blanks nobody has set a number on keep the old behaviour."""
        plain = RawProduct.objects.create(
            name="Plain", category=self.category, price="5.00"
        )
        self.assertEqual(plain.finished_par_default, 8)

    def test_changing_the_blanks_default_never_rewrites_an_existing_par(self):
        """An existing par is someone's decision; the matrix form is about
        counts and never asked. Rewriting it here would quietly re-schedule
        production for every colorway in the blank."""
        self._post_matrix("Sunrise", 3)
        fp = FinishedProduct.objects.get(recipe__name="Sunrise")
        fp.par = 40
        fp.save()

        self.raw.finished_par_default = 2
        self.raw.save()
        self._post_matrix("Sunrise", 9)

        fp.refresh_from_db()
        self.assertEqual(fp.par, 40)
        self.assertEqual(fp.number_on_hand, 9)


class SetRawStockTests(TestCase):
    """Counting the shelf and nudging a number are different questions, and
    the page only answered the second one. `set_to` answers the first, the way
    the bulk inventory page already does for finished products.
    """

    def setUp(self):
        self.category = RawProductCategory.objects.create(name="Silk")
        self.raw = RawProduct.objects.create(
            name="8mm Habotai", category=self.category, price="5.00",
            number_on_hand=12,
        )
        User.objects.create_user("staff", "s@example.test", "pw")
        self.client.login(username="staff", password="pw")
        self.url = reverse("adjust_raw_stock", args=[self.raw.pk])
        self.next = reverse("raw_inventory", args=[self.category.pk])

    def _post(self, data):
        data.setdefault("next", self.next)
        return self.client.post(self.url, data)

    def test_setting_an_absolute_count_replaces_what_was_there(self):
        self._post({"set_to": "5"})
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 5)

    def test_setting_a_higher_count_works_too(self):
        self._post({"set_to": "30"})
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 30)

    def test_setting_zero_empties_the_shelf(self):
        """0 is a real count, and the falsy-string trap would read it as 'blank'
        and fall through to the delta path, which changes nothing."""
        self._post({"set_to": "0"})
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 0)

    def test_the_delta_buttons_still_work(self):
        self._post({"delta": "-3"})
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 9)

    def test_an_empty_box_falls_through_to_the_delta(self):
        """Both forms post to the same endpoint; a blank number field must not
        stop the +1 button next to it from working."""
        self._post({"set_to": "", "delta": "1"})
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 13)

    def test_a_count_wins_over_a_delta(self):
        self._post({"set_to": "4", "delta": "100"})
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 4)

    def test_nonsense_leaves_the_count_alone(self):
        for bad in ("twelve", "-2", "3.5"):
            with self.subTest(bad=bad):
                self._post({"set_to": bad})
                self.raw.refresh_from_db()
                self.assertEqual(self.raw.number_on_hand, 12)

    def test_setting_it_to_what_it_already_is_is_not_an_error(self):
        response = self._post({"set_to": "12"})
        self.assertEqual(response.status_code, 302)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 12)

    def test_the_page_offers_a_box_to_type_in(self):
        response = self.client.get(self.next)
        self.assertContains(response, 'name="set_to"')


class BehindABathTests(TestCase):
    """The production page's red highlight asks about the *next dye bath*, not
    about the shelf being empty.

    A bath is a fixed size, so overshooting par is normal — which means "below
    par" marks nearly every row and therefore points at nothing. The rows worth
    walking to are the ones where a whole bath still lands at or under par: a
    session's work there is fully used, and nothing is wasted rounding up.
    """

    def setUp(self):
        self.category = RawProductCategory.objects.create(name="Silk")
        self.raw = RawProduct.objects.create(
            name="8mm Habotai",
            category=self.category,
            price="5.00",
            number_per_dye_bath=4,
        )
        User.objects.create_user("staff", "s@example.test", "pw")
        self.client.login(username="staff", password="pw")

    def _product(self, name, on_hand, par=8):
        return FinishedProduct.objects.create(
            name=name,
            raw_product=self.raw,
            recipe=make_recipe(name.lower().replace(" ", "-")),
            price="30.00",
            par=par,
            number_on_hand=on_hand,
        )

    def test_the_worked_example(self):
        """Par 8, bath 4: 5 is inside the rounding, 4 is not."""
        self.assertFalse(self._product("Five", 5).behind_a_bath)
        self.assertTrue(self._product("Four", 4).behind_a_bath)

    def test_a_bath_landing_exactly_on_par_still_counts(self):
        """4 + 4 = 8 exactly. 'At or below par' includes at."""
        self.assertTrue(self._product("Exact", 4).behind_a_bath)

    def test_one_short_of_a_full_bath_does_not(self):
        self.assertFalse(self._product("Nearly", 5).behind_a_bath)

    def test_an_empty_shelf_still_counts(self):
        """The old rule's only case has to survive the new one."""
        self.assertTrue(self._product("Empty", 0).behind_a_bath)

    def test_a_bigger_bath_moves_the_line(self):
        """The threshold is the bath, so the same shortage reads differently on
        a blank that dyes 8 at a time."""
        self.raw.number_per_dye_bath = 8
        self.raw.save()
        self.assertFalse(self._product("Four", 4, par=8).behind_a_bath)
        self.assertTrue(self._product("Zero", 0, par=8).behind_a_bath)

    def test_a_missing_bath_size_is_treated_as_one(self):
        """`record_dye_bath` already reads 0 as 1; disagreeing here would paint
        every below-par row red."""
        self.raw.number_per_dye_bath = 0
        self.raw.save()
        self.assertTrue(self._product("Short", 7, par=8).behind_a_bath)
        self.assertFalse(self._product("AtPar", 8, par=8).behind_a_bath)

    def test_at_or_over_par_is_never_behind(self):
        self.assertFalse(self._product("AtPar", 8).behind_a_bath)
        self.assertFalse(self._product("Over", 12).behind_a_bath)

    def _rows(self, html, name):
        """The <tr> for one product, as rendered."""
        import re
        match = re.search(
            r'<tr id="fp-\d+"[^>]*>\s*<td>' + re.escape(name) + r'</td>', html
        )
        return match.group(0) if match else ""

    def test_the_page_paints_only_the_rows_a_bath_would_not_fix(self):
        self._product("Five On Hand", 5)
        self._product("Four On Hand", 4)

        html = self.client.get(reverse("production_needed")).content.decode()
        self.assertIn('class="behind"', self._rows(html, "Four On Hand"))
        self.assertNotIn('class="behind"', self._rows(html, "Five On Hand"))

    def test_the_htmx_swap_agrees_with_the_page(self):
        """The row partial is shared, but the swap re-renders one row after a
        bath — the highlight has to clear itself when the bath fixes it."""
        fp = self._product("Four On Hand", 4)
        response = self.client.post(
            reverse("record_dye_bath", args=[fp.pk]),
            {"next": reverse("production_needed")},
            HTTP_HX_REQUEST="true",
        )
        fp.refresh_from_db()
        self.assertEqual(fp.number_on_hand, 8)
        self.assertNotIn('class="behind"', response.content.decode())

    def test_a_recipe_with_a_behind_row_sorts_above_one_without(self):
        """Farthest-from-goal first, which is the whole point of the ordering."""
        self._product("Rounding Only", 5)
        self._product("Bath Short", 1)

        html = self.client.get(reverse("production_needed")).content.decode()
        self.assertLess(html.index("bath-short"), html.index("rounding-only"))

    def test_the_group_banner_follows_the_same_rule(self):
        self._product("Rounding Only", 5)
        html = self.client.get(reverse("production_needed")).content.decode()
        self.assertNotIn('class="warn"', html)

        self._product("Bath Short", 1)
        html = self.client.get(reverse("production_needed")).content.decode()
        self.assertIn('class="warn"', html)


# --- Timekeeping ------------------------------------------------------------


def make_employee(name, pin="1234", active=True):
    return Employee.objects.create(name=name, pin=pin, is_active=active)



class PayWeekTests(TestCase):
    """Saturday-to-Friday, which no date library assumes for you.

    Worth pinning hard: getting it wrong still renders seven columns, they're
    just the wrong seven, and the totals belong to a week nobody is paying for.
    """

    def test_a_saturday_is_its_own_week_start(self):
        saturday = date(2026, 8, 1)
        self.assertEqual(saturday.weekday(), 5)
        self.assertEqual(timesheets.week_start(saturday), saturday)

    def test_every_day_of_a_week_maps_to_the_same_saturday(self):
        saturday = date(2026, 8, 1)
        for offset in range(7):
            day = saturday + timedelta(days=offset)
            with self.subTest(day=day):
                self.assertEqual(timesheets.week_start(day), saturday)

    def test_the_next_saturday_starts_a_new_week(self):
        self.assertEqual(
            timesheets.week_start(date(2026, 8, 8)), date(2026, 8, 8)
        )

    def test_a_friday_closes_the_week_it_belongs_to(self):
        self.assertEqual(timesheets.week_end(date(2026, 8, 1)), date(2026, 8, 7))

    def test_the_week_runs_saturday_to_friday(self):
        days = timesheets.week_days(date(2026, 8, 1))
        self.assertEqual(len(days), 7)
        self.assertEqual(days[0].strftime("%A"), "Saturday")
        self.assertEqual(days[-1].strftime("%A"), "Friday")

    def test_a_week_param_is_snapped_to_its_saturday(self):
        """Any day inside the week is a valid way to ask for it."""
        self.assertEqual(
            timesheets.parse_week("2026-08-05", date(2026, 8, 7)), date(2026, 8, 1)
        )

    def test_an_unreadable_week_param_falls_back_to_this_week(self):
        for bad in ["", "not-a-date", "2026-13-45", "08/01/2026", None]:
            with self.subTest(value=bad):
                self.assertEqual(
                    timesheets.parse_week(bad, date(2026, 8, 7)), date(2026, 8, 1)
                )


class HoursFormTests(TestCase):
    """What the public form will and won't accept."""

    def setUp(self):
        self.today = date(2026, 8, 7)
        self.sam = make_employee("Sam", pin="4821")

    def _data(self, **overrides):
        data = {
            "employee": self.sam.pk,
            "pin": "4821",
            "hours": "9.5",
            "work_date": "2026-08-07",
        }
        data.update(overrides)
        return data

    def test_a_good_submission_validates(self):
        form = HoursForm(self._data(), today=self.today)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["hours"], Decimal("9.5"))

    def test_the_wrong_pin_is_rejected(self):
        form = HoursForm(self._data(pin="0000"), today=self.today)
        self.assertFalse(form.is_valid())
        self.assertIn("pin", form.errors)

    def test_another_persons_pin_does_not_work(self):
        """The PIN is checked against the name picked, not against every PIN."""
        make_employee("Alex", pin="1111")
        form = HoursForm(self._data(pin="1111"), today=self.today)
        self.assertFalse(form.is_valid())
        self.assertIn("pin", form.errors)

    def test_a_pin_that_is_not_four_digits_is_rejected(self):
        for bad in ["123", "12345", "abcd", "12 4", ""]:
            with self.subTest(pin=bad):
                form = HoursForm(self._data(pin=bad), today=self.today)
                self.assertFalse(form.is_valid())
                self.assertIn("pin", form.errors)

    def test_an_inactive_employee_is_not_on_the_list(self):
        gone = make_employee("Gone", pin="9999", active=False)
        form = HoursForm(self._data(employee=gone.pk, pin="9999"), today=self.today)
        self.assertFalse(form.is_valid())
        self.assertIn("employee", form.errors)

    def test_a_future_day_is_rejected(self):
        form = HoursForm(self._data(work_date="2026-08-08"), today=self.today)
        self.assertFalse(form.is_valid())
        self.assertIn("work_date", form.errors)

    def test_today_is_accepted(self):
        form = HoursForm(self._data(work_date="2026-08-07"), today=self.today)
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_day_beyond_the_backdate_window_is_rejected(self):
        old = self.today - timedelta(days=HoursForm.MAX_BACKDATE_DAYS + 1)
        form = HoursForm(self._data(work_date=old.isoformat()), today=self.today)
        self.assertFalse(form.is_valid())
        self.assertIn("work_date", form.errors)

    def test_the_edge_of_the_backdate_window_is_accepted(self):
        edge = self.today - timedelta(days=HoursForm.MAX_BACKDATE_DAYS)
        form = HoursForm(self._data(work_date=edge.isoformat()), today=self.today)
        self.assertTrue(form.is_valid(), form.errors)

    def test_hours_outside_the_picker_are_rejected(self):
        """The picker is a whitelist, so a hand-crafted POST can't beat it."""
        for bad in ["0", "-4", "24", "9.33", "999"]:
            with self.subTest(hours=bad):
                form = HoursForm(self._data(hours=bad), today=self.today)
                self.assertFalse(form.is_valid())
                self.assertIn("hours", form.errors)

    def test_the_picker_runs_in_quarter_hours(self):
        values = [v for v, _ in HoursForm.hour_choices() if v]
        self.assertIn("0.25", values)
        self.assertIn("9.5", values)
        self.assertNotIn("9.1", values)

    def test_one_hour_is_not_labelled_hours(self):
        labels = dict(HoursForm.hour_choices())
        self.assertEqual(labels["1"], "1 hour")
        self.assertEqual(labels["9.5"], "9.5 hours")
        # 10 must not come out as "1E+1" and put a hole in the picker.
        self.assertEqual(labels["10"], "10 hours")


class HoursEntryViewTests(TestCase):
    """The public form end to end — no login anywhere in here on purpose."""

    def setUp(self):
        self.sam = make_employee("Sam", pin="4821")
        self.url = reverse("hours_entry")

    def _post(self, **overrides):
        data = {
            "employee": self.sam.pk,
            "pin": "4821",
            "hours": "9.5",
            "work_date": timezone.localdate().isoformat(),
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_the_form_serves_an_anonymous_visitor(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Report your hours")

    def test_a_submission_records_the_hours(self):
        response = self._post()
        self.assertEqual(response.status_code, 302)
        entry = TimeEntry.objects.get()
        self.assertEqual(entry.employee, self.sam)
        self.assertEqual(entry.hours, Decimal("9.5"))

    def test_the_receipt_shows_after_the_redirect(self):
        """Post/redirect/get: the confirmation survives, a refresh doesn't resubmit."""
        response = self.client.post(
            self.url,
            {
                "employee": self.sam.pk,
                "pin": "4821",
                "hours": "9.5",
                "work_date": timezone.localdate().isoformat(),
            },
            follow=True,
        )
        self.assertContains(response, "Got it")
        self.assertContains(response, "Sam")

        # Second GET: the receipt was popped, so a refresh is a clean form.
        again = self.client.get(self.url)
        self.assertNotContains(again, "Got it")
        self.assertEqual(TimeEntry.objects.count(), 1)

    def test_a_wrong_pin_records_nothing(self):
        response = self._post(pin="0000")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TimeEntry.objects.count(), 0)

    def test_reporting_the_same_day_twice_asks_before_replacing(self):
        """The double-tapped Submit. Without this it books the day twice."""
        self._post(hours="9.5")
        self.assertEqual(TimeEntry.objects.count(), 1)

        response = self._post(hours="6")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already reported that day")

        # Still the original figure — nothing was overwritten by the ask.
        self.assertEqual(TimeEntry.objects.get().hours, Decimal("9.5"))

    def test_a_confirmed_replacement_overwrites_rather_than_adding(self):
        self._post(hours="9.5")
        response = self._post(hours="6", confirm_replace="9.50")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TimeEntry.objects.count(), 1)
        self.assertEqual(TimeEntry.objects.get().hours, Decimal("6"))

    def test_a_stale_confirmation_does_not_overwrite(self):
        """The token is the figure being replaced, so a form left open in
        another tab can't confirm away a number it never showed."""
        self._post(hours="9.5")
        response = self._post(hours="6", confirm_replace="3.00")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TimeEntry.objects.get().hours, Decimal("9.5"))

    def test_two_people_can_report_the_same_day(self):
        alex = make_employee("Alex", pin="1111")
        self._post()
        self.client.post(self.url, {
            "employee": alex.pk,
            "pin": "1111",
            "hours": "7",
            "work_date": timezone.localdate().isoformat(),
        })
        self.assertEqual(TimeEntry.objects.count(), 2)

    def test_repeated_wrong_pins_stop_being_answered(self):
        for _ in range(HOURS_PIN_ATTEMPT_LIMIT):
            self._post(pin="0000")

        # Even the right PIN gets nowhere now: the throttle is checked first.
        response = self._post()
        self.assertContains(response, "Too many wrong PINs")
        self.assertEqual(TimeEntry.objects.count(), 0)

    def test_a_correct_pin_clears_the_attempt_count(self):
        for _ in range(HOURS_PIN_ATTEMPT_LIMIT - 1):
            self._post(pin="0000")
        self._post()
        self.assertEqual(self.client.session["hours_pin_attempts"], 0)

    def test_the_form_never_shows_anybody_a_pin(self):
        """It lists names — it must not list the numbers that go with them."""
        html = self.client.get(self.url).content.decode()
        self.assertIn("Sam", html)
        self.assertNotIn("4821", html)


class TimesheetViewTests(TestCase):
    """The weekly sheet: staff-only, and the thing that replaces the mental math."""

    def setUp(self):
        self.user = User.objects.create_superuser("boss", "b@example.test", "pw")
        self.client.force_login(self.user)
        self.sam = make_employee("Sam", pin="4821")
        self.alex = make_employee("Alex", pin="1111")
        self.url = reverse("timesheet")
        # The week of Sat 1 Aug – Fri 7 Aug 2026.
        self.week = date(2026, 8, 1)

    def _entry(self, employee, day, hours, created=None):
        """An entry reported on the day it was worked, unless `created` says
        otherwise.

        Defaulting `created_at` matters: left on `auto_now_add` it takes the
        real wall clock, which drifts away from this class's hardcoded August
        2026 fixtures until every entry looks reported weeks late and picks up
        a "reported Nd later" flag. That turned these tests into a time bomb
        that went off a week after they were written — and only
        `test_an_ordinary_day_is_not_flagged` noticed, because it's the one
        asserting the flag list is *empty*. The rest use `assertIn` and would
        have sailed on with a spurious flag.
        """
        entry = TimeEntry.objects.create(
            employee=employee, work_date=day, hours=Decimal(str(hours))
        )
        if created is None:
            created = timezone.make_aware(datetime.combine(day, time(17, 0)))
        # Both timestamps move together. `updated_at` is auto_now, so leaving
        # it on the wall clock while created_at goes back to 2026 makes
        # `was_revised` (updated_at - created_at > 1s) true for every entry.
        TimeEntry.objects.filter(pk=entry.pk).update(
            created_at=created, updated_at=created
        )
        entry.refresh_from_db()
        return entry

    def test_it_needs_a_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_an_empty_week_says_so_rather_than_erroring(self):
        response = self.client.get(self.url, {"week": "2026-08-01"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nobody reported hours")

    def test_a_week_totals_each_person(self):
        self._entry(self.sam, date(2026, 8, 1), "9.5")
        self._entry(self.sam, date(2026, 8, 2), "6")
        self._entry(self.alex, date(2026, 8, 1), "4.25")

        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]

        totals = {r["employee"].name: r["total"] for r in summary["rows"]}
        self.assertEqual(totals, {"Sam": Decimal("15.5"), "Alex": Decimal("4.25")})
        self.assertEqual(summary["total"], Decimal("19.75"))

    def test_the_neighbouring_weeks_are_excluded(self):
        """The Friday before and the Saturday after both belong elsewhere."""
        self._entry(self.sam, date(2026, 7, 31), "8")   # previous week's Friday
        self._entry(self.sam, date(2026, 8, 1), "5")    # this week's Saturday
        self._entry(self.sam, date(2026, 8, 8), "8")    # next week's Saturday

        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]
        self.assertEqual(summary["total"], Decimal("5"))

    def test_any_day_in_the_week_lands_on_the_same_sheet(self):
        self._entry(self.sam, date(2026, 8, 1), "5")
        for day in ["2026-08-01", "2026-08-04", "2026-08-07"]:
            with self.subTest(week=day):
                summary = self.client.get(self.url, {"week": day}).context["summary"]
                self.assertEqual(summary["start"], date(2026, 8, 1))

    def test_it_defaults_to_the_current_week(self):
        summary = self.client.get(self.url).context["summary"]
        self.assertEqual(
            summary["start"], timesheets.week_start(timezone.localdate())
        )

    def test_every_day_gets_a_column_even_when_nobody_worked_it(self):
        self._entry(self.sam, date(2026, 8, 1), "5")
        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]
        cells = summary["rows"][0]["cells"]
        self.assertEqual(len(cells), 7)
        self.assertEqual(sum(1 for c in cells if c["entry"]), 1)

    def test_a_long_day_is_flagged(self):
        self._entry(self.sam, date(2026, 8, 1), "13")
        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]
        flags = [f for c in summary["rows"][0]["cells"] for f in c["flags"]]
        self.assertIn("long day", flags)

    def test_an_ordinary_day_is_not_flagged(self):
        self._entry(self.sam, date(2026, 8, 1), "9.5")
        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]
        self.assertEqual(
            [f for c in summary["rows"][0]["cells"] for f in c["flags"]], []
        )

    def test_a_long_week_is_flagged(self):
        for offset in range(6):
            self._entry(self.sam, date(2026, 8, 1) + timedelta(days=offset), "10")
        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]
        self.assertIn("long week", summary["rows"][0]["flags"])

    def test_a_figure_reported_long_after_the_fact_is_flagged(self):
        self._entry(
            self.sam, date(2026, 8, 1), "8",
            created=timezone.make_aware(datetime(2026, 8, 20, 12, 0)),
        )
        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]
        flags = [f for c in summary["rows"][0]["cells"] for f in c["flags"]]
        self.assertTrue(any("later" in f for f in flags), flags)

    def test_a_revised_figure_is_flagged(self):
        entry = self._entry(self.sam, date(2026, 8, 1), "8")
        TimeEntry.objects.filter(pk=entry.pk).update(
            updated_at=entry.created_at + timedelta(minutes=5)
        )
        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]
        flags = [f for c in summary["rows"][0]["cells"] for f in c["flags"]]
        self.assertIn("revised", flags)

    def test_a_fresh_entry_is_not_called_revised(self):
        """auto_now and auto_now_add land microseconds apart on create."""
        entry = self._entry(self.sam, date(2026, 8, 1), "8")
        self.assertFalse(entry.was_revised)

    def test_the_sheet_tells_you_where_staff_report_their_hours(self):
        response = self.client.get(self.url)
        self.assertContains(response, reverse("hours_entry"))


class TimeEntryModelTests(TestCase):
    def setUp(self):
        self.sam = make_employee("Sam", pin="4821")

    def test_one_entry_per_person_per_day_is_enforced_by_the_database(self):
        from django.db import IntegrityError

        TimeEntry.objects.create(
            employee=self.sam, work_date=date(2026, 8, 1), hours=Decimal("8")
        )
        with self.assertRaises(IntegrityError):
            TimeEntry.objects.create(
                employee=self.sam, work_date=date(2026, 8, 1), hours=Decimal("6")
            )

    def test_an_employee_with_hours_cannot_be_deleted_out_from_under_them(self):
        from django.db.models import ProtectedError

        TimeEntry.objects.create(
            employee=self.sam, work_date=date(2026, 8, 1), hours=Decimal("8")
        )
        with self.assertRaises(ProtectedError):
            self.sam.delete()

    def test_reported_late_by_counts_from_the_day_worked(self):
        entry = TimeEntry.objects.create(
            employee=self.sam, work_date=timezone.localdate() - timedelta(days=3),
            hours=Decimal("8"),
        )
        self.assertEqual(entry.reported_late_by, 3)

    def test_same_day_reporting_is_not_late(self):
        entry = TimeEntry.objects.create(
            employee=self.sam, work_date=timezone.localdate(), hours=Decimal("8")
        )
        self.assertEqual(entry.reported_late_by, 0)


def make_stock(**overrides):
    """The stock we actually buy: OL25WX / Avery 5167, 4 × 20."""
    fields = {
        "name": "Test 1.75 x 0.5",
        "page_width_in": Decimal("8.5"),
        "page_height_in": Decimal("11"),
        "label_width_in": Decimal("1.75"),
        "label_height_in": Decimal("0.5"),
        "columns": 4,
        "rows": 20,
        "margin_left_in": Decimal("0.32812"),
        "margin_top_in": Decimal("0.5"),
        "pitch_x_in": Decimal("2.03125"),
        "pitch_y_in": Decimal("0.5"),
    }
    fields.update(overrides)
    return LabelStock.objects.create(**fields)


def _pdf_text(pdf_bytes):
    """Visible text from a reportlab PDF, for asserting on rendered output.

    reportlab writes content streams as ASCII85 + Flate, so this inflates them
    and pulls out the `(...) Tj` operands. Worth the few lines: without it the
    only thing a PDF test can check is that bytes came back, which passes just
    as happily when the page is blank.
    """
    import base64 as _b64, zlib as _zlib

    out = []
    for m in re.finditer(rb"stream\n(.*?)endstream", pdf_bytes, re.S):
        blob = m.group(1).strip()
        try:
            blob = _zlib.decompress(_b64.a85decode(blob, adobe=True))
        except Exception:
            try:
                blob = _zlib.decompress(blob)
            except Exception:
                continue
        out.append(blob.decode("latin-1"))
    content = "\n".join(out)
    return "".join(re.findall(r"\((.*?)\)\s*Tj", content, re.S))


def _pdf_streams(pdf_bytes):
    import base64 as _b64, zlib as _zlib

    out = []
    for m in re.finditer(rb"stream\n(.*?)endstream", pdf_bytes, re.S):
        blob = m.group(1).strip()
        try:
            blob = _zlib.decompress(_b64.a85decode(blob, adobe=True))
        except Exception:
            continue
        out.append(blob.decode("latin-1"))
    return "\n".join(out)


def _pdf_text_items(pdf_bytes):
    """(x, y, text) for each positioned string reportlab wrote.

    Matches the `Tm ... Tj` operators directly rather than carving the stream
    into `BT ... ET` blocks first. The obvious block regex is non-greedy, and
    "SHEET" ends in "ET" — so it truncates mid-string on exactly the banner
    this is used to locate, and reports nothing rather than reporting wrong.
    """
    return [
        (float(x), float(y), text)
        for x, y, text in re.findall(
            r"1 0 0 1 ([-\d.]+) ([-\d.]+) Tm \((.*?)\) Tj",
            _pdf_streams(pdf_bytes),
            re.S,
        )
    ]


def _pdf_lines(pdf_bytes, x_max=None):
    """Both endpoints of every stroked line segment, in page coordinates.

    reportlab emits `canvas.line()` as `n x1 y1 m x2 y2 l S` on one line —
    enough to assert where the registration ticks landed without pulling in a
    PDF library.
    """
    content = _pdf_streams(pdf_bytes)
    pts = []
    for x1, y1, x2, y2 in re.findall(
        r"([\d.]+) ([\d.]+) m ([\d.]+) ([\d.]+) l S", content
    ):
        pts.append((float(x1), float(y1)))
        pts.append((float(x2), float(y2)))
    if x_max is not None:
        pts = [p for p in pts if p[0] <= x_max]
    return pts


class LabelStockGeometryTests(TestCase):
    """The eight numbers are transcribed by hand off a vendor page, and a
    transposed digit prints one ruined sheet before anyone notices. These are
    the checks that make that a test failure instead."""

    def test_seeded_stock_fits_its_sheet(self):
        """Every stock shipped in a migration closes to the page size."""
        for stock in LabelStock.objects.all():
            with self.subTest(stock=stock.name):
                over_x, over_y = stock.overflow_in()
                self.assertLessEqual(over_x, Decimal("0.01"), "runs off the right edge")
                self.assertLessEqual(over_y, Decimal("0.01"), "runs off the bottom")

    def test_the_migration_seeded_the_stock_we_buy(self):
        stock = LabelStock.objects.get(name__startswith="Avery 5167")
        self.assertEqual(stock.labels_per_sheet, 80)
        self.assertEqual((stock.columns, stock.rows), (4, 20))
        self.assertTrue(stock.purchase_url, "the page offers a 'buy more' link")

    def test_bad_geometry_is_refused(self):
        from django.core.exceptions import ValidationError

        stock = LabelStock(
            name="Transposed pitch",
            page_width_in=Decimal("8.5"), page_height_in=Decimal("11"),
            label_width_in=Decimal("1.75"), label_height_in=Decimal("0.5"),
            columns=4, rows=20,
            margin_left_in=Decimal("0.32812"), margin_top_in=Decimal("0.5"),
            pitch_x_in=Decimal("3.20125"),  # digits swapped
            pitch_y_in=Decimal("0.5"),
        )
        with self.assertRaises(ValidationError):
            stock.full_clean()

    def test_every_label_lands_inside_the_page(self):
        stock = make_stock()
        page_w = labelmod._pt(stock.page_width_in)
        page_h = labelmod._pt(stock.page_height_in)
        label_w = labelmod._pt(stock.label_width_in)
        label_h = labelmod._pt(stock.label_height_in)

        for index in range(stock.labels_per_sheet):
            slot = labelmod.slot_for(stock, index)
            with self.subTest(index=index):
                self.assertGreaterEqual(slot.x, 0)
                self.assertGreaterEqual(slot.y, 0)
                self.assertLessEqual(slot.x + label_w, page_w + 0.5)
                self.assertLessEqual(slot.y + label_h, page_h + 0.5)

    def test_labels_do_not_overlap(self):
        stock = make_stock()
        label_w = labelmod._pt(stock.label_width_in)
        label_h = labelmod._pt(stock.label_height_in)

        boxes = [labelmod.slot_for(stock, i) for i in range(stock.labels_per_sheet)]
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                overlap = (
                    a.x < b.x + label_w and b.x < a.x + label_w
                    and a.y < b.y + label_h and b.y < a.y + label_h
                )
                self.assertFalse(overlap, f"{a} overlaps {b}")

    def test_labels_run_left_to_right_then_down(self):
        stock = make_stock()
        first, second, fifth = (labelmod.slot_for(stock, i) for i in (0, 1, 4))
        self.assertGreater(second.x, first.x, "label 2 is to the right of label 1")
        self.assertAlmostEqual(second.y, first.y, places=6, msg="still row 1")
        self.assertLess(fifth.y, first.y, "label 5 has dropped to row 2")
        self.assertAlmostEqual(fifth.x, first.x, places=6)

    def test_offsets_move_every_label_together(self):
        plain = make_stock()
        nudged = make_stock(name="Nudged", x_offset_mm=Decimal("2"), y_offset_mm=Decimal("-1"))
        for index in (0, 7, 79):
            a, b = labelmod.slot_for(plain, index), labelmod.slot_for(nudged, index)
            self.assertAlmostEqual(b.x - a.x, labelmod._mm_pt(2), places=6)
            self.assertAlmostEqual(b.y - a.y, labelmod._mm_pt(-1), places=6)


class LabelMarkerTests(TestCase):
    """Resuming a part-used sheet is the whole reason the marker exists.

    A weekly run leaves a part-used sheet nearly every time. If the next run
    can't find where to start, that sheet gets binned — and 19 wasted rows
    reads as waste no matter what it cost. The marker puts the answer on the
    paper, so it survives a cleared cache and a different laptop.
    """

    def setUp(self):
        self.stock = make_stock()

    def test_marker_goes_straight_after_the_last_label(self):
        self.assertEqual(labelmod.marker_index_for(self.stock, 63, start_at=0), 63)

    def test_marker_says_the_label_after_itself(self):
        plan = labelmod.plan_sheets(self.stock, 63, start_at=0)
        self.assertEqual(plan.marker_index, 63)
        self.assertEqual(plan.resume_at, 65, "64 is the marker; the next run starts at 65")
        self.assertEqual(plan.free_after, 16)

    def test_resuming_where_the_marker_said_reuses_the_sheet(self):
        """Print 63, resume at 65, and the second run continues the same sheet
        rather than starting a fresh one."""
        first = labelmod.plan_sheets(self.stock, 63, start_at=0)
        second = labelmod.plan_sheets(self.stock, 10, start_at=first.resume_at - 1)
        self.assertEqual(second.sheets[0]["number"], 1)
        self.assertEqual(
            [c["state"] for c in second.sheets[0]["cells"]][:64],
            ["used"] * 64,
            "everything up to and including the old marker is already peeled",
        )
        self.assertEqual(second.sheets[0]["cells"][64]["state"], "printing")

    def test_no_marker_when_the_run_ends_the_sheet_exactly(self):
        """There'd be nowhere to put it but a fresh sheet, and a fresh sheet
        needs no marker."""
        plan = labelmod.plan_sheets(self.stock, 80, start_at=0)
        self.assertIsNone(plan.marker_index)
        self.assertTrue(plan.finishes_sheet)
        self.assertEqual(plan.resume_at, 1)
        self.assertEqual(plan.sheet_count, 1, "the marker must not spill a second sheet")

    def test_marker_can_start_the_next_sheet(self):
        """81 labels fill sheet one and put one on sheet two; the marker
        follows it there."""
        plan = labelmod.plan_sheets(self.stock, 81, start_at=0)
        self.assertEqual(plan.sheet_count, 2)
        self.assertEqual(plan.marker_index, 81)
        self.assertEqual(plan.resume_at, 3)

    def test_partial_sheet_start_marks_earlier_labels_used(self):
        plan = labelmod.plan_sheets(self.stock, 4, start_at=64)
        states = [c["state"] for c in plan.sheets[0]["cells"]]
        self.assertEqual(states[:64], ["used"] * 64)
        self.assertEqual(states[64:68], ["printing"] * 4)
        self.assertEqual(states[68], "marker")


class LabelRunSelectionTests(TestCase):
    """Which products get stickers, and how many."""

    def setUp(self):
        self.recipe = make_recipe("Label Recipe")
        self.a = make_product(self.recipe, "Label A", with_image=False)
        self.b = make_product(self.recipe, "Label B", with_image=False)
        self.a.sku, self.a.number_on_hand = "AAA-ONE", 3
        self.a.save()
        self.b.sku, self.b.number_on_hand = "BBB-TWO", 0
        self.b.save()

    def _log(self, product, qty, when, precision=InventoryLog.EXACT):
        log = InventoryLog.objects.create(
            finished_product=product,
            log_type=InventoryLog.PRODUCTION,
            quantity=qty,
            date_precision=precision,
        )
        InventoryLog.objects.filter(pk=log.pk).update(created_at=when)
        return log

    def test_extra_is_added_per_product(self):
        """The user's arithmetic: base 2 with +2 prints 4, with +1 prints 3."""
        self.a.number_on_hand = 2
        self.a.save()
        for extra, expected in ((0, 2), (1, 3), (2, 4)):
            run = labelmod.inventory_run(extra=extra)
            row = next(r for r in run.rows if r.product.sku == "AAA-ONE")
            self.assertEqual(row.quantity, expected, f"+{extra}")
            self.assertEqual(row.base, 2, "the underlying count is reported unchanged")

    def test_zero_on_hand_is_skipped_by_default(self):
        run = labelmod.inventory_run(extra=2)
        self.assertNotIn("BBB-TWO", [r.product.sku for r in run.rows])

    def test_zero_on_hand_can_be_included(self):
        run = labelmod.inventory_run(extra=2, include_zero=True)
        row = next(r for r in run.rows if r.product.sku == "BBB-TWO")
        self.assertEqual(row.quantity, 2, "just the extras")

    def test_products_without_a_sku_are_reported_not_dropped(self):
        """A silently missing sticker is a scarf that won't scan at the till."""
        self.b.number_on_hand = 5
        self.b.save()
        # save() fills a blank SKU now, so an empty one only arrives via a
        # queryset write — a row created before generation moved to create.
        FinishedProduct.objects.filter(pk=self.b.pk).update(sku="")
        run = labelmod.inventory_run()
        self.assertEqual([p.name for p in run.skipped_no_sku], ["Label B"])
        self.assertNotIn("", [r.product.sku for r in run.rows])

    def test_rows_are_sorted_by_sku(self):
        """A dye bath yields 3–5 of one SKU, so sorting by SKU is what puts
        each bath's stickers together on the sheet."""
        self.b.number_on_hand = 4
        self.b.save()
        run = labelmod.inventory_run()
        self.assertEqual([r.product.sku for r in run.rows], ["AAA-ONE", "BBB-TWO"])
        self.assertEqual(
            [p.sku for p in run.flat()],
            ["AAA-ONE"] * 3 + ["BBB-TWO"] * 4,
            "all copies of a SKU are contiguous",
        )

    def test_produced_since_counts_production_logs(self):
        now = timezone.now()
        self._log(self.a, 4, now - timedelta(days=2))
        self._log(self.a, 3, now - timedelta(days=30))
        run = labelmod.produced_since((now - timedelta(days=7)).date())
        row = next(r for r in run.rows if r.product.sku == "AAA-ONE")
        self.assertEqual(row.quantity, 4, "only the recent bath")

    def test_produced_since_ignores_sales(self):
        """Stickers are for what was made, not for what's left on the shelf."""
        now = timezone.now()
        self._log(self.a, 5, now - timedelta(days=1))
        InventoryLog.objects.create(
            finished_product=self.a, log_type=InventoryLog.SALE, quantity=-3,
        )
        run = labelmod.produced_since((now - timedelta(days=7)).date())
        row = next(r for r in run.rows if r.product.sku == "AAA-ONE")
        self.assertEqual(row.quantity, 5)

    def test_backdated_cards_do_not_ask_for_stickers(self):
        """A 2024 kanban card entered today carries created_at of 2024, so it
        correctly falls outside a recent cutoff — those scarves are long gone."""
        self._log(self.a, 6, timezone.make_aware(datetime(2024, 9, 1)),
                  precision=InventoryLog.MONTH)
        run = labelmod.produced_since(timezone.localdate() - timedelta(days=7))
        self.assertEqual(run.rows, [])

    def test_month_precision_rows_near_the_cutoff_are_counted_not_dropped(self):
        """A month-only row is stored on the 1st as sort padding, so a
        mid-month cutoff excludes it even though its real date is unknown.
        The page has to say so rather than quietly losing production."""
        today = timezone.localdate()
        first = today.replace(day=1)
        self._log(self.a, 4, timezone.make_aware(datetime(first.year, first.month, 1)),
                  precision=InventoryLog.MONTH)

        cutoff = first + timedelta(days=14)
        run = labelmod.produced_since(cutoff)
        self.assertEqual(run.ambiguous_month_logs, 1)

        from_the_first = labelmod.produced_since(first)
        self.assertEqual(from_the_first.ambiguous_month_logs, 0,
                         "no ambiguity when the cutoff is the 1st")
        self.assertEqual(from_the_first.total, 4)


class LabelDensityTests(TestCase):
    """A barcode too dense to scan is a silent failure — it looks like a
    sticker and fails at the till with a queue behind you."""

    def setUp(self):
        self.stock = make_stock()
        self.recipe = make_recipe("Density Recipe")

    def test_the_stock_we_buy_prints_a_full_length_sku_readably(self):
        """SKUs are SLUG6-SLUG6, so 13 characters is the normal maximum."""
        _, mil = labelmod.barcode_for("SILKSC-STORMY", self.stock)
        self.assertGreater(mil, labelmod.MIN_MODULE_MIL)
        self.assertGreater(mil, 7.0, "comfortably scannable, not just legal")

    def test_shorter_skus_get_fatter_bars(self):
        """Sizing per label rather than per run is what buys this."""
        _, short = labelmod.barcode_for("SILK-TEAL", self.stock)
        _, long = labelmod.barcode_for("SILKSC-STORMY", self.stock)
        self.assertGreater(short, long)

    def test_a_narrow_stock_is_reported_as_a_problem(self):
        """The 1in stock we nearly bought, with a normal SKU."""
        narrow = make_stock(
            name="1in", label_width_in=Decimal("1.0"), columns=7,
            margin_left_in=Decimal("0.45"), pitch_x_in=Decimal("1.1"),
        )
        product = make_product(self.recipe, "Dense", with_image=False)
        product.sku, product.number_on_hand = "SILKSC-STORMY", 1
        product.save()

        run = labelmod.inventory_run()
        problems = labelmod.density_problems(run, narrow)
        self.assertEqual([sku for sku, _ in problems], ["SILKSC-STORMY"])
        self.assertEqual(labelmod.density_problems(run, self.stock), [],
                         "the stock we actually buy is fine")


class LabelViewTests(TestCase):
    """The page and the two PDFs."""

    def setUp(self):
        self.user = User.objects.create_superuser("labels", "l@example.test", "pw")
        self.client.force_login(self.user)
        self.stock = LabelStock.objects.get(name__startswith="Avery 5167")
        self.recipe = make_recipe("View Recipe")
        self.product = make_product(self.recipe, "View Product", with_image=False)
        self.product.sku, self.product.number_on_hand = "VIEW-ONE", 3
        self.product.save()

    def _params(self, **overrides):
        params = {
            "dataset": "inventory", "extra": "0",
            "stock": str(self.stock.pk), "start_at": "1",
        }
        params.update(overrides)
        return params

    def test_empty_page_renders_the_form(self):
        response = self.client.get(reverse("label_index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Start at label")

    def test_preview_reports_counts_before_anything_prints(self):
        response = self.client.get(reverse("label_index"), self._params(extra="2"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "VIEW-ONE")
        self.assertContains(response, "5 labels")   # 3 on hand + 2 extra

    def test_preview_shows_what_the_marker_will_say(self):
        response = self.client.get(reverse("label_index"), self._params())
        self.assertContains(response, "START AT 5")  # 3 labels, marker at 4

    def test_pdf_renders(self):
        response = self.client.get(reverse("label_pdf"), self._params())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_pdf_refuses_an_unreadable_run_instead_of_printing_it(self):
        narrow = make_stock(
            name="Too narrow", label_width_in=Decimal("1.0"), columns=7,
            margin_left_in=Decimal("0.45"), pitch_x_in=Decimal("1.1"),
        )
        self.product.sku = "SILKSC-STORMY"
        self.product.save()
        response = self.client.get(
            reverse("label_pdf"), self._params(stock=str(narrow.pk)), follow=True
        )
        self.assertContains(response, "too long for")
        self.assertNotEqual(response["Content-Type"], "application/pdf")

    def test_pdf_redirects_when_there_is_nothing_to_print(self):
        self.product.number_on_hand = 0
        self.product.save()
        response = self.client.get(reverse("label_pdf"), self._params(), follow=True)
        self.assertContains(response, "Nothing to print")

    def test_start_at_beyond_the_sheet_is_rejected(self):
        form = LabelRunForm(self._params(start_at="200"))
        self.assertFalse(form.is_valid())
        self.assertIn("start_at", form.errors)

    def test_since_dataset_needs_a_date(self):
        form = LabelRunForm(self._params(dataset="since", since=""))
        self.assertFalse(form.is_valid())
        self.assertIn("since", form.errors)

    def test_calibration_sheet_renders(self):
        response = self.client.get(
            reverse("label_calibration_pdf"), {"stock": self.stock.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))


class ContinuousRollTests(TestCase):
    """A thermal label printer is the same table with a 1 × 1 grid.

    Worth pinning because the sheet-oriented behaviours are actively wrong on
    a roll: a marker sticker would print after every run, cost a label, and be
    read by nobody.
    """

    def setUp(self):
        self.roll = make_stock(
            name="Rollo 2.25 x 1.25 roll",
            page_width_in=Decimal("2.25"), page_height_in=Decimal("1.25"),
            label_width_in=Decimal("2.25"), label_height_in=Decimal("1.25"),
            columns=1, rows=1,
            margin_left_in=Decimal("0"), margin_top_in=Decimal("0"),
            pitch_x_in=Decimal("2.25"), pitch_y_in=Decimal("1.25"),
        )
        recipe = make_recipe("Roll Recipe")
        self.product = make_product(recipe, "Roll Product", with_image=False)
        self.product.sku, self.product.number_on_hand = "ROLL-ONE", 5
        self.product.save()

    def test_a_one_by_one_stock_is_a_roll(self):
        self.assertTrue(self.roll.is_continuous)
        self.assertFalse(make_stock(name="Sheet").is_continuous)

    def test_no_marker_is_printed_on_a_roll(self):
        plan = labelmod.plan_sheets(self.roll, 5)
        self.assertIsNone(plan.marker_index)
        self.assertEqual(plan.sheet_count, 5, "one label per page")

    def test_one_label_per_page_at_the_media_size(self):
        run = labelmod.inventory_run()
        pdf = labelmod.render_run(run, self.roll)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(pdf.count(b"/Type /Page\n"), 5, "five labels, five pages")

    def test_a_roll_has_room_for_a_full_length_sku(self):
        _, mil = labelmod.barcode_for("SILKSC-STORMY", self.roll)
        self.assertGreater(mil, labelmod.MIN_MODULE_MIL)

    def test_geometry_still_has_to_close(self):
        self.assertEqual(self.roll.overflow_in(), (Decimal("0"), Decimal("0")))


class PrintShopTests(TestCase):
    """Printing at a shop rather than on a printer you own.

    Two assumptions break: you can't calibrate the machine beforehand, and you
    have no computer with you when the first sheet comes out wrong. So the
    offset has to be adjustable from the URL, and every sheet has to carry its
    own proof it wasn't scaled.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("shop", "s@example.test", "pw")
        self.client.force_login(self.user)
        self.stock = LabelStock.objects.get(name__startswith="Avery 5167")
        recipe = make_recipe("Shop Recipe")
        self.product = make_product(recipe, "Shop Product", with_image=False)
        self.product.sku, self.product.number_on_hand = "SHOP-ONE", 3
        self.product.save()

    def _params(self, **overrides):
        params = {
            "dataset": "inventory", "extra": "0",
            "stock": str(self.stock.pk), "start_at": "1",
        }
        params.update(overrides)
        return params

    def test_offset_can_be_overridden_from_the_query_string(self):
        form = LabelRunForm(self._params(x_offset_mm="1.5", y_offset_mm="-2"))
        self.assertTrue(form.is_valid(), form.errors)

        from scarves.views import _label_stock_from
        stock = _label_stock_from(form)
        self.assertEqual(stock.x_offset_mm, Decimal("1.5"))
        self.assertEqual(stock.y_offset_mm, Decimal("-2"))

    def test_an_override_is_never_written_back_to_the_stock(self):
        """A correction for one shop's machine on one day is not a property
        of the paper."""
        form = LabelRunForm(self._params(x_offset_mm="3"))
        self.assertTrue(form.is_valid(), form.errors)

        from scarves.views import _label_stock_from
        _label_stock_from(form)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.x_offset_mm, Decimal("0"))

    def test_a_blank_override_keeps_the_saved_offset(self):
        LabelStock.objects.filter(pk=self.stock.pk).update(x_offset_mm=Decimal("1"))
        form = LabelRunForm(self._params(x_offset_mm="", y_offset_mm=""))
        self.assertTrue(form.is_valid(), form.errors)

        from scarves.views import _label_stock_from
        self.assertEqual(_label_stock_from(form).x_offset_mm, Decimal("1"))

    def test_the_override_actually_moves_the_labels(self):
        plain = self.client.get(reverse("label_pdf"), self._params())
        nudged = self.client.get(reverse("label_pdf"), self._params(x_offset_mm="2"))
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(nudged.status_code, 200)
        self.assertNotEqual(plain.content, nudged.content)

    def test_absurd_offsets_are_refused(self):
        self.assertFalse(LabelRunForm(self._params(x_offset_mm="50")).is_valid())

    def test_every_sheet_carries_a_scale_check(self):
        """Catches a print dialog left on 'fit to page' — the failure that
        looks fine at the top of the sheet and cuts through labels at the
        bottom."""
        self.product.number_on_hand = 100   # spills onto a second sheet
        self.product.save()
        pdf = self.client.get(reverse("label_pdf"), self._params()).content
        text = _pdf_text(pdf)
        self.assertEqual(pdf.count(b"/Type /Page\n"), 2)
        self.assertEqual(text.count("must line up with a row of die-cuts"), 2,
                         "one check per sheet, not just the last")

    def test_the_check_spans_the_whole_sheet_not_one_inch(self):
        """The point of using the die-cuts: a 1in reference at 98% scale is
        out by half a millimetre and unreadable, while the same error over the
        full column stack is five and obvious. One tick per row, top to
        bottom, in the left margin."""
        pdf = self.client.get(reverse("label_pdf"), self._params()).content
        ticks = _pdf_lines(pdf, x_max=20)
        ys = sorted({round(y, 1) for _, y in ticks})
        self.assertGreaterEqual(len(ys), self.stock.rows,
                                "a tick for every row of die-cuts")

        expected_top = labelmod.slot_for(self.stock, 0).y + labelmod._pt(
            self.stock.label_height_in)
        expected_bottom = labelmod.slot_for(
            self.stock, (self.stock.rows - 1) * self.stock.columns).y
        self.assertAlmostEqual(max(ys), expected_top, delta=0.5)
        self.assertAlmostEqual(min(ys), expected_bottom, delta=0.5)

    def test_the_scale_check_stays_off_the_labels(self):
        """It's diagnostics, not product — it belongs on the liner."""
        pdf = self.client.get(reverse("label_pdf"), self._params()).content
        first_column_x = labelmod._pt(self.stock.margin_left_in)
        for x, _ in _pdf_lines(pdf, x_max=20):
            self.assertLess(x, first_column_x,
                            "ticks must stay left of the first label column")

    def test_the_scale_check_names_the_offset_in_use(self):
        """So a reprint at a different nudge is tellable from the one before."""
        pdf = self.client.get(reverse("label_pdf"), self._params(x_offset_mm="1.5")).content
        self.assertIn("nudge 1.5/0", _pdf_text(pdf))

    def test_a_roll_gets_no_ruler(self):
        """The page is the label; there's no margin to put one in."""
        roll = make_stock(
            name="Roll", page_width_in=Decimal("2.25"), page_height_in=Decimal("1.25"),
            label_width_in=Decimal("2.25"), label_height_in=Decimal("1.25"),
            columns=1, rows=1, margin_left_in=Decimal("0"), margin_top_in=Decimal("0"),
            pitch_x_in=Decimal("2.25"), pitch_y_in=Decimal("1.25"),
        )
        pdf = self.client.get(reverse("label_pdf"), self._params(stock=str(roll.pk))).content
        self.assertNotIn("must measure exactly 1 inch", _pdf_text(pdf))


class CalibrationSheetTests(TestCase):
    """The plain-paper sheet that makes a first trip to the print shop enough."""

    def setUp(self):
        self.user = User.objects.create_superuser("cal", "c@example.test", "pw")
        self.client.force_login(self.user)
        self.stock = LabelStock.objects.get(name__startswith="Avery 5167")

    def _pdf(self, stock=None):
        return self.client.get(
            reverse("label_calibration_pdf"), {"stock": (stock or self.stock).pk}
        ).content

    def test_it_answers_orientation_before_millimetres(self):
        """Four ways a sheet goes into a bypass tray, three of them wrong. A
        180° error reads as a plausible offset and sends you chasing a nudge
        that never converges, so this has to be settled first."""
        text = _pdf_text(self._pdf())
        self.assertIn("TOP OF PRINT", text)
        self.assertIn("Write TOP and a feed arrow", text)

    def test_the_top_banner_is_actually_at_the_top(self):
        """Stating the top edge somewhere other than the top would be worse
        than not stating it."""
        first_row_top = (
            labelmod.slot_for(self.stock, 0).y
            + labelmod._pt(self.stock.label_height_in)
        )
        page_w = labelmod._pt(self.stock.page_width_in)
        page_h = labelmod._pt(self.stock.page_height_in)

        banner = [i for i in _pdf_text_items(self._pdf()) if "TOP OF PRINT" in i[2]]
        self.assertEqual(len(banner), 1)
        x, y, _ = banner[0]
        self.assertGreater(y, first_row_top, "banner sits in the top margin")
        self.assertLess(y, page_h)
        self.assertAlmostEqual(x, page_w / 2, delta=60, msg="roughly centred")

    def test_it_carries_a_millimetre_vernier(self):
        text = _pdf_text(self._pdf())
        self.assertIn("read the die-cut corner", text)
        for label in ("+1", "-1", "+3", "-3"):
            self.assertIn(label, text)

    def test_it_says_which_scale_is_which_axis(self):
        """Inferring it from the geometry is possible and takes a moment
        nobody has at a print counter; guessing the sign doubles the error."""
        text = _pdf_text(self._pdf())
        self.assertIn('TOP scale = "nudge right" mm', text)
        self.assertIn('LEFT scale = "nudge up" mm', text)
        self.assertIn("no sign to flip", text)

    def test_the_horizontal_scale_is_above_and_the_vertical_one_left(self):
        """What the caption claims has to match where the ink actually is."""
        corner = labelmod.slot_for(self.stock, 0)
        ox = corner.x
        oy = corner.y + labelmod._pt(self.stock.label_height_in)

        numbered = [i for i in _pdf_text_items(self._pdf())
                    if i[2] in ("+1", "-1", "+2", "-2", "+3", "-3")]
        self.assertEqual(len(numbered), 12, "six labels on each of two scales")

        # Tell the scales apart by which axis they hold constant, not by
        # position — the left scale's positive labels also sit above the
        # corner, so a position filter catches both.
        from collections import Counter

        # The horizontal scale is centred on each tick, so all six share one
        # baseline exactly. Group on that; the vertical scale's x values are
        # *not* uniform, because it's right-aligned and Helvetica's "+" is
        # wider than its "-".
        rows = Counter(round(i[1]) for i in numbered)
        row_y, row_n = rows.most_common(1)[0]
        self.assertEqual(row_n, 6, "six labels sharing one baseline")

        across = [i for i in numbered if round(i[1]) == row_y]
        down = [i for i in numbered if round(i[1]) != row_y]
        self.assertEqual(len(down), 6)

        # Swapped, the sheet would be perfectly readable and wrong.
        self.assertGreater(row_y, oy, "the 'nudge right' scale sits above")
        self.assertEqual(len({round(i[0]) for i in across}), 6,
                         "the top scale steps sideways")
        self.assertEqual(len({round(i[1]) for i in down}), 6,
                         "the left scale steps vertically")
        for x, _, _ in down:
            self.assertLess(x, ox, "the 'nudge up' scale sits to the left")

    def test_every_label_position_is_numbered(self):
        """So a marker sticker reading 57 can be found on the sheet."""
        text = _pdf_text(self._pdf())
        for n in ("1", "40", "80"):
            self.assertIn(n, text)

    def test_a_stock_with_no_top_margin_skips_the_banner(self):
        roll = make_stock(
            name="Roll", page_width_in=Decimal("2.25"), page_height_in=Decimal("1.25"),
            label_width_in=Decimal("2.25"), label_height_in=Decimal("1.25"),
            columns=1, rows=1, margin_left_in=Decimal("0"), margin_top_in=Decimal("0"),
            pitch_x_in=Decimal("2.25"), pitch_y_in=Decimal("1.25"),
        )
        self.assertNotIn("TOP OF PRINT", _pdf_text(self._pdf(roll)))


class LabelFormFieldVisibilityTests(TestCase):
    """Controls the chosen dataset doesn't read are hidden client-side.

    A date box that changes nothing is worse than no date box — it looks like
    it's filtering and quietly isn't. The toggling itself is JS, so what's
    checked here is the markup it hangs off: if a field loses its `data-when`
    in a refactor, it silently becomes a dead control again.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("vis", "v@example.test", "pw")
        self.client.force_login(self.user)

    def test_each_dataset_only_field_is_tagged(self):
        html = self.client.get(reverse("label_index")).content.decode()

        for field, dataset in (
            ("since", "since"),
            ("category", "inventory"),
            ("raw_products", "inventory"),
            ("include_zero", "inventory"),
        ):
            with self.subTest(field=field):
                block = re.search(
                    r'<div class="field" data-when="(\w+)">(?:(?!</div>).)*?'
                    r'id_' + field,
                    html, re.S,
                )
                self.assertIsNotNone(block, f"{field} is not tagged data-when")
                self.assertEqual(block.group(1), dataset)

    def test_fields_every_dataset_reads_are_not_tagged(self):
        """stock and start_at apply to any run and must always show."""
        html = self.client.get(reverse("label_index")).content.decode()
        for field in ("stock", "start_at"):
            with self.subTest(field=field):
                block = re.search(
                    r'<div class="field"( data-when="\w+")?>'
                    r'(?:(?!</div>).)*?id_' + field,
                    html, re.S,
                )
                self.assertIsNotNone(block)
                self.assertIsNone(block.group(1), f"{field} must not be hidden")

    def test_hidden_fields_still_submit_and_are_ignored(self):
        """They stay in the DOM, so the view has to tolerate values the
        dataset doesn't use rather than erroring on them."""
        response = self.client.get(reverse("label_index"), {
            "dataset": "since",
            "since": (timezone.localdate() - timedelta(days=7)).isoformat(),
            "extra": "0",
            "stock": str(LabelStock.objects.first().pk),
            "start_at": "1",
            "include_zero": "on",      # inventory-only, sent anyway
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].is_valid(),
                        response.context["form"].errors)


class BlankFilterTests(TestCase):
    """Narrowing a bulk re-label to particular blanks."""

    def setUp(self):
        recipe = make_recipe("Blank Filter Recipe")
        self.products = {}
        for name in ("Scarf", "Skein", "Runner"):
            product = make_product(recipe, f"{name} Product", with_image=False)
            product.raw_product.name = f"raw-{name}"
            product.raw_product.save()
            product.sku, product.number_on_hand = f"{name[:4].upper()}-ONE", 2
            product.save()
            self.products[name] = product

    def test_no_ticks_means_every_blank(self):
        """Not 'none' — a filter that silently prints nothing when you forget
        to tick is a wasted trip to the print shop."""
        self.assertEqual(len(labelmod.inventory_run().rows), 3)
        self.assertEqual(len(labelmod.inventory_run(raw_products=[]).rows), 3)
        self.assertEqual(len(labelmod.inventory_run(raw_products=None).rows), 3)

    def test_several_blanks_can_be_picked_at_once(self):
        picked = [self.products[n].raw_product for n in ("Scarf", "Runner")]
        run = labelmod.inventory_run(raw_products=picked)
        self.assertEqual(
            sorted(r.product.sku for r in run.rows), ["RUNN-ONE", "SCAR-ONE"]
        )

    def test_one_blank_still_works(self):
        run = labelmod.inventory_run(raw_products=[self.products["Skein"].raw_product])
        self.assertEqual([r.product.sku for r in run.rows], ["SKEI-ONE"])

    def test_the_page_renders_them_as_checkboxes(self):
        user = User.objects.create_superuser("blank", "b@example.test", "pw")
        self.client.force_login(user)
        html = self.client.get(reverse("label_index")).content.decode()
        self.assertIn('type="checkbox" name="raw_products"', html)
        self.assertEqual(html.count('name="raw_products"'), 3)

    def test_picking_blanks_through_the_view(self):
        user = User.objects.create_superuser("blank2", "b2@example.test", "pw")
        self.client.force_login(user)
        picked = [self.products[n].raw_product.pk for n in ("Scarf", "Runner")]
        response = self.client.get(reverse("label_index"), {
            "dataset": "inventory", "extra": "0",
            "stock": str(LabelStock.objects.first().pk), "start_at": "1",
            "raw_products": [str(pk) for pk in picked],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sorted(r.product.sku for r in response.context["run"].rows),
            ["RUNN-ONE", "SCAR-ONE"],
        )


class SpecificItemsTests(TestCase):
    """The hand-picked run: exactly these products, exactly these counts.

    Built because deferring it was the reliable way to need it. The shape is
    additive — type three SKUs — rather than unticking 297 rows off a filter.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("pick", "p@example.test", "pw")
        self.client.force_login(self.user)
        recipe = make_recipe("Picked Recipe")
        self.a = make_product(recipe, "Picked A", with_image=False)
        self.b = make_product(recipe, "Picked B", with_image=False)
        self.a.sku, self.a.number_on_hand = "PICK-AAA", 99
        self.a.save()
        self.b.sku, self.b.number_on_hand = "PICK-BBB", 0
        self.b.save()

    def _params(self, items, **overrides):
        params = {
            "dataset": "items", "extra": "0",
            "stock": str(LabelStock.objects.first().pk), "start_at": "1",
            "items": items,
        }
        params.update(overrides)
        return params

    def test_it_prints_exactly_what_was_asked_for(self):
        run = labelmod.specific_items([(self.a, 3), (self.b, 1)])
        self.assertEqual(run.total, 4)
        self.assertEqual([(r.product.sku, r.quantity) for r in run.rows],
                         [("PICK-AAA", 3), ("PICK-BBB", 1)])

    def test_on_hand_is_irrelevant(self):
        """Unlike the bulk runs — you asked for it, so it prints."""
        run = labelmod.specific_items([(self.b, 5)])
        self.assertEqual(run.total, 5)

    def test_extras_do_not_apply(self):
        """The bulk datasets add spares because their counts are derived.
        Here somebody typed the number, so adding to it would surprise."""
        response = self.client.get(
            reverse("label_index"), self._params([f"{self.a.pk}:3"], extra="5")
        )
        self.assertEqual(response.context["run"].total, 3)

    def test_the_same_item_twice_sums(self):
        """Two rows for one SKU is a sum written confusingly."""
        form = LabelRunForm(self._params([f"{self.a.pk}:2", f"{self.a.pk}:3"]))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["items"], [(self.a, 5)])

    def test_a_run_survives_as_a_url(self):
        """The whole point of keeping it in the query string: reopen it."""
        params = self._params([f"{self.a.pk}:2", f"{self.b.pk}:1"])
        response = self.client.get(reverse("label_index"), params)
        self.assertEqual(response.context["run"].total, 3)

        pdf = self.client.get(reverse("label_pdf"), params)
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(_pdf_text(pdf.content).count("PICK-AAA"), 2)
        self.assertEqual(_pdf_text(pdf.content).count("PICK-BBB"), 1)

    def test_picking_nothing_is_an_error_not_an_empty_sheet(self):
        form = LabelRunForm(self._params([]))
        self.assertFalse(form.is_valid())
        self.assertIn("items", form.errors)

    def test_absurd_counts_are_refused(self):
        self.assertFalse(LabelRunForm(self._params([f"{self.a.pk}:400"])).is_valid())
        self.assertFalse(LabelRunForm(self._params([f"{self.a.pk}:0"])).is_valid())

    def test_a_deleted_item_is_reported_not_skipped(self):
        form = LabelRunForm(self._params(["99999:2"]))
        self.assertFalse(form.is_valid())
        self.assertIn("no longer exist", str(form.errors["items"]))

    def test_the_list_survives_an_unrelated_error(self):
        """A bad start row must not wipe a list somebody just built by hand."""
        form = LabelRunForm(self._params([f"{self.a.pk}:2"], start_at="900"))
        self.assertFalse(form.is_valid())
        self.assertEqual(form.items_value, [(self.a, 2)])

    def test_the_page_renders_picked_rows_back(self):
        response = self.client.get(
            reverse("label_index"), self._params([f"{self.a.pk}:4"])
        )
        html = response.content.decode()
        self.assertIn('data-pk="%d"' % self.a.pk, html)
        self.assertIn('value="4"', html)


class LabelItemSearchTests(TestCase):
    """The type-ahead, reusing the upload page's endpoint."""

    def setUp(self):
        self.user = User.objects.create_superuser("srch", "s@example.test", "pw")
        self.client.force_login(self.user)
        recipe = make_recipe("Search Recipe")
        self.withsku = make_product(recipe, "Stormy Silk", with_image=False)
        self.withsku.sku = "SILKSC-STORMY"
        self.withsku.save()
        self.nosku = make_product(recipe, "Stormy Unlabelled", with_image=False)
        # Predates SKU-on-create; only a queryset write can make one now.
        FinishedProduct.objects.filter(pk=self.nosku.pk).update(sku="")

    def test_it_finds_by_name_and_by_sku(self):
        for q in ("Stormy", "SILKSC"):
            response = self.client.get(
                reverse("product_search"), {"q": q, "mode": "labels"}
            )
            self.assertContains(response, "SILKSC-STORMY")

    def test_products_without_a_sku_are_shown_but_unpickable(self):
        """Filtering them out silently means someone searches, doesn't see
        their product, and has no idea why. Shown and disabled says both that
        it exists and what to do about it."""
        response = self.client.get(
            reverse("product_search"), {"q": "Stormy", "mode": "labels"}
        )
        self.assertContains(response, "Stormy Unlabelled")
        self.assertContains(response, "run generate_skus")

        html = response.content.decode()
        block = re.search(r"<button[^>]*>\s*Stormy Unlabelled.*?</button>", html, re.S)
        self.assertIsNotNone(block)
        self.assertIn("disabled", block.group(0))
        self.assertNotIn("data-pk", block.group(0),
                         "a disabled result must carry nothing the adder can use")

    def test_the_upload_picker_still_offers_them(self):
        """That flow assigns a photo and doesn't care about barcodes."""
        response = self.client.get(
            reverse("product_search"), {"q": "Stormy", "upload_id": "1"}
        )
        self.assertContains(response, "Stormy Unlabelled")

    def test_results_carry_what_a_row_needs(self):
        response = self.client.get(
            reverse("product_search"), {"q": "Stormy", "mode": "labels"}
        )
        self.assertContains(response, 'data-pk="%d"' % self.withsku.pk)
        self.assertContains(response, 'data-sku="SILKSC-STORMY"')

    def test_it_needs_a_login(self):
        self.client.logout()
        response = self.client.get(reverse("product_search"), {"q": "x"})
        self.assertEqual(response.status_code, 302)


class GenerateSkusOverwriteTests(TestCase):
    """`--overwrite` got dangerous the day labels became printable.

    A SKU in the database is an edit; a SKU on a sticker stuck to a scarf, and
    in Square's catalogue, is neither. Regenerating orphans both, and the
    symptom is an item scanning to nothing at the till weeks later.
    """

    def setUp(self):
        recipe = make_recipe("SKU Recipe")
        self.existing = make_product(recipe, "Has A Sku", with_image=False)
        self.existing.sku = "PRINTED-CODE"
        self.existing.save()
        self.blank = make_product(recipe, "Needs A Sku", with_image=False)
        FinishedProduct.objects.filter(pk=self.blank.pk).update(sku="")
        self.blank.refresh_from_db()

    def _run(self, **kwargs):
        out = StringIO()
        call_command("generate_skus", stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_a_plain_run_only_fills_blanks(self):
        self._run()
        self.existing.refresh_from_db()
        self.blank.refresh_from_db()
        self.assertEqual(self.existing.sku, "PRINTED-CODE", "never touched")
        self.assertTrue(self.blank.sku)

    def test_overwrite_aborts_unless_confirmed(self):
        with mock.patch("builtins.input", return_value="no"):
            output = self._run(overwrite=True)
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.sku, "PRINTED-CODE")
        self.assertIn("Aborted", output)

    def test_overwrite_says_what_it_will_break(self):
        with mock.patch("builtins.input", return_value="no"):
            output = self._run(overwrite=True)
        self.assertIn("1 SKU(s)", output)
        self.assertIn("scan to nothing", output)

    def test_overwrite_proceeds_once_confirmed(self):
        with mock.patch("builtins.input", return_value="yes"):
            self._run(overwrite=True)
        self.existing.refresh_from_db()
        self.assertNotEqual(self.existing.sku, "PRINTED-CODE")

    def test_noinput_skips_the_prompt(self):
        """For scripts — but it still prints the warning."""
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            output = self._run(overwrite=True, interactive=False)
        self.existing.refresh_from_db()
        self.assertNotEqual(self.existing.sku, "PRINTED-CODE")
        self.assertIn("scan to nothing", output)

    def test_no_prompt_when_nothing_is_at_risk(self):
        FinishedProduct.objects.update(sku="")
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            self._run(overwrite=True)
        self.blank.refresh_from_db()
        self.assertTrue(self.blank.sku)


class SkuOnCreateTests(TestCase):
    """SKUs are assigned when a product is created, not when someone
    remembers to run a command.

    Generation used to live only in `generate_skus`, so anything made through
    the admin, the bulk matrix or a shell had no barcode — and nothing said
    so. It simply wasn't printable and wasn't scannable.
    """

    def setUp(self):
        self.recipe = make_recipe("Sunset Glow")
        self.category, _ = RawProductCategory.objects.get_or_create(name="Silk")

    def _raw(self, name):
        raw, _ = RawProduct.objects.get_or_create(
            name=name, category=self.category, defaults={"price": "5.00"}
        )
        return raw

    def test_a_new_product_gets_a_sku(self):
        fp = FinishedProduct.objects.create(
            name="Sunset Silk", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00",
        )
        self.assertEqual(fp.sku, "SILKSC-SUNSET")

    def test_it_survives_a_reload(self):
        """Set in memory but not persisted would be the subtle version."""
        fp = FinishedProduct.objects.create(
            name="Sunset Silk", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00",
        )
        fp.refresh_from_db()
        self.assertEqual(fp.sku, "SILKSC-SUNSET")

    def test_an_explicit_sku_is_respected(self):
        fp = FinishedProduct.objects.create(
            name="Sunset Silk", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00", sku="HAND-PICKED",
        )
        self.assertEqual(fp.sku, "HAND-PICKED")

    def test_an_existing_sku_is_never_rewritten(self):
        """It's on stickers and in Square; this app can rewrite neither."""
        fp = FinishedProduct.objects.create(
            name="Sunset Silk", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00",
        )
        original = fp.sku
        fp.raw_product.name = "Completely Different Blank"
        fp.raw_product.save()
        fp.price = "35.00"
        fp.save()
        fp.refresh_from_db()
        self.assertEqual(fp.sku, original)

    def test_collisions_get_a_suffix(self):
        first = FinishedProduct.objects.create(
            name="One", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00",
        )
        second = FinishedProduct.objects.create(
            name="Two", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00",
        )
        self.assertEqual(first.sku, "SILKSC-SUNSET")
        self.assertEqual(second.sku, "SILKSC-SUNSET2")

    def test_update_fields_still_persists_a_generated_sku(self):
        """A caller narrowing the write didn't know a SKU was coming; without
        adding it the value is set in memory and silently dropped."""
        fp = FinishedProduct.objects.create(
            name="Sunset Silk", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00",
        )
        FinishedProduct.objects.filter(pk=fp.pk).update(sku="")
        fp.refresh_from_db()
        self.assertEqual(fp.sku, "")

        fp.number_on_hand = 7
        fp.save(update_fields=["number_on_hand"])
        fp.refresh_from_db()
        self.assertEqual(fp.number_on_hand, 7)
        self.assertTrue(fp.sku, "the generated SKU has to reach the database")

    def test_the_command_and_save_agree(self):
        """One definition of a SKU, used by both paths."""
        fp = FinishedProduct.objects.create(
            name="Sunset Silk", raw_product=self._raw("Silk Scarf"),
            recipe=self.recipe, price="30.00",
        )
        by_save = fp.sku
        FinishedProduct.objects.filter(pk=fp.pk).update(sku="")
        call_command("generate_skus", stdout=StringIO())
        fp.refresh_from_db()
        self.assertEqual(fp.sku, by_save)


class FixtureSkuTests(TestCase):
    """`loaddata` must not invent SKUs.

    It goes through `save_base(raw=True)` rather than `save()`, so a fixture
    that deliberately carries a blank SKU stays blank. Pinned because the
    alternative — fixtures quietly gaining generated values — would make
    `diff_fixture` round-trips report changes nobody made.
    """

    def test_a_blank_sku_in_a_fixture_stays_blank(self):
        from django.core import serializers

        recipe = make_recipe("Fixture Recipe")
        product = make_product(recipe, "Fixture Product", with_image=False)
        self.assertTrue(product.sku, "created normally, so it has one")

        payload = serializers.serialize("json", [product])
        payload = payload.replace(f'"sku": "{product.sku}"', '"sku": ""')

        for deserialized in serializers.deserialize("json", payload):
            deserialized.save()          # the path loaddata uses

        product.refresh_from_db()
        self.assertEqual(product.sku, "", "loaddata must not generate one")


class FakeSquareResult:
    """Stands in for the SDK's ApiResponse."""

    def __init__(self, body=None, errors=None):
        self.body = body or {}
        self.errors = errors or []

    def is_error(self):
        return bool(self.errors)

    def is_success(self):
        return not self.errors


class FakeSquareClient:
    """Records what the command would send, and replays canned responses.

    Deliberately dumb: the point is to exercise our payload building and our
    write-back, not to reimplement Square. Anything it can't answer honestly
    it refuses to answer at all.
    """

    def __init__(self, upsert_results=None, retrieve_result=None,
                 inventory_result=None, locations_result=None,
                 image_results=None, retrieve_results=None):
        self.upserts = []
        self.retrieves = []
        self.inventory_changes = []
        self.images = []
        self._image_results = list(image_results or [])
        self._image_seq = 0
        self._upsert_results = list(upsert_results or [])
        self._retrieve_result = retrieve_result or FakeSquareResult({"objects": []})
        # A run can read Square more than once — `--update` reads versions and
        # the ordering pass then reads whole items — so a canned sequence is
        # sometimes needed where one answer used to do.
        self._retrieve_results = list(retrieve_results or [])
        self._inventory_result = inventory_result or FakeSquareResult()
        self._locations_result = locations_result or FakeSquareResult(
            {"locations": [{"id": "LOC123"}]}
        )
        self.catalog = self._Catalog(self)
        self.inventory = self._Inventory(self)
        self.locations = self._Locations(self)

    class _Catalog:
        def __init__(self, outer):
            self.outer = outer

        def batch_upsert_catalog_objects(self, body):
            self.outer.upserts.append(body)
            if self.outer._upsert_results:
                return self.outer._upsert_results.pop(0)
            return FakeSquareResult({"id_mappings": []})

        def batch_retrieve_catalog_objects(self, body):
            self.outer.retrieves.append(body)
            if self.outer._retrieve_results:
                return self.outer._retrieve_results.pop(0)
            return self.outer._retrieve_result

        def create_catalog_image(self, request, image_file):
            # The bytes are read here rather than kept, because the thing
            # worth asserting is that a real file reached the call at all —
            # the bucket read is the step most likely to be silently skipped.
            self.outer.images.append((request, image_file.read()))
            if self.outer._image_results:
                return self.outer._image_results.pop(0)
            self.outer._image_seq += 1
            return FakeSquareResult({
                "image": {"id": f"SQ_IMG_{self.outer._image_seq}"},
            })

    class _Inventory:
        def __init__(self, outer):
            self.outer = outer

        def batch_change_inventory(self, body):
            self.outer.inventory_changes.append(body)
            return self.outer._inventory_result

    class _Locations:
        def __init__(self, outer):
            self.outer = outer

        def list_locations(self):
            return self.outer._locations_result


@override_settings(
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_LOCATION_ID="LOC123",
    SQUARE_ENVIRONMENT="sandbox",
)
class SyncToSquareTests(TestCase):
    """`sync_to_square` had no test coverage at all.

    It can't be exercised against the real Square API from here, so these
    drive the command with a stand-in client: what matters is that the payload
    we build is the payload we meant, and that IDs coming back get written to
    the right rows. A wrong payload is not a crash — it's a catalogue that
    quietly disagrees with the shop.
    """

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_product(self.recipe, "Stormy Silk", with_image=False)
        self.product.number_on_hand = 6
        self.product.price = Decimal("32.00")
        self.product.save()
        self.raw = self.product.raw_product

    def _run(self, client, **kwargs):
        out, err = StringIO(), StringIO()
        with mock.patch("square.client.Client", return_value=client):
            call_command("sync_to_square", stdout=out, stderr=err, **kwargs)
        return out.getvalue() + err.getvalue()

    # --- new catalogue ---------------------------------------------------

    def test_a_new_raw_product_is_sent_as_an_item_with_its_variations(self):
        client = FakeSquareClient()
        self._run(client)

        self.assertEqual(len(client.upserts), 1)
        objects = client.upserts[0]["batches"][0]["objects"]
        self.assertEqual(len(objects), 1)

        item = objects[0]
        self.assertEqual(item["type"], "ITEM")
        self.assertEqual(item["id"], f"#rp_{self.raw.pk}")
        self.assertEqual(item["item_data"]["name"], self.raw.name)

        variation = item["item_data"]["variations"][0]
        self.assertEqual(variation["id"], f"#fp_{self.product.pk}")
        self.assertEqual(variation["item_variation_data"]["name"], self.recipe.name)
        self.assertEqual(
            variation["item_variation_data"]["price_money"],
            {"amount": 3200, "currency": "USD"},
        )

    def test_the_sku_now_always_rides_along(self):
        """Since SKUs are assigned at creation, a variation can no longer
        reach Square with nothing to scan."""
        client = FakeSquareClient()
        self._run(client)
        variation = client.upserts[0]["batches"][0]["objects"][0]["item_data"]["variations"][0]
        self.assertEqual(
            variation["item_variation_data"]["sku"], self.product.sku
        )
        self.assertTrue(self.product.sku)

    def test_returned_ids_are_written_back(self):
        """Without this the next run creates everything a second time."""
        client = FakeSquareClient(upsert_results=[FakeSquareResult({
            "id_mappings": [
                {"client_object_id": f"#rp_{self.raw.pk}", "object_id": "SQ_ITEM"},
                {"client_object_id": f"#fp_{self.product.pk}", "object_id": "SQ_VAR"},
            ],
        })])
        self._run(client)

        self.raw.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.raw.square_item_id, "SQ_ITEM")
        self.assertEqual(self.product.square_variation_id, "SQ_VAR")

    def test_an_already_linked_item_sends_only_the_new_variation(self):
        RawProduct.objects.filter(pk=self.raw.pk).update(square_item_id="SQ_ITEM")
        client = FakeSquareClient()
        self._run(client)

        obj = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(obj["type"], "ITEM_VARIATION")
        self.assertEqual(obj["item_variation_data"]["item_id"], "SQ_ITEM")

    def test_a_fully_linked_catalogue_skips_the_upsert(self):
        RawProduct.objects.filter(pk=self.raw.pk).update(square_item_id="SQ_ITEM")
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        client = FakeSquareClient()
        output = self._run(client)
        self.assertEqual(client.upserts, [])
        self.assertIn("Nothing new to sync", output)
        self.assertEqual(len(client.inventory_changes), 1, "still pushes stock")

    # --- inventory -------------------------------------------------------

    def test_inventory_is_pushed_for_linked_variations(self):
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        client = FakeSquareClient()
        self._run(client, inventory_only=True)

        self.assertEqual(client.upserts, [], "--inventory-only skips catalogue")
        change = client.inventory_changes[0]["changes"][0]
        self.assertEqual(change["type"], "PHYSICAL_COUNT")
        self.assertEqual(change["physical_count"]["catalog_object_id"], "SQ_VAR")
        self.assertEqual(change["physical_count"]["location_id"], "LOC123")
        self.assertEqual(change["physical_count"]["quantity"], "6")

    def test_unlinked_products_are_left_out_of_the_inventory_push(self):
        client = FakeSquareClient()
        output = self._run(client, inventory_only=True)
        self.assertEqual(client.inventory_changes, [])
        self.assertIn("No variations with Square IDs", output)

    # --- failure ---------------------------------------------------------

    def test_a_catalogue_error_stops_before_writing_ids_or_stock(self):
        client = FakeSquareClient(upsert_results=[FakeSquareResult(
            errors=[{"category": "API_ERROR", "detail": "boom"}]
        )])
        with self.assertRaises(CommandError) as caught:
            self._run(client)

        self.raw.refresh_from_db()
        self.assertEqual(self.raw.square_item_id, "")
        self.assertEqual(client.inventory_changes, [],
                         "a failed catalogue must not be followed by a stock push")
        self.assertIn("boom", str(caught.exception))

    def test_an_inventory_error_is_reported(self):
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        client = FakeSquareClient(inventory_result=FakeSquareResult(
            errors=[{"category": "API_ERROR", "detail": "stock boom"}]
        ))
        with self.assertRaises(CommandError) as caught:
            self._run(client, inventory_only=True)
        self.assertIn("stock boom", str(caught.exception))

    # --- --update --------------------------------------------------------

    def test_update_sends_current_price_and_the_version_square_gave_us(self):
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        RawProduct.objects.filter(pk=self.raw.pk).update(square_item_id="SQ_ITEM")
        client = FakeSquareClient(retrieve_result=FakeSquareResult({
            "objects": [{"id": "SQ_VAR", "version": 42}],
        }))
        self._run(client, update=True)

        self.assertEqual(client.retrieves[0]["object_ids"], ["SQ_VAR"])
        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(sent["version"], 42)
        self.assertEqual(sent["item_variation_data"]["price_money"]["amount"], 3200)
        self.assertEqual(sent["item_variation_data"]["sku"], self.product.sku)


class EdgesMatchTheRuleTests(TestCase):
    """`HUE_EDGES` restates numbers that live as literals in `band_for_hsl`.

    Threading seven constants through that function would cost more clarity
    than it buys, so the list is pinned by probing the classifier either side
    of each line instead. That tests the real thing rather than restating it:
    if somebody moves a boundary in the rule and not in the list, the page
    starts drawing its line in the wrong place and this fails.
    """

    def test_every_listed_edge_is_a_real_edge(self):
        from .colorbands import EDGE_PROBE, HUE_EDGES, band_for_hsl

        sat, light = EDGE_PROBE
        for slug, degrees in HUE_EDGES:
            if slug == "pink-red":
                # Real in the rule, invisible at any single lightness: the two
                # zones differ only in how pale a colour must be to read pink.
                continue
            with self.subTest(slug):
                self.assertNotEqual(
                    band_for_hsl(degrees - 0.5, sat, light),
                    band_for_hsl(degrees + 0.5, sat, light),
                    f"{slug} at {degrees} divides nothing",
                )

    def test_the_probe_avoids_the_brown_and_grey_rules(self):
        """A duller or darker probe gets caught by them first and would label
        half the lines 'brown', which is true of the probe and not of the line."""
        from .colorbands import EDGE_PROBE, HUE_EDGES, band_for_hsl

        sat, light = EDGE_PROBE
        for _slug, degrees in HUE_EDGES:
            for h in (degrees - 0.5, degrees + 0.5):
                self.assertNotIn(band_for_hsl(h, sat, light), ("brown", "grey", "black"))


class ColorBandsPageTests(TestCase):
    """The public piece about the classifier.

    Almost all template, so the things worth testing are the two reasons it
    isn't a static file: the dyes are live, and the boundary is read from the
    code rather than typed into the prose.
    """

    def setUp(self):
        brand, _ = DyeBrand.objects.get_or_create(name="Dharma Acid Dyes")
        self.avocado = Dye.objects.create(
            name="461 Avocado", brand=brand, hex_color="#6f752c"
        )
        Dye.objects.create(name="445 Fluor. Lemon", brand=brand, hex_color="#ffff00")

    def test_anyone_can_read_it(self):
        response = self.client.get(reverse("color_bands_page"))
        self.assertEqual(response.status_code, 200)

    def test_each_dye_carries_the_band_python_gave_it(self):
        """The slider reclassifies in the browser, so the page must ship the
        real answer alongside — otherwise an exploration reads as what the app
        does."""
        from .colorbands import band_for_hex

        response = self.client.get(reverse("color_bands_page"))
        rows = {d["name"]: d for d in response.context["dyes_json"]}
        for name, row in rows.items():
            self.assertEqual(row["band"], band_for_hex(row["hex"]), name)
        self.assertEqual(rows["461 Avocado"]["band"], "green")

    def test_the_boundaries_come_from_the_code(self):
        """A page quoting 70 after somebody moved it to 61 would be worse than
        no page at all."""
        from .colorbands import YELLOW_ENDS

        response = self.client.get(reverse("color_bands_page"))
        offered = response.context["edges_json"]
        self.assertEqual(offered["yellow-green"], YELLOW_ENDS)

    def test_which_boundary_rides_in_the_query_string(self):
        """So a particular argument is a link somebody can send, rather than a
        state of the session they happen to be in."""
        response = self.client.get(reverse("color_bands_page"), {"edge": "green-blue"})
        self.assertEqual(response.context["edge"]["slug"], "green-blue")

    def test_it_opens_on_the_argument_people_actually_have(self):
        """Chartreuse and avocado are the jars two reasonable people fall out
        over. Red/orange is first only because it is first round the wheel."""
        response = self.client.get(reverse("color_bands_page"))
        self.assertEqual(response.context["edge"]["slug"], "yellow-green")

    def test_an_unknown_edge_falls_back_rather_than_erroring(self):
        """A hand-edited or stale link lands on a working page."""
        response = self.client.get(reverse("color_bands_page"), {"edge": "chartreuse-ish"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["edge"]["slug"], "yellow-green")

    def test_where_the_slider_sits_rides_in_the_url_too(self):
        """The page's job is to start an argument, and an argument you can't
        send is one you have to win in person."""
        response = self.client.get(
            reverse("color_bands_page"), {"edge": "yellow-green", "cut": "64.5"}
        )
        self.assertEqual(response.context["at"], 64.5)

    def test_a_hand_edited_position_lands_somewhere_readable(self):
        """Same rule as `done=`: a stale or mangled link degrades to the page
        rather than to an error."""
        for bad in ("banana", "", "-40", "999"):
            with self.subTest(bad):
                response = self.client.get(
                    reverse("color_bands_page"), {"edge": "yellow-green", "cut": bad}
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["at"], 61.0)

    def test_a_position_snaps_to_the_sliders_own_step(self):
        """Otherwise a link carries a number the control can't return to, and
        the page opens somewhere the sender was never standing."""
        response = self.client.get(
            reverse("color_bands_page"), {"edge": "yellow-green", "cut": "64.31"}
        )
        self.assertEqual(response.context["at"], 64.5)

    def test_a_line_no_mid_tone_can_see_across_is_not_offered(self):
        """345 separates two zones differing only in how light a colour has to
        be to read pink. Offering it would be a slider that does nothing."""
        response = self.client.get(reverse("color_bands_page"))
        self.assertNotIn("pink-red", response.context["edges_json"])

    def test_a_dye_with_no_colour_is_left_out(self):
        """It contributes nothing to a band, a palette or a rainbow sheet, and
        it would plot at hue zero — a red swatch nobody chose."""
        Dye.objects.create(
            name="Typed in at the sink",
            brand=DyeBrand.objects.get(name="Dharma Acid Dyes"),
            hex_color="",
        )
        response = self.client.get(reverse("color_bands_page"))
        names = [d["name"] for d in response.context["dyes_json"]]
        self.assertNotIn("Typed in at the sink", names)

    def test_dyes_arrive_sorted_by_hue(self):
        """The ruler plots them by hue; the catalogue strips read in the same
        order, and sorting once server-side is what keeps the two agreeing."""
        response = self.client.get(reverse("color_bands_page"))
        hues = [d["hue"] for d in response.context["dyes_json"]]
        self.assertEqual(hues, sorted(hues))


class ImportDyebookTests(TestCase):
    """The dye book is a photograph of a notebook, and it is the only record
    that joins a sales-floor name to a formula.

    Which makes the transcription canon and the resolution the dangerous part:
    the shorthand was written at speed, and the catalogue holds two Sapphires,
    two Lilacs and three Blacks. A wrong jar is a wrong hex, which reaches the
    rainbow sheet as a band the scarf was never dyed in — silent, and found by
    a customer looking under the wrong colour. So these are mostly about
    refusing.
    """

    def setUp(self):
        brand, _ = DyeBrand.objects.get_or_create(name="Dharma Acid Dyes")
        other, _ = DyeBrand.objects.get_or_create(name="Jacquard Acid Dyes")
        self.dyes = {}
        for name, b in [
            ("600 Ecru", other), ("475 Aubergine", brand), ("Avocado", brand),
            ("460 Saffron Spice", brand), ("616 Russet", other),
            ("635 Brown", other), ("452 Forest Green", brand),
            # Both brands sell one, which is the whole problem.
            ("431 Lilac", brand), ("612 Lilac", other),
        ]:
            self.dyes[name] = Dye.objects.create(
                name=name, brand=b, hex_color="#123456"
            )

    def _run(self, **kwargs):
        out = StringIO()
        call_command("import_dyebook", stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def _as_if_unsettled(self, *words):
        """Run as though `words` had not been settled yet.

        The tests below are about the *mechanism* — refusing a word two jars
        answer to, grouping blockers by word, suggesting across a spelling
        difference. Asserting that against whatever the live tables currently
        say means finishing the transcription deletes its own coverage, which
        is exactly what happened: `Lilac` was resolved to `612 Lilac` and
        `Grey` was parked in UNSETTLED on purpose, and three tests went with
        them. Pinning the input keeps the rule under test after the data moves
        on.
        """
        from scarves.management.commands import import_dyebook

        return mock.patch.multiple(
            import_dyebook,
            ALIASES={
                k: v for k, v in import_dyebook.ALIASES.items() if k not in words
            },
            UNSETTLED={
                k: v for k, v in import_dyebook.UNSETTLED.items() if k not in words
            },
        )

    def test_a_fully_resolved_recipe_gets_its_dyes_in_page_order(self):
        recipe = Recipe.objects.create(name="Wasteland")
        self._run()
        self.assertEqual(
            [rd.dye.name for rd in recipe.recipe_dyes.order_by("order")],
            ["600 Ecru", "475 Aubergine", "Avocado"],
            "order is the order the page lists them, not the dye table's",
        )

    def test_an_unresolvable_word_blocks_the_whole_recipe(self):
        """Not two dyes out of three.

        A recipe short one jar prints a collection list short one jar, and the
        person at the shelf has no way to see the gap — the same reason the
        production sheet counts the recipes it can't cover instead of quietly
        printing less.
        """
        recipe = Recipe.objects.create(name="Summer Shoals")   # Champ/Slate/Avo
        output = self._run()
        self.assertEqual(recipe.recipe_dyes.count(), 0)
        self.assertIn("Slate", output)

    def test_a_word_matching_two_dyes_is_refused_not_picked(self):
        recipe = Recipe.objects.create(name="Agean Sea")   # Grey/Lilac/ElecV
        with self._as_if_unsettled("Lilac"):
            output = self._run()
        self.assertEqual(recipe.recipe_dyes.count(), 0)
        self.assertIn("431 Lilac", output)
        self.assertIn("612 Lilac", output)

    def test_blocked_words_are_grouped_by_word_with_a_count(self):
        """One answer usually unblocks several recipes, and a list of recipes
        reads as a chore where a list of words reads as a short sitting."""
        Recipe.objects.create(name="Agean Sea")
        Recipe.objects.create(name="Lavendar Haze")
        with self._as_if_unsettled("Lilac"):
            output = self._run()
        self.assertRegex(output, r"'Lilac'[^\n]*\n\s*blocks 2:")

    def test_an_existing_subset_is_completed(self):
        """The page names three, the row holds two of them. That row was an
        earlier pass at this same page, not a disagreement with it."""
        recipe = Recipe.objects.create(name="Autumn Leaves")
        RecipeDye.objects.create(recipe=recipe, dye=self.dyes["616 Russet"], order=1)
        RecipeDye.objects.create(recipe=recipe, dye=self.dyes["635 Brown"], order=2)
        self._run()
        self.assertEqual(
            [rd.dye.name for rd in recipe.recipe_dyes.order_by("order")],
            ["460 Saffron Spice", "616 Russet", "635 Brown"],
        )

    def test_a_dye_the_page_does_not_name_is_a_conflict_and_is_left_alone(self):
        """Somebody put it there. It might be the correction, and the page is
        a transcription of handwriting — same bargain `import_dyes` makes."""
        recipe = Recipe.objects.create(name="Wasteland")
        RecipeDye.objects.create(recipe=recipe, dye=self.dyes["612 Lilac"], order=1)
        output = self._run()
        self.assertEqual(
            [rd.dye.name for rd in recipe.recipe_dyes.all()], ["612 Lilac"]
        )
        self.assertIn("Wasteland", output)
        self.assertIn("doesn't name", output)

    def test_a_blocked_word_suggests_jars_across_a_spelling_difference(self):
        """`Grey` blocks four recipes and the catalogue spells it `Gray`.

        A substring search calls that unheard-of, which is the report's most
        useful line being its most misleading one.
        """
        Dye.objects.create(
            name="446 Silver Gray",
            brand=DyeBrand.objects.get(name="Dharma Acid Dyes"),
            hex_color="#8a8a8c",
        )
        Recipe.objects.create(name="Sea Smoke")     # Grey/Gun/Black
        with self._as_if_unsettled("Grey"):
            output = self._run()
        self.assertIn("446 Silver Gray", output)

    def test_a_dry_run_writes_nothing(self):
        recipe = Recipe.objects.create(name="Wasteland")
        output = self._run(dry_run=True)
        self.assertEqual(recipe.recipe_dyes.count(), 0)
        self.assertIn("DRY RUN", output)

    def test_a_name_on_the_page_with_no_recipe_is_reported(self):
        output = self._run()
        self.assertIn("match no recipe", output)

    def test_an_alias_pointing_at_no_dye_is_reported_in_its_own_section(self):
        """Two causes — a typo in the table, or a catalogue nobody imported —
        and the command can't tell them apart. Mixed in with the shorthand it
        would read as one more thing to look up; on its own it reads as a
        table to fix."""
        from scarves.management.commands import import_dyebook

        recipe = Recipe.objects.create(name="Wasteland")
        with mock.patch.dict(import_dyebook.ALIASES, {"Ecru": "999 Nonexistent"}):
            output = self._run()
        self.assertEqual(recipe.recipe_dyes.count(), 0, "and it blocks the write")
        self.assertIn("999 Nonexistent", output)
        self.assertIn("database doesn't have", output)


@override_settings(
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_LOCATION_ID="LOC123",
    SQUARE_ENVIRONMENT="sandbox",
)
class SquareVariationOrderTests(TestCase):
    """Variations come out of the till in catalogue order, not alphabetical.

    A new colourway is appended, so the list at the stall ends up in the order
    the dye baths happened — which is nobody's mental model of a colour. The
    only lever Square offers is position in the parent item's `variations`
    list, so the pass rewrites whole ITEMs, and an ITEM upsert deletes any
    variation missing from that list. These tests are mostly about the second
    sentence: what the pass refuses to touch matters more than what it sorts.
    """

    def setUp(self):
        recipe = make_recipe("Zinnia")
        self.product = make_product(recipe, "Zinnia Silk", with_image=False)
        self.raw = self.product.raw_product
        RawProduct.objects.filter(pk=self.raw.pk).update(square_item_id="SQ_ITEM")
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR_Z"
        )

    def _run(self, client, **kwargs):
        out, err = StringIO(), StringIO()
        with mock.patch("square.client.Client", return_value=client):
            call_command("sync_to_square", stdout=out, stderr=err, **kwargs)
        return out.getvalue() + err.getvalue()

    def _item(self, *names_and_ids, item_id="SQ_ITEM"):
        """A retrieve response shaped the way Square answers for an ITEM."""
        return FakeSquareResult({"objects": [{
            "type": "ITEM",
            "id": item_id,
            "version": 7,
            "updated_at": "2026-08-01T00:00:00Z",
            "item_data": {
                "name": "Silk Scarf",
                "variations": [
                    {
                        "type": "ITEM_VARIATION",
                        "id": var_id,
                        "version": 11,
                        "item_variation_data": {
                            "item_id": item_id,
                            "name": name,
                            "ordinal": i,
                        },
                    }
                    for i, (name, var_id) in enumerate(names_and_ids)
                ],
            },
        }]})

    def test_variations_go_back_alphabetised(self):
        client = FakeSquareClient(retrieve_results=[
            self._item(("Zinnia", "SQ_VAR_Z"), ("Amber", "SQ_VAR_A")),
        ])
        self._run(client, reorder=True)

        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(sent["type"], "ITEM")
        self.assertEqual(sent["id"], "SQ_ITEM")
        self.assertEqual(sent["version"], 7, "an update needs the version back")
        self.assertEqual(
            [v["item_variation_data"]["name"] for v in sent["item_data"]["variations"]],
            ["Amber", "Zinnia"],
        )
        self.assertEqual(
            [v["id"] for v in sent["item_data"]["variations"]],
            ["SQ_VAR_A", "SQ_VAR_Z"],
            "the rows are Square's own, only permuted",
        )
        self.assertEqual(
            [v["version"] for v in sent["item_data"]["variations"]], [11, 11]
        )

    def test_the_whole_variation_list_is_sent_back(self):
        """The one that costs stock if it's wrong.

        An ITEM upsert replaces the variation list, so a variation left out is
        deleted along with its Square ID and its count. Three go up, three come
        back — including the two this app has never heard of.
        """
        client = FakeSquareClient(retrieve_results=[
            self._item(
                ("Zinnia", "SQ_VAR_Z"),
                ("Amber", "SQ_VAR_A"),
                ("Moss", "SQ_VAR_UNKNOWN_TO_US"),
            ),
        ])
        self._run(client, reorder=True)

        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(
            [v["id"] for v in sent["item_data"]["variations"]],
            ["SQ_VAR_A", "SQ_VAR_UNKNOWN_TO_US", "SQ_VAR_Z"],
        )

    def test_an_item_that_came_back_without_variations_is_left_alone(self):
        """Sending that back would empty the item.

        An answer we didn't understand looks exactly like an item with nothing
        under it, and the difference is the whole catalogue.
        """
        client = FakeSquareClient(retrieve_results=[FakeSquareResult({
            "objects": [{"type": "ITEM", "id": "SQ_ITEM", "version": 7,
                         "item_data": {"name": "Silk Scarf"}}],
        })])
        output = self._run(client, reorder=True)
        self.assertEqual(client.upserts, [])
        self.assertIn("already alphabetical", output)

    def test_a_variation_named_by_an_item_option_is_left_alone(self):
        """It carries no name of its own and is ordered by the option's values.

        Sorting on the empty string would bunch those at the top and fight
        whatever set that order.
        """
        response = self._item(("Zinnia", "SQ_VAR_Z"), ("Amber", "SQ_VAR_A"))
        del response.body["objects"][0]["item_data"]["variations"][1] \
            ["item_variation_data"]["name"]
        client = FakeSquareClient(retrieve_results=[response])
        self._run(client, reorder=True)
        self.assertEqual(client.upserts, [])

    def test_an_item_with_no_positions_is_written_even_when_it_reads_sorted(self):
        """The state most of the live catalogue was found in.

        Square assigns ordinals only when a parent item's list is written, and
        a variation added on its own never is — which is how every colourway
        after the first reached Square. The API hands those back in name
        order, so the item reads as sorted while the till, with no positions
        to go on, shows them as they were created. Comparing names alone would
        skip exactly the items with the reported symptom.
        """
        response = self._item(("Amber", "SQ_VAR_A"), ("Zinnia", "SQ_VAR_Z"))
        for variation in response.body["objects"][0]["item_data"]["variations"]:
            del variation["item_variation_data"]["ordinal"]
        client = FakeSquareClient(retrieve_results=[response])
        output = self._run(client, reorder=True)

        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(
            [v["id"] for v in sent["item_data"]["variations"]],
            ["SQ_VAR_A", "SQ_VAR_Z"],
        )
        self.assertIn("no positions", output)

    def test_an_item_already_in_order_is_not_rewritten(self):
        """Otherwise every run bumps every version for nothing."""
        client = FakeSquareClient(retrieve_results=[
            self._item(("Amber", "SQ_VAR_A"), ("Zinnia", "SQ_VAR_Z")),
        ])
        output = self._run(client, reorder=True)
        self.assertEqual(client.upserts, [])
        self.assertIn("already alphabetical", output)

    def test_sorting_ignores_case(self):
        client = FakeSquareClient(retrieve_results=[
            self._item(("zinnia", "SQ_VAR_Z"), ("Amber", "SQ_VAR_A")),
        ])
        self._run(client, reorder=True)
        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(
            [v["id"] for v in sent["item_data"]["variations"]],
            ["SQ_VAR_A", "SQ_VAR_Z"],
        )

    def test_the_reported_ordinal_is_not_sent_back(self):
        """It is read-only, and the stale number beside the new position makes
        a dry run read as though nothing was being asked for."""
        client = FakeSquareClient(retrieve_results=[
            self._item(("Zinnia", "SQ_VAR_Z"), ("Amber", "SQ_VAR_A")),
        ])
        self._run(client, reorder=True)
        sent = client.upserts[0]["batches"][0]["objects"][0]
        for variation in sent["item_data"]["variations"]:
            self.assertNotIn("ordinal", variation["item_variation_data"])

    def test_a_group_item_is_reordered_too(self):
        """Undyed stock is one item whose variations are the blanks — which is
        exactly the list somebody scrolls at the till."""
        category = RawProductCategory.objects.get_or_create(name="Yarn")[0]
        group = CatalogGroup.objects.create(
            name="Undyed Yarn", category=category, square_item_id="SQ_GROUP"
        )
        client = FakeSquareClient(retrieve_results=[
            self._item(("Wool", "SQ_VAR_W"), ("Alpaca", "SQ_VAR_AL"),
                       item_id="SQ_GROUP"),
        ])
        self._run(client, reorder=True)

        self.assertIn("SQ_GROUP", client.retrieves[0]["object_ids"])
        self.assertEqual(group.square_item_id, "SQ_GROUP")
        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(sent["id"], "SQ_GROUP")
        self.assertEqual(
            [v["id"] for v in sent["item_data"]["variations"]],
            ["SQ_VAR_AL", "SQ_VAR_W"],
        )

    def test_reorder_alone_touches_no_stock(self):
        client = FakeSquareClient(retrieve_results=[
            self._item(("Zinnia", "SQ_VAR_Z"), ("Amber", "SQ_VAR_A")),
        ])
        self._run(client, reorder=True)
        self.assertEqual(client.inventory_changes, [])

    def test_a_read_error_stops_the_command(self):
        """A swallowed read is an empty answer, and an empty answer is
        indistinguishable from a catalogue already in order."""
        client = FakeSquareClient(retrieve_results=[FakeSquareResult(
            errors=[{"category": "API_ERROR", "detail": "read boom"}]
        )])
        with self.assertRaises(CommandError) as caught:
            self._run(client, reorder=True)
        self.assertIn("read boom", str(caught.exception))

    def test_a_normal_sync_ends_by_putting_the_order_right(self):
        """The run that creates a variation is the run that breaks the order,
        so the fix can't live in a command someone has to remember."""
        client = FakeSquareClient(retrieve_results=[
            self._item(("Zinnia", "SQ_VAR_Z"), ("Amber", "SQ_VAR_A")),
        ])
        self._run(client)

        self.assertEqual(client.retrieves[0]["object_ids"], ["SQ_ITEM"])
        sent = client.upserts[-1]["batches"][0]["objects"][0]
        self.assertEqual(
            [v["id"] for v in sent["item_data"]["variations"]],
            ["SQ_VAR_A", "SQ_VAR_Z"],
        )
        self.assertEqual(len(client.inventory_changes), 1,
                         "and stock still goes up afterwards")

    def test_a_dry_run_reorders_nothing(self):
        client = FakeSquareClient(retrieve_results=[
            self._item(("Zinnia", "SQ_VAR_Z"), ("Amber", "SQ_VAR_A")),
        ])
        output = self._run(client, reorder=True, dry_run=True)
        self.assertEqual(client.upserts, [])
        self.assertIn("Amber, Zinnia", output)


@override_settings(
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_LOCATION_ID="LOC123",
    SQUARE_ENVIRONMENT="sandbox",
)
class SquareFailsLoudlyTests(TestCase):
    """The likely real failure is an expired token, and it must not be quiet.

    Every error path used to print to stderr and `return`, which exits 0. On a
    schedule that reads as a successful run, and a catalogue that stopped
    syncing looks identical to one that had nothing to do — until somebody
    can't ring up a sale.
    """

    def setUp(self):
        recipe = make_recipe("Loud Recipe")
        self.product = make_product(recipe, "Loud Product", with_image=False)

    def _run(self, client, **kwargs):
        out = StringIO()
        with mock.patch("square.client.Client", return_value=client):
            call_command("sync_to_square", stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_a_rejected_token_fails_the_command(self):
        client = FakeSquareClient(locations_result=FakeSquareResult(
            errors=[{"category": "AUTHENTICATION_ERROR",
                     "detail": "This request could not be authorized."}]
        ))
        with self.assertRaises(CommandError) as caught:
            self._run(client)
        message = str(caught.exception)
        self.assertIn("expired or revoked", message,
                      "the message has to name the likely cause")
        self.assertEqual(client.upserts, [], "nothing was sent")

    def test_check_verifies_credentials_and_changes_nothing(self):
        client = FakeSquareClient()
        output = self._run(client, check=True)
        self.assertIn("Credentials OK", output)
        self.assertEqual(client.upserts, [])
        self.assertEqual(client.inventory_changes, [])

    @override_settings(SQUARE_ACCESS_TOKEN="")
    def test_a_missing_token_is_caught_before_any_call(self):
        with self.assertRaises(CommandError) as caught:
            self._run(FakeSquareClient())
        self.assertIn("SQUARE_ACCESS_TOKEN", str(caught.exception))

    @override_settings(SQUARE_LOCATION_ID="")
    def test_a_missing_location_is_caught_before_any_call(self):
        with self.assertRaises(CommandError) as caught:
            self._run(FakeSquareClient())
        self.assertIn("SQUARE_LOCATION_ID", str(caught.exception))

    def test_a_location_from_another_account_is_refused(self):
        """Inventory would be pushed nowhere, successfully."""
        client = FakeSquareClient(locations_result=FakeSquareResult(
            {"locations": [{"id": "SOMEONE_ELSE"}]}
        ))
        with self.assertRaises(CommandError) as caught:
            self._run(client)
        self.assertIn("not one of this account's locations", str(caught.exception))

    def test_a_failed_version_read_does_not_send_null_versions(self):
        """Swallowing it meant every variation went back up with
        version: None, which is not the update anyone intended."""
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        client = FakeSquareClient(retrieve_result=FakeSquareResult(
            errors=[{"category": "AUTHENTICATION_ERROR", "detail": "nope"}]
        ))
        with self.assertRaises(CommandError) as caught:
            self._run(client, update=True)
        self.assertIn("current variation versions", str(caught.exception))
        self.assertEqual(client.upserts, [])

    def test_a_variation_square_has_forgotten_is_skipped_not_duplicated(self):
        """No version means Square doesn't know the ID; sending it anyway
        creates a second variation rather than updating the first."""
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="GONE"
        )
        client = FakeSquareClient(retrieve_result=FakeSquareResult({"objects": []}))
        output = self._run(client, update=True)
        self.assertIn("doesn't recognise", output)
        sent = client.upserts[0]["batches"][0]["objects"] if client.upserts else []
        self.assertEqual(sent, [], "nothing with a null version went up")


@override_settings(
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_LOCATION_ID="LOC123",
    SQUARE_ENVIRONMENT="sandbox",
)
class SquareDryRunTests(TestCase):
    """`--dry-run` stands in for a sandbox account.

    The sandbox token process has been troublesome, so the way to avoid
    production being the first thing that ever runs this is to build the whole
    payload and print it instead of sending it.
    """

    def setUp(self):
        recipe = make_recipe("Dry Recipe")
        self.product = make_product(recipe, "Dry Product", with_image=False)
        self.product.price = Decimal("18.50")
        self.product.number_on_hand = 4
        self.product.save()

    def _run(self, client, **kwargs):
        out = StringIO()
        with mock.patch("square.client.Client", return_value=client):
            call_command("sync_to_square", stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_it_sends_nothing(self):
        client = FakeSquareClient()
        self._run(client, dry_run=True)
        self.assertEqual(client.upserts, [])
        self.assertEqual(client.inventory_changes, [])

    def test_it_writes_no_ids_back(self):
        client = FakeSquareClient(upsert_results=[FakeSquareResult({
            "id_mappings": [{"client_object_id": f"#rp_{self.product.raw_product.pk}",
                             "object_id": "SHOULD_NOT_BE_SAVED"}],
        })])
        self._run(client, dry_run=True)
        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.raw_product.square_item_id, "")

    def test_it_shows_what_would_be_created(self):
        output = self._run(FakeSquareClient(), dry_run=True)
        self.assertIn("DRY RUN", output)
        self.assertIn(self.product.raw_product.name, output)
        self.assertIn(self.product.sku, output)
        self.assertIn("$18.50", output)

    def test_it_shows_the_stock_counts_it_would_set(self):
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        RawProduct.objects.filter(pk=self.product.raw_product.pk).update(
            square_item_id="SQ_ITEM"
        )
        output = self._run(FakeSquareClient(), dry_run=True, inventory_only=True)
        self.assertIn("SQ_VAR -> 4", output)
        self.assertIn("LOC123", output)

    def test_it_still_checks_the_credentials(self):
        """A dry run that passes with a dead token teaches nothing."""
        client = FakeSquareClient(locations_result=FakeSquareResult(
            errors=[{"category": "AUTHENTICATION_ERROR", "detail": "nope"}]
        ))
        with self.assertRaises(CommandError):
            self._run(client, dry_run=True)


@override_settings(
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_LOCATION_ID="LOC123",
    SQUARE_ENVIRONMENT="sandbox",
)
class SquarePartialBatchTests(TestCase):
    """A failure partway through must not orphan what Square already created.

    Over 100 objects is more than one call. If a later chunk fails and the
    IDs from the earlier ones are discarded, those objects exist in Square
    with nothing here pointing at them — and the next run creates them again.
    The only symptom is a catalogue with everything in it twice, which is
    tedious to unpick by hand.
    """

    def setUp(self):
        self.recipe = make_recipe("Batch Recipe")
        category, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.products = []
        for i in range(150):
            raw = RawProduct.objects.create(
                name=f"Blank {i:03d}", category=category, price="5.00"
            )
            self.products.append(FinishedProduct.objects.create(
                name=f"Product {i:03d}", raw_product=raw,
                recipe=self.recipe, price="20.00",
            ))

    def test_ids_from_a_successful_chunk_survive_a_later_failure(self):
        first = self.products[0]
        client = FakeSquareClient(upsert_results=[
            FakeSquareResult({"id_mappings": [
                {"client_object_id": f"#rp_{first.raw_product.pk}",
                 "object_id": "SAVED_ITEM"},
                {"client_object_id": f"#fp_{first.pk}",
                 "object_id": "SAVED_VAR"},
            ]}),
            FakeSquareResult(errors=[{"category": "API_ERROR", "detail": "later boom"}]),
        ])

        out = StringIO()
        with mock.patch("square.client.Client", return_value=client):
            with self.assertRaises(CommandError):
                call_command("sync_to_square", stdout=out, stderr=out)

        first.refresh_from_db()
        first.raw_product.refresh_from_db()
        self.assertEqual(first.raw_product.square_item_id, "SAVED_ITEM")
        self.assertEqual(first.square_variation_id, "SAVED_VAR")
        self.assertIn("re-run to continue", out.getvalue())

    def test_a_rerun_does_not_recreate_what_is_already_linked(self):
        linked = self.products[0]
        RawProduct.objects.filter(pk=linked.raw_product.pk).update(
            square_item_id="SAVED_ITEM"
        )
        FinishedProduct.objects.filter(pk=linked.pk).update(
            square_variation_id="SAVED_VAR"
        )
        client = FakeSquareClient()
        with mock.patch("square.client.Client", return_value=client):
            call_command("sync_to_square", stdout=StringIO(), stderr=StringIO())

        sent = [o for body in client.upserts
                for o in body["batches"][0]["objects"]]
        self.assertNotIn(f"#rp_{linked.raw_product.pk}", [o["id"] for o in sent])


class SheetMapNoiseTests(TestCase):
    """The map draws only sheets that differ from "all 80 used".

    A real run — 2435 labels over 31 sheets — drew 29 identical full grids,
    which buries the two that carry information: the part-used sheet you start
    on and the one you finish on.
    """

    def setUp(self):
        self.stock = make_stock()   # 4 × 20 = 80

    def test_a_long_run_draws_only_the_unfinished_sheet(self):
        plan = labelmod.plan_sheets(self.stock, 2435, start_at=0)
        self.assertEqual(plan.sheet_count, 31)
        self.assertEqual([s["number"] for s in plan.sheets], [31])
        self.assertEqual(plan.full_sheets, 30)
        self.assertEqual((plan.full_from, plan.full_to), (1, 30))

    def test_a_partial_start_keeps_the_first_sheet_too(self):
        # 44 already peeled + 2435 printed ends at index 2478, on sheet 31.
        plan = labelmod.plan_sheets(self.stock, 2435, start_at=44)
        self.assertEqual([s["number"] for s in plan.sheets], [1, 31])
        self.assertEqual(plan.full_sheets, 29)
        self.assertEqual((plan.full_from, plan.full_to), (2, 30))

    def test_a_single_sheet_is_always_drawn(self):
        plan = labelmod.plan_sheets(self.stock, 12, start_at=0)
        self.assertEqual([s["number"] for s in plan.sheets], [1])
        self.assertEqual(plan.full_sheets, 0)

    def test_an_exactly_full_single_sheet_is_still_drawn(self):
        """One sheet, no marker — but with nothing else on screen, hiding the
        only diagram would say less than showing it."""
        plan = labelmod.plan_sheets(self.stock, 80, start_at=0)
        self.assertEqual(plan.sheet_count, 1)
        self.assertEqual(plan.full_sheets, 1)
        self.assertEqual(plan.sheets, [])

    def test_the_marker_sheet_is_never_hidden(self):
        """It's the one that says where to start next time."""
        plan = labelmod.plan_sheets(self.stock, 159, start_at=0)
        self.assertEqual(plan.marker_index, 159)
        drawn = [s["number"] for s in plan.sheets]
        self.assertIn(2, drawn)
        marker_cells = [
            c for s in plan.sheets for c in s["cells"] if c["state"] == "marker"
        ]
        self.assertEqual(len(marker_cells), 1)

    def test_drawn_cells_are_only_built_for_drawn_sheets(self):
        """31 sheets × 80 cells is 2480 dicts nobody looks at."""
        plan = labelmod.plan_sheets(self.stock, 2435, start_at=0)
        self.assertEqual(sum(len(s["cells"]) for s in plan.sheets), 80)

    def test_the_page_says_how_many_it_left_out(self):
        user = User.objects.create_superuser("noise", "n@example.test", "pw")
        self.client.force_login(user)
        recipe = make_recipe("Noise Recipe")
        product = make_product(recipe, "Noise Product", with_image=False)
        product.number_on_hand = 500
        product.save()

        response = self.client.get(reverse("label_index"), {
            "dataset": "inventory", "extra": "0",
            "stock": str(LabelStock.objects.first().pk), "start_at": "1",
        })
        self.assertContains(response, "used end to end")
        self.assertContains(response, "Only the part-used sheets are drawn")


class RowBreakTests(TestCase):
    """The whole-catalogue export starts each blank on a fresh row.

    So a 31-sheet stack can be split by blank without a seam landing
    mid-row. Deliberately off everywhere else: the padding is ~20 labels
    either way, which rounds to nothing across 31 sheets and is a third of a
    3-sheet weekly run.
    """

    def setUp(self):
        self.stock = make_stock()      # 4 columns
        self.recipe = make_recipe("Dawn")
        category, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.category = category
        # SKUs come out BLANK-DAWN, so the prefix is the blank.
        self.blanks = {}
        for blank, on_hand in (("Alpha Scarf", 6), ("Beta Scarf", 3)):
            raw = RawProduct.objects.create(
                name=blank, category=category, price="5.00"
            )
            product = FinishedProduct.objects.create(
                name=f"{blank} Dawn", raw_product=raw, recipe=self.recipe,
                price="20.00", number_on_hand=on_hand,
            )
            self.blanks[blank] = product

    def test_a_new_blank_starts_on_a_fresh_row(self):
        run = labelmod.inventory_run()
        sequence = run.sequence(columns=4)

        # 6 of ALPHAS fills a row and two of the next; the remaining two
        # positions are padded so BETASC starts clean.
        skus = [p.sku if p else None for p in sequence]
        self.assertEqual(skus[:6], ["ALPHAS-DAWN"] * 6)
        self.assertEqual(skus[6:8], [None, None])
        self.assertEqual(skus[8:], ["BETASC-DAWN"] * 3)
        self.assertEqual(len(sequence) % 4, 3 % 4)

    def test_stickers_printed_is_unchanged_by_padding(self):
        run = labelmod.inventory_run()
        self.assertEqual(run.total, 9)
        self.assertEqual(len(run.flat(columns=4)), 9)
        self.assertEqual(len(run.sequence(columns=4)), 11)

    def test_no_padding_when_a_group_already_ends_on_a_row_boundary(self):
        self.blanks["Alpha Scarf"].number_on_hand = 8
        self.blanks["Alpha Scarf"].save()
        run = labelmod.inventory_run()
        self.assertEqual(len(run.sequence(columns=4)), 11)
        self.assertNotIn(None, run.sequence(columns=4))

    def test_a_filtered_run_does_not_pad(self):
        """Same ~20 labels, but a third of a short run instead of nothing."""
        run = labelmod.inventory_run(category=self.category)
        self.assertFalse(run.row_break_on_group)
        self.assertNotIn(None, run.sequence(columns=4))

        picked = [self.blanks["Alpha Scarf"].raw_product]
        run = labelmod.inventory_run(raw_products=picked)
        self.assertFalse(run.row_break_on_group)

    def test_the_weekly_run_does_not_pad(self):
        """~20 products over ~80 labels — breaking per group would waste a
        quarter of it."""
        InventoryLog.objects.create(
            finished_product=self.blanks["Alpha Scarf"],
            log_type=InventoryLog.PRODUCTION, quantity=6,
        )
        run = labelmod.produced_since(timezone.localdate() - timedelta(days=1))
        self.assertFalse(run.row_break_on_group)
        self.assertNotIn(None, run.sequence(columns=4))

    def test_hand_picked_runs_do_not_pad(self):
        run = labelmod.specific_items([(self.blanks["Alpha Scarf"], 6),
                                       (self.blanks["Beta Scarf"], 3)])
        self.assertFalse(run.row_break_on_group)
        self.assertNotIn(None, run.sequence(columns=4))

    def test_padding_consumes_a_sheet_position_but_prints_nothing(self):
        run = labelmod.inventory_run()
        pdf = labelmod.render_run(run, self.stock, start_at=0)
        text = _pdf_text(pdf)
        self.assertEqual(text.count("ALPHAS-DAWN"), 6)
        self.assertEqual(text.count("BETASC-DAWN"), 3)

        # Position 8 is the first of the second blank, i.e. row 3 column 1.
        items = [i for i in _pdf_text_items(pdf) if i[2] == "BETASC-DAWN"]
        first_beta_x = min(i[0] for i in items)
        alphas = [i for i in _pdf_text_items(pdf) if i[2] == "ALPHAS-DAWN"]
        self.assertAlmostEqual(first_beta_x, min(i[0] for i in alphas), delta=0.5,
                               msg="the new blank starts in column 1")

    def test_the_page_reports_the_padding(self):
        user = User.objects.create_superuser("pad", "p@example.test", "pw")
        self.client.force_login(user)
        response = self.client.get(reverse("label_index"), {
            "dataset": "inventory", "extra": "0",
            "stock": str(LabelStock.objects.first().pk), "start_at": "1",
        })
        self.assertEqual(response.context["padding"], 2)
        self.assertContains(response, "skipped so each blank starts its own row")


@override_settings(
    SQUARE_WEBHOOK_SIGNATURE_KEY="test-signature-key",
    SQUARE_WEBHOOK_URL="https://example.test/scarves/webhooks/square",
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_ENVIRONMENT="sandbox",
)
class SquareWebhookTests(TestCase):
    """What the webhook does with a line item, in all four shapes.

    The one that matters is the line it *can't* place. That used to be a bare
    `continue`: a scarf nobody could name was rung up, walked out of the tent,
    and left no trace — Square had the money, this app still had the stock,
    and nothing anywhere said the two disagreed.
    """

    def setUp(self):
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        raw = RawProduct.objects.create(name="Infinity", category=self.silk, price="5.00")
        self.product = FinishedProduct.objects.create(
            name="Aegean Infinity", raw_product=raw, recipe=make_recipe("Aegean Sea"),
            price="30.00", number_on_hand=4, square_variation_id="VAR-AEGEAN",
        )
        self.sold_at = "2026-08-15T18:30:00Z"

    def _post(self, line_items, order_id="ORDER-1", closed_at=None):
        payload = json.dumps({
            "type": "order.updated",
            "data": {"object": {"order_updated": {
                "state": "COMPLETED", "order_id": order_id,
            }}},
        })
        signature = base64.b64encode(
            hmac.new(
                b"test-signature-key",
                (settings.SQUARE_WEBHOOK_URL + payload).encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode()

        order = {"line_items": line_items, "closed_at": closed_at or self.sold_at}
        with mock.patch("square.client.Client") as client:
            client.return_value.orders.retrieve_order.return_value = FakeSquareResult(
                {"order": order}
            )
            return self.client.post(
                reverse("square_webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_SQUARE_HMACSHA256_SIGNATURE=signature,
            )

    def test_a_known_variation_still_leaves_inventory(self):
        response = self._post([{
            "uid": "L1", "catalog_object_id": "VAR-AEGEAN", "quantity": "2",
            "name": "Infinity", "variation_name": "Aegean Sea",
        }])

        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 2)
        self.assertEqual(UnmatchedSale.objects.count(), 0)

    def test_an_unknown_variation_is_queued_instead_of_dropped(self):
        self._post([{
            "uid": "L1", "catalog_object_id": "VAR-WHO-KNOWS", "quantity": "1",
            "name": "Scarf", "variation_name": "Regular",
            "total_money": {"amount": 3000},
        }])

        sale = UnmatchedSale.objects.get()
        self.assertEqual(sale.square_variation_id, "VAR-WHO-KNOWS")
        self.assertEqual(sale.quantity, 1)
        self.assertEqual(sale.amount_cents, 3000)
        self.assertTrue(sale.is_open)

    def test_a_line_with_no_catalog_object_is_queued_too(self):
        """A custom amount rung up at the till has no catalog object at all."""
        self._post([{"uid": "L1", "quantity": "1", "name": "Custom Amount",
                     "total_money": {"amount": 2500}}])

        self.assertEqual(UnmatchedSale.objects.get().name, "Custom Amount")

    def test_the_queued_row_carries_squares_time_not_ours(self):
        """The review screen pairs on this timestamp — receipt time would drift
        by however long the webhook took to arrive."""
        self._post([{"uid": "L1", "quantity": "1", "name": "Scarf"}])

        sale = UnmatchedSale.objects.get()
        self.assertEqual(sale.sold_at.isoformat(), "2026-08-15T18:30:00+00:00")

    def test_a_redelivered_order_does_not_queue_it_twice(self):
        """Square sends order.updated more than once for the same order."""
        line = [{"uid": "L1", "quantity": "1", "name": "Scarf"}]
        self._post(line)
        self._post(line)

        self.assertEqual(UnmatchedSale.objects.count(), 1)

    def test_a_redelivered_order_does_not_sell_the_same_scarf_twice(self):
        line = [{"uid": "L1", "catalog_object_id": "VAR-AEGEAN", "quantity": "1"}]
        self._post(line)
        self._post(line)

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 3)
        self.assertEqual(
            InventoryLog.objects.filter(log_type=InventoryLog.SALE).count(), 1
        )


class BoothPhotoFormTests(TestCase):
    """The crew's form. PIN, and the one rule worth refusing a photo over."""

    def setUp(self):
        self.employee = Employee.objects.create(name="Robin", pin="4821")

    def _data(self, **overrides):
        data = {
            "employee": self.employee.pk,
            "pin": "4821",
            "reason": BoothPhoto.REASON_SHARE,
        }
        data.update(overrides)
        return data

    def _files(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return {"photo": SimpleUploadedFile(
            "booth.jpg", make_jpeg((60, 40)), content_type="image/jpeg"
        )}

    def _form(self, **overrides):
        from .forms import BoothPhotoForm
        return BoothPhotoForm(self._data(**overrides), self._files())

    def test_the_wrong_pin_is_rejected(self):
        form = self._form(pin="0000")
        self.assertFalse(form.is_valid())
        self.assertIn("pin", form.errors)

    def test_a_person_in_the_photo_needs_their_own_yes(self):
        """The sender's tick is the sender's permission. It is not the
        permission of the person in the picture, and the form won't pretend it
        is."""
        form = self._form(share_instagram=True, people_in_photo=True)

        self.assertFalse(form.is_valid())
        self.assertIn("people_agreed", form.errors)

    def test_a_person_in_the_photo_is_fine_when_nothing_is_ticked(self):
        """Sending it with no destination is a legitimate answer — 'here, your
        call' — and must not be blocked."""
        self.assertTrue(self._form(people_in_photo=True).is_valid())

    def test_the_barcode_prefix_is_trimmed_to_what_it_means(self):
        form = self._form(
            reason=BoothPhoto.REASON_UNIDENTIFIED, sku_prefix="infi-aeg"
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["sku_prefix"], "INFIAE"[:6])

    def test_an_unidentified_report_defaults_to_now(self):
        """Reported straight after the sale is the normal case, and the moment
        it was sent is what the ±15 minute match looks for."""
        form = self._form(reason=BoothPhoto.REASON_UNIDENTIFIED)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNotNone(form.cleaned_data["sold_at"])

    def test_a_sale_in_the_future_is_refused(self):
        future = (timezone.localtime() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        form = self._form(reason=BoothPhoto.REASON_UNIDENTIFIED, sold_at=future)

        self.assertFalse(form.is_valid())
        self.assertIn("sold_at", form.errors)


class BoothPhotoViewTests(TestCase):
    """The page itself: no login, PIN in the page, and only the half of the
    form that the reason applies to is kept."""

    def setUp(self):
        self.employee = Employee.objects.create(name="Robin", pin="4821")
        self.url = reverse("booth_photo")

    def _send(self, **overrides):
        from django.core.files.uploadedfile import SimpleUploadedFile
        data = {
            "employee": self.employee.pk,
            "pin": "4821",
            "reason": BoothPhoto.REASON_SHARE,
            "photo": SimpleUploadedFile(
                "booth.jpg", make_jpeg((60, 40)), content_type="image/jpeg"
            ),
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_it_serves_an_anonymous_get(self):
        """A login here would lock out exactly the people it is for, and the
        only symptom would be that nobody ever reports."""
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_share_photo_is_stored_with_its_permissions(self):
        self._send(share_website=True, caption="Best hat all weekend", tag="@someone")

        photo = BoothPhoto.objects.get()
        self.assertTrue(photo.share_website)
        self.assertFalse(photo.share_instagram)
        self.assertEqual(photo.caption, "Best hat all weekend")
        self.assertEqual(photo.employee, self.employee)
        self.assertTrue(photo.image.name.startswith("booth/"))

    def test_a_sale_report_keeps_no_sharing_permission(self):
        """Both halves always submit. A report that changed reason mid-thought
        must not leave a permission to post it behind."""
        self._send(
            reason=BoothPhoto.REASON_UNIDENTIFIED,
            share_instagram=True,
            caption="ignore me",
            sku_prefix="INFI",
        )

        photo = BoothPhoto.objects.get()
        self.assertFalse(photo.share_instagram)
        self.assertEqual(photo.caption, "")
        self.assertEqual(photo.sku_prefix, "INFI")

    def test_the_photo_is_downscaled_on_the_way_in(self):
        self._send()

        photo = BoothPhoto.objects.get()
        with photo.image.open("rb") as fh:
            self.assertLessEqual(max(image_size(fh.read())), IMAGE_MAX_EDGE)

    def test_a_bad_pin_saves_nothing(self):
        self._send(pin="1111")

        self.assertEqual(BoothPhoto.objects.count(), 0)


class ShareableTests(TestCase):
    """`shareable` is what the gallery reads, so it has to be the whole rule
    rather than the two destination ticks."""

    def _photo(self, **kwargs):
        employee = Employee.objects.create(name=f"E{BoothPhoto.objects.count()}", pin="1234")
        return BoothPhoto(employee=employee, reason=BoothPhoto.REASON_SHARE, **kwargs)

    def test_nothing_ticked_is_not_shareable(self):
        self.assertFalse(self._photo().shareable)

    def test_a_destination_alone_is_enough_with_nobody_in_shot(self):
        self.assertTrue(self._photo(share_website=True).shareable)

    def test_a_person_in_shot_without_their_yes_is_not_shareable(self):
        self.assertFalse(
            self._photo(share_website=True, people_in_photo=True).shareable
        )

    def test_a_person_in_shot_who_agreed_is_shareable(self):
        self.assertTrue(
            self._photo(
                share_website=True, people_in_photo=True, people_agreed=True
            ).shareable
        )


class UnmatchedSaleReviewTests(TestCase):
    """Pairing sales with photos, and what resolving one actually does."""

    def setUp(self):
        self.user = User.objects.create_superuser("owner", "o@example.test", "pw")
        self.client.force_login(self.user)
        self.employee = Employee.objects.create(name="Robin", pin="4821")
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.raw = RawProduct.objects.create(
            name="Infinity", category=self.silk, price="5.00"
        )
        self.product = FinishedProduct.objects.create(
            name="Aegean Infinity", raw_product=self.raw,
            recipe=make_recipe("Aegean Sea"), price="30.00", number_on_hand=4,
        )
        self.sold_at = timezone.now() - timedelta(hours=3)
        self.sale = UnmatchedSale.objects.create(
            order_id="ORDER-1", line_uid="L1", name="Scarf",
            quantity=1, amount_cents=3000, sold_at=self.sold_at,
        )
        self.url = reverse("unmatched_sales")

    def _report(self, minutes_off=0, prefix="", image=True):
        photo = BoothPhoto(
            employee=self.employee,
            reason=BoothPhoto.REASON_UNIDENTIFIED,
            sold_at=self.sold_at + timedelta(minutes=minutes_off),
            sku_prefix=prefix,
        )
        if image:
            photo.image.save("r.jpg", ContentFile(make_jpeg((40, 30))), save=False)
        photo.save()
        return photo

    def _day(self):
        return timezone.localtime(self.sold_at).date().isoformat()

    def _rows(self):
        return self.client.get(f"{self.url}?day={self._day()}").context["rows"]

    def test_a_photo_within_the_window_is_offered_against_the_sale(self):
        report = self._report(minutes_off=7)

        self.assertEqual([r.pk for r in self._rows()[0]["reports"]], [report.pk])

    def test_a_photo_well_outside_the_window_is_not(self):
        self._report(minutes_off=45)

        response = self.client.get(f"{self.url}?day={self._day()}")
        self.assertEqual(response.context["rows"][0]["reports"], [])
        # Not dropped either: a report with no sale beside it is the
        # interesting case, so it stays on the page.
        self.assertEqual(len(response.context["orphans"]), 1)

    def test_a_reported_barcode_narrows_the_products_offered(self):
        other = RawProduct.objects.create(name="Sash Belt", category=self.silk, price="5")
        FinishedProduct.objects.create(
            name="Aegean Belt", raw_product=other,
            recipe=Recipe.objects.get(name="Aegean Sea"), price="20.00",
        )
        self._report(minutes_off=2, prefix=self.product.sku[:6])

        row = self._rows()[0]
        self.assertTrue(row["narrowed"])
        self.assertEqual([p.pk for p in row["options"]], [self.product.pk])

    def test_with_no_barcode_reported_the_whole_catalogue_is_offered(self):
        self._report(minutes_off=2, prefix="")

        row = self._rows()[0]
        self.assertFalse(row["narrowed"])
        self.assertEqual(len(row["options"]), FinishedProduct.objects.count())

    def _resolve(self, **extra):
        data = {"product_id": self.product.pk, "day": self._day()}
        data.update(extra)
        return self.client.post(
            reverse("resolve_unmatched_sale", args=[self.sale.pk]), data
        )

    def test_matching_takes_the_scarf_out_of_stock(self):
        self._resolve()

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 3)

    def test_the_log_row_is_dated_when_it_sold_not_when_it_was_matched(self):
        """Otherwise the sale lands on the day someone got round to the queue,
        which is a day nothing happened."""
        self._resolve()

        log = InventoryLog.objects.get(log_type=InventoryLog.SALE)
        self.assertEqual(log.created_at, self.sold_at)
        self.assertEqual(log.sale_reference, "ORDER-1")
        self.assertIn("by hand", log.notes)

    def test_matching_twice_only_sells_it_once(self):
        """A double-submitted review screen must not take two scarves off."""
        self._resolve()
        self._resolve()

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 3)
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_dismissing_closes_the_row_without_touching_stock(self):
        """Not everything Square couldn't place was a scarf — a queue that
        can't be emptied stops being read."""
        self.client.post(
            reverse("resolve_unmatched_sale", args=[self.sale.pk]),
            {"dismiss": "1", "dismissed_reason": "tip jar", "day": self._day()},
        )

        self.sale.refresh_from_db()
        self.product.refresh_from_db()
        self.assertFalse(self.sale.is_open)
        self.assertEqual(self.sale.dismissed_reason, "tip jar")
        self.assertEqual(self.product.number_on_hand, 4)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_the_photo_can_be_filed_against_the_product(self):
        """The scarf nobody could name now has a picture, so next time it's
        identifiable."""
        report = self._report(minutes_off=3)
        self._resolve(report_id=report.pk, file_photo="1")

        image = self.product.images.get()
        self.assertEqual(image.image.name, report.image.name)

    def test_the_photo_is_not_filed_unless_asked(self):
        report = self._report(minutes_off=3)
        self._resolve(report_id=report.pk)

        self.assertEqual(self.product.images.count(), 0)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.resolved_photo, report)

    def test_a_resolved_photo_leaves_the_queue(self):
        report = self._report(minutes_off=3)
        self._resolve(report_id=report.pk)

        second = UnmatchedSale.objects.create(
            order_id="ORDER-2", line_uid="L1", name="Scarf",
            quantity=1, sold_at=self.sold_at,
        )
        rows = self._rows()
        self.assertEqual([r["sale"].pk for r in rows], [second.pk])
        self.assertEqual(rows[0]["reports"], [])


class CrewCookieTests(TestCase):
    """Remembering name and PIN on the two `secret/` pages.

    The friction this removes is the whole reason the PIN is acceptable at
    all: a four-digit challenge at the moment a scarf sells is enough to mean
    the photo never gets sent. So the cases worth pinning are the ones where
    remembering could quietly do harm — a stale PIN, a departed employee, a
    forged cookie, and above all a cookie that starts standing in for the
    check instead of just typing it.
    """

    def setUp(self):
        self.sam = make_employee("Sam", pin="4821")
        self.hours_url = reverse("hours_entry")
        self.booth_url = reverse("booth_photo")

    def _report_hours(self, **overrides):
        data = {
            "employee": self.sam.pk,
            "pin": "4821",
            "hours": "9.5",
            "work_date": timezone.localdate().isoformat(),
        }
        data.update(overrides)
        return self.client.post(self.hours_url, data)

    def _send_photo(self, **overrides):
        from django.core.files.uploadedfile import SimpleUploadedFile
        data = {
            "employee": self.sam.pk,
            "pin": "4821",
            "reason": BoothPhoto.REASON_SHARE,
            "photo": SimpleUploadedFile(
                "booth.jpg", make_jpeg((60, 40)), content_type="image/jpeg"
            ),
        }
        data.update(overrides)
        return self.client.post(self.booth_url, data)

    def _prefilled(self, url):
        """`(employee pk, pin)` the form at `url` opens with."""
        form = self.client.get(url).context["form"]
        return form.initial.get("employee"), form.initial.get("pin")

    # --- the point of the whole thing ----------------------------------

    def test_reporting_hours_leaves_the_form_filled_in_next_time(self):
        self._report_hours()

        self.assertEqual(self._prefilled(self.hours_url), (self.sam.pk, "4821"))

    def test_one_cookie_covers_both_pages(self):
        """Somebody who has just sent a photo shouldn't have to re-introduce
        themselves to the hours form."""
        self._send_photo()

        self.assertEqual(self._prefilled(self.hours_url), (self.sam.pk, "4821"))

    def test_nothing_is_remembered_before_a_first_submission(self):
        self.assertEqual(self._prefilled(self.booth_url), (None, None))

    # --- it fills the form in; it never stands in for the PIN ----------

    def test_the_cookie_does_not_authorise_a_submission(self):
        """The cookie types for you. It is not a credential, and a POST
        carrying it with the wrong PIN has to fail exactly as before —
        otherwise a found phone submits with no check anywhere."""
        self._report_hours()
        TimeEntry.objects.all().delete()

        self._send_photo(pin="1111")

        self.assertEqual(BoothPhoto.objects.count(), 0)

    def test_a_missing_pin_is_still_a_missing_pin(self):
        self._report_hours()
        TimeEntry.objects.all().delete()

        self._report_hours(pin="")

        self.assertEqual(TimeEntry.objects.count(), 0)

    def test_a_rejected_pin_is_never_remembered(self):
        """Remembering a wrong answer is worse than remembering nothing: the
        page opens looking ready and rejects whatever is submitted."""
        self._report_hours(pin="1111")

        self.assertEqual(self._prefilled(self.hours_url), (None, None))

    # --- a cookie can outlive the facts in it --------------------------

    def test_a_changed_pin_keeps_the_name_and_drops_the_pin(self):
        """The name is still right, so the page still knows who this is and
        asks for the one thing that actually changed."""
        self._report_hours()
        Employee.objects.filter(pk=self.sam.pk).update(pin="9999")

        self.assertEqual(self._prefilled(self.hours_url), (self.sam.pk, None))

    def test_someone_who_has_left_is_forgotten_entirely(self):
        self._report_hours()
        Employee.objects.filter(pk=self.sam.pk).update(is_active=False)

        self.assertEqual(self._prefilled(self.hours_url), (None, None))

    def test_a_forged_cookie_is_ignored_rather_than_trusted(self):
        self.client.cookies[crew.COOKIE] = f"{self.sam.pk}:4821"

        self.assertEqual(self._prefilled(self.hours_url), (None, None))

    # --- not you? ------------------------------------------------------

    def test_the_not_you_link_forgets_both_pages(self):
        """Personal phones make this rare, not never — phones get lent."""
        self._report_hours()

        response = self.client.get(f"{self.hours_url}?{crew.FORGET}=1")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._prefilled(self.booth_url), (None, None))

    def test_the_page_says_the_name_was_filled_in_for_you(self):
        """A pre-filled name nobody mentions is how one person's hours get
        filed under another."""
        self._report_hours()

        response = self.client.get(self.hours_url)

        self.assertContains(response, "Not you?")
        self.assertContains(response, f"{crew.FORGET}=1")


@override_settings(
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_LOCATION_ID="LOC123",
    SQUARE_ENVIRONMENT="sandbox",
    MEDIA_ROOT=tempfile.mkdtemp(),
)
class SquareImageSyncTests(TestCase):
    """`sync_to_square --images`.

    The failure this is built around is a re-run stacking the same photo on
    the same variation: Square appends to `image_ids` and has no way to tell
    it is being handed a picture it already holds, so nothing but our own
    record stops it. Every test here is ultimately about that record being
    written at the right moment.
    """

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.recipe = make_recipe("Stormy Sea")
        self.product = make_product(self.recipe, "Stormy Silk", with_image=False)
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        self.product.refresh_from_db()
        self.image = FinishedProductImage.objects.create(
            finished_product=self.product,
            image=SimpleUploadedFile(
                "stormy.jpg", make_jpeg((60, 40)), content_type="image/jpeg"
            ),
        )

    def _run(self, client, **kwargs):
        out, err = StringIO(), StringIO()
        with mock.patch("square.client.Client", return_value=client):
            call_command("sync_to_square", "--images", stdout=out, stderr=err, **kwargs)
        return out.getvalue() + err.getvalue()

    def _second_photo(self, order=2):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return FinishedProductImage.objects.create(
            finished_product=self.product,
            order=order,
            image=SimpleUploadedFile(
                "stormy-2.jpg", make_jpeg((60, 40)), content_type="image/jpeg"
            ),
        )

    # --- the upload ------------------------------------------------------

    def test_a_photo_is_attached_to_its_variation_not_its_item(self):
        """An item here is a style and every variation under it looks
        completely different — a photo on the item mislabels all but one."""
        client = FakeSquareClient()
        self._run(client)

        self.assertEqual(len(client.images), 1)
        request, payload = client.images[0]
        self.assertEqual(request["object_id"], "SQ_VAR")
        self.assertTrue(request["is_primary"])
        self.assertEqual(request["image"]["type"], "IMAGE")
        self.assertTrue(payload, "the file's bytes have to reach the call")

    def test_the_square_id_is_recorded(self):
        client = FakeSquareClient()
        self._run(client)

        self.image.refresh_from_db()
        self.assertEqual(self.image.square_image_id, "SQ_IMG_1")

    def test_a_second_run_sends_nothing(self):
        """The whole reason the column exists."""
        self._run(FakeSquareClient())

        again = FakeSquareClient()
        output = self._run(again)

        self.assertEqual(again.images, [])
        self.assertIn("already on Square", output)

    def test_only_the_first_photo_on_a_variation_is_primary(self):
        """A later photo must not displace the picture the POS shows."""
        self._second_photo()
        client = FakeSquareClient()
        self._run(client)

        self.assertEqual([r["is_primary"] for r, _ in client.images], [True, False])

    def test_a_photo_added_later_is_not_primary_either(self):
        self._run(FakeSquareClient())
        self._second_photo()

        client = FakeSquareClient()
        self._run(client)

        self.assertEqual(len(client.images), 1)
        self.assertFalse(client.images[0][0]["is_primary"])

    # --- what it can't send ----------------------------------------------

    def test_a_product_square_has_never_seen_is_named_not_dropped(self):
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id=""
        )
        client = FakeSquareClient()
        output = self._run(client)

        self.assertEqual(client.images, [])
        self.assertIn("never seen", output)
        self.assertIn(self.product.sku, output)

    def test_an_external_url_with_no_file_is_named_not_dropped(self):
        """Square's image endpoint takes bytes, and there are none here."""
        FinishedProductImage.objects.filter(pk=self.image.pk).delete()
        FinishedProductImage.objects.create(
            finished_product=self.product,
            image_url="https://example.test/elsewhere.jpg",
        )
        client = FakeSquareClient()
        output = self._run(client)

        self.assertEqual(client.images, [])
        self.assertIn("external URLs", output)

    def test_an_unreadable_file_is_skipped_and_the_run_carries_on(self):
        """One missing object in the bucket is that photo's problem. An API
        error is everybody's problem and stops the run — see below."""
        second = self._second_photo()
        FinishedProductImage.objects.filter(pk=self.image.pk).update(
            image="finished_products/gone.jpg"
        )
        client = FakeSquareClient()
        output = self._run(client)

        self.assertEqual(len(client.images), 1)
        second.refresh_from_db()
        self.assertEqual(second.square_image_id, "SQ_IMG_1")
        self.assertIn("could not be read", output)

    # --- failure ----------------------------------------------------------

    def test_an_api_error_stops_the_run_and_keeps_what_went_up(self):
        self._second_photo()
        client = FakeSquareClient(image_results=[
            FakeSquareResult({"image": {"id": "SQ_IMG_1"}}),
            FakeSquareResult(errors=[{"category": "API_ERROR", "detail": "boom"}]),
        ])
        with self.assertRaises(CommandError) as caught:
            self._run(client)

        self.image.refresh_from_db()
        self.assertEqual(self.image.square_image_id, "SQ_IMG_1",
                         "what succeeded must stay recorded or the re-run duplicates it")
        self.assertIn("boom", str(caught.exception))

    def test_an_accepted_photo_with_no_id_back_stops_the_run(self):
        """The one case where a success is worse than an error: Square has
        the photo, we have nothing to record, and a re-run stacks it."""
        client = FakeSquareClient(image_results=[FakeSquareResult({"image": {}})])
        with self.assertRaises(CommandError) as caught:
            self._run(client)

        self.image.refresh_from_db()
        self.assertEqual(self.image.square_image_id, "")
        self.assertIn("twice", str(caught.exception))

    # --- mode -------------------------------------------------------------

    def test_a_dry_run_uploads_nothing(self):
        client = FakeSquareClient()
        output = self._run(client, dry_run=True)

        self.assertEqual(client.images, [])
        self.assertIn("DRY RUN", output)
        self.image.refresh_from_db()
        self.assertEqual(self.image.square_image_id, "")

    def test_images_is_a_mode_of_its_own(self):
        """Slow and one-at-a-time — it has no business on the schedule that
        pushes stock counts."""
        client = FakeSquareClient()
        self._run(client)

        self.assertEqual(client.upserts, [])
        self.assertEqual(client.inventory_changes, [])


def make_bathable(recipe, name, on_hand=0, par=8, bath=4):
    """A finished product with the stock numbers a production sheet reads."""
    product = make_product(recipe, name, with_image=False)
    FinishedProduct.objects.filter(pk=product.pk).update(
        number_on_hand=on_hand, par=par
    )
    RawProduct.objects.filter(pk=product.raw_product_id).update(
        number_per_dye_bath=bath, number_on_hand=100
    )
    product.refresh_from_db()
    product.raw_product.refresh_from_db()
    return product


class ProductionPlanTests(TestCase):
    """Which baths land on a sheet, and in what order.

    The sheet is a work order somebody walks to a dye room with, so the two
    things worth pinning are that it asks for whole baths and that it asks
    for the right ones — a sheet listing shortages that a bath would round
    away is a sheet that wastes a session.
    """

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea")

    def test_a_row_is_a_bath_not_a_scarf(self):
        """Shortage 8, bath of 4 — two baths, two rows."""
        make_bathable(self.recipe, "Stormy Silk", on_hand=0, par=8, bath=4)

        baths = production.plan_baths(10)

        self.assertEqual(len(baths), 2)
        self.assertEqual([b.quantity for b in baths], [4, 4])

    def test_overshoot_products_are_left_off_by_default(self):
        """Short by less than a bath: dyeing it overshoots par, and the
        shortage gets rounded away next time the recipe runs anyway."""
        make_bathable(self.recipe, "Stormy Silk", on_hand=6, par=8, bath=4)

        self.assertEqual(production.plan_baths(10), [])

    def test_the_checkbox_puts_them_back(self):
        make_bathable(self.recipe, "Stormy Silk", on_hand=6, par=8, bath=4)

        baths = production.plan_baths(10, include_overshoot=True)

        self.assertEqual(len(baths), 1)

    def test_an_empty_shelf_comes_first(self):
        """Zero is the only state a customer can see — a colorway at zero is
        missing from the table, one at half par is a shorter stack."""
        other = make_recipe("Aegean")
        make_bathable(self.recipe, "Stormy Silk", on_hand=4, par=20, bath=4)
        make_bathable(other, "Aegean Silk", on_hand=0, par=8, bath=4)

        baths = production.plan_baths(10)

        self.assertEqual(baths[0].recipe_name, "Aegean")

    def test_baths_of_one_recipe_stay_together(self):
        """One mix, one pot, several loads — consecutive rows share a dye
        bath's setup, which is what makes the session cheaper."""
        other = make_recipe("Aegean")
        make_bathable(self.recipe, "Stormy Silk", on_hand=0, par=8, bath=4)
        make_bathable(self.recipe, "Stormy Wool", on_hand=0, par=8, bath=4)
        make_bathable(other, "Aegean Silk", on_hand=0, par=8, bath=4)

        names = [b.recipe_name for b in production.plan_baths(20)]

        self.assertEqual(len(names), 6)
        # Each recipe appears as exactly one unbroken block. Which recipe
        # leads is an urgency question and tested separately; what matters
        # here is that nobody has to mix the same dye twice.
        blocks = [n for i, n in enumerate(names) if i == 0 or names[i - 1] != n]
        self.assertEqual(len(blocks), len(set(blocks)))
        self.assertEqual(sorted(blocks), ["Aegean", "Stormy Sea"])

    def test_the_limit_is_in_baths(self):
        make_bathable(self.recipe, "Stormy Silk", on_hand=0, par=40, bath=4)

        self.assertEqual(len(production.plan_baths(3)), 3)

    def test_a_category_filter_narrows_it(self):
        wool_cat = RawProductCategory.objects.create(name="Wool")
        product = make_bathable(self.recipe, "Stormy Silk", on_hand=0, par=8, bath=4)
        RawProduct.objects.filter(pk=product.raw_product_id).update(category=wool_cat)

        silk = RawProductCategory.objects.get(name="Silk")
        self.assertEqual(production.plan_baths(10, category=silk), [])
        self.assertEqual(len(production.plan_baths(10, category=wool_cat)), 2)

    def test_it_says_when_there_arent_enough_blanks(self):
        """Said, not enforced — the order may already be placed."""
        product = make_bathable(self.recipe, "Stormy Silk", on_hand=0, par=40, bath=4)
        RawProduct.objects.filter(pk=product.raw_product_id).update(number_on_hand=6)

        baths = production.plan_baths(10)
        short = production.short_blanks(baths)

        self.assertEqual(len(short), 1)
        raw, needed, on_hand = short[0]
        self.assertEqual(on_hand, 6)
        self.assertGreater(needed, 6)


class ProductionSheetViewTests(TestCase):
    """Planning and printing, from the office side."""

    def setUp(self):
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_bathable(self.recipe, "Stormy Silk", on_hand=0, par=8, bath=4)
        self.url = reverse("production_sheet_index")

    def test_previewing_creates_no_run(self):
        """Browsing the options has to leave nothing behind — a run exists
        only once somebody has decided paper will."""
        response = self.client.get(self.url, {"baths": 10})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ProductionRun.objects.count(), 0)

    def test_printing_creates_the_run_and_its_rows(self):
        response = self.client.post(self.url, {"baths": 10})

        run = ProductionRun.objects.get()
        self.assertEqual(run.rows.count(), 2)
        self.assertRedirects(response, reverse("production_run_detail", args=[run.pk]))

    def test_the_row_remembers_the_bath_size_it_printed(self):
        """The paper says x4. If somebody edits the bath size next week this
        row still has to mean what it said in their hand."""
        self.client.post(self.url, {"baths": 10})
        RawProduct.objects.filter(pk=self.product.raw_product_id).update(
            number_per_dye_bath=9
        )

        self.assertEqual(
            list(ProductionRun.objects.get().rows.values_list("quantity", flat=True)),
            [4, 4],
        )

    def test_nothing_to_dye_prints_no_sheet(self):
        FinishedProduct.objects.filter(pk=self.product.pk).update(number_on_hand=99)

        self.client.post(self.url, {"baths": 10})

        self.assertEqual(ProductionRun.objects.count(), 0)

    def test_sheets_you_might_still_be_working_from_are_listed(self):
        """A convenience list, not a queue to be worked off — the record of
        what was dyed is the inventory log."""
        self.client.post(self.url, {"baths": 10})

        response = self.client.get(self.url)

        self.assertContains(response, "still be working from")

    def test_the_pdf_renders(self):
        self.client.post(self.url, {"baths": 10})
        run = ProductionRun.objects.get()

        response = self.client.get(reverse("production_sheet_pdf", args=[run.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))


class ProductionReturnTests(TestCase):
    """The crew's half: one scan, tick what got done, stock moves.

    The failure this is built against is applying a bath twice. The return
    URL is printed on paper that can be scanned again, the submit button can
    be double-tapped, and somebody who remembers one more bath will reopen
    the page — all three are normal, and all three used to be a way for one
    dye bath to be counted into stock more than once.
    """

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_bathable(self.recipe, "Stormy Silk", on_hand=0, par=8, bath=4)
        self.run = ProductionRun.objects.create()
        self.rows = [
            ProductionRunRow.objects.create(
                run=self.run, finished_product=self.product, order=i, quantity=4
            )
            for i in (1, 2)
        ]
        self.url = reverse("production_run", args=[self.run.token])

    def _report(self, *rows):
        return self.client.post(self.url, {"done": [str(r.pk) for r in rows]})

    def test_it_serves_an_anonymous_get(self):
        """The crew have no accounts, and a login here would mean the sheet
        never gets reported."""
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_bad_token_is_not_a_page(self):
        self.assertEqual(
            self.client.get(reverse("production_run", args=["nope"])).status_code, 404
        )

    def test_ticking_a_bath_moves_stock_both_ways(self):
        self._report(self.rows[0])

        self.product.refresh_from_db()
        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 4)
        self.assertEqual(self.product.raw_product.number_on_hand, 96)
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_an_unticked_bath_moves_nothing(self):
        """Ten of twenty is the normal outcome, not an error state."""
        self._report(self.rows[0])

        self.rows[1].refresh_from_db()
        self.assertIsNone(self.rows[1].done_at)
        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 4)

    def test_reporting_the_same_bath_twice_applies_it_once(self):
        self._report(self.rows[0])
        self._report(self.rows[0])

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 4)
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_a_bath_remembered_later_still_goes_in(self):
        """Reopening the page and adding one is normal, and must add rather
        than replace."""
        self._report(self.rows[0])
        self._report(self.rows[1])

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 8)
        self.assertEqual(InventoryLog.objects.count(), 2)

    def test_reporting_closes_the_sheet(self):
        self._report(self.rows[0])

        self.run.refresh_from_db()
        self.assertIsNotNone(self.run.submitted_at)
        self.assertFalse(self.run.is_open)

    def test_an_applied_row_is_shown_not_hidden(self):
        """A row that vanished would read as 'I never ticked that'."""
        self._report(self.rows[0])

        response = self.client.get(self.url)

        self.assertContains(response, "Already recorded")

    def test_the_phone_records_who_reported_if_it_knows(self):
        """A record, not a check — the token on the paper is what lets the
        report through."""
        employee = make_employee("Sam", pin="4821")
        self.client.post(reverse("hours_entry"), {
            "employee": employee.pk, "pin": "4821", "hours": "9.5",
            "work_date": timezone.localdate().isoformat(),
        })

        self._report(self.rows[0])

        self.run.refresh_from_db()
        self.assertEqual(self.run.submitted_by, employee)

    def test_an_unknown_phone_still_reports(self):
        self._report(self.rows[0])

        self.run.refresh_from_db()
        self.assertIsNone(self.run.submitted_by)
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_the_fallback_page_lists_open_sheets(self):
        """For a cracked camera or a photocopied sheet."""
        response = self.client.get(reverse("production_run_index"))

        self.assertContains(response, f"Run {self.run.pk}")

    def test_a_reported_sheet_leaves_the_fallback_list(self):
        self._report(self.rows[0])

        response = self.client.get(reverse("production_run_index"))

        self.assertNotContains(response, f"Run {self.run.pk}")


class BoothSignedInTests(TestCase):
    """A staff login shouldn't be asked to prove itself twice.

    The crew path is unchanged and tested elsewhere; what matters here is
    that the name and PIN come off the page for someone already
    authenticated, and that removing them doesn't quietly remove the
    attribution `BoothPhoto` depends on.
    """

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.SimpleUploadedFile = SimpleUploadedFile
        self.user = User.objects.create_user("owner", password="pw")
        self.employee = Employee.objects.create(name="Robin", pin="4821", user=self.user)
        self.url = reverse("booth_photo")

    def _photo(self):
        return self.SimpleUploadedFile(
            "booth.jpg", make_jpeg((60, 40)), content_type="image/jpeg"
        )

    def test_a_linked_login_is_asked_for_neither(self):
        self.client.force_login(self.user)

        form = self.client.get(self.url).context["form"]

        self.assertNotIn("employee", form.fields)
        self.assertNotIn("pin", form.fields)

    def test_the_photo_is_still_attributed(self):
        """`BoothPhoto.employee` is required on purpose — a sharing
        permission nobody can attribute isn't a permission."""
        self.client.force_login(self.user)

        self.client.post(self.url, {
            "reason": BoothPhoto.REASON_SHARE,
            "photo": self._photo(),
            "share_website": True,
        })

        photo = BoothPhoto.objects.get()
        self.assertEqual(photo.employee, self.employee)
        self.assertTrue(photo.share_website)

    def test_an_unlinked_login_still_picks_a_name(self):
        """The app genuinely doesn't know which employee this is, and
        guessing would put someone else's name on a permission."""
        other = User.objects.create_user("stranger", password="pw")
        self.client.force_login(other)

        form = self.client.get(self.url).context["form"]

        self.assertIn("employee", form.fields)
        self.assertNotIn("pin", form.fields, "the login already proved more than a PIN")

    def test_an_unlinked_login_can_still_send(self):
        other = User.objects.create_user("stranger", password="pw")
        self.client.force_login(other)

        self.client.post(self.url, {
            "employee": self.employee.pk,
            "reason": BoothPhoto.REASON_SHARE,
            "photo": self._photo(),
        })

        self.assertEqual(BoothPhoto.objects.get().employee, self.employee)

    def test_the_crew_are_unaffected(self):
        """Anonymous is still name plus PIN, and a wrong PIN still stops."""
        form = self.client.get(self.url).context["form"]
        self.assertIn("employee", form.fields)
        self.assertIn("pin", form.fields)

        self.client.post(self.url, {
            "employee": self.employee.pk,
            "pin": "1111",
            "reason": BoothPhoto.REASON_SHARE,
            "photo": self._photo(),
        })
        self.assertEqual(BoothPhoto.objects.count(), 0)

    def test_a_signed_in_post_cannot_smuggle_a_pin_field(self):
        """The fields are removed, not hidden — so a hand-built POST has
        nothing to fill in."""
        self.client.force_login(self.user)
        other = Employee.objects.create(name="Someone Else", pin="1234")

        self.client.post(self.url, {
            "employee": other.pk,
            "pin": "1234",
            "reason": BoothPhoto.REASON_SHARE,
            "photo": self._photo(),
        })

        self.assertEqual(BoothPhoto.objects.get().employee, self.employee)

    def test_signing_in_writes_no_crew_cookie(self):
        """There's no PIN to remember, and the login outlives a cookie."""
        self.client.force_login(self.user)

        self.client.post(self.url, {
            "reason": BoothPhoto.REASON_SHARE,
            "photo": self._photo(),
        })

        self.assertNotIn(crew.COOKIE, self.client.cookies)


class BoothReasonHalvesTests(TestCase):
    """Only the half the reason applies to is shown.

    Cosmetic by design — the view already stores only the matching half — so
    what's pinned is that the rule is on the page at all, and that it's the
    kind that still works when the network doesn't.
    """

    def test_each_half_is_addressable_and_hidden_by_the_other_reason(self):
        response = self.client.get(reverse("booth_photo"))
        body = response.content.decode()

        self.assertIn('class="half share"', body)
        self.assertIn('class="half unidentified"', body)
        self.assertIn(
            'form:has(input[name="reason"][value="share"]:checked) .half.unidentified',
            body,
        )

    def test_the_toggle_needs_no_request(self):
        """A stall has one bar of signal; a toggle that needs the network is
        a toggle that sometimes doesn't happen."""
        body = self.client.get(reverse("booth_photo")).content.decode()

        self.assertNotIn("hx-get", body)
        self.assertNotIn("hx-post", body)


class ProductionPickerFeedbackTests(TestCase):
    """A bad number must not read back as "nothing needs dyeing"."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        make_bathable(make_recipe("Stormy Sea"), "Stormy Silk", on_hand=0, par=8, bath=4)
        self.url = reverse("production_sheet_index")

    def test_a_typo_shows_the_error_not_an_empty_queue(self):
        response = self.client.get(self.url, {"baths": "900"})

        self.assertNotContains(response, "Nothing needs dyeing")
        self.assertTrue(response.context["form"].errors)

    def test_a_real_preview_still_answers(self):
        response = self.client.get(self.url, {"baths": "10"})

        self.assertEqual(len(response.context["baths"]), 2)


def link_dye(recipe, name, brand_name="Jacquard", hex_color="#3355aa", in_stock=True, order=1):
    brand, _ = DyeBrand.objects.get_or_create(name=brand_name)
    dye, _ = Dye.objects.get_or_create(
        name=name, brand=brand,
        defaults={"hex_color": hex_color, "in_stock": in_stock},
    )
    Dye.objects.filter(pk=dye.pk).update(in_stock=in_stock)
    dye.refresh_from_db()
    RecipeDye.objects.create(recipe=recipe, dye=dye, order=order)
    return dye


class DyePlanTests(TestCase):
    """The shelf list: one walk instead of twenty.

    The field that matters most is `unrecorded`. A recipe with no dyes on
    file contributes nothing, so an unannounced short list is worse than no
    list at all — you collect what it says, walk to the dye room, and find
    baths whose requirements were never written down.
    """

    def setUp(self):
        # No dyes to start with, so each test says exactly what is on file.
        self.stormy = make_recipe("Stormy Sea", hexes=())
        self.aegean = make_recipe("Aegean", hexes=())

    def test_a_dye_shared_by_two_baths_is_listed_once(self):
        """The whole point — the dyes colorways share are exactly the ones
        you don't want a second trip for."""
        black = link_dye(self.stormy, "Black")
        link_dye(self.aegean, "Black")

        plan = production.dye_plan([self.stormy, self.aegean])

        self.assertEqual([d for d, _ in plan.entries], [black])
        self.assertEqual(plan.entries[0][1], 2, "counted per bath")

    def test_repeated_baths_of_one_recipe_count_each(self):
        """'Get the black out' and 'get a lot of the black out' are
        different instructions."""
        link_dye(self.stormy, "Black")

        plan = production.dye_plan([self.stormy] * 3)

        self.assertEqual(plan.entries[0][1], 3)

    def test_dyes_come_out_in_shelf_order(self):
        link_dye(self.stormy, "Turquoise", brand_name="Dharma")
        link_dye(self.stormy, "Black", brand_name="Jacquard", order=2)

        plan = production.dye_plan([self.stormy])

        self.assertEqual(
            [(d.brand.name, d.name) for d, _ in plan.entries],
            [("Dharma", "Turquoise"), ("Jacquard", "Black")],
        )

    def test_a_recipe_with_no_dyes_is_named_not_skipped(self):
        link_dye(self.stormy, "Black")

        plan = production.dye_plan([self.stormy, self.aegean, self.aegean])

        self.assertFalse(plan.is_complete)
        self.assertEqual(plan.unrecorded, ["Aegean"])
        self.assertEqual(plan.unrecorded_baths, 2, "counted per bath, not per recipe")

    def test_a_fully_recorded_run_says_so(self):
        link_dye(self.stormy, "Black")
        link_dye(self.aegean, "Turquoise")

        plan = production.dye_plan([self.stormy, self.aegean])

        self.assertTrue(plan.is_complete)
        self.assertEqual(plan.unrecorded, [])

    def test_an_out_of_stock_dye_is_surfaced(self):
        """A missing dye is a bath that can't run, and finding that out at
        the sink is the expensive version."""
        gone = link_dye(self.stormy, "Fuchsia", in_stock=False)
        link_dye(self.stormy, "Black", order=2)

        plan = production.dye_plan([self.stormy])

        self.assertEqual(plan.out_of_stock, [gone])

    def test_no_dyes_anywhere_is_not_a_crash(self):
        plan = production.dye_plan([self.stormy])

        self.assertEqual(plan.entries, [])
        self.assertEqual(plan.unrecorded, ["Stormy Sea"])


class DyePlanOnThePageTests(TestCase):
    """Where the list shows up, and how the gap is framed.

    Three dyes fetched in one walk is already worth printing, so the block
    leads with what it covers. The gap is named recipe by recipe and linked
    to the page that fixes it, because a count reads as a chore and six
    names read as an afternoon.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.recipe = make_recipe("Stormy Sea", hexes=())
        make_bathable(self.recipe, "Stormy Silk", on_hand=0, par=8, bath=4)
        self.url = reverse("production_sheet_index")

    def test_the_preview_lists_the_dyes(self):
        link_dye(self.recipe, "Black")

        response = self.client.get(self.url, {"baths": "10"})

        self.assertContains(response, "Dyes to collect")
        self.assertContains(response, "Black")
        self.assertContains(response, "2 baths")

    def test_a_missing_recipe_is_named_and_linked(self):
        response = self.client.get(self.url, {"baths": "10"})

        self.assertContains(response, "no dyes on file")
        self.assertContains(response, "Stormy Sea")
        self.assertContains(response, "missing=true")

    def test_a_complete_run_shows_no_backlog_nag(self):
        link_dye(self.recipe, "Black")

        response = self.client.get(self.url, {"baths": "10"})

        self.assertNotContains(response, "no dyes on file")

    def test_a_printed_run_carries_the_list_too(self):
        link_dye(self.recipe, "Black")
        self.client.post(self.url, {"baths": "10"})
        run = ProductionRun.objects.get()

        response = self.client.get(reverse("production_run_detail", args=[run.pk]))

        self.assertContains(response, "Dyes to collect")
        self.assertContains(response, "Black")

    def test_the_pdf_still_renders_with_a_dye_page(self):
        link_dye(self.recipe, "Black")
        self.client.post(self.url, {"baths": "10"})
        run = ProductionRun.objects.get()

        response = self.client.get(reverse("production_sheet_pdf", args=[run.pk]))

        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_the_pdf_renders_when_no_recipe_has_dyes(self):
        """The common case today, and it must not be the one that breaks."""
        self.client.post(self.url, {"baths": "10"})
        run = ProductionRun.objects.get()

        response = self.client.get(reverse("production_sheet_pdf", args=[run.pk]))

        self.assertTrue(response.content.startswith(b"%PDF"))


def post_square_order(test_client, order_id, variation_id, qty,
                     sold_at="2026-08-15T18:30:00Z"):
    """Drive the webhook with one line item for `variation_id`.

    Signs the payload the way Square does, so the view's own signature check
    runs rather than being bypassed.
    """
    payload = json.dumps({
        "type": "order.updated",
        "data": {"object": {"order_updated": {
            "state": "COMPLETED", "order_id": order_id,
        }}},
    })
    signature = base64.b64encode(
        hmac.new(
            b"test-signature-key",
            (settings.SQUARE_WEBHOOK_URL + payload).encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode()
    order = {
        "line_items": [{
            "uid": "L1", "catalog_object_id": variation_id,
            "quantity": str(qty), "name": "Yarn",
        }],
        "closed_at": sold_at,
    }
    with mock.patch("square.client.Client") as client:
        client.return_value.orders.retrieve_order.return_value = FakeSquareResult(
            {"order": order}
        )
        return test_client.post(
            reverse("square_webhook"),
            data=payload,
            content_type="application/json",
            HTTP_X_SQUARE_HMACSHA256_SIGNATURE=signature,
        )


def make_undyed(name, category_name="Yarn", group=None, on_hand=0, par_level=10):
    """A yarn sold exactly as it arrives: raw product, no recipe."""
    category, _ = RawProductCategory.objects.get_or_create(name=category_name)
    raw = RawProduct.objects.create(
        name=name, category=category, price="9.00",
        number_on_hand=on_hand, par_level=par_level,
        catalog_group=group,
    )
    product = FinishedProduct.objects.create(
        name=name, raw_product=raw, recipe=None, price="18.00",
    )
    product.refresh_from_db()
    return product


class PassthroughStockTests(TestCase):
    """An undyed yarn is one pile with two rows pointing at it.

    That is the whole difference from a dyed scarf, where the raw blank and
    the finished item are two piles and the dye bath is what moves one to the
    other. Two independently-kept counts for one pile drift, silently, and in
    the direction that matters — the reorder signal is the entire reason this
    stock is tracked at all.
    """

    def setUp(self):
        self.product = make_undyed("Merino Worsted Natural", on_hand=12)
        self.raw = self.product.raw_product

    def test_it_knows_it_was_never_dyed(self):
        self.assertTrue(self.product.is_passthrough)
        self.assertIsNone(self.product.recipe)

    def test_the_count_follows_the_raw_pile(self):
        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 12)

    def test_booking_in_a_delivery_moves_both_rows(self):
        self.raw.number_on_hand = 30
        self.raw.save()

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 30)

    def test_a_dyed_product_is_left_alone(self):
        """The mirror must not reach past the passthroughs — a scarf's raw
        blank and finished stock are genuinely different numbers."""
        recipe = make_recipe("Stormy Sea")
        dyed = make_product(recipe, "Stormy Silk", with_image=False)
        FinishedProduct.objects.filter(pk=dyed.pk).update(number_on_hand=5)

        dyed.raw_product.number_on_hand = 99
        dyed.raw_product.save()

        dyed.refresh_from_db()
        self.assertEqual(dyed.number_on_hand, 5)

    def test_it_gets_a_sku_shaped_like_every_other(self):
        """`BLANK-DYEBATH` is what the unidentified-sales page reads the
        first six characters of; a passthrough can't be the one without a
        dash."""
        self.assertEqual(self.product.sku, "MERINO-UNDYED")

    def test_two_yarns_that_slug_alike_still_get_their_own(self):
        other = make_undyed("Merino DK Natural")
        self.assertNotEqual(other.sku, self.product.sku)

    def test_the_variation_is_named_for_the_yarn(self):
        """The item is the group, so the thing being chosen between is the
        blank."""
        self.assertEqual(self.product.variation_name, "Merino Worsted Natural")


@override_settings(
    SQUARE_WEBHOOK_SIGNATURE_KEY="test-signature-key",
    SQUARE_WEBHOOK_URL="https://example.test/scarves/webhooks/square",
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_ENVIRONMENT="sandbox",
)
class PassthroughSaleTests(TestCase):
    """A sale has to come off the pile the reorder page reads."""

    def setUp(self):
        self.product = make_undyed("Merino Worsted Natural", on_hand=12)
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        self.product.refresh_from_db()

    def _sell(self, qty=2, order_id="ORDER-1"):
        return post_square_order(self.client, order_id, "SQ_VAR", qty)

    def test_a_sale_decrements_the_raw_stock(self):
        self._sell(qty=2)

        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.raw_product.number_on_hand, 10)

    def test_the_finished_row_follows(self):
        self._sell(qty=2)

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 10)

    def test_it_is_still_logged_as_a_sale(self):
        self._sell(qty=2)

        log = InventoryLog.objects.get()
        self.assertEqual(log.log_type, InventoryLog.SALE)
        self.assertEqual(log.quantity, -2)
        self.assertEqual(log.raw_product, self.product.raw_product)

    def test_a_redelivered_order_still_only_counts_once(self):
        self._sell(qty=2)
        self._sell(qty=2)

        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.raw_product.number_on_hand, 10)


class PassthroughIsNotProducedTests(TestCase):
    """You order these; you don't dye them."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.product = make_undyed("Merino Worsted Natural", on_hand=0)
        FinishedProduct.objects.filter(pk=self.product.pk).update(par=10)

    def test_it_never_reaches_a_production_sheet(self):
        """Without this the sheet says '4 × ' with no colorway and sends
        somebody to the dye room for something that arrives in a box."""
        self.assertEqual(production.plan_baths(20), [])

    def test_it_is_not_on_the_production_page_either(self):
        response = self.client.get(reverse("production_needed"))

        self.assertNotContains(response, "Merino Worsted Natural")

    def test_its_shortfall_shows_where_ordering_happens(self):
        """The raw inventory page is the reorder workflow, and it already
        works — that is the point of keeping the pile on the raw row."""
        raw = self.product.raw_product
        response = self.client.get(
            reverse("raw_inventory", args=[raw.category_id])
        )

        self.assertContains(response, "Merino Worsted Natural")


@override_settings(
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_LOCATION_ID="LOC123",
    SQUARE_ENVIRONMENT="sandbox",
)
class PassthroughCatalogTests(TestCase):
    """One Square item, variations named for the yarns under it."""

    def setUp(self):
        self.category = RawProductCategory.objects.create(name="Yarn")
        self.group = CatalogGroup.objects.create(
            name="Undyed Yarn", category=self.category
        )
        self.merino = make_undyed("Merino Worsted Natural", group=self.group, on_hand=5)
        self.bfl = make_undyed("BFL DK Ecru", group=self.group, on_hand=3)

    def _run(self, client, **kwargs):
        out, err = StringIO(), StringIO()
        with mock.patch("square.client.Client", return_value=client):
            call_command("sync_to_square", stdout=out, stderr=err, **kwargs)
        return out.getvalue() + err.getvalue()

    def test_the_group_goes_up_as_one_item(self):
        client = FakeSquareClient()
        self._run(client)

        items = [o for o in client.upserts[0]["batches"][0]["objects"]
                 if o["type"] == "ITEM"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item_data"]["name"], "Undyed Yarn")

    def test_each_yarn_is_a_variation_named_for_itself(self):
        client = FakeSquareClient()
        self._run(client)

        item = [o for o in client.upserts[0]["batches"][0]["objects"]
                if o["type"] == "ITEM"][0]
        names = sorted(
            v["item_variation_data"]["name"] for v in item["item_data"]["variations"]
        )
        self.assertEqual(names, ["BFL DK Ecru", "Merino Worsted Natural"])

    def test_the_group_id_is_written_back(self):
        """Losing it means the next run creates a second 'Undyed Yarn' and
        splits the shelf across two items."""
        client = FakeSquareClient(upsert_results=[FakeSquareResult({
            "id_mappings": [
                {"client_object_id": f"#cg_{self.group.pk}", "object_id": "SQ_ITEM"},
            ],
        })])
        self._run(client)

        self.group.refresh_from_db()
        self.assertEqual(self.group.square_item_id, "SQ_ITEM")

    def test_a_known_group_sends_only_new_variations(self):
        CatalogGroup.objects.filter(pk=self.group.pk).update(square_item_id="SQ_ITEM")
        FinishedProduct.objects.filter(pk=self.merino.pk).update(
            square_variation_id="SQ_VAR"
        )
        client = FakeSquareClient()
        self._run(client)

        objects = client.upserts[0]["batches"][0]["objects"]
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]["type"], "ITEM_VARIATION")
        self.assertEqual(objects[0]["item_variation_data"]["item_id"], "SQ_ITEM")
        self.assertEqual(
            objects[0]["item_variation_data"]["name"], "BFL DK Ecru"
        )

    def test_the_stock_pushed_is_the_raw_pile(self):
        FinishedProduct.objects.filter(pk=self.merino.pk).update(
            square_variation_id="SQ_VAR"
        )
        client = FakeSquareClient()
        self._run(client, inventory_only=True)

        counts = {
            c["physical_count"]["catalog_object_id"]: c["physical_count"]["quantity"]
            for c in client.inventory_changes[0]["changes"]
        }
        self.assertEqual(counts["SQ_VAR"], "5")

    def test_update_points_a_grouped_variation_at_its_group(self):
        """Reading raw_product.square_item_id here would send a blank item id
        and move the variation to nowhere."""
        CatalogGroup.objects.filter(pk=self.group.pk).update(square_item_id="SQ_ITEM")
        FinishedProduct.objects.filter(pk=self.merino.pk).update(
            square_variation_id="SQ_VAR"
        )
        client = FakeSquareClient(retrieve_result=FakeSquareResult({
            "objects": [{"id": "SQ_VAR", "version": 7}],
        }))
        self._run(client, update=True)

        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(sent["item_variation_data"]["item_id"], "SQ_ITEM")

    def test_an_ungrouped_blank_is_still_its_own_item(self):
        """Everything dyed leaves catalog_group blank, and nothing about it
        changes."""
        recipe = make_recipe("Stormy Sea")
        make_product(recipe, "Stormy Silk", with_image=False)
        client = FakeSquareClient()
        self._run(client)

        items = [o for o in client.upserts[0]["batches"][0]["objects"]
                 if o["type"] == "ITEM"]
        self.assertEqual(len(items), 2, "the group, plus the scarf's own item")


class PassthroughStockTakeTests(TestCase):
    """A stock take has to land on the row that holds the pile."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.product = make_undyed("Merino Worsted Natural", on_hand=12)

    def test_counting_them_writes_through_to_the_raw_row(self):
        """Writing the finished count instead would be writing to a mirror —
        `save()` re-derives it, so the number snaps back and the stock take
        looks like it never happened."""
        self.product.set_on_hand(20)

        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.raw_product.number_on_hand, 20)

    def test_the_finished_row_agrees_afterwards(self):
        self.product.set_on_hand(20)

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 20)

    def test_a_dyed_product_is_written_directly(self):
        dyed = make_product(make_recipe("Stormy Sea"), "Stormy Silk", with_image=False)
        dyed.raw_product.number_on_hand = 40
        dyed.raw_product.save()

        dyed.set_on_hand(7)

        dyed.refresh_from_db()
        dyed.raw_product.refresh_from_db()
        self.assertEqual(dyed.number_on_hand, 7)
        self.assertEqual(dyed.raw_product.number_on_hand, 40, "raw is untouched")

    def test_it_is_not_offered_for_card_backfill(self):
        """A kanban card records a dye bath; there wasn't one."""
        response = self.client.get(reverse("card_backfill_index"))

        self.assertNotContains(response, "Merino Worsted Natural")


class OutstandingSheetCapTests(TestCase):
    """Only the newest few sheets stay outstanding.

    Five out at once already means the reporting loop has stopped working.
    But *blocking* a sixth print deadlocks exactly when the paper has gone
    missing, which is the same moment a sheet gets abandoned — so the newest
    five are kept and the rest retire. Nothing is lost by that: a run is a
    work aid, and the record of what was actually dyed is the inventory log.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        make_bathable(make_recipe("Stormy Sea"), "Stormy Silk", on_hand=0, par=80, bath=4)
        self.url = reverse("production_sheet_index")

    def _print(self):
        return self.client.post(self.url, {"baths": "2"})

    def _open_runs(self):
        return list(
            ProductionRun.objects.filter(submitted_at__isnull=True)
            .order_by("-created_at", "-pk")
        )

    def test_printing_is_never_refused(self):
        for _ in range(production.MAX_OPEN_RUNS + 3):
            self._print()

        self.assertEqual(
            ProductionRun.objects.count(), production.MAX_OPEN_RUNS + 3
        )

    def test_only_the_newest_stay_outstanding(self):
        for _ in range(production.MAX_OPEN_RUNS + 2):
            self._print()

        self.assertEqual(len(self._open_runs()), production.MAX_OPEN_RUNS)

    def test_it_is_the_oldest_that_go(self):
        """Most recent five, not a random five."""
        for _ in range(production.MAX_OPEN_RUNS + 2):
            self._print()

        newest = list(
            ProductionRun.objects.order_by("-created_at", "-pk")[:production.MAX_OPEN_RUNS]
        )
        self.assertEqual([r.pk for r in self._open_runs()], [r.pk for r in newest])

    def test_a_retired_sheet_is_closed_not_deleted(self):
        for _ in range(production.MAX_OPEN_RUNS + 1):
            self._print()

        retired = ProductionRun.objects.exclude(submitted_at__isnull=True).get()
        self.assertIn("Closed automatically", retired.note)
        self.assertEqual(retired.done_count, 0)

    def test_retiring_moves_no_stock(self):
        """A sheet nobody reported describes a session that didn't happen."""
        for _ in range(production.MAX_OPEN_RUNS + 1):
            self._print()

        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_one_tick_closes_a_sheet(self):
        """Somebody is working from it, so the loop is closing — it doesn't
        need to keep showing up as outstanding."""
        self._print()
        run = ProductionRun.objects.get()
        self.client.post(
            reverse("production_run", args=[run.token]),
            {"done": [str(run.rows.first().pk)]},
        )

        self.assertEqual(self._open_runs(), [])

    def test_a_closed_sheet_is_still_reachable_by_its_code(self):
        """The QR is how you get back to it, and adding the rest later has
        to keep working."""
        self._print()
        run = ProductionRun.objects.get()
        rows = list(run.rows.all())
        url = reverse("production_run", args=[run.token])
        self.client.post(url, {"done": [str(rows[0].pk)]})

        self.assertEqual(self.client.get(url).status_code, 200)

        self.client.post(url, {"done": [str(rows[1].pk)]})
        run.refresh_from_db()
        self.assertEqual(run.done_count, 2)


def sheet_photo(rows, filled=(), ink=(190, 30, 40), token=None, scale=5.0,
                partial=()):
    """A synthetic photograph of a printed production sheet.

    Draws the same geometry `production._draw_row` does — the box from
    `BOX_LEFT`/`BOX_BASELINE_OFFSET`, the bars from the very symbol
    `barcode_symbol()` returns — so a test that reads it back is exercising
    the real agreement between what the PDF prints and what the scanner
    looks for. `ink` is a colour rather than "black" on purpose: the whole
    question about pen colour is answered by passing a different one.
    """
    from string import ascii_lowercase, ascii_uppercase

    from io import BytesIO

    from PIL import Image, ImageDraw
    from reportlab.lib.pagesizes import letter

    page_w, page_h = letter
    image = Image.new("RGB", (int(page_w * scale), int(page_h * scale)), "white")
    draw = ImageDraw.Draw(image)

    def px(x, y):
        """PDF point to image pixel — y flips, and rounds to a whole pixel.

        Rounding here rather than letting PIL do it per edge is what makes
        adjacent bars tile exactly. Left to floats, a module a few pixels
        wide picks up half a pixel of error per edge, which is a large
        fraction of a bar and enough to stop the symbol decoding.
        """
        return round(x * scale), round((page_h - y) * scale)

    if token:
        widget = __import__(
            "reportlab.graphics.barcode.qr", fromlist=["qr"]
        ).QrCodeWidget(f"https://x.test/scarves/secret/production/{token}/")
        widget.qr.make()
        count = widget.qr.getModuleCount()
        module = production.QR_SIZE / count
        origin_x = page_w - production.PAGE_MARGIN - production.QR_SIZE
        origin_y = page_h - production.PAGE_MARGIN
        for r in range(count):
            for c in range(count):
                if not widget.qr.isDark(r, c):
                    continue
                a = px(origin_x + c * module, origin_y - r * module)
                b = px(origin_x + (c + 1) * module, origin_y - (r + 1) * module)
                draw.rectangle([a[0], a[1], b[0] - 1, b[1] - 1], fill="black")

    y = page_h - production.PAGE_MARGIN - production.HEADER_HEIGHT
    for index, row in enumerate(rows):
        baseline = y - production.ROW_HEIGHT + 12

        box_x = production.BOX_LEFT
        box_y = baseline + production.BOX_BASELINE_OFFSET
        top_left = px(box_x, box_y + production.BOX_SIZE)
        bottom_right = px(box_x + production.BOX_SIZE, box_y)
        draw.rectangle(
            [top_left[0], top_left[1], bottom_right[0], bottom_right[1]],
            outline="black", width=max(int(1.6 * scale), 1),
        )
        if index in filled or index in partial:
            # A partial mark is a corner smudge — enough ink to notice, not
            # enough to be an answer.
            pad = (production.BOX_SIZE * (0.34 if index in partial else 0.12)) * scale
            draw.rectangle(
                [top_left[0] + pad, top_left[1] + pad,
                 bottom_right[0] - pad, bottom_right[1] - pad],
                fill=ink,
            )

        symbol = production.barcode_symbol(production.row_code(row))
        symbol.validated = symbol.validate()
        symbol.encoded = symbol.encode()
        left = (production.BOX_LEFT + production.BOX_SIZE
                + production.BOX_TO_BARCODE + symbol.lquiet)
        bar_top = baseline + production.BARCODE_BASELINE_OFFSET + production.BARCODE_HEIGHT
        bar_bottom = baseline + production.BARCODE_BASELINE_OFFSET
        for char in symbol.decompose():
            if char in ascii_lowercase:
                left += (ord(char) - ord("a") + 1) * symbol.barWidth
            elif char in ascii_uppercase:
                width = (ord(char) - ord("A") + 1) * symbol.barWidth
                a = px(left, bar_top)
                b = px(left + width, bar_bottom)
                draw.rectangle([a[0], a[1], b[0] - 1, b[1]], fill="black")
                left += width
        y -= production.ROW_HEIGHT

    out = BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


class SheetScanTests(TestCase):
    """Reading tick boxes off a photo of a sheet.

    The barcode does the hard part: every row prints one a fixed distance
    from its box, so a decoded symbol gives the row's identity *and* the
    position and scale of everything beside it. Finding a box is then
    arithmetic rather than checkbox recognition.

    Nothing here applies anything — the scan fills the form in and a person
    submits it, which is what makes it safe to be approximate.
    """

    def setUp(self):
        self.run = ProductionRun.objects.create()
        self.rows = []
        for i, (recipe_name, product_name) in enumerate([
            ("Stormy Sea", "Silk Infinity"),
            ("Aegean", "Silk Rectangle"),
            ("Ember", "Wool Wrap"),
            ("Moss", "Silk Square"),
        ], start=1):
            recipe = make_recipe(recipe_name, hexes=())
            product = make_bathable(recipe, product_name, on_hand=0, par=8, bath=4)
            self.rows.append(ProductionRunRow.objects.create(
                run=self.run, finished_product=product, order=i, quantity=4,
            ))
        self.codes = [production.row_code(r) for r in self.rows]

    def _read(self, **kwargs):
        return sheetscan.read_sheet(sheet_photo(self.rows, **kwargs))

    # --- the reading ------------------------------------------------------

    def test_every_row_is_found(self):
        scan = self._read()

        self.assertEqual(scan.error, "")
        self.assertEqual(len(scan.marks), len(self.rows))

    def test_a_filled_box_reads_filled_and_a_blank_one_blank(self):
        scan = self._read(filled=(0, 2))

        states = {m.code: m.state for m in scan.marks}
        self.assertEqual(states[self.codes[0]], sheetscan.FILLED)
        self.assertEqual(states[self.codes[1]], sheetscan.EMPTY)
        self.assertEqual(states[self.codes[2]], sheetscan.FILLED)
        self.assertEqual(states[self.codes[3]], sheetscan.EMPTY)

    def test_a_red_pen_works(self):
        """Luminance, not blackness — red sits far nearer black than paper."""
        scan = self._read(filled=(0,), ink=(200, 25, 35))

        self.assertEqual(len(scan.filled), 1)

    def test_a_pencil_works(self):
        scan = self._read(filled=(0,), ink=(105, 105, 108))

        self.assertEqual(len(scan.filled), 1)

    def test_a_blue_pen_works(self):
        scan = self._read(filled=(0,), ink=(25, 45, 160))

        self.assertEqual(len(scan.filled), 1)

    def test_a_yellow_highlighter_does_not(self):
        """It is about as bright as the page. This is why the sheet says any
        pen but yellow, and it has to fail as 'empty' rather than 'maybe'."""
        scan = self._read(filled=(0,), ink=(255, 246, 90))

        self.assertEqual(scan.filled, [])

    def test_a_smudge_is_reported_unsure_rather_than_guessed(self):
        """There is somewhere for an ambiguous mark to go, and it is a
        person's eyes."""
        scan = self._read(partial=(1,))

        self.assertEqual([m.code for m in scan.unsure], [self.codes[1]])

    # --- scale and framing -------------------------------------------------

    def test_it_does_not_care_how_close_the_phone_was(self):
        """Scale comes off each barcode's own width, so a tighter or wider
        frame is the same reading."""
        for scale in (4.0, 7.0):
            scan = sheetscan.read_sheet(
                sheet_photo(self.rows, filled=(0, 2), scale=scale),
            )
            self.assertEqual(len(scan.filled), 2, f"at scale {scale}")

    def test_rows_not_on_this_run_are_reported(self):
        """Expected to be empty forever; if it isn't, the photo is of some
        other sheet and the matched marks would land here unremarked."""
        scan = sheetscan.read_sheet(sheet_photo(self.rows, filled=(0,)))

        self.assertEqual(sheetscan.strays(self.run, scan), [])

    def test_junk_is_an_error_not_a_crash(self):
        scan = sheetscan.read_sheet(b"not a photo")

        self.assertTrue(scan.error)
        self.assertEqual(scan.marks, [])

    # --- the wrong sheet ---------------------------------------------------

    def test_the_qr_confirms_which_sheet_this_is(self):
        scan = sheetscan.read_sheet(
            sheet_photo(self.rows, filled=(0,), token=self.run.token))

        self.assertEqual(scan.qr_token, self.run.token)
        self.assertEqual(len(scan.filled), 1)

    def test_a_photo_of_another_sheet_is_refused(self):
        """Two sheets printed days apart share most of their rows, so marks
        off the wrong one land on rows that look right."""
        scan = sheetscan.read_sheet(
            sheet_photo(self.rows, filled=(0, 1, 2), token="SOMEOTHERTOKEN"))

        self.assertEqual(scan.qr_token, "SOMEOTHERTOKEN")

    # --- turning marks into rows -------------------------------------------

    def test_marks_become_rows_to_tick(self):
        scan = self._read(filled=(0, 2))

        ticked = sheetscan.rows_to_tick(self.run, scan)

        self.assertEqual(set(ticked), {self.rows[0].pk, self.rows[2].pk})

    def test_repeated_baths_of_one_colorway_are_counted_separately(self):
        """The case a sheet is *expected* to contain — `plan_baths` groups
        repeated baths together on purpose. A decoder returns one result per
        distinct symbol, so SKU-only barcodes would collapse these into one
        and report a single bath where three were marked."""
        extra = ProductionRunRow.objects.create(
            run=self.run, finished_product=self.rows[0].finished_product,
            order=9, quantity=4,
        )
        rows = self.rows + [extra]
        scan = sheetscan.read_sheet(sheet_photo(rows, filled=(0, 4)))

        self.assertEqual(len(scan.filled), 2)
        ticked = sheetscan.rows_to_tick(self.run, scan)
        self.assertEqual(set(ticked), {self.rows[0].pk, extra.pk})

    def test_re_reading_the_same_photo_ticks_nothing_new(self):
        """Somebody re-uploads the original picture after it has been acted
        on. The marks for rows already recorded must not go looking for
        another row to land on."""
        scan = self._read(filled=(0, 1))
        for pk in sheetscan.rows_to_tick(self.run, scan):
            production.apply_row(ProductionRunRow.objects.get(pk=pk))

        again = self._read(filled=(0, 1))

        self.assertEqual(sheetscan.rows_to_tick(self.run, again), [])

    def test_an_already_recorded_row_is_not_offered_again(self):
        production.apply_row(self.rows[0])
        scan = self._read(filled=(0,))

        self.assertEqual(sheetscan.rows_to_tick(self.run, scan), [])


class RunCodeTests(TestCase):
    """The code on a sheet, which a person reads off paper and types."""

    def test_it_reads_as_words(self):
        token = new_run_token()
        number, adjective, animal = token.split("-")

        self.assertEqual(len(number), 2)
        self.assertTrue(number.isdigit())
        self.assertIn(adjective, RUN_ADJECTIVES)
        self.assertIn(animal, RUN_ANIMALS)

    def test_it_fits_the_column(self):
        longest = max(len(new_run_token()) for _ in range(2000))
        field = ProductionRun._meta.get_field("token")

        self.assertLessEqual(longest, field.max_length)

    def test_typing_it_is_forgiving(self):
        """Punctuation and case are how a phone keyboard differs from a
        printed page, not how one sheet differs from another."""
        token = "42-brisk-wombat"

        for typed in ("42-brisk-wombat", "42 Brisk Wombat", "42BRISKWOMBAT",
                      "  42-BRISK-WOMBAT  "):
            self.assertEqual(normalize_token(typed), normalize_token(token), typed)

    def test_a_different_code_is_still_different(self):
        self.assertNotEqual(
            normalize_token("42-brisk-wombat"), normalize_token("43-brisk-wombat")
        )


class PhotoUploadFlowTests(TestCase):
    """Camera first: photograph any sheet at one page, and the photo says
    which run it is.

    That is what makes the QR do real work — it isn't a second presentation
    of something the address bar already proved, it is the only thing that
    names the sheet.
    """

    def setUp(self):
        self.run = ProductionRun.objects.create()
        recipe = make_recipe("Stormy Sea", hexes=())
        self.product = make_bathable(recipe, "Silk Infinity", on_hand=0, par=8, bath=4)
        self.rows = [
            ProductionRunRow.objects.create(
                run=self.run, finished_product=self.product, order=i, quantity=4)
            for i in (1, 2, 3)
        ]
        self.url = reverse("production_upload")

    def _send(self, rows=None, **kwargs):
        from django.core.files.uploadedfile import SimpleUploadedFile
        photo = sheet_photo(rows if rows is not None else self.rows, **kwargs)
        return self.client.post(self.url, {
            "sheet": SimpleUploadedFile("s.png", photo, content_type="image/png"),
        })

    def test_it_serves_an_anonymous_get(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_photo_lands_on_its_own_run_pre_ticked(self):
        response = self._send(filled=(0, 2), token=self.run.token)

        self.assertEqual(response.status_code, 302)
        self.assertIn(self.run.token, response["Location"])
        self.assertIn("done=", response["Location"])

        page = self.client.get(response["Location"])
        self.assertEqual(page.context["prefilled"], {self.rows[0].pk, self.rows[2].pk})

    def test_nothing_is_recorded_by_uploading(self):
        response = self._send(filled=(0, 2), token=self.run.token)
        self.client.get(response["Location"])

        self.assertEqual(InventoryLog.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 0)

    def test_submitting_on_the_run_page_is_what_records(self):
        response = self._send(filled=(0,), token=self.run.token)
        self.client.get(response["Location"])
        self.client.post(
            reverse("production_run", args=[self.run.token]),
            {"done": [str(self.rows[0].pk)]},
        )

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 4)

    def test_no_readable_code_asks_for_it_and_then_hands_over(self):
        """Nearly always a soft photo rather than a wrong sheet, so this is a
        way through rather than an interrogation."""
        self._send(filled=(0,))
        page = self.client.get(self.url)
        self.assertContains(page, "Type the code printed beside it")

        response = self.client.post(self.url, {"sheet_code": self.run.token})

        self.assertEqual(response.status_code, 302)
        page = self.client.get(response["Location"])
        self.assertEqual(page.context["prefilled"], {self.rows[0].pk})

    def test_a_typed_code_is_forgiving(self):
        self._send(filled=(0,))

        response = self.client.post(self.url, {
            "sheet_code": self.run.token.replace("-", " ").upper(),
        })

        self.assertEqual(response.status_code, 302)

    def test_an_unknown_typed_code_says_so(self):
        self._send(filled=(0,))

        self.client.post(self.url, {"sheet_code": "99-wrong-badger"})
        page = self.client.get(self.url)

        self.assertContains(page, "99-wrong-badger")

    def test_an_unreadable_photo_says_so(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.post(self.url, {
            "sheet": SimpleUploadedFile("s.png", b"junk", content_type="image/png"),
        })

        self.assertContains(self.client.get(self.url), "Try again")

    def test_the_run_page_reports_what_the_photo_missed(self):
        """The everyday failure is a soft photo, and it fails partially."""
        response = self._send(rows=self.rows[:2], filled=(0,), token=self.run.token)

        page = self.client.get(response["Location"])

        self.assertEqual(page.context["scan"]["read"], 2)
        self.assertEqual(page.context["scan"]["total"], 3)
        self.assertContains(page, "come out of the photo")

    def test_a_stale_link_degrades_to_an_empty_form(self):
        """Ids that aren't this run's are dropped rather than half-ticking a
        page from some other sheet."""
        other = ProductionRun.objects.create()
        url = reverse("production_run", args=[self.run.token])

        page = self.client.get(f"{url}?done=99999&done=abc&read=3&filled=1")

        self.assertEqual(page.context["prefilled"], set())
        self.assertEqual(other.rows.count(), 0)

    def test_an_already_recorded_row_is_not_re_ticked(self):
        production.apply_row(self.rows[0])
        response = self._send(filled=(0, 1), token=self.run.token)

        page = self.client.get(response["Location"])

        self.assertEqual(page.context["prefilled"], {self.rows[1].pk})

    def test_the_run_page_no_longer_takes_photos(self):
        """Arriving at the run URL first means answering by hand — the user's
        'what not to do' path — so the camera lives on the upload page."""
        page = self.client.get(reverse("production_run", args=[self.run.token]))

        self.assertNotContains(page, 'name="sheet"')
        self.assertContains(page, reverse("production_upload"))


class CreatePassthroughProductsTests(TestCase):
    """The one-off that makes the sellable half of each undyed yarn.

    Creating one is two rows — a raw product for the pile, a finished product
    for the thing Square sells — and the second is mechanical enough to be
    worth doing in a pass rather than by hand per yarn.
    """

    def setUp(self):
        self.category = RawProductCategory.objects.create(name="Yarn")
        self.group = CatalogGroup.objects.create(
            name="Undyed Yarn", category=self.category
        )
        self.merino = RawProduct.objects.create(
            name="Merino Worsted Natural", category=self.category,
            price="9.00", suggested_price="24.00", catalog_group=self.group,
            number_on_hand=12, par_level=10,
        )
        self.bfl = RawProduct.objects.create(
            name="BFL DK Ecru", category=self.category, price="8.00",
            catalog_group=self.group, number_on_hand=4,
        )

    def _run(self, **kwargs):
        out, err = StringIO(), StringIO()
        call_command("create_passthrough_products", group="Undyed Yarn",
                     stdout=out, stderr=err, **kwargs)
        return out.getvalue() + err.getvalue()

    def test_it_makes_one_per_raw_product(self):
        self._run()

        self.assertEqual(FinishedProduct.objects.count(), 2)
        names = set(FinishedProduct.objects.values_list("name", flat=True))
        self.assertEqual(names, {"Merino Worsted Natural", "BFL DK Ecru"})

    def test_they_are_passthroughs(self):
        self._run()

        for product in FinishedProduct.objects.all():
            self.assertTrue(product.is_passthrough)
            self.assertIsNone(product.recipe)

    def test_the_suggested_price_is_used_when_there_is_one(self):
        self._run()

        product = FinishedProduct.objects.get(name="Merino Worsted Natural")
        self.assertEqual(product.price, Decimal("24.00"))

    def test_a_missing_price_is_conspicuous_and_reported(self):
        """A plausible price might reach a customer unlooked-at; a pound
        gets noticed and fixed."""
        output = self._run()

        product = FinishedProduct.objects.get(name="BFL DK Ecru")
        self.assertEqual(product.price, Decimal("1.00"))
        self.assertIn("no usable suggested price", output)
        self.assertIn("BFL DK Ecru", output)

    def test_a_deliberate_zero_is_honoured(self):
        """Null and zero are different things — the field is nullable, so
        null means nobody set a price and zero means somebody set it. A
        giveaway is a real product."""
        RawProduct.objects.filter(pk=self.bfl.pk).update(suggested_price="0.00")

        self._run()

        self.assertEqual(
            FinishedProduct.objects.get(name="BFL DK Ecru").price, Decimal("0.00")
        )

    def test_a_free_item_is_reported(self):
        """Free is the one price nobody notices until it has been charged."""
        RawProduct.objects.filter(pk=self.bfl.pk).update(suggested_price="0.00")

        output = self._run()

        self.assertIn("ring up free", output)
        self.assertIn("BFL DK Ecru", output)

    def test_a_missing_price_is_not_treated_as_free(self):
        self._run()

        self.assertEqual(
            FinishedProduct.objects.get(name="BFL DK Ecru").price, Decimal("1.00")
        )

    def test_they_get_skus_without_being_asked(self):
        self._run()

        for product in FinishedProduct.objects.all():
            self.assertTrue(product.sku.endswith("-UNDYED"), product.sku)

    def test_stock_comes_from_the_raw_pile(self):
        self._run()

        product = FinishedProduct.objects.get(name="Merino Worsted Natural")
        self.assertEqual(product.number_on_hand, 12)

    def test_no_par_is_set_here(self):
        """The par that matters lives on the raw product — you order these
        rather than making them, and a par here is a number nothing reads."""
        self._run()

        self.assertEqual(
            set(FinishedProduct.objects.values_list("par", flat=True)), {0}
        )

    def test_running_it_twice_creates_nothing_new(self):
        self._run()
        output = self._run()

        self.assertEqual(FinishedProduct.objects.count(), 2)
        self.assertIn("already had one", output)

    def test_a_dry_run_creates_nothing(self):
        output = self._run(dry_run=True)

        self.assertEqual(FinishedProduct.objects.count(), 0)
        self.assertIn("Would create 2", output)

    def test_a_dyed_colorway_on_the_same_blank_does_not_block_it(self):
        """A blank sold undyed *and* dyed into colorways could exist; its
        colorways must not stop the undyed one being made."""
        FinishedProduct.objects.create(
            name="Merino Stormy", raw_product=self.merino,
            recipe=make_recipe("Stormy Sea", hexes=()), price="30.00",
        )

        self._run()

        self.assertEqual(
            FinishedProduct.objects.filter(
                raw_product=self.merino, recipe__isnull=True).count(),
            1,
        )

    def test_an_unknown_group_stops_and_names_the_real_ones(self):
        with self.assertRaises(CommandError) as caught:
            call_command("create_passthrough_products", group="Nope",
                         stdout=StringIO(), stderr=StringIO())

        self.assertIn("Undyed Yarn", str(caught.exception))

    def test_an_empty_group_stops_rather_than_reporting_success(self):
        RawProduct.objects.filter(catalog_group=self.group).update(catalog_group=None)

        with self.assertRaises(CommandError) as caught:
            self._run()

        self.assertIn("no active raw products", str(caught.exception))


class BlankCollectionTests(TestCase):
    """The blanks half of the shelf list.

    Same errand as the dyes: one walk before the session rather than a trip
    per bath. The difference is what happens when the count looks wrong.
    """

    def setUp(self):
        self.run = ProductionRun.objects.create()
        self.rows = []
        for name, baths in (("Silk Infinity", 3), ("Wool Wrap", 2)):
            recipe = make_recipe(f"{name} colorway", hexes=())
            product = make_bathable(recipe, name, on_hand=0, par=8, bath=4)
            for i in range(baths):
                self.rows.append(ProductionRunRow.objects.create(
                    run=self.run, finished_product=product,
                    order=len(self.rows) + 1, quantity=4,
                ))

    def test_it_totals_the_blanks_a_sheet_eats(self):
        demand = production.blank_demand(self.rows)

        needed = {raw.name: qty for raw, qty, _ in demand}
        self.assertEqual(needed["raw-Silk Infinity"], 12)
        self.assertEqual(needed["raw-Wool Wrap"], 8)

    def test_one_line_per_blank_not_per_bath(self):
        self.assertEqual(len(production.blank_demand(self.rows)), 2)

    def test_a_blank_we_think_is_out_is_still_listed(self):
        """A count nobody updated is likelier than an empty shelf, and
        leaving it off would turn a stale number into a bath that never got
        dyed."""
        raw = self.rows[0].finished_product.raw_product
        RawProduct.objects.filter(pk=raw.pk).update(number_on_hand=0)

        names = [r.name for r, _, _ in production.blank_demand(self.rows)]

        self.assertIn("raw-Silk Infinity", names)

    def test_the_belief_travels_with_the_requirement(self):
        raw = self.rows[0].finished_product.raw_product
        RawProduct.objects.filter(pk=raw.pk).update(number_on_hand=2)
        # Re-read: the rows in memory carry the counts they were loaded
        # with, and `render_sheet` fetches its own.
        rows = list(ProductionRunRow.objects.filter(run=self.run)
                    .select_related("finished_product__raw_product"))

        demand = dict(
            (r.name, (needed, on_hand))
            for r, needed, on_hand in production.blank_demand(rows)
        )

        self.assertEqual(demand["raw-Silk Infinity"], (12, 2))

    def test_the_sheet_prints_them(self):
        raw = self.rows[0].finished_product.raw_product
        RawProduct.objects.filter(pk=raw.pk).update(number_on_hand=2)
        run = ProductionRun.objects.get(pk=self.run.pk)

        pdf = production.render_sheet(run, "https://example.test/x/")

        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_the_header_clears_the_code_beneath_the_qr(self):
        """The first list row printed over the URL when the header block was
        shorter than the tallest thing drawn into it."""
        self.assertGreater(
            production.HEADER_HEIGHT, production.QR_SIZE + 30,
            "header must clear the QR plus the code and URL under it",
        )


class ProductionRunAdminTests(TestCase):
    """Runs in the admin, mostly so the useless ones can be deleted.

    A run is scaffolding rather than a record, so throwing one away is cheap
    — but what survives the deletion is the part worth pinning.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("boss", "b@x.test", "pw")
        self.client.force_login(self.user)
        self.run = ProductionRun.objects.create()
        recipe = make_recipe("Stormy Sea", hexes=())
        self.product = make_bathable(recipe, "Silk Infinity", on_hand=0, par=8, bath=4)
        self.rows = [
            ProductionRunRow.objects.create(
                run=self.run, finished_product=self.product, order=i, quantity=4)
            for i in (1, 2)
        ]

    def test_the_list_page_loads(self):
        response = self.client.get(reverse("admin:scarves_productionrun_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.run.token)

    def test_the_detail_page_shows_its_rows(self):
        response = self.client.get(
            reverse("admin:scarves_productionrun_change", args=[self.run.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Silk Infinity")

    def test_an_unreported_run_deletes_cleanly(self):
        self.client.post(
            reverse("admin:scarves_productionrun_delete", args=[self.run.pk]),
            {"post": "yes"},
        )

        self.assertEqual(ProductionRun.objects.count(), 0)
        self.assertEqual(ProductionRunRow.objects.count(), 0)

    def test_deleting_a_run_does_not_un_move_stock(self):
        """Those baths really were dyed. What goes is the trail from the
        sheet to the movement, not the movement."""
        production.apply_row(self.rows[0])
        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 4)

        self.client.post(
            reverse("admin:scarves_productionrun_delete", args=[self.run.pk]),
            {"post": "yes"},
        )

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 4)
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_the_list_says_how_much_was_reported(self):
        """A run showing 0 reported has moved nothing and is free to go."""
        production.apply_row(self.rows[0])

        response = self.client.get(reverse("admin:scarves_productionrun_changelist"))

        self.assertContains(response, "1 of 2")

    def test_the_token_cannot_be_edited(self):
        """It is printed on paper and encoded in that sheet's QR code, and
        this app can rewrite neither."""
        from scarves.admin import ProductionRunAdmin
        from django.contrib.admin.sites import site

        admin_obj = ProductionRunAdmin(ProductionRun, site)
        self.assertIn("token", admin_obj.get_readonly_fields(None, self.run))

    def test_rows_are_not_editable_from_here(self):
        from scarves.admin import ProductionRunRowInline
        from django.contrib.admin.sites import site

        inline = ProductionRunRowInline(ProductionRun, site)
        self.assertFalse(inline.has_add_permission(None))
        self.assertFalse(inline.can_delete)


class RetireDontDeleteTests(TestCase):
    """Products are retired, not deleted.

    A product that ever sold is referenced by inventory logs, resolved sales
    and production rows, and we care about that history long after we stop
    selling the thing. `is_active` is the retire flag; the database is what
    stops anyone taking the other route by accident.
    """

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_product(self.recipe, "Stormy Silk", with_image=False)

    def test_a_product_with_history_cannot_be_deleted(self):
        InventoryLog.objects.create(
            finished_product=self.product,
            raw_product=self.product.raw_product,
            log_type=InventoryLog.SALE, quantity=-1,
        )

        with self.assertRaises(ProtectedError):
            self.product.delete()

        self.assertEqual(FinishedProduct.objects.count(), 1)

    def test_a_product_on_a_production_sheet_cannot_be_deleted(self):
        run = ProductionRun.objects.create()
        ProductionRunRow.objects.create(
            run=run, finished_product=self.product, order=1, quantity=4)

        with self.assertRaises(ProtectedError):
            self.product.delete()

    def test_a_product_a_sale_resolved_to_cannot_be_deleted(self):
        UnmatchedSale.objects.create(
            order_id="O-1", line_uid="L1", name="Scarf", quantity=1,
            sold_at=timezone.now(), resolved_product=self.product,
        )

        with self.assertRaises(ProtectedError):
            self.product.delete()

    def test_retiring_is_the_supported_move(self):
        """It takes the product out of everything that matters without
        touching a row anybody might want to read later."""
        InventoryLog.objects.create(
            finished_product=self.product,
            raw_product=self.product.raw_product,
            log_type=InventoryLog.SALE, quantity=-1,
        )

        self.product.is_active = False
        self.product.save(update_fields=["is_active"])

        self.assertEqual(production.plan_baths(20), [])
        self.assertEqual(InventoryLog.objects.count(), 1)
        self.assertEqual(labelmod.inventory_run().rows, [])

    def test_a_raw_product_with_history_is_protected_too(self):
        InventoryLog.objects.create(
            finished_product=self.product,
            raw_product=self.product.raw_product,
            log_type=InventoryLog.PRODUCTION, quantity=4,
        )

        with self.assertRaises(ProtectedError):
            self.product.raw_product.delete()

    def test_a_mistake_row_with_no_history_still_deletes(self):
        """Nothing points at it, so there is nothing to preserve — a product
        typed in by accident shouldn't need retiring."""
        self.product.delete()

        self.assertEqual(FinishedProduct.objects.count(), 0)


class DyePickerTests(TestCase):
    """The dye boxes on the recipe pages.

    Two failures, both quiet. A hundred dyes in catalog order is a list
    nobody reads to the end, so the dye that is there doesn't get used; and a
    dye that isn't on the list at all can't be recorded, so the recipe gets
    saved with the dyes that *were* on the list and looks complete.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("dyer", "d@example.test", "pw")
        self.brand = DyeBrand.objects.create(name="Dharma Acid Dyes")
        self.peacock = Dye.objects.create(
            name="416 Peacock Blue", hex_color="#064e7e", brand=self.brand
        )
        self.aqua = Dye.objects.create(
            name="422 Bright Aqua", hex_color="#5ccfbf", brand=self.brand
        )

    def test_the_catalog_number_does_not_decide_the_order(self):
        """`416 Peacock Blue` files under P, not between 415 and 417."""
        self.assertEqual(self.peacock.sort_name, "Peacock Blue")
        self.assertEqual(self.aqua.sort_name, "Bright Aqua")

        form = RecipeDyesForm()
        html = str(form["dye1"])
        self.assertLess(
            html.index("422 Bright Aqua"), html.index("416 Peacock Blue"),
            "the picker is still sorted by the number on the jar",
        )

    def test_a_name_with_only_a_number_keeps_it(self):
        """Better a dye called `27` than a dye called nothing."""
        odd = Dye.objects.create(name="27", brand=self.brand)
        self.assertEqual(odd.sort_name, "27")

    def test_an_option_carries_what_it_can_be_found_by(self):
        html = str(RecipeDyesForm()["dye1"])

        self.assertIn('data-search="416 peacock blue peacock blue dharma acid dyes"', html)
        self.assertIn('data-hex="#064e7e"', html)

    def test_out_of_stock_dyes_are_still_offered(self):
        """Hiding them was survivable while the list was take-it-or-leave-it.

        Now that a missing dye can be typed in, hiding one is how a second
        `Peacock Blue` gets created beside the first.
        """
        Dye.objects.filter(pk=self.peacock.pk).update(in_stock=False)

        html = str(QuickRecipeRowForm()["dye1"])

        self.assertIn("416 Peacock Blue", html)
        self.assertIn("data-out-of-stock", html)

    def test_a_dye_with_no_colour_says_so_rather_than_showing_one(self):
        blank = Dye.objects.create(name="Cayenne", brand=self.brand)

        html = str(RecipeDyesForm()["dye1"])

        self.assertIn('data-hex=""', html)
        self.assertNotIn("#FF0000", html)
        self.assertFalse(blank.hex_color)


class AddADyeTests(TestCase):
    """Adding a dye from the picker, mid-recipe.

    The endpoint's job is to always leave the person with something selected:
    the alternative is an empty slot and a recipe that reads as finished.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("adder", "a@example.test", "pw")
        self.client.force_login(self.user)
        self.url = reverse("dye_create")
        self.brand = DyeBrand.objects.create(name="Dharma Acid Dyes")

    def test_a_new_dye_is_a_name_and_nothing_else(self):
        response = self.client.post(self.url, {"name": "  Muddy   Ochre "})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["created"])

        dye = Dye.objects.get(pk=body["id"])
        self.assertEqual(dye.name, "Muddy Ochre")
        self.assertEqual(dye.brand.name, UNCATEGORIZED_BRAND)
        self.assertEqual(dye.hex_color, "", "a made-up colour would reach the sheets")
        self.assertTrue(dye.needs_review)

    def test_it_hands_back_an_option_the_picker_can_use(self):
        """Same attributes the widget renders, so the new dye is searchable
        in every picker on the page without a reload."""
        body = self.client.post(self.url, {"name": "Muddy Ochre"}).json()

        self.assertEqual(body["attrs"]["data-name"], "Muddy Ochre")
        self.assertEqual(body["attrs"]["data-sort"], "muddy ochre")
        self.assertIn("muddy ochre", body["attrs"]["data-search"])
        self.assertEqual(body["attrs"]["data-hex"], "")

    def test_a_name_that_already_exists_picks_that_dye(self):
        existing = Dye.objects.create(name="Cayenne", brand=self.brand)

        body = self.client.post(self.url, {"name": "cayenne"}).json()

        self.assertFalse(body["created"])
        self.assertEqual(body["id"], existing.pk)
        self.assertEqual(Dye.objects.count(), 1)

    def test_the_catalog_number_is_not_what_makes_it_a_different_dye(self):
        """Typed from memory, the number is the first thing left off."""
        existing = Dye.objects.create(name="416 Peacock Blue", brand=self.brand)

        body = self.client.post(self.url, {"name": "Peacock Blue"}).json()

        self.assertFalse(body["created"])
        self.assertEqual(body["id"], existing.pk)

    def test_neither_is_the_catalog_tag(self):
        """Dharma tags its mixing primaries; nobody types the tag.

        Ten of the 84 acid dyes carry one, so getting this wrong duplicates
        the most-used dyes in the range and nothing anywhere says so.
        """
        existing = Dye.objects.create(
            name="402 Fire Engine Red (Primary)", brand=self.brand
        )

        body = self.client.post(self.url, {"name": "fire engine red"}).json()

        self.assertFalse(body["created"])
        self.assertEqual(body["id"], existing.pk)
        self.assertEqual(Dye.objects.count(), 1)

    def test_nor_a_trailing_mark(self):
        existing = Dye.objects.create(name="409 Dark Navy*", brand=self.brand)

        body = self.client.post(self.url, {"name": "Dark Navy"}).json()

        self.assertFalse(body["created"])
        self.assertEqual(body["id"], existing.pk)

    def test_two_genuinely_different_dyes_stay_different(self):
        """The key strips furniture, not words: this must not over-merge."""
        Dye.objects.create(name="404 Sapphire Blue", brand=self.brand)

        body = self.client.post(self.url, {"name": "Peacock Blue"}).json()

        self.assertTrue(body["created"])
        self.assertEqual(Dye.objects.count(), 2)

    def test_a_blank_name_is_refused(self):
        response = self.client.post(self.url, {"name": "   "})

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertEqual(Dye.objects.count(), 0)

    def test_it_takes_no_anonymous_writes(self):
        self.client.logout()

        response = self.client.post(self.url, {"name": "Muddy Ochre"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])
        self.assertEqual(Dye.objects.count(), 0)

    def test_a_dye_added_mid_entry_saves_onto_a_recipe(self):
        """End to end: the whole point is the recipe that comes out of it."""
        recipe = Recipe.objects.create(name="New Colorway")
        added = self.client.post(self.url, {"name": "Muddy Ochre"}).json()

        form = RecipeDyesForm({"dye1": str(added["id"])})
        self.assertTrue(form.is_valid(), form.errors)
        form.save(recipe)

        self.assertEqual(
            [rd.dye.name for rd in recipe.recipe_dyes.all()], ["Muddy Ochre"]
        )

    def test_a_colourless_dye_claims_no_band_and_no_palette(self):
        """The reason a blank colour is safe to defer.

        It contributes nothing anywhere rather than contributing a guess —
        the same bargain colorbands makes on the classify page.
        """
        recipe = Recipe.objects.create(name="Half-known")
        added = self.client.post(self.url, {"name": "Muddy Ochre"}).json()
        RecipeDye.objects.create(recipe=recipe, dye_id=added["id"], order=1)

        self.assertEqual(colorbands.bands_from_dyes(recipe), [])
        self.assertEqual(recipe_palette(recipe), [])


class ImportDyesTests(TestCase):
    """Re-importing the catalog file over a live dye list.

    `loaddata` can't do this: the fixtures carry primary keys and RecipeDye
    points at a dye by primary key, so loading over drifted pks repoints
    recipes at other colours with no error anywhere.
    """

    def setUp(self):
        self.brand = DyeBrand.objects.create(name="Dharma Acid Dyes")
        self.path = tempfile.mkdtemp() + "/dyes.json"

    def write(self, entries):
        with open(self.path, "w") as handle:
            json.dump(entries, handle)
        return self.path

    def run_import(self, extra=(), **kwargs):
        out = StringIO()
        call_command(
            "import_dyes", self.path, "--brand", "Dharma Acid Dyes",
            *extra, stdout=out, **kwargs
        )
        return out.getvalue()

    def test_a_colour_already_on_file_is_not_imported_again(self):
        Dye.objects.create(
            name="401 Brilliant Yellow", hex_color="#ffec05", brand=self.brand
        )
        self.write({
            "401 Brilliant Yellow (Primary)": "#FFEC05",   # same colour, tidier name
            "490 Tornado Gray": "#8b8b8b",
        })

        output = self.run_import()

        self.assertEqual(Dye.objects.count(), 2)
        self.assertTrue(Dye.objects.filter(name="490 Tornado Gray").exists())
        self.assertIn("skipped 1 already on file", output)

    def test_it_finds_the_hand_typed_dye_under_the_catalog_tag(self):
        """`Fire Engine Red` and `402 Fire Engine Red (Primary)` are one dye,
        so the file fills the first in rather than adding the second."""
        typed = Dye.objects.create(name="Fire Engine Red", brand=self.brand)
        self.write({"402 Fire Engine Red (Primary)": "#c41d33"})

        self.run_import()

        typed.refresh_from_db()
        self.assertEqual(Dye.objects.count(), 1)
        self.assertEqual(typed.hex_color, "#c41d33")
        self.assertEqual(typed.name, "402 Fire Engine Red (Primary)")

    def test_it_fills_in_a_dye_that_was_typed_in_by_hand(self):
        """The picker's half-finished dye, met by the file that knows the
        rest. This is the cleanup the deferral was banking on."""
        typed = Dye.objects.create(name="Tornado Gray", brand=self.brand)
        self.write({"490 Tornado Gray": "#8b8b8b"})

        self.run_import()

        typed.refresh_from_db()
        self.assertEqual(typed.hex_color, "#8b8b8b")
        self.assertEqual(typed.name, "490 Tornado Gray", "the number is on the jar")
        self.assertEqual(Dye.objects.count(), 1)

    def test_a_colour_somebody_recorded_is_never_overwritten(self):
        mine = Dye.objects.create(
            name="490 Tornado Gray", hex_color="#777777", brand=self.brand
        )
        self.write({"490 Tornado Gray": "#8b8b8b"})

        output = self.run_import()

        mine.refresh_from_db()
        self.assertEqual(mine.hex_color, "#777777")
        self.assertEqual(Dye.objects.count(), 1)
        self.assertIn("conflict", output)
        self.assertIn("490 Tornado Gray", output)

    def test_a_dry_run_writes_nothing_and_says_what_it_would_do(self):
        self.write({"490 Tornado Gray": "#8b8b8b"})

        output = self.run_import(extra=["--dry-run"])

        self.assertEqual(Dye.objects.count(), 0)
        self.assertIn("Would add 1", output)
        self.assertIn("490 Tornado Gray", output)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self.write({"490 Tornado Gray": "#8b8b8b", "489 Silver Gray": "#c0c0c0"})

        self.run_import()
        output = self.run_import()

        self.assertEqual(Dye.objects.count(), 2)
        self.assertIn("Added 0", output)
        self.assertIn("skipped 2", output)

    def test_a_supplier_range_can_land_out_of_stock(self):
        self.write({"490 Tornado Gray": "#8b8b8b"})

        self.run_import(extra=["--out-of-stock"])

        self.assertFalse(Dye.objects.get(name="490 Tornado Gray").in_stock)

    def test_it_reads_the_fixture_shape_too(self):
        """Both files on disk hold this data; either can be pointed at it."""
        self.write([
            {"model": "scarves.dyebrand", "pk": 1, "fields": {"name": "Dharma Acid Dyes"}},
            {"model": "scarves.dye", "pk": 1, "fields": {
                "name": "490 Tornado Gray", "hex_color": "#8b8b8b", "brand": 1}},
        ])

        self.run_import()

        self.assertTrue(Dye.objects.filter(name="490 Tornado Gray").exists())

    def test_an_unreadable_colour_is_named_rather_than_guessed(self):
        self.write({"490 Tornado Gray": "", "489 Silver Gray": "#c0c0c0"})

        output = self.run_import()

        self.assertEqual(Dye.objects.count(), 1)
        self.assertIn("no readable colour", output)
        self.assertIn("490 Tornado Gray", output)

    def test_the_same_name_under_another_brand_is_another_jar(self):
        """Jacquard's Peacock Blue is not Dharma's 416, and a colour typed
        onto one must not be written over from the other's catalog."""
        jacquard = DyeBrand.objects.create(name="Jacquard")
        theirs = Dye.objects.create(
            name="Peacock Blue", hex_color="#115577", brand=jacquard
        )
        self.write({"416 Peacock Blue (Primary)": "#064e7e"})

        self.run_import()

        theirs.refresh_from_db()
        self.assertEqual(theirs.hex_color, "#115577")
        self.assertEqual(theirs.name, "Peacock Blue")
        self.assertEqual(Dye.objects.count(), 2)

    def test_a_dye_typed_in_from_a_picker_gets_its_brand_too(self):
        """The half-finished row the picker leaves is finished in one pass:
        colour, catalog number and brand all come off the file."""
        typed = Dye.objects.create(
            name="Tornado Gray",
            brand=DyeBrand.objects.create(name=UNCATEGORIZED_BRAND),
        )
        self.write({"490 Tornado Gray": "#8b8b8b"})

        self.run_import()

        typed.refresh_from_db()
        self.assertEqual(typed.brand.name, "Dharma Acid Dyes")
        self.assertEqual(typed.name, "490 Tornado Gray")
        self.assertEqual(typed.hex_color, "#8b8b8b")
        self.assertFalse(typed.needs_review)

    def test_a_dry_run_says_when_the_brand_is_a_new_one(self):
        """What a typo in --brand looks like, before it splits the range
        across two brands."""
        self.write({"490 Tornado Gray": "#8b8b8b"})
        out = StringIO()
        call_command(
            "import_dyes", self.path, "--brand", "Dharma Acid Dies",
            "--dry-run", stdout=out,
        )

        self.assertIn("Would create a new brand", out.getvalue())
        self.assertIn("Dharma Acid Dyes", out.getvalue(), "should list what is on file")

    def test_a_missing_file_is_an_error_not_an_empty_run(self):
        with self.assertRaises(CommandError):
            call_command("import_dyes", "/nope.json", "--brand", "X", stdout=StringIO())


class BulkInventoryReasonTests(TestCase):
    """A bulk count says why it moved, at whichever grain fits.

    Every row used to be logged as the fixed string "Bulk inventory update.",
    which names the page and explains nothing. That is the same silence the
    rest of the app is organised against: a count corrected for a good reason
    is indistinguishable a month later from one that drifted, and those two
    want opposite responses.

    Two grains, because a save can hold two stories — the rack recounted, and
    one row that moved for its own reason. The row wins where it is given.
    """

    def setUp(self):
        self.user = User.objects.create_user("staff-bulk", password="pw")
        self.client.force_login(self.user)
        self.recipe_a = make_recipe("Stormy Sea")
        self.recipe_b = make_recipe("Ember")
        self.a = make_product(self.recipe_a, "Stormy Silk", with_image=False)
        self.b = FinishedProduct.objects.create(
            name="Ember Silk",
            raw_product=self.a.raw_product,
            recipe=self.recipe_b,
            price="30.00",
        )
        for p in (self.a, self.b):
            p.number_on_hand = 4
            p.save()
        self.raw_ids = str(self.a.raw_product_id)

    def _save(self, **post):
        return self.client.post(
            f"{reverse('bulk_inventory_update')}?raw_ids={self.raw_ids}",
            {
                f"count_{self.a.id}": str(self.a.number_on_hand),
                f"count_{self.b.id}": str(self.b.number_on_hand),
                **post,
            },
        )

    def _note(self, product):
        return InventoryLog.objects.get(finished_product=product).notes

    def test_the_form_reason_lands_on_every_changed_row(self):
        self._save(
            **{f"count_{self.a.id}": "6", f"count_{self.b.id}": "9"},
            reason="counted the display rack in with the back stock",
        )

        for product in (self.a, self.b):
            self.assertIn("counted the display rack in", self._note(product))

    def test_a_row_reason_wins_over_the_form_reason(self):
        self._save(
            **{
                f"count_{self.a.id}": "6",
                f"count_{self.b.id}": "9",
                f"reason_{self.b.id}": "two damaged, pulled from sale",
            },
            reason="annual recount",
        )

        self.assertIn("annual recount", self._note(self.a))
        self.assertIn("two damaged, pulled from sale", self._note(self.b))
        self.assertNotIn("annual recount", self._note(self.b))

    def test_a_row_reason_works_with_no_form_reason(self):
        self._save(
            **{
                f"count_{self.a.id}": "6",
                f"reason_{self.a.id}": "found a bag under the cutting table",
            },
        )

        self.assertIn("found a bag under the cutting table", self._note(self.a))

    def test_no_reason_keeps_the_old_note(self):
        self._save(**{f"count_{self.a.id}": "6"})

        self.assertEqual(self._note(self.a), "Bulk inventory update.")

    def test_a_blank_reason_never_blocks_the_count(self):
        """Refusing the save to extract a sentence would cost a real stock
        correction to punish a missing one."""
        self._save(**{f"count_{self.a.id}": "6"})

        self.a.refresh_from_db()
        self.assertEqual(self.a.number_on_hand, 6)

    def test_a_reason_on_an_unchanged_row_writes_nothing(self):
        """No movement, no row. A log entry here would be a change that never
        happened, carrying an explanation for it."""
        self._save(**{f"reason_{self.a.id}": "typed then thought better of it"})

        self.assertFalse(InventoryLog.objects.exists())

    def test_the_counts_are_number_inputs(self):
        """The +/- controls are the browser's own, so the field has to stay a
        number input — a widget swap would silently take them away."""
        html = self.client.get(
            f"{reverse('bulk_inventory_update')}?raw_ids={self.raw_ids}"
        ).content.decode()

        self.assertIn(f'type="number" name="count_{self.a.id}"', html)
        self.assertIn("::-webkit-inner-spin-button", html)


class LabelsIncludeAddedStockTests(TestCase):
    """Stock counted in gets barcodes too.

    `produced_since` filtered on PRODUCTION, so anything entering through
    `bulk_inventory_update` — a bag found in a cupboard, a display rack folded
    back into inventory, stock that predates this app — got no labels at all.
    Nothing said so. The symptom arrives later and elsewhere: a scarf that
    won't scan at the till, in front of a customer, with the queue waiting.

    The fix errs toward printing, which is the cheap direction. A spare
    sticker sits in a drawer; a missing one costs the sale.
    """

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_product(self.recipe, "Stormy Silk", with_image=False)
        self.cutoff = timezone.localdate() - timedelta(days=7)

    def _log(self, log_type, quantity):
        return InventoryLog.objects.create(
            finished_product=self.product,
            raw_product=self.product.raw_product,
            log_type=log_type,
            quantity=quantity,
        )

    def _quantity(self):
        run = labelmod.produced_since(self.cutoff)
        rows = [r for r in run.rows if r.product.pk == self.product.pk]
        return sum(r.quantity for r in rows)

    def test_a_bulk_adjustment_gets_labels(self):
        self._log(InventoryLog.ADJUSTMENT, 12)

        self.assertEqual(self._quantity(), 12)

    def test_dyeing_and_added_stock_add_up(self):
        self._log(InventoryLog.PRODUCTION, 4)
        self._log(InventoryLog.ADJUSTMENT, 12)

        self.assertEqual(self._quantity(), 16)

    def test_stock_leaving_asks_for_no_labels(self):
        """A barcode answers 'what is this thing in my hand'. Nothing is in
        anyone's hand when the count goes down."""
        self._log(InventoryLog.ADJUSTMENT, -3)

        self.assertEqual(self._quantity(), 0)

    def test_a_downward_correction_does_not_eat_a_bath_s_stickers(self):
        """The scarves from that bath exist and need labelling. A separate
        correction in the same week is about different units."""
        self._log(InventoryLog.PRODUCTION, 4)
        self._log(InventoryLog.ADJUSTMENT, -3)

        self.assertEqual(self._quantity(), 4)

    def test_sales_are_still_ignored(self):
        """Unchanged, and load-bearing: a sold scarf left wearing its sticker,
        so netting sales in would subtract labels already stuck to things."""
        self._log(InventoryLog.PRODUCTION, 5)
        self._log(InventoryLog.SALE, -3)

        self.assertEqual(self._quantity(), 5)

    def test_an_old_adjustment_is_outside_the_cutoff(self):
        log = self._log(InventoryLog.ADJUSTMENT, 9)
        InventoryLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timedelta(days=60)
        )

        self.assertEqual(self._quantity(), 0)


class LabelStyleTests(TestCase):
    """Three flavours of one sticker, off one pipeline.

    The barcode was assumed to be the important half of a label until the
    physical job was looked at: stickers are applied by hand, off a sheet,
    onto a pile of scarves. You cannot apply a sticker you cannot read, so the
    text is what makes the sheet usable and the barcode is what makes the
    scarf usable later. Both matter; they just aren't the same job.

    What the text says follows from what gets confused. Nobody mistakes a
    rectangle for a half-circle, so the blank is visible and `BLANK-DYEBATH`
    spends half a small label saying it. Two reds called Valentine and L Word
    is the confusion a label can fix.
    """

    def setUp(self):
        self.stock = LabelStock.objects.create(
            name="Test 80up", page_width_in=Decimal("8.5"),
            page_height_in=Decimal("11"), label_width_in=Decimal("1.75"),
            label_height_in=Decimal("0.5"), rows=20, columns=4,
            margin_left_in=Decimal("0.3"), margin_top_in=Decimal("0.5"),
            pitch_x_in=Decimal("2.0"), pitch_y_in=Decimal("0.5"),
        )
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_product(self.recipe, "Stormy Silk", with_image=False)
        self.product.number_on_hand = 3
        self.product.save()

    def _run(self, style):
        return labelmod.inventory_run(style=style)

    def test_the_name_style_prints_the_colorway_not_the_product(self):
        """`variation_name` is what Square calls the variation, so the sticker
        says the same string the crew is hunting for in the list — not a
        translation of it."""
        self.assertEqual(self.product.variation_name, "Stormy Sea")

    def test_a_name_run_renders(self):
        pdf = labelmod.render_run(self._run(labelmod.NAME), self.stock)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_the_density_guard_does_not_refuse_a_run_with_no_bars(self):
        """The guard exists because unscannable bars fail silently at the
        till. A sticker reading 'Stormy Sea' has nothing to fail — and letting
        this fire would refuse the very style invented to cope with stock too
        narrow for the SKU."""
        narrow = LabelStock.objects.create(
            name="Too narrow", page_width_in=Decimal("8.5"),
            page_height_in=Decimal("11"), label_width_in=Decimal("0.6"),
            label_height_in=Decimal("0.5"), rows=20, columns=4,
            margin_left_in=Decimal("0.3"), margin_top_in=Decimal("0.5"),
            pitch_x_in=Decimal("0.7"), pitch_y_in=Decimal("0.5"),
        )
        self.assertTrue(
            labelmod.density_problems(self._run(labelmod.BARCODE), narrow),
            "the barcode style should still object to this stock",
        )
        self.assertEqual(
            labelmod.density_problems(self._run(labelmod.NAME), narrow), [],
        )

    def test_a_product_with_no_sku_still_gets_a_name_label(self):
        """A barcode of nothing is unprintable; "Stormy Sea" needs no SKU. The
        two styles are independent runs, so they are free to disagree here."""
        blank_sku = make_product(make_recipe("Nameless"), "Nameless Silk",
                                 with_image=False)
        blank_sku.number_on_hand = 4
        blank_sku.save()
        FinishedProduct.objects.filter(pk=blank_sku.pk).update(sku="")

        barcode_run = self._run(labelmod.BARCODE)
        self.assertIn(blank_sku.pk, [p.pk for p in barcode_run.skipped_no_sku])

        name_run = self._run(labelmod.NAME)
        self.assertEqual(name_run.skipped_no_sku, [])
        self.assertIn(blank_sku.pk,
                      [p.pk for p in name_run.flat(self.stock.columns)])

    def test_the_barcode_style_is_unchanged(self):
        """Today's sheet keeps working exactly as it does — bars with the SKU
        underneath — and stays the default for a form that never mentions
        style."""
        run = self._run(labelmod.BARCODE)
        self.assertTrue(run.needs_barcode)
        self.assertEqual(labelmod.LabelRun([], [], 0).style, labelmod.BARCODE)

    def test_a_long_colorway_is_shortened_rather_than_overflowed(self):
        """Text running past the die-cut lands on the *next* label and
        mislabels a second scarf, so the floor truncates instead."""
        from reportlab.pdfgen import canvas
        import io

        pdf = canvas.Canvas(io.BytesIO())
        size, lines = labelmod._wrap_to_fit(
            pdf, "Extraordinarily Verbose Colorway Name That Will Never Fit",
            max_w=40, max_h=28.8,
            max_pt=labelmod.NAME_MAX_PT, min_pt=labelmod.NAME_MIN_PT,
        )
        self.assertGreaterEqual(size, labelmod.NAME_MIN_PT)
        for line in lines:
            self.assertLessEqual(
                pdf.stringWidth(line, "Helvetica-Bold", size), 40 + 0.01
            )

    def test_the_style_reaches_the_run_from_the_form(self):
        user = User.objects.create_user("labels-staff", password="pw")
        self.client.force_login(user)

        response = self.client.get(reverse("label_index"), {
            "dataset": "inventory", "style": labelmod.NAME,
            "extra": "0", "start_at": "1", "stock": str(self.stock.pk),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["run"].style, labelmod.NAME)


class NameBoxWidthTests(TestCase):
    """Text is measured against the bars, not against the label.

    `barcode.width` is the trap and it is the one CLAUDE.md already warns
    about from the production-sheet side: reportlab pins Code128 quiet zones
    at a quarter inch a side and never scales them, so the drawn object comes
    out *wider than the sticker* — 134.8pt of object on a 126pt label. Text
    fitted to the label got a fifth of an inch a side more room than the bars
    above it used, ran visibly wider than the symbol, and read as overflowing.
    """

    def setUp(self):
        self.stock = LabelStock.objects.create(
            name="1.75x0.5", page_width_in=Decimal("8.5"),
            page_height_in=Decimal("11"), label_width_in=Decimal("1.75"),
            label_height_in=Decimal("0.5"), rows=20, columns=4,
            margin_left_in=Decimal("0.3"), margin_top_in=Decimal("0.5"),
            pitch_x_in=Decimal("2.0"), pitch_y_in=Decimal("0.5"),
        )
        self.product = make_product(make_recipe("Stormy Sea"), "Stormy Silk",
                                    with_image=False)

    def test_the_object_really_is_wider_than_the_label(self):
        """Pinned because everything else here follows from it, and because a
        reportlab change would otherwise silently move the text box."""
        barcode, _ = labelmod.barcode_for(self.product.sku, self.stock)
        label_pt = float(self.stock.label_width_in) * 72

        self.assertGreater(barcode.width, label_pt)
        self.assertEqual(barcode.lquiet, 18.0)
        self.assertEqual(barcode.rquiet, 18.0)

    def test_bars_only_width_strips_the_quiet_zones(self):
        barcode, _ = labelmod.barcode_for(self.product.sku, self.stock)

        self.assertAlmostEqual(
            labelmod.bars_only_width(barcode),
            barcode.width - barcode.lquiet - barcode.rquiet,
        )

    def test_the_text_box_is_the_bars_and_fits_the_label(self):
        box = labelmod.name_box_width(self.product, self.stock)
        barcode, _ = labelmod.barcode_for(self.product.sku, self.stock)
        label_pt = float(self.stock.label_width_in) * 72

        self.assertAlmostEqual(box, labelmod.bars_only_width(barcode))
        self.assertLess(box, label_pt)

    def test_a_product_with_no_sku_falls_back_to_the_padded_label(self):
        """Only reachable in a name-only run, where there are no bars to
        disagree with."""
        FinishedProduct.objects.filter(pk=self.product.pk).update(sku="")
        self.product.refresh_from_db()

        expected = float(self.stock.label_width_in) * 72 - labelmod._pt(labelmod.PAD_IN) * 2
        self.assertAlmostEqual(
            labelmod.name_box_width(self.product, self.stock), expected)

    def test_text_never_exceeds_the_box(self):
        from reportlab.pdfgen import canvas
        import io

        pdf = canvas.Canvas(io.BytesIO())
        box = labelmod.name_box_width(self.product, self.stock)
        for name in ("Stormy Sea", "Valentine", "Chartreuse Neon",
                     "Autumn Harvest Moonrise", "Supercalifragilistic"):
            size, lines = labelmod._wrap_to_fit(
                pdf, name, box, 28.8,
                max_pt=labelmod.NAME_MAX_PT, min_pt=labelmod.NAME_MIN_PT,
            )
            for line in lines:
                self.assertLessEqual(
                    pdf.stringWidth(line, "Helvetica-Bold", size), box + 0.01,
                    f"{name!r} at {size}pt runs past the bars",
                )

    def test_a_tall_block_is_clamped_to_its_band(self):
        """Wrapping to a second line to satisfy the width is what makes a
        block too tall, so a size passing one test can fail the other."""
        from reportlab.pdfgen import canvas
        import io

        pdf = canvas.Canvas(io.BytesIO())
        size, lines = labelmod._wrap_to_fit(
            pdf, "Autumn Harvest Moonrise", 98.8, 12.0,
            max_pt=labelmod.NAME_MAX_PT, min_pt=labelmod.NAME_MIN_PT,
        )

        self.assertLessEqual(size * labelmod.LINE_SPACING * len(lines), 12.0)


class BothSetsTests(TestCase):
    """`both` is two sets of stickers in one file, not two things on a sticker.

    Each label stays exactly what it is. A barcode label keeps the SKU
    underneath — that is what the bars encode, so it is the text that belongs
    beside them; captioning a barcode with the recipe would label it with
    something it doesn't say. A name label carries the recipe, which is what
    gets confused when you are matching a sticker to a scarf by hand.

    Printing them as one job rather than two is the whole feature: the sets
    run continuously, so the name set starts in the gap the barcode set left
    on the last sheet instead of wasting it.
    """

    def setUp(self):
        self.stock = LabelStock.objects.create(
            name="1.75x0.5", page_width_in=Decimal("8.5"),
            page_height_in=Decimal("11"), label_width_in=Decimal("1.75"),
            label_height_in=Decimal("0.5"), rows=20, columns=4,
            margin_left_in=Decimal("0.3"), margin_top_in=Decimal("0.5"),
            pitch_x_in=Decimal("2.0"), pitch_y_in=Decimal("0.5"),
        )
        self.a = make_product(make_recipe("Stormy Sea"), "Stormy Silk",
                              with_image=False)
        self.b = FinishedProduct.objects.create(
            name="Ember Silk", raw_product=self.a.raw_product,
            recipe=make_recipe("Ember"), price="30.00",
        )
        for p, n in ((self.a, 3), (self.b, 2)):
            p.number_on_hand = n
            p.save()

    def _run(self, style):
        return labelmod.inventory_run(style=style)

    def test_it_prints_a_barcode_set_then_a_name_set(self):
        run = self._run(labelmod.NAME_AND_BARCODE)
        self.assertEqual(run.segments, [labelmod.BARCODE, labelmod.NAME])

    def test_every_sticker_is_one_style_or_the_other(self):
        styled = self._run(labelmod.NAME_AND_BARCODE).styled_sequence(
            self.stock.columns)
        used = [st for p, st in styled if p is not None]

        self.assertEqual(set(used), {labelmod.BARCODE, labelmod.NAME})
        # Contiguous: all of one, then all of the other.
        self.assertEqual(used, sorted(used, key=lambda st: st != labelmod.BARCODE))

    def test_it_is_exactly_twice_a_single_set(self):
        single = self._run(labelmod.BARCODE)
        both = self._run(labelmod.NAME_AND_BARCODE)

        self.assertEqual(both.total, single.total * 2)
        self.assertEqual(
            len(both.sequence(self.stock.columns)),
            len(single.sequence(self.stock.columns)) * 2,
        )

    def test_the_two_halves_hold_the_same_products(self):
        styled = self._run(labelmod.NAME_AND_BARCODE).styled_sequence(
            self.stock.columns)
        bars = [p.pk for p, st in styled if p and st == labelmod.BARCODE]
        names = [p.pk for p, st in styled if p and st == labelmod.NAME]

        self.assertEqual(bars, names)

    def test_a_single_style_run_is_one_set(self):
        for style in (labelmod.BARCODE, labelmod.NAME):
            run = self._run(style)
            self.assertEqual(run.segments, [style])
            self.assertEqual(run.total, sum(r.quantity for r in run.rows))

    def test_it_renders(self):
        pdf = labelmod.render_run(self._run(labelmod.NAME_AND_BARCODE), self.stock)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_it_needs_a_sku_and_the_density_guard_applies(self):
        """It prints bars, so both barcode rules still hold."""
        run = self._run(labelmod.NAME_AND_BARCODE)
        self.assertTrue(run.needs_barcode)

        narrow = LabelStock.objects.create(
            name="Too narrow", page_width_in=Decimal("8.5"),
            page_height_in=Decimal("11"), label_width_in=Decimal("0.6"),
            label_height_in=Decimal("0.5"), rows=20, columns=4,
            margin_left_in=Decimal("0.3"), margin_top_in=Decimal("0.5"),
            pitch_x_in=Decimal("0.7"), pitch_y_in=Decimal("0.5"),
        )
        self.assertTrue(labelmod.density_problems(run, narrow))

    def test_the_style_is_offered_on_the_page(self):
        user = User.objects.create_user("both-staff", password="pw")
        self.client.force_login(user)

        html = self.client.get(reverse("label_index")).content.decode()
        self.assertIn(f'value="{labelmod.NAME_AND_BARCODE}"', html)


class BulkReasonPresetTests(TestCase):
    """A list of reasons, because a free box gets left blank.

    Asking someone to compose a sentence at the moment they want to be
    finished reliably produces nothing, which is the state the field exists to
    end. A short list makes the common answer one click, and the box is still
    there for the one nobody predicted.

    Presets are stored as their own text rather than as codes: the value of a
    reason is that it reads back plainly in `InventoryLog.notes` two seasons
    later, and a code would need this list to still exist and still mean the
    same thing.
    """

    def setUp(self):
        self.user = User.objects.create_user("preset-staff", password="pw")
        self.client.force_login(self.user)
        self.a = make_product(make_recipe("Stormy Sea"), "Stormy Silk",
                              with_image=False)
        self.b = FinishedProduct.objects.create(
            name="Ember Silk", raw_product=self.a.raw_product,
            recipe=make_recipe("Ember"), price="30.00",
        )
        for p in (self.a, self.b):
            p.number_on_hand = 4
            p.save()
        self.raw_ids = str(self.a.raw_product_id)

    def _save(self, **post):
        return self.client.post(
            f"{reverse('bulk_inventory_update')}?raw_ids={self.raw_ids}",
            {
                f"count_{self.a.id}": str(self.a.number_on_hand),
                f"count_{self.b.id}": str(self.b.number_on_hand),
                **post,
            },
        )

    def _note(self, product):
        return InventoryLog.objects.get(finished_product=product).notes

    def test_a_preset_alone_becomes_the_reason(self):
        self._save(**{f"count_{self.a.id}": "6"}, reason_preset="Found items")

        self.assertIn("Found items", self._note(self.a))

    def test_a_preset_and_free_text_read_as_one_line(self):
        """The category and the detail. Neither substitutes for the other."""
        self._save(**{f"count_{self.a.id}": "6"},
                   reason_preset="Found items",
                   reason="under the cutting table")

        self.assertIn("Found items — under the cutting table", self._note(self.a))

    def test_free_text_alone_still_works(self):
        self._save(**{f"count_{self.a.id}": "6"}, reason="sister recounted the rack")

        self.assertIn("sister recounted the rack", self._note(self.a))

    def test_neither_keeps_the_old_note(self):
        self._save(**{f"count_{self.a.id}": "6"})

        self.assertEqual(self._note(self.a), "Bulk inventory update.")

    def test_a_row_reason_replaces_the_form_reason_whole(self):
        """Falling back field by field would blend a row's preset with the
        form's free text and produce a sentence nobody wrote."""
        self._save(
            **{
                f"count_{self.a.id}": "6",
                f"count_{self.b.id}": "2",
                f"reason_preset_{self.b.id}": "Damaged or unsellable",
            },
            reason_preset="Recount",
            reason="whole rack, Tuesday",
        )

        self.assertIn("Recount — whole rack, Tuesday", self._note(self.a))
        self.assertIn("Damaged or unsellable", self._note(self.b))
        self.assertNotIn("whole rack", self._note(self.b))
        self.assertNotIn("Recount", self._note(self.b))

    def test_an_unknown_preset_is_rejected_not_stored(self):
        """It is a ChoiceField, so a hand-built POST can't write arbitrary
        text through the dropdown — the free box is the way to say something
        new, and it is length-capped."""
        response = self._save(**{f"count_{self.a.id}": "6"},
                              reason_preset="Fell off a truck")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(InventoryLog.objects.exists())

    def test_both_directions_are_offered(self):
        """A bulk count moves either way, and the pair that gets confused is
        'more than I thought' versus 'these came back'."""
        html = self.client.get(
            f"{reverse('bulk_inventory_update')}?raw_ids={self.raw_ids}"
        ).content.decode()

        for preset in ("Found items", "Recount", "Damaged or unsellable"):
            self.assertIn(preset, html)

    def test_blank_stays_the_first_option(self):
        """A count with no reason is still worth having."""
        self.assertEqual(viewsmod.BULK_REASON_CHOICES[0][0], "")

    def test_the_combiner_trims_and_drops_empties(self):
        self.assertEqual(viewsmod.bulk_reason("  Found items ", "  "), "Found items")
        self.assertEqual(viewsmod.bulk_reason("", " typed  "), "typed")
        self.assertEqual(viewsmod.bulk_reason("", ""), "")


class ImportSquareSalesTests(TestCase):
    """The CSV recovery path, which is what runs after a webhook gap.

    It is a desk tool, not a field one — a CSV export off the dashboard,
    matched on SKU. That makes it the only reconciliation route that needs no
    Square API token at all, so an expired token takes out the webhook and the
    inventory push while leaving this intact.
    """

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea")
        self.dyed = make_product(self.recipe, "Stormy Silk", with_image=False)
        FinishedProduct.objects.filter(pk=self.dyed.pk).update(number_on_hand=10)
        self.dyed.refresh_from_db()

        self.undyed = make_undyed("Merino Worsted Natural", on_hand=12)

    def _run(self, rows, **opts):
        """Write `rows` as a Square export and import it."""
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".csv", newline="", delete=False, encoding="utf-8"
        )
        self.addCleanup(os.unlink, handle.name)
        with handle as f:
            writer = csv.DictWriter(
                f, fieldnames=["Date", "Transaction ID", "Item", "SKU", "Qty"]
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

        out = StringIO()
        call_command("import_square_sales", handle.name, stdout=out, **opts)
        return out.getvalue()

    def _row(self, product, qty=2, txn="ORDER-1"):
        return {
            "Date": "2026-08-24",
            "Transaction ID": txn,
            "Item": product.name,
            "SKU": product.sku,
            "Qty": str(qty),
        }

    def test_a_dyed_sale_comes_off_the_finished_row(self):
        self._run([self._row(self.dyed, qty=3)])

        self.dyed.refresh_from_db()
        self.assertEqual(self.dyed.number_on_hand, 7)

    def test_an_undyed_sale_comes_off_the_raw_pile(self):
        """The bug this class was written for.

        Writing `number_on_hand` on a passthrough writes to the mirror:
        `save()` re-derives it from the raw row, the number snaps back, and the
        command reports OK having moved nothing. The reorder signal — the whole
        reason undyed stock is counted — never moves.
        """
        self._run([self._row(self.undyed, qty=2)])

        self.undyed.raw_product.refresh_from_db()
        self.assertEqual(self.undyed.raw_product.number_on_hand, 10)

    def test_the_undyed_mirror_follows(self):
        self._run([self._row(self.undyed, qty=2)])

        self.undyed.refresh_from_db()
        self.assertEqual(self.undyed.number_on_hand, 10)

    def test_it_never_drives_stock_negative(self):
        self._run([self._row(self.undyed, qty=99)])

        self.undyed.raw_product.refresh_from_db()
        self.assertEqual(self.undyed.raw_product.number_on_hand, 0)

    def test_a_sale_the_webhook_already_logged_is_skipped(self):
        """The double-dip guard, and the contract it rests on.

        Square's CSV "Transaction ID" column carries the *order* id — the same
        value `square_webhook` writes to `sale_reference`. That is what makes it
        safe to import a period the webhook partly handled. If either side ever
        keys off something else this fails, which is the point: the symptom
        otherwise is every already-recorded sale decremented a second time.
        """
        InventoryLog.objects.create(
            finished_product=self.dyed,
            raw_product=self.dyed.raw_product,
            log_type=InventoryLog.SALE,
            quantity=-3,
            sale_reference="ORDER-1",
            notes="Square sale via webhook.",
        )

        output = self._run([self._row(self.dyed, qty=3, txn="ORDER-1")])

        self.dyed.refresh_from_db()
        self.assertEqual(self.dyed.number_on_hand, 10)
        self.assertIn("1 duplicate", output)

    def test_running_the_same_export_twice_changes_nothing(self):
        rows = [self._row(self.dyed, qty=3, txn="ORDER-1")]
        self._run(rows)
        self._run(rows)

        self.dyed.refresh_from_db()
        self.assertEqual(self.dyed.number_on_hand, 7)
        self.assertEqual(InventoryLog.objects.filter(log_type=InventoryLog.SALE).count(), 1)

    def test_a_dry_run_moves_nothing(self):
        self._run([self._row(self.undyed, qty=2)], dry_run=True)

        self.undyed.raw_product.refresh_from_db()
        self.assertEqual(self.undyed.raw_product.number_on_hand, 12)
        self.assertFalse(InventoryLog.objects.exists())

    def test_a_line_with_no_sku_is_counted_rather_than_dropped(self):
        """A hand-keyed sale carries no SKU. It can't be recovered here, but
        an unrecoverable line that says nothing is how the last one went
        missing."""
        row = self._row(self.dyed)
        row["SKU"] = ""

        output = self._run([row])

        self.assertIn("1 skipped (no SKU)", output)


def make_close_product(name, on_hand=0, par=3):
    """A finished product with a known count, for the close tests."""
    product = make_product(make_recipe(f"{name} Recipe"), name, with_image=False)
    FinishedProduct.objects.filter(pk=product.pk).update(
        number_on_hand=on_hand, par=par
    )
    product.refresh_from_db()
    return product


class SundayCloseTests(TestCase):
    """The close's three answers, and what each one is allowed to move.

    The expensive failures here are all silent. A tag filed as the wrong kind
    of disagreement corrupts the only number the page produces; an adjustment
    applied twice takes stock the shelf still has; a closed day that still
    accepts answers rewrites a record somebody already read.
    """

    def setUp(self):
        self.employee = Employee.objects.create(name="Close Tester", pin="4321")

    # --- what lands on the list -------------------------------------------

    def test_expected_list_is_the_zeros_and_only_the_zeros(self):
        out = make_close_product("Out Of Stock", on_hand=0)
        in_stock = make_close_product("Still Has Some", on_hand=2)

        expected = list(closing.expected_products())
        self.assertIn(out, expected)
        self.assertNotIn(in_stock, expected)

    def test_a_passthrough_never_asks_for_a_tag(self):
        """Undyed stock is ordered, not made, and has no kanban card.

        It is excluded by the null recipe it always has — the same test every
        dyed-only query in the app relies on.
        """
        category, _ = RawProductCategory.objects.get_or_create(name="Yarn")
        raw = RawProduct.objects.create(
            name="Undyed Sock Yarn", category=category, price="12.00"
        )
        passthrough = FinishedProduct.objects.create(
            name="Undyed Sock Yarn", raw_product=raw, recipe=None, price="18.00"
        )
        FinishedProduct.objects.filter(pk=passthrough.pk).update(
            number_on_hand=0, par=5
        )

        self.assertNotIn(passthrough, list(closing.expected_products()))

    def test_a_retired_product_is_not_asked_about(self):
        product = make_close_product("Retired", on_hand=0)
        FinishedProduct.objects.filter(pk=product.pk).update(is_active=False)
        self.assertNotIn(product, list(closing.expected_products()))

    # --- one run per day ---------------------------------------------------

    def test_opening_the_close_twice_in_a_day_is_one_run(self):
        """Reopening is resuming. Two rows would split one night's findings."""
        make_close_product("Zeroed", on_hand=0)
        first, created_first = closing.run_for_today(employee=self.employee)
        second, created_second = closing.run_for_today(employee=self.employee)

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(CloseRun.objects.count(), 1)

    def test_a_product_that_sells_out_later_joins_the_open_run(self):
        """A close started at noon still has to ask about the four o'clock sale."""
        make_close_product("Sold Out At Noon", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        self.assertEqual(run.rows.count(), 1)

        afternoon = make_close_product("Sold Out At Four", on_hand=0)
        closing.sync_expected(run)

        self.assertEqual(run.rows.count(), 2)
        self.assertIn(afternoon, [row.finished_product for row in run.rows.all()])

    def test_syncing_never_rewrites_a_row_that_was_already_answered(self):
        """The frozen `on_hand_before` is what the disagreement was measured
        against — re-reading it would read back the number this close fixed."""
        product = make_close_product("Answered", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get()
        closing.record_missing(run, row, 4)

        closing.sync_expected(run)
        row.refresh_from_db()

        self.assertEqual(row.on_hand_before, 0)
        self.assertEqual(row.counted, 4)
        self.assertEqual(run.rows.count(), 1)

    # --- the three answers -------------------------------------------------

    def test_a_confirmed_tag_moves_nothing_and_logs_nothing(self):
        """Agreement is the common case and must be free. Recorded, not logged."""
        make_close_product("Agrees", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        closing.confirm(run, row)
        row.refresh_from_db()

        self.assertEqual(row.outcome, CloseRunRow.CONFIRMED)
        self.assertIsNone(row.applied_log)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_a_missing_tag_trues_the_count_up_and_tags_the_source(self):
        product = make_close_product("Undercounted", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        closing.record_missing(run, row, 5)
        product.refresh_from_db()
        row.refresh_from_db()

        self.assertEqual(product.number_on_hand, 5)
        self.assertEqual(row.outcome, CloseRunRow.MISSING)
        self.assertEqual(row.counted, 5)

        log = row.applied_log
        self.assertIsNotNone(log)
        self.assertEqual(log.quantity, 5)
        self.assertEqual(log.log_type, InventoryLog.ADJUSTMENT)
        self.assertEqual(log.source, InventoryLog.SOURCE_SUNDAY_CLOSE)

    def test_no_tag_and_an_empty_bag_is_recorded_but_moves_nothing(self):
        """The tag protocol broke rather than the count did — still an answer."""
        product = make_close_product("Empty Bag", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        closing.record_missing(run, row, 0)
        row.refresh_from_db()
        product.refresh_from_db()

        self.assertEqual(row.outcome, CloseRunRow.MISSING)
        self.assertEqual(row.counted, 0)
        self.assertIsNone(row.applied_log)
        self.assertEqual(product.number_on_hand, 0)

    def test_an_unpredicted_tag_zeroes_the_count_and_tags_the_source(self):
        product = make_close_product("Overcounted", on_hand=3)
        run, _ = closing.run_for_today()

        row, created = closing.add_tag(run, product)
        product.refresh_from_db()

        self.assertTrue(created)
        self.assertEqual(row.outcome, CloseRunRow.EXTRA)
        self.assertEqual(row.on_hand_before, 3)
        self.assertEqual(product.number_on_hand, 0)
        self.assertEqual(row.applied_log.quantity, -3)
        self.assertEqual(row.applied_log.source, InventoryLog.SOURCE_SUNDAY_CLOSE)

    def test_a_tag_for_something_already_at_zero_is_an_agreement(self):
        """The trap this page could most easily set for itself.

        A product at zero that never made the expected list (no par, say)
        still has a tag. Filing that as an overcount would put a fault into
        the one number the close exists to produce, and it would be a fault
        in the direction that reads as "the till is losing sales".
        """
        product = make_close_product("Zero But Unlisted", on_hand=0, par=0)
        run, _ = closing.run_for_today()
        self.assertEqual(run.rows.count(), 0)   # par 0, so not predicted

        row, created = closing.add_tag(run, product)

        self.assertTrue(created)
        self.assertEqual(row.outcome, CloseRunRow.CONFIRMED)
        self.assertIsNone(row.applied_log)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_an_unpredicted_passthrough_tag_writes_to_the_raw_row(self):
        """One pile, one count. Writing the mirror would snap back on save."""
        category, _ = RawProductCategory.objects.get_or_create(name="Yarn")
        raw = RawProduct.objects.create(
            name="Undyed DK", category=category, price="12.00", number_on_hand=6
        )
        passthrough = FinishedProduct.objects.create(
            name="Undyed DK", raw_product=raw, recipe=None, price="18.00"
        )
        run, _ = closing.run_for_today()

        closing.add_tag(run, passthrough)
        raw.refresh_from_db()
        passthrough.refresh_from_db()

        self.assertEqual(raw.number_on_hand, 0)
        self.assertEqual(passthrough.number_on_hand, 0)

    # --- applying twice ----------------------------------------------------

    def test_a_row_that_moved_stock_is_never_applied_again(self):
        """The page gets reopened and the button gets double-tapped."""
        product = make_close_product("Double Tap", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        closing.record_missing(run, row, 4)
        closing.record_missing(run, row, 9)
        product.refresh_from_db()

        self.assertEqual(product.number_on_hand, 4)
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_the_same_tag_added_twice_adjusts_once(self):
        product = make_close_product("Scanned Twice", on_hand=2)
        run, _ = closing.run_for_today()

        closing.add_tag(run, product)
        row, created = closing.add_tag(run, product)
        product.refresh_from_db()

        self.assertFalse(created)
        self.assertEqual(product.number_on_hand, 0)
        self.assertEqual(InventoryLog.objects.filter(quantity=-2).count(), 1)

    def test_a_tick_can_be_taken_back_but_a_correction_cannot(self):
        """Un-ticking is safe precisely because nothing moved."""
        make_close_product("Mis-tapped", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        closing.confirm(run, row)
        closing.unconfirm(run, row)
        row.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.PENDING)

        closing.record_missing(run, row, 2)
        closing.unconfirm(run, row)
        row.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.MISSING)

    # --- yesterday is a record ---------------------------------------------

    def test_yesterdays_close_takes_no_more_answers(self):
        product = make_close_product("Yesterday", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        CloseRun.objects.filter(pk=run.pk).update(
            day=timezone.localdate() - timedelta(days=1)
        )
        run.refresh_from_db()
        self.assertFalse(run.is_open)

        closing.confirm(run, row)
        closing.record_missing(run, row, 7)
        added, created = closing.add_tag(run, make_close_product("Late", on_hand=4))
        row.refresh_from_db()
        product.refresh_from_db()

        self.assertEqual(row.outcome, CloseRunRow.PENDING)
        self.assertEqual(product.number_on_hand, 0)
        self.assertIsNone(added)
        self.assertFalse(created)

    def test_syncing_a_finished_day_adds_nothing(self):
        run, _ = closing.run_for_today()
        CloseRun.objects.filter(pk=run.pk).update(
            day=timezone.localdate() - timedelta(days=1)
        )
        run.refresh_from_db()

        make_close_product("Sold Out Tomorrow", on_hand=0)
        self.assertEqual(closing.sync_expected(run), [])
        self.assertEqual(run.rows.count(), 0)

    # --- the tally ---------------------------------------------------------

    def test_the_tally_counts_failures_and_keeps_the_directions_apart(self):
        """Never a net figure: a bad intake would cancel out a dead webhook."""
        under = make_close_product("Under", on_hand=0)
        agreed = make_close_product("Agreed", on_hand=0)
        over = make_close_product("Over", on_hand=2)
        run, _ = closing.run_for_today()

        closing.confirm(run, run.rows.get(finished_product=agreed))
        closing.record_missing(run, run.rows.get(finished_product=under), 3)
        closing.add_tag(run, over)

        tally = closing.tally(run)
        self.assertEqual(tally["missing"], 1)
        self.assertEqual(tally["extra"], 1)
        self.assertEqual(tally["confirmed"], 1)
        self.assertEqual(tally["disagreements"], 2)
        self.assertEqual(tally["under_units"], 3)
        self.assertEqual(tally["over_units"], 2)
        self.assertNotIn("rate", tally)


class SundayClosePageTests(TestCase):
    """The pages, including the PIN and the parts a stale tab can reach."""

    def setUp(self):
        self.employee = Employee.objects.create(name="Page Tester", pin="1234")

    def test_the_close_opens_for_someone_with_no_account(self):
        """secret/ means unlisted, not logged in — a redirect here is the bug."""
        response = self.client.get(reverse("close_index"))
        self.assertEqual(response.status_code, 200)

    def test_a_wrong_pin_starts_no_close(self):
        response = self.client.post(reverse("close_index"), {
            "employee": self.employee.pk,
            "pin": "9999",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CloseRun.objects.count(), 0)

    def test_the_right_pin_opens_the_day_and_redirects_to_it(self):
        make_close_product("On The List", on_hand=0)
        response = self.client.post(reverse("close_index"), {
            "employee": self.employee.pk,
            "pin": "1234",
        })
        run = CloseRun.objects.get()
        self.assertRedirects(response, reverse("close_run", args=[run.token]))
        self.assertEqual(run.employee, self.employee)
        self.assertEqual(run.rows.count(), 1)

    def test_ticking_confirms_and_leaving_blank_does_not_guess(self):
        """An unticked box means "not counted yet", never "I have none"."""
        make_close_product("Ticked", on_hand=0)
        make_close_product("Untouched", on_hand=0)
        run, _ = closing.run_for_today()
        ticked = run.rows.first()

        self.client.post(reverse("close_run", args=[run.token]), {
            "step": "confirm",
            "held": [str(ticked.pk)],
        })

        ticked.refresh_from_db()
        self.assertEqual(ticked.outcome, CloseRunRow.CONFIRMED)
        self.assertEqual(
            run.rows.filter(outcome=CloseRunRow.PENDING).count(), 1
        )
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_a_product_that_sold_out_since_appears_on_the_very_next_load(self):
        """The four o'clock sale has to be on the seven o'clock page.

        Not the one after it. The close gets worked in passes across an
        evening, and a row that arrives one request late is one nobody is
        asked about while they are standing in front of the tags — the list
        reads as complete and the scarf is simply missing from it.

        This is a regression test for a prefetch cache: the rows were being
        read into memory before `sync_expected` added to them.
        """
        first = make_close_product("Out At Four", on_hand=0)
        run, _ = closing.run_for_today()
        self.client.get(reverse("close_run", args=[run.token]))

        later = make_close_product("Out At Seven", on_hand=0)
        html = self.client.get(
            reverse("close_run", args=[run.token])
        ).content.decode()

        row = run.rows.get(finished_product=later)
        self.assertIn(f'value="{row.pk}"', html)
        self.assertIn(later.name, html)

    def test_only_the_tags_nobody_found_are_asked_for_a_count(self):
        """Step 2 asks about the rows step 1 left unanswered, and no others.

        A confirmed row is one the tag was in hand for. Drawing a count box
        beside it invites a number that contradicts the answer already given,
        and taking that number would reclassify an agreement as a
        disagreement — in the one table whose whole output is the count of
        disagreements.
        """
        held = make_close_product("Tag In Hand", on_hand=0)
        absent = make_close_product("Tag Missing", on_hand=0)
        run, _ = closing.run_for_today()
        held_row = run.rows.get(finished_product=held)
        absent_row = run.rows.get(finished_product=absent)

        self.client.post(reverse("close_run", args=[run.token]), {
            "step": "confirm",
            "held": [str(held_row.pk)],
        })
        html = self.client.get(
            reverse("close_run", args=[run.token])
        ).content.decode()

        self.assertIn(f"counted_{absent_row.pk}", html)
        self.assertNotIn(f"counted_{held_row.pk}", html)

    def test_a_lost_bag_turns_up_and_the_tick_comes_back_off(self):
        """Confirmed at four, a bag of them found at seven.

        This is the whole reason a confirmed row stays un-frozen: ticking one
        moves no stock, so the tick can come back off, and the row rejoins the
        count list where the bag's real contents get typed in. A correction
        that had already moved stock could not be walked back this way — that
        is an adjustment with a reason on it.

        It lands as `missing`, and the label is worth reading carefully: the
        tag was in hand, so "no tag" isn't literally what happened. What the
        outcome records is the *direction* — the app was under, which is the
        stock-arrived-unrecorded end of the pipeline — and that is what the
        count is counting.
        """
        product = make_close_product("Lost Bag", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()
        url = reverse("close_run", args=[run.token])

        # Four o'clock: tag in hand, the app agrees, nothing to count.
        self.client.post(url, {"step": "confirm", "held": [str(row.pk)]})
        row.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.CONFIRMED)
        html = self.client.get(url).content.decode()
        self.assertNotIn(f"counted_{row.pk}", html)

        # Seven o'clock: a bag of six turns up under the table. Untick it.
        self.client.post(url, {"step": "confirm", "held": []})
        row.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.PENDING)

        html = self.client.get(url).content.decode()
        self.assertIn(f"counted_{row.pk}", html)

        self.client.post(url, {"step": "count", f"counted_{row.pk}": "6"})
        row.refresh_from_db()
        product.refresh_from_db()

        self.assertEqual(row.outcome, CloseRunRow.MISSING)
        self.assertEqual(row.counted, 6)
        self.assertEqual(product.number_on_hand, 6)
        self.assertEqual(row.applied_log.quantity, 6)
        self.assertEqual(row.applied_log.source, InventoryLog.SOURCE_SUNDAY_CLOSE)
        self.assertEqual(closing.tally(run)["missing"], 1)
        self.assertEqual(closing.tally(run)["confirmed"], 0)

    def test_unticking_after_the_count_cannot_take_the_stock_back(self):
        """Once stock has moved, the box is not the way to undo it.

        The asymmetry is deliberate and it is the same rule the production
        sheet applies: taking a movement back is an inventory adjustment with
        a reason attached, not an untick on a page with no login.
        """
        product = make_close_product("Already Moved", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()
        url = reverse("close_run", args=[run.token])

        self.client.post(url, {"step": "count", f"counted_{row.pk}": "5"})
        self.client.post(url, {"step": "confirm", "held": []})

        row.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.MISSING)
        self.assertEqual(product.number_on_hand, 5)
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_a_count_posted_for_a_confirmed_row_is_refused(self):
        """The same rule, enforced where a hand-built POST would arrive."""
        held = make_close_product("Confirmed Then Counted", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get(finished_product=held)
        closing.confirm(run, row)

        self.client.post(reverse("close_run", args=[run.token]), {
            "step": "count",
            f"counted_{row.pk}": "8",
        })

        row.refresh_from_db()
        held.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.CONFIRMED)
        self.assertEqual(held.number_on_hand, 0)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_a_blank_count_is_left_for_later_rather_than_read_as_zero(self):
        product = make_close_product("Not Counted Yet", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        self.client.post(reverse("close_run", args=[run.token]), {
            "step": "count",
            f"counted_{row.pk}": "",
        })

        row.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.PENDING)
        self.assertEqual(product.number_on_hand, 0)

    def test_counting_through_the_page_moves_stock_and_tags_the_log(self):
        product = make_close_product("Counted", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        self.client.post(reverse("close_run", args=[run.token]), {
            "step": "count",
            f"counted_{row.pk}": "6",
        })

        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 6)
        self.assertEqual(
            InventoryLog.objects.get().source, InventoryLog.SOURCE_SUNDAY_CLOSE
        )

    def test_adding_a_tag_through_the_page_zeroes_it(self):
        product = make_close_product("Held", on_hand=4)
        run, _ = closing.run_for_today()

        response = self.client.post(reverse("close_add_tag", args=[run.token]), {
            "product_id": product.pk,
        })

        self.assertRedirects(response, reverse("close_run", args=[run.token]))
        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 0)

    def test_a_stale_tab_cannot_write_to_a_finished_day(self):
        """The van is unpacked by now — this is a bookmark, not a person
        standing in front of the tags."""
        product = make_close_product("Yesterdays", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()
        CloseRun.objects.filter(pk=run.pk).update(
            day=timezone.localdate() - timedelta(days=1)
        )

        self.client.post(reverse("close_run", args=[run.token]), {
            "step": "count",
            f"counted_{row.pk}": "9",
        })
        self.client.post(reverse("close_add_tag", args=[run.token]), {
            "product_id": product.pk,
        })

        row.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.PENDING)
        self.assertEqual(product.number_on_hand, 0)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_a_finished_day_still_reads(self):
        """Shown read-only rather than 404'd: a page that vanishes reads as a
        lost close rather than a closed one."""
        make_close_product("Readable", on_hand=0)
        run, _ = closing.run_for_today()
        CloseRun.objects.filter(pk=run.pk).update(
            day=timezone.localdate() - timedelta(days=1)
        )

        response = self.client.get(reverse("close_run", args=[run.token]))
        self.assertEqual(response.status_code, 200)

    def test_the_history_page_is_staff_only(self):
        response = self.client.get(reverse("close_history"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])


class WebhookOutageRecoveryTests(TestCase):
    """The worst realistic day: Square up, webhooks down, nothing zeroes out.

    Staff end the day holding a fistful of tags for products the app still
    believes are in stock. The recovery is to import the missed sales from
    the CSV *and then* run the close — and the order is the whole point,
    because the close writes adjustments while the import writes sales, so
    the import's dedupe cannot see what the close did.
    """

    def setUp(self):
        self.employee = Employee.objects.create(name="Owner", pin="1234")
        self.products = [
            make_close_product("Stormy", on_hand=2),
            make_close_product("Ember", on_hand=1),
        ]

    def _csv(self):
        import csv
        import tempfile

        path = tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, newline=""
        )
        writer = csv.DictWriter(
            path, fieldnames=["Date", "Transaction ID", "SKU", "Item", "Qty"]
        )
        writer.writeheader()
        for i, product in enumerate(self.products, start=1):
            writer.writerow({
                "Date": "2026-08-30",
                "Transaction ID": f"ORD{i}",
                "SKU": product.sku,
                "Item": product.name,
                "Qty": product.number_on_hand,
            })
        path.close()
        return path.name

    def test_importing_first_leaves_the_ledger_booked_once(self):
        """The documented recovery, end to end.

        The import zeroes the products out, they join the close's expected
        list on the next load, and every tag is then an ordinary agreement.
        Net movement equals the stock that actually left.
        """
        call_command("import_square_sales", self._csv(), verbosity=0)

        for product in self.products:
            product.refresh_from_db()
            self.assertEqual(product.number_on_hand, 0)

        run, _ = closing.run_for_today(employee=self.employee)
        self.assertEqual(run.rows.count(), len(self.products))

        self.client.post(reverse("close_run", args=[run.token]), {
            "step": "confirm",
            "held": [str(row.pk) for row in run.rows.all()],
        })

        tally = closing.tally(run)
        self.assertEqual(tally["confirmed"], len(self.products))
        self.assertEqual(tally["disagreements"], 0)

        movement = sum(log.quantity for log in InventoryLog.objects.all())
        self.assertEqual(movement, -3)
        self.assertEqual(
            InventoryLog.objects.filter(
                source=InventoryLog.SOURCE_SQUARE_IMPORT
            ).count(),
            len(self.products),
        )
        self.assertEqual(
            InventoryLog.objects.filter(
                source=InventoryLog.SOURCE_SUNDAY_CLOSE
            ).count(),
            0,
        )

    def test_closing_first_still_lands_on_the_right_count(self):
        """Stock survives the wrong order — the ledger doesn't.

        Zeroing the tags by hand and importing afterwards ends with exactly
        the same shelf, because `set_on_hand` clamps at zero. What differs is
        the trail: the same physical sale is booked twice, once as a close
        adjustment and once as an imported sale, and the import's dedupe
        cannot prevent it because a close adjustment carries no
        `sale_reference` to match on.

        Pinned rather than fixed. Guessing that an adjustment and a sale are
        the same event would need the close to claim an order id it was never
        told, and a wrong guess would suppress a real sale.
        """
        run, _ = closing.run_for_today(employee=self.employee)
        for product in self.products:
            closing.add_tag(run, product)

        call_command("import_square_sales", self._csv(), verbosity=0)

        for product in self.products:
            product.refresh_from_db()
            self.assertEqual(product.number_on_hand, 0)

        movement = sum(log.quantity for log in InventoryLog.objects.all())
        self.assertEqual(movement, -6)          # twice the -3 that really left


class InventoryLogSourceTests(TestCase):
    """Every flow that moves stock says which one it was.

    The point of the field is that the close's corrections can be counted
    against the ways stock is *supposed* to move. A site that forgets to set
    it doesn't error — it just quietly drops out of every total, which is the
    same failure the notes-matching it replaced already had.
    """

    def test_every_creation_site_names_itself(self):
        import ast
        import inspect

        from . import production, views
        from .management.commands import fake_sale, import_square_sales

        missing = []
        for module in (views, production, fake_sale, import_square_sales):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                # InventoryLog.objects.create(...)
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr == "create"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "objects"
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "InventoryLog"
                ):
                    continue
                if not any(kw.arg == "source" for kw in node.keywords):
                    missing.append(f"{module.__name__}:{node.lineno}")

        self.assertEqual(
            missing, [],
            "an InventoryLog written without a source drops out of every "
            "count silently — give it one of InventoryLog.SOURCE_*",
        )

    def test_a_bulk_update_is_told_apart_from_a_close(self):
        user = User.objects.create_superuser("src", "s@example.test", "pw")
        self.client.force_login(user)
        product = make_close_product("Bulk Adjusted", on_hand=1)

        self.client.post(
            f"{reverse('bulk_inventory_update')}?raw_ids={product.raw_product_id}",
            {f"count_{product.pk}": "4", "reason_preset": "", "reason": "stock take"},
        )

        log = InventoryLog.objects.get()
        self.assertEqual(log.source, InventoryLog.SOURCE_BULK_UPDATE)
        self.assertEqual(
            InventoryLog.objects.filter(
                source=InventoryLog.SOURCE_SUNDAY_CLOSE
            ).count(),
            0,
        )
