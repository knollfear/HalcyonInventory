"""The site map, the URL buckets, the pinned nav and the template layers.

The reasoning behind these is in `docs/claude/nav.md`.
"""
import pathlib
import re
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from .. import (
    closing, colorbands, crew, fancy, nav, photowalk, production, restock,
    sales, seasonreport, seasons, sheetscan, skus, slowsellers, timesheets,
    weather,
)
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


class PinnedNavTests(TestCase):
    """The handful of pages in the corner of every staff page.

    Hub-and-spoke is right for a directory nobody memorises and wrong for the
    four pages somebody opens every day — those cost two clicks each, forever.
    """

    def setUp(self):
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)
        self.page = reverse("recipe_showcase")

    def _pin(self, value):
        self.client.get(reverse("navigation"), {nav.PARAM: value}, follow=True)

    # --- the corner -------------------------------------------------------

    def test_the_pins_render_beside_the_site_map_link(self):
        self._pin("recipe_showcase,color_classify")

        html = self.client.get(reverse("production_needed")).content.decode()

        self.assertIn("Recipe Showcase", html)
        self.assertIn("Colour Classification", html)
        self.assertIn("← Site map", html)

    def test_the_site_map_link_keeps_the_corner_it_always_had(self):
        """The pins grow leftward from it, so whatever anybody has already
        learned about where the way out is stays true."""
        self._pin("recipe_showcase,color_classify")

        html = self.client.get(reverse("production_needed")).content.decode()
        bar = html.split('<nav class="navbar"')[1].split("</nav>")[0]

        self.assertLess(bar.index("Recipe Showcase"), bar.index("← Site map"))

    def test_the_order_is_the_order_given(self):
        """A fixed nav's whole value is that the third pill is always the
        third pill."""
        self._pin("color_classify,recipe_showcase")

        bar = self.client.get(reverse("production_needed")).content.decode()
        bar = bar.split('<nav class="navbar"')[1].split("</nav>")[0]

        self.assertLess(bar.index("Colour Classification"), bar.index("Recipe Showcase"))

    def test_the_page_you_are_on_is_dimmed_rather_than_dropped(self):
        """A set that silently loses whichever one you are looking at changes
        shape as you move through it, which is the thing a fixed nav exists
        not to do."""
        self._pin("recipe_showcase")

        html = self.client.get(self.page).content.decode()

        self.assertIn("navpin here", html)

    def test_no_staff_nav_reaches_a_public_page(self):
        """`secret/` pages extend base_public too, so this is also what keeps
        the crew's pages from advertising the staff ones."""
        self._pin("recipe_showcase")

        html = self.client.get(reverse("public_index")).content.decode()

        self.assertNotIn("navpin", html)

    def test_a_malformed_cookie_is_an_empty_nav_and_never_an_error(self):
        """Read on every staff page render, so the only safe failure is a
        shorter nav."""
        self.client.cookies[nav.PIN_COOKIE] = "%%%,,::,not_a_route:x"

        response = self.client.get(self.page)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["nav_pins"], [])
        self.assertNotIn('<a class="navpin', response.content.decode())

    # --- the link ---------------------------------------------------------

    def test_a_link_sets_the_pins_and_says_that_it_did(self):
        """A link that silently rearranged somebody's screen leaves them no
        way to work out what happened — the crew cookie's argument about a
        pre-filled name nothing mentions."""
        response = self.client.get(
            reverse("navigation"), {nav.PARAM: "recipe_showcase"}, follow=True
        )

        self.assertContains(response, "Set from a link")
        self.assertContains(response, "Forget it")
        self.assertEqual(
            self.client.cookies[nav.PIN_COOKIE].value, "recipe_showcase"
        )

    def test_a_route_that_went_nowhere_is_named_and_never_dropped_quietly(self):
        """This app has no 404, so a bad link would otherwise land on the
        public map looking like a working page."""
        response = self.client.get(
            reverse("navigation"),
            {nav.PARAM: "recipe_showcase,renamed_last_year"},
            follow=True,
        )

        self.assertContains(response, "renamed_last_year")
        self.assertContains(response, "went nowhere")
        self.assertEqual(len(response.context["pins"]), 1)

    def test_a_link_can_relabel_a_page(self):
        """`@page_meta` titles are written for a site map card; a pill in the
        corner has room for a word."""
        self._pin("recipe_showcase:Recipes")

        html = self.client.get(reverse("production_needed")).content.decode()

        self.assertIn(">Recipes<", html)

    def test_a_label_cannot_be_a_sentence(self):
        long_label = "x" * 200
        self._pin(f"recipe_showcase:{long_label}")

        pins, _ = nav.parse(f"recipe_showcase:{long_label}")

        self.assertEqual(len(pins[0]["label"]), nav.MAX_LABEL)

    def test_more_than_the_cap_is_refused_and_reported(self):
        """Past about four this is a second site map, worse organised than the
        one it sits next to."""
        every = ",".join(p["name"] for p in nav.pinnable()[:8])

        response = self.client.get(
            reverse("navigation"), {nav.PARAM: every}, follow=True
        )

        self.assertEqual(len(response.context["pins"]), nav.MAX_PINS)
        self.assertTrue(response.context["dropped"])

    def test_forget_clears_them(self):
        self._pin("recipe_showcase")

        response = self.client.get(
            reverse("navigation"), {nav.PARAM: nav.FORGET}, follow=True
        )

        self.assertContains(response, "Forgotten")
        self.assertFalse(self.client.cookies[nav.PIN_COOKIE].value)

    def test_saving_the_form_pins_what_was_ticked(self):
        response = self.client.post(
            reverse("navigation"),
            {"pin": ["recipe_showcase", "color_classify"]},
            follow=True,
        )

        self.assertContains(response, "Saved")
        self.assertEqual(
            self.client.cookies[nav.PIN_COOKIE].value,
            "recipe_showcase,color_classify",
        )

    def test_saving_redirects_so_the_corner_agrees_with_the_preview(self):
        """The corner is rendered by a context processor reading
        `request.COOKIES`, so a response that sets the cookie *and* renders
        shows the new pins in the preview and the old ones in the corner — on
        the one page whose job is to show what the corner will look like."""
        self._pin("color_classify")

        response = self.client.post(
            reverse("navigation"), {"pin": ["recipe_showcase"]}
        )
        self.assertEqual(response.status_code, 302)

        landed = self.client.get(response["Location"])
        bar = landed.content.decode().split('<nav class="navbar"')[1].split("</nav>")[0]
        self.assertIn("Recipe Showcase", bar)
        self.assertNotIn("Colour Classification", bar)

    def test_clearing_redirects_too_and_empties_the_corner(self):
        self._pin("recipe_showcase")

        response = self.client.get(reverse("navigation"), {nav.PARAM: nav.FORGET})
        self.assertEqual(response.status_code, 302)

        landed = self.client.get(response["Location"])
        bar = landed.content.decode().split('<nav class="navbar"')[1].split("</nav>")[0]
        self.assertNotIn('<a class="navpin', bar)
        self.assertIn("← Site map", bar)

    def test_only_pages_the_site_map_lists_can_be_pinned(self):
        """Which excludes two groups for free rather than by a rule written
        here: anything with no `@page_meta` (POST endpoints, htmx fragments,
        the webhook), and anything parameterised, since it reverses to
        nothing and a pin needs somewhere to go."""
        names = {p["name"] for p in nav.pinnable()}

        self.assertIn("recipe_showcase", names)
        self.assertNotIn("recipe_dyes_save", names)   # POST only
        self.assertNotIn("recipe_history", names)     # htmx fragment
        self.assertNotIn("square_webhook", names)
        self.assertNotIn("recipe_detail", names)      # needs a pk
        for name in names:
            self.assertTrue(reverse(name), name)

    # --- the counter, which decides nothing --------------------------------

    def test_opening_a_page_counts_it(self):
        self.client.get(self.page)
        self.client.get(self.page)

        self.assertEqual(
            nav.read_seen(self._request_with_cookies())["recipe_showcase"], 2
        )

    def _request_with_cookies(self):
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.COOKIES = {k: v.value for k, v in self.client.cookies.items()}
        return request

    def test_an_htmx_fragment_is_not_somewhere_you_went(self):
        """The recipe showcase would otherwise out-count every page in the app
        by the width of an afternoon's dye entry."""
        recipe = make_recipe("Cabernet")

        self.client.get(
            reverse("recipe_row", args=[recipe.pk]), HTTP_HX_REQUEST="true"
        )

        self.assertNotIn("recipe_row", nav.read_seen(self._request_with_cookies()))

    def test_a_login_redirect_is_not_a_visit(self):
        self.client.logout()

        self.client.get(self.page)

        self.assertEqual(nav.read_seen(self._request_with_cookies()), {})

    def test_a_post_is_not_a_visit(self):
        recipe = make_recipe("Cabernet")

        self.client.post(reverse("recipe_dyes_save", args=[recipe.pk]), {})

        self.assertNotIn(
            "recipe_dyes_save", nav.read_seen(self._request_with_cookies())
        )

    def test_the_count_never_chooses_what_is_pinned(self):
        """The load-bearing one. Promoting the top four on its own would be
        ranking navigation on a number nobody chose — self-reinforcing, since
        a pinned page is one click away and so gets opened more — and it is
        the `par` mistake exactly: a derived figure that reads as a fact
        because it came out of a counter, with nowhere to disagree with it."""
        for _ in range(20):
            self.client.get(reverse("production_needed"))

        response = self.client.get(self.page)

        self.assertEqual(response.context["nav_pins"], [])
        self.assertNotIn('<a class="navpin', response.content.decode())
        self.assertGreater(
            nav.read_seen(self._request_with_cookies())["production_needed"], 10
        )

    def test_the_counter_cookie_is_bounded(self):
        """It rides on every request, so an unbounded tally of every page ever
        opened is paid for on requests that never read it."""
        counts = {}
        for i in range(nav.MAX_SEEN + 25):
            counts = nav.bump(counts, f"page_{i:03d}")

        self.assertEqual(len(counts), nav.MAX_SEEN)
        self.assertIn(f"page_{nav.MAX_SEEN + 24:03d}", counts)

    def test_the_navigation_page_shows_the_counts_as_evidence(self):
        self.client.get(reverse("production_needed"))

        response = self.client.get(reverse("navigation"))
        rows = {r["name"]: r for r in response.context["rows"]}

        self.assertEqual(rows["production_needed"]["seen"], 1)
        self.assertContains(response, "decides nothing")
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

        root = Path(__file__).resolve().parent.parent / "templates"
        # A root that resolves to nothing finds no offenders and reads as a
        # pass, which is this test's own failure mode wearing a green tick.
        self.assertTrue(root.is_dir(), f"template root is not there: {root}")
        pattern = re.compile(r"\{#(?:(?!#\}).)*?\n(?:(?!#\}).)*?#\}", re.S)

        offenders = []
        for path in sorted(root.rglob("*.html")):
            text = path.read_text()
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(root)}:{line}")

        self.assertEqual(offenders, [], "use {% comment %} for multi-line comments")
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

        root = Path(__file__).resolve().parent.parent / "templates" / "scarves"
        pages = [p for p in sorted(root.glob("*.html")) if not p.name.startswith("base")]
        # Same reason as above: an empty list passes every assertion built on it.
        self.assertTrue(pages, f"no page templates found under {root}")
        return pages

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
class ReportsAreTheirOwnCategoryTests(TestCase):
    """The read-only pages sit together, and none of them writes.

    `Reports` started as a category of one when Slow Sellers arrived, with
    Top Sellers, Season Pace and Close History filed under `Inventory`
    alongside the pages that actually move stock. Two names for one kind of
    page is how a site map stops being readable.
    """

    REPORTS = ["Top Sellers", "Slow Sellers", "Season Pace", "Close History"]

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))

    def _map(self):
        return self.client.get(reverse("index")).content.decode()

    def test_every_report_is_on_the_map(self):
        body = self._map()

        for title in self.REPORTS:
            self.assertIn(title, body)

    def test_they_share_one_category(self):
        from scarves import views

        cats = {
            fn.page_meta["category"]
            for name, fn in vars(views).items()
            if callable(fn) and getattr(fn, "page_meta", None)
            and fn.page_meta.get("title") in self.REPORTS
        }

        self.assertEqual(cats, {"Reports"})

    def test_a_report_writes_nothing(self):
        """What makes them one category: they read and produce a page, and
        the operational pages they used to sit with move stock."""
        import scarves.sales as sales
        import scarves.slowsellers as slow

        for module in (sales, slow):
            source = pathlib.Path(module.__file__).read_text()
            for forbidden in (".save(", ".create(", ".update(", ".delete("):
                self.assertNotIn(
                    forbidden, source,
                    f"{module.__name__} should not write: {forbidden}",
                )
