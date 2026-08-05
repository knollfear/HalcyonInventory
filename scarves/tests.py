"""
Tests for the public games — the matching board and the name quiz.

Two things here are worth more than they look:

* the dedupe guarantee — an infinity and a rectangle from the same dye bath are
  the same recipe and must never both appear, or the board can't be won by sight;
* the CORS preflight — htmx's `HX-Request` header makes the browser preflight,
  so getting `Access-Control-Allow-Headers` wrong breaks every embed while the
  Django page itself keeps working perfectly.
"""
import random
import re
import shutil
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.urls import reverse

from .colorutils import (
    delta_e,
    hex_to_lab,
    hex_to_rgb,
    nearest_by_color,
    palette_distance,
    pick_color_cluster,
    recipe_palette,
)
from .forms import RecipeDyesForm
from .models import (
    Dye,
    DyeBrand,
    FinishedProduct,
    FinishedProductImage,
    ProductImageUpload,
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
        hrefs = re.findall(rb'href="(/scarves/raw-inventory/\d+/)"', response.content)
        self.assertEqual(len(hrefs), 2)
        for href in hrefs:
            self.assertEqual(self.client.get(href.decode()).status_code, 200)


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
