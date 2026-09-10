"""The rainbow band classifier and the pages that confirm it.

The reasoning behind these is in `docs/claude/recipes.md`.
"""
import random
import tempfile
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from .. import (
    closing, colorbands, crew, fancy, nav, photowalk, production, restock,
    sales, seasonreport, seasons, sheetscan, skus, slowsellers, timesheets,
    weather,
)
from ..colorutils import (
    delta_e,
    hex_to_lab,
    hex_to_rgb,
    nearest_by_color,
    palette_distance,
    pick_color_cluster,
    recipe_palette,
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
        from ..views import _by_color_pages
        return _by_color_pages(category or self.silk)

    def test_a_two_band_colorway_prints_in_both_sections(self):
        self._colorway("Sunset", ["red", "blue"])

        self.assertEqual([slug for slug, _, _, _, _ in self._pages()], ["red", "blue"])

    def test_a_rainbow_gets_one_page_at_the_end_not_eight_through_the_middle(self):
        """The section earns its place by what it keeps *out*. Four rainbow
        colorways across seventeen products, claiming every band, would be
        eight extra pages in every section — each one the least useful answer
        to the question that section asks."""
        self._colorway("Classic Rainbow", ["rainbow"])
        self._colorway("Sunset", ["red"])

        pages = [(slug, recipe.name) for slug, _, _, recipe, _ in self._pages()]
        self.assertEqual(pages, [("red", "Sunset"), ("rainbow", "Classic Rainbow")])

    def test_the_kinds_of_rainbow_are_recipes_not_a_dimension(self):
        """Three or four kinds, and they are already separate colorways. A
        "kind of rainbow" field would be the fiber-field mistake."""
        self._colorway("Neon Rainbow", ["rainbow"])
        self._colorway("Pastel Rainbow", ["rainbow"])

        self.assertEqual(
            [recipe.name for slug, _, _, recipe, _ in self._pages()],
            ["Neon Rainbow", "Pastel Rainbow"],
        )

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
        from ..views import _band_tab_painter

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

    from ..colorbands import PHOTO_CROP

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
        from ..colorbands import band_for_hex

        # #000000 has hue 0 and #000001 has hue 240. Lightness is what saves it.
        self.assertEqual(band_for_hex("#000000"), "black")     # 639 Jet Black
        self.assertEqual(band_for_hex("#000001"), "black")     # 413 True Black

    def test_greys_are_caught_by_saturation_not_hue(self):
        from ..colorbands import band_for_hex

        self.assertEqual(band_for_hex("#708090"), "grey")      # Slate, hue 210
        self.assertEqual(band_for_hex("#877c85"), "grey")      # 638 Silver
        self.assertEqual(band_for_hex("#2a3439"), "grey")      # Gun

    def test_creams_are_caught_by_lightness_not_hue(self):
        from ..colorbands import band_for_hex

        self.assertEqual(band_for_hex("#f3ead7"), "grey")      # 488 Ivory, hue 41
        self.assertEqual(band_for_hex("#e9d6ba"), "grey")      # 486 Champagne

    def test_a_pale_pink_is_not_swept_up_as_white(self):
        """The cream rule has to spare saturated tints, or pink loses its palest
        members to grey — `481 Ballerina Pink` is lighter than Ivory."""
        from ..colorbands import band_for_hex

        self.assertEqual(band_for_hex("#facbca"), "pink")

    def test_brown_needs_all_three_axes(self):
        from ..colorbands import band_for_hex

        # 635 Brown: hue 8.6 says "red", and only dark + dull together say brown.
        self.assertEqual(band_for_hex("#33211e"), "brown")
        # A bright, saturated colour at a similar hue stays red.
        self.assertEqual(band_for_hex("#b72026"), "red")       # 616 Russet

    def test_forest_green_is_green_not_blue(self):
        """Regression: at a 170-degree green/blue line this landed in blue, one
        degree the wrong side. The teals must stay blue all the same."""
        from ..colorbands import band_for_hex

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
        from ..colorbands import band_for_hex

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
        from ..colorbands import band_for_hex

        self.assertEqual(band_for_hex("#f3ead7"), "grey")      # 488 Ivory, hue 41
        self.assertEqual(band_for_hex("#c2b264"), "yellow")    # 435 Soft Tan, hue 50

    def test_light_reds_read_as_pink(self):
        from ..colorbands import band_for_hex

        self.assertEqual(band_for_hex("#f37b70"), "pink")      # 607 Salmon
        self.assertEqual(band_for_hex("#a12033"), "red")       # 440 Oxblood Red

    def test_unparseable_hex_is_none_rather_than_a_guess(self):
        from ..colorbands import band_for_hex

        self.assertIsNone(band_for_hex(""))
        self.assertIsNone(band_for_hex("not a colour"))
        self.assertIsNone(band_for_hex(None))

    def test_bands_come_back_in_rainbow_order(self):
        from ..colorbands import sort_bands

        self.assertEqual(
            sort_bands(["blue", "red", "grey", "green", "red"]),
            ["red", "green", "blue", "grey"],
        )
class BandsFromDyesTests(TestCase):
    def test_each_dye_contributes_its_band(self):
        from ..colorbands import bands_from_dyes

        recipe = make_recipe("Sunset", hexes=("#b72026", "#f78d1e"))
        self.assertEqual(bands_from_dyes(recipe), ["red", "orange"])

    def test_two_dyes_in_one_band_collapse_to_one(self):
        from ..colorbands import bands_from_dyes

        recipe = make_recipe("Two Blues", hexes=("#0e2a5e", "#1e3277"))
        self.assertEqual(bands_from_dyes(recipe), ["blue"])

    def test_a_recipe_with_no_dyes_claims_nothing(self):
        """Not 'grey' — an unrecorded recipe is unknown, not colourless."""
        from ..colorbands import bands_from_dyes

        self.assertEqual(bands_from_dyes(make_recipe("Blank", hexes=())), [])

    def test_black_grounds_a_colourway_rather_than_claiming_it(self):
        """Black, grey and cream are working dyes, not colorways — they shade
        the colours beside them. Left in, they would have been the biggest
        section on the sheet without one scarf in them anybody calls grey."""
        from ..colorbands import bands_from_dyes

        recipe = make_recipe("Turquoise on Black", hexes=("#009fda", "#000000"))
        self.assertEqual(bands_from_dyes(recipe), ["blue"])

    def test_an_all_achromatic_recipe_still_claims_its_section(self):
        """Suppressing grey and black only makes sense when there is something
        to suppress them in favour of. A genuinely grey scarf keeps its
        section — and a black-and-slate one claims both, because the split is
        the point: someone holding a black scarf looks under black."""
        from ..colorbands import bands_from_dyes

        recipe = make_recipe("Charcoal", hexes=("#000000", "#708090"))
        self.assertEqual(bands_from_dyes(recipe), ["grey", "black"])

    def test_black_is_its_own_section_not_a_shade_of_grey(self):
        from ..colorbands import bands_from_dyes

        recipe = make_recipe("Jet", hexes=("#000000",))
        self.assertEqual(bands_from_dyes(recipe), ["black"])

    def test_a_minor_dye_still_gets_its_band(self):
        """Ratio says how much cloth a dye covers, not whether you can see it.
        Someone hunting for green will still spot the green stripe."""
        from ..colorbands import bands_from_dyes

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

        from ..colorbands import bands_from_image

        data = make_band_image(background=(30, 60, 160))
        self.assertEqual(bands_from_image(BytesIO(data)), ["blue"])

    def test_two_colours_yield_both_bands(self):
        from io import BytesIO

        from ..colorbands import bands_from_image

        data = make_band_image(
            patches=[((30, 60, 160), 0.5)], background=(20, 130, 70)
        )
        self.assertEqual(bands_from_image(BytesIO(data)), ["green", "blue"])

    def test_the_background_cannot_dilute_the_scarf(self):
        """The whole reason shares are measured against chromatic pixels only:
        posterboard, barcode card and granite are all neutral, so a scarf that
        fills a third of the frame still reports its colour at full strength."""
        from io import BytesIO

        from ..colorbands import bands_from_image

        data = make_band_image(
            patches=[((30, 60, 160), 0.3)], background=(210, 210, 210)
        )
        self.assertIn("blue", bands_from_image(BytesIO(data)))

    def test_a_genuinely_grey_scarf_reads_as_grey(self):
        from io import BytesIO

        from ..colorbands import bands_from_image

        data = make_band_image(background=(130, 130, 132))
        self.assertEqual(bands_from_image(BytesIO(data)), ["grey"])

    def test_a_genuinely_black_scarf_reads_as_black_not_grey(self):
        """The whole reason the band was split: these two used to come back
        with the same answer, and the black scarf was findable only under a
        heading nobody would look for it under."""
        from io import BytesIO

        from ..colorbands import bands_from_image

        data = make_band_image(background=(8, 8, 10))
        self.assertEqual(bands_from_image(BytesIO(data)), ["black"])

    def test_a_speck_of_colour_does_not_earn_a_band(self):
        """Regression: dividing by a tiny chromatic mass amplified sensor noise
        into confident bands, and a grey scarf came back claiming orange, blue
        and brown. A band has to cover real area, not just dominate the dregs."""
        from io import BytesIO

        from ..colorbands import bands_from_image

        data = make_band_image(
            patches=[((30, 60, 160), 0.01)], background=(130, 130, 132)
        )
        self.assertEqual(bands_from_image(BytesIO(data)), ["grey"])

    def test_a_mostly_grey_scarf_claims_grey_as_well_as_its_colour(self):
        """A muted colourway is both things at once, and which section it
        belongs in is a judgement — so both are offered and a person picks."""
        from io import BytesIO

        from ..colorbands import bands_from_image

        data = make_band_image(
            patches=[((30, 60, 160), 0.15)], background=(130, 130, 132)
        )
        self.assertEqual(bands_from_image(BytesIO(data)), ["blue", "grey"])

    def test_an_unreadable_file_leaves_the_row_unsuggested(self):
        from io import BytesIO

        from ..colorbands import bands_from_image

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

    def test_a_rainbow_claims_one_section_rather_than_all_of_them(self):
        """The section exists so four colorways don't print in all eight.

        Both answers without it are bad: claim every band and a rainbow turns
        up in the red section as the least useful answer to the question that
        section asks; claim none and it prints nowhere at all, which is what
        was actually happening.
        """
        from .. import colorbands

        spread = ["red", "orange", "yellow", "green", "blue"]
        self.assertEqual(colorbands.fold_rainbow(spread), ["rainbow"])
        # Neutrals fold in too — a rainbow with black in it is still what
        # somebody means by "rainbow".
        self.assertEqual(
            colorbands.fold_rainbow(spread + ["black"]), ["rainbow"]
        )

    def test_a_busy_colorway_is_not_a_rainbow(self):
        """Five sits in an empty corridor, the way the yellow/green boundary
        at 61 degrees does. The two widest confirmed colorways in stock reach
        four bands and are emphatically not rainbows — a line at four would
        have swallowed both."""
        from .. import colorbands

        forest_fire = ["red", "orange", "green", "brown"]
        mooney = ["green", "blue", "purple", "grey"]
        self.assertEqual(colorbands.fold_rainbow(forest_fire), forest_fire)
        self.assertEqual(colorbands.fold_rainbow(mooney), mooney)

    def test_no_single_colour_is_ever_classified_as_rainbow(self):
        """It is a property of a set, not of a colour. Anything that returned
        it from a hex would be putting a scarf in the rainbow section on the
        strength of one dye."""
        from .. import colorbands

        for _, _, color in colorbands.BANDS:
            self.assertNotEqual(colorbands.band_for_hex(color), colorbands.RAINBOW)
        self.assertNotIn(colorbands.RAINBOW, colorbands.CHROMATIC)

    def test_ticking_rainbow_and_a_colour_is_allowed(self):
        """The fold is a suggestion and lives in the classifier, the same way
        the neutral rule does. A warm rainbow that genuinely reads red is a
        judgement about that scarf, and a save that deleted the red tick would
        be the app deciding."""
        self.client.post(
            reverse("color_bands_save", args=[self.recipe.pk]),
            {"bands": ["red", "rainbow"]},
        )
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.color_bands, ["red", "rainbow"])

    def test_the_todo_filter_shows_only_unconfirmed_recipes(self):
        done = make_recipe("Already Done", hexes=("#1e3277",))
        done.bands_confirmed_at = timezone.now()
        done.save()

        response = self.client.get(reverse("color_classify"), {"todo": "true"})
        self.assertContains(response, "Sunset Silk")
        self.assertNotContains(response, "Already Done")

    def test_the_products_filter_hides_colorways_nothing_is_made_in(self):
        """The noise this removes. A colorway with no active product prints on
        no sheet and hangs on no peg, so confirming it changes nothing anybody
        can see today — worth doing eventually, not what you are looking at
        when you want the sheet to stop leaving scarves out."""
        make_product(self.recipe, "Sunset Infinity", with_image=False)
        make_recipe("Book Only", hexes=("#1e3277",))

        response = self.client.get(
            reverse("color_classify"), {"with_products": "true"}
        )
        self.assertContains(response, "Sunset Silk")
        self.assertNotContains(response, "Book Only")

    def test_a_colorway_on_several_blanks_is_listed_once(self):
        """A colorway is normally dyed onto several blanks, and the join would
        otherwise list it once per product."""
        make_product(self.recipe, "Sunset Infinity", with_image=False)
        make_product(self.recipe, "Sunset Sash", with_image=False)

        response = self.client.get(
            reverse("color_classify"), {"with_products": "true"}
        )
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(response.context["total_count"], 1)

    def test_the_two_filters_combine_and_each_pill_keeps_the_other(self):
        """The pair that matters is "has a product and isn't confirmed" — the
        colorways a customer can ask for that the sheet is leaving out. A pill
        that reset the other axis would make it unreachable in one place and
        unsendable as a link."""
        make_product(self.recipe, "Sunset Infinity", with_image=False)
        done = make_recipe("Done And Sold", hexes=("#1e3277",))
        make_product(done, "Done Infinity", with_image=False)
        done.bands_confirmed_at = timezone.now()
        done.save()
        make_recipe("Book Only", hexes=("#1e3277",))

        response = self.client.get(
            reverse("color_classify"), {"todo": "true", "with_products": "true"}
        )
        self.assertContains(response, "Sunset Silk")
        self.assertNotContains(response, "Done And Sold")
        self.assertNotContains(response, "Book Only")

        # Each pill toggles its own axis and carries the other.
        self.assertEqual(response.context["todo_href"], "%s?todo=true&with_products=true" % reverse("color_classify"))
        self.assertEqual(response.context["all_href"], "%s?with_products=true" % reverse("color_classify"))
        self.assertEqual(response.context["products_href"], "%s?todo=true" % reverse("color_classify"))

    def test_counts_follow_the_list_on_screen(self):
        """A pill reading "Unconfirmed 57" over a list of nine is the page
        contradicting itself, and the number people act on is the one beside
        the list they are looking at."""
        make_product(self.recipe, "Sunset Infinity", with_image=False)
        make_recipe("Book Only", hexes=("#1e3277",))

        response = self.client.get(reverse("color_classify"))
        self.assertEqual(response.context["total_count"], 2)

        response = self.client.get(
            reverse("color_classify"), {"with_products": "true"}
        )
        self.assertEqual(response.context["total_count"], 1)
        self.assertEqual(response.context["todo_count"], 1)
        # And the whole-catalogue figure stays available, because "what is
        # left on the ones you sell" is the point of the filter.
        self.assertEqual(response.context["with_products_count"], 1)

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
class EdgesMatchTheRuleTests(TestCase):
    """`HUE_EDGES` restates numbers that live as literals in `band_for_hsl`.

    Threading seven constants through that function would cost more clarity
    than it buys, so the list is pinned by probing the classifier either side
    of each line instead. That tests the real thing rather than restating it:
    if somebody moves a boundary in the rule and not in the list, the page
    starts drawing its line in the wrong place and this fails.
    """

    def test_every_listed_edge_is_a_real_edge(self):
        from ..colorbands import EDGE_PROBE, HUE_EDGES, band_for_hsl

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
        from ..colorbands import EDGE_PROBE, HUE_EDGES, band_for_hsl

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
        from ..colorbands import band_for_hex

        response = self.client.get(reverse("color_bands_page"))
        rows = {d["name"]: d for d in response.context["dyes_json"]}
        for name, row in rows.items():
            self.assertEqual(row["band"], band_for_hex(row["hex"]), name)
        self.assertEqual(rows["461 Avocado"]["band"], "green")

    def test_the_boundaries_come_from_the_code(self):
        """A page quoting 70 after somebody moved it to 61 would be worse than
        no page at all."""
        from ..colorbands import YELLOW_ENDS

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
