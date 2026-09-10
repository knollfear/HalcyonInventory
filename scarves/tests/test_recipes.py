"""The colorway list, the recipe page, the dye picker and the dye catalogue.

The reasoning behind these is in `docs/claude/recipes.md`.
"""
import json
import re
import tempfile
from datetime import date, datetime, time, timedelta
from io import StringIO
from unittest import mock
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
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
from ..forms import (
    CrewHandbookForm,
    HoursForm,
    LabelRunForm,
    PickedBathsField,
    ProductionSheetForm,
    QuickRecipeRowForm,
    RecipeDyesForm,
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
    make_bathable,
    make_product,
    make_recipe,
)


class RecipeEditTests(TestCase):
    """Edit mode on the recipe showcase — filling in the dye backlog."""

    def setUp(self):
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)
        # A recipe that already has dyes (the copy source) and one without.
        self.source = make_recipe("blueeyes-mid-navy", hexes=("#2b5fa8", "#1a2340"))
        self.target = make_recipe("Agean Sea", hexes=())
        make_product(self.target, "Half Circle Veil - Agean Sea")

    def test_every_row_offers_editing_with_no_mode_to_switch_into(self):
        """Editing used to be a mode behind `?edit=true`, which demanded a
        decision before the job: you came to look at a colorway, wanted to
        change a dye, and had to reload the page into a different version of
        itself to be allowed to."""
        response = self.client.get(reverse("recipe_showcase"))

        self.assertContains(
            response, 'class="btn editlink"', count=len(response.context["rows"])
        )

    def test_the_list_renders_no_pickers_until_a_row_is_opened(self):
        """The whole reason the page is quick. A DyeSelect carries the entire
        dye catalogue and each option carries the type-ahead's search text, so
        five of them is ~150 KB of markup — 162 rows of that was 27 MB and
        thirteen seconds of server render before anybody could type."""
        response = self.client.get(reverse("recipe_showcase"))

        self.assertNotContains(response, "dye-select")

    def test_one_row_opens_from_the_query_string(self):
        """`?row=` is the no-script door into the editor, and the same address
        htmx would have swapped into — so a link into a row is sendable."""
        response = self.client.get(
            reverse("recipe_showcase"), {"row": self.target.pk}
        )

        self.assertContains(response, "dye-select")
        open_rows = [r["recipe"].pk for r in response.context["rows"] if r["edit_mode"]]
        self.assertEqual(open_rows, [self.target.pk])

    def test_the_recipe_page_links_straight_to_its_own_row(self):
        """Not to the top of a list of a hundred and sixty two. `?row=` is the
        address the Edit button on the showcase builds, so both doors land in
        the same place."""
        response = self.client.get(reverse("recipe_detail", args=[self.target.pk]))

        self.assertContains(
            response, f'{reverse("recipe_showcase")}?row={self.target.pk}'
        )

    def test_the_row_endpoint_is_closed_unless_asked(self):
        """Closed is the safer default for what a dropped parameter does:
        lose it and you get the row as the page already reads, where an
        editor default would spring five pickers open on a row nobody asked
        to change."""
        closed = self.client.get(reverse("recipe_row", args=[self.target.pk]))
        self.assertNotContains(closed, "dye-select")
        self.assertContains(closed, "Edit dyes")

        opened = self.client.get(
            reverse("recipe_row", args=[self.target.pk]), {"edit": "1"}
        )
        self.assertContains(opened, "dye-select")

    def test_cancel_closes_the_row_and_writes_nothing(self):
        """Closing *is* the reset — nothing on the row was written, so a form
        thrown away leaves the recipe exactly as the closed row shows it."""
        before = [rd.dye_id for rd in self.source.recipe_dyes.all()]

        response = self.client.get(reverse("recipe_row", args=[self.source.pk]))

        self.assertNotContains(response, "dye-select")
        self.assertEqual([rd.dye_id for rd in self.source.recipe_dyes.all()], before)

    def test_nothing_offers_to_copy_another_colorway_s_dyes(self):
        """The copy-from picker is gone. It sat above the dye boxes on every
        open row and answered a question nobody was asking there — the row is
        for this colorway, and a second recipe named on it read as though it
        were part of the record."""
        for response in (
            self.client.get(reverse("recipe_showcase")),
            self.client.get(reverse("recipe_row", args=[self.target.pk]), {"edit": "1"}),
        ):
            self.assertNotContains(response, "Copy dyes from")
            self.assertNotContains(response, 'name="source"')

    def test_an_unreadable_row_id_opens_nothing_rather_than_erroring(self):
        """A filter is navigation: the worst a stale link should do is show
        the list it was a link into."""
        for bad in ("banana", "", "999999"):
            response = self.client.get(
                reverse("recipe_showcase"), {"row": bad}
            )
            self.assertEqual(response.status_code, 200, bad)
            self.assertFalse(
                any(r["edit_mode"] for r in response.context["rows"]), bad
            )

    def test_a_saved_row_comes_back_closed_and_showing_its_dyes(self):
        """Pickers that reappear identical are the weakest confirmation there
        is; the closed row shows the chips and swatches just recorded, which
        is a save you check by looking."""
        d1 = self.source.recipe_dyes.first().dye

        response = self.client.post(
            reverse("recipe_dyes_save", args=[self.target.pk]), {"dye1": d1.pk}
        )

        self.assertNotContains(response, "dye-select")
        self.assertContains(response, "Edit dyes")
        self.assertContains(response, d1.name)

    def test_missing_filter_shows_only_dyeless_recipes(self):
        response = self.client.get(
            reverse("recipe_showcase"), {"missing": "true"}
        )
        names = [row["recipe"].name for row in response.context["rows"]]
        self.assertIn("Agean Sea", names)
        self.assertNotIn("blueeyes-mid-navy", names)

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
        self.assertFalse(response.context["row"]["form"].is_valid())
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
            reverse("recipe_showcase"),
            reverse("recipe_row", args=[self.target.pk]) + "?edit=1",
        ]:
            self.assertEqual(self.client.get(url).status_code, 302, url)
        self.assertEqual(
            self.client.post(reverse("recipe_dyes_save", args=[self.target.pk])).status_code,
            302,
        )
class RecipeShowcaseTableTests(TestCase):
    """Which table at the stall — and what that does and does not narrow.

    Category means "which table", which is why the reference sheets print per
    category. Here it narrows *which colorways are listed* and never which
    products a listed colorway shows, because a colour dyed on a yarn and on a
    silk is one colour.
    """

    def setUp(self):
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)

        self.silk = RawProductCategory.objects.get_or_create(name="Silk")[0]
        self.yarn = RawProductCategory.objects.get_or_create(name="Yarn")[0]

        self.both = make_recipe("Cabernet")
        self.on_silk(self.both, "Infinity Scarf - Cabernet")
        self.on_yarn(self.both, "Artisan - Cabernet")

        self.silk_only = make_recipe("Agean Sea")
        self.on_silk(self.silk_only, "Half Circle Veil - Agean Sea")

        self.yarn_only = make_recipe("Ochre")
        self.on_yarn(self.yarn_only, "Noble - Ochre")

    def _product(self, recipe, name, category):
        raw, _ = RawProduct.objects.get_or_create(
            name=f"raw-{name}", category=category, defaults={"price": "5.00"}
        )
        return FinishedProduct.objects.create(
            name=name, raw_product=raw, recipe=recipe, price="30.00"
        )

    def on_silk(self, recipe, name):
        return self._product(recipe, name, self.silk)

    def on_yarn(self, recipe, name):
        return self._product(recipe, name, self.yarn)

    def _listed(self, **params):
        response = self.client.get(reverse("recipe_showcase"), params)
        return [row["recipe"].name for row in response.context["rows"]]

    def test_a_table_lists_only_colorways_on_it(self):
        self.assertEqual(sorted(self._listed(category="Yarn")),
                         ["Cabernet", "Ochre"])
        self.assertEqual(sorted(self._listed(category="Silk")),
                         ["Agean Sea", "Cabernet"])

    def test_a_colorway_on_both_tables_is_on_both_lists(self):
        """One colour, two tables. Dropping it from either would leave a
        shortage of exactly the kind that is visible from nowhere."""
        self.assertIn("Cabernet", self._listed(category="Yarn"))
        self.assertIn("Cabernet", self._listed(category="Silk"))

    def test_it_is_listed_once_however_many_products_match(self):
        """A colorway on three yarns joins three rows; without distinct() the
        page prints it three times and each one is separately editable."""
        self.on_yarn(self.both, "Heavenly - Cabernet")
        self.on_yarn(self.both, "Homespun - Cabernet")

        self.assertEqual(self._listed(category="Yarn").count("Cabernet"), 1)

    def test_the_row_still_shows_the_whole_colorway(self):
        """Narrowing the list must not narrow the row. The same recipe name
        carrying different products depending on how you arrived, with nothing
        on the row to say so, is worse than no filter at all."""
        response = self.client.get(reverse("recipe_showcase"), {"category": "Yarn"})

        self.assertContains(response, "Artisan - Cabernet")
        self.assertContains(response, "Infinity Scarf - Cabernet")

    def test_an_unknown_table_is_no_filter_rather_than_an_error(self):
        response = self.client.get(reverse("recipe_showcase"), {"category": "Wax"})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["category"])
        self.assertEqual(len(response.context["rows"]), 3)

    def test_the_pills_are_derived_from_the_rows(self):
        """Not a list of names: a shop that grows a third table gets a third
        pill with nothing to change."""
        names = [c.name for c in self.client.get(
            reverse("recipe_showcase")).context["categories"]]

        self.assertEqual(names, ["Silk", "Yarn"])

    def test_a_category_with_nothing_active_draws_no_pill(self):
        RawProductCategory.objects.get_or_create(name="Wax")

        names = [c.name for c in self.client.get(
            reverse("recipe_showcase")).context["categories"]]

        self.assertNotIn("Wax", names)

    def test_counts_are_scoped_to_what_is_on_screen(self):
        """A count over the whole catalogue printed above a filtered list is
        the page contradicting itself — the same rule the colour page's pills
        and the close's banner follow."""
        dyeless = make_recipe("Stormy", hexes=())
        self.on_yarn(dyeless, "Artisan - Stormy")

        response = self.client.get(reverse("recipe_showcase"), {"category": "Yarn"})

        self.assertEqual(response.context["total_count"], 3)      # Cabernet, Ochre, Stormy
        self.assertEqual(response.context["missing_count"], 1)    # Stormy
        self.assertEqual(response.context["total_count"],
                         len(response.context["rows"]))

    def test_every_control_carries_the_table(self):
        """Switching to edit mode, or opening a row, must not silently drop
        you back to every table — the same rule the oven tick follows on the
        production picker, where each carrier that forgets is a silent one."""
        response = self.client.get(
            reverse("recipe_showcase"), {"category": "Yarn"}
        )

        self.assertIn("category=Yarn", response.context["url_missing"])
        self.assertIn("category=Yarn", response.context["url_all_recipes"])
        for row in response.context["rows"]:
            self.assertIn("category=Yarn", row["edit_row_url"], row["recipe"].name)

    def test_the_table_pills_carry_the_mode(self):
        response = self.client.get(
            reverse("recipe_showcase"), {"missing": "true"}
        )

        for link in response.context["category_links"]:
            self.assertIn("missing=true", link["url"], link["name"])

    def test_the_all_pill_drops_the_table_and_keeps_everything_else(self):
        response = self.client.get(
            reverse("recipe_showcase"),
            {"missing": "true", "category": "Yarn"},
        )

        every = response.context["url_every_table"]
        self.assertNotIn("category=", every)
        self.assertIn("missing=true", every)
class RecipeRowBandTests(TestCase):
    """The rainbow chips, brought onto the row that edits the dyes.

    The colorway is in front of you and its dyes are in the boxes above, which
    is the moment somebody can answer which sections of the sheet it prints
    in. What has to survive the move is the rule the whole classifier exists
    for: never print an unconfirmed guess.
    """

    def setUp(self):
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)
        self.recipe = make_recipe("Cabernet", hexes=("#8c1c2f",))
        make_product(self.recipe, "Infinity Scarf - Cabernet", with_image=False)

    def _post(self, **extra):
        data = {f"dye{i}": "" for i in range(1, 6)}
        data.update(extra)
        return self.client.post(
            reverse("recipe_dyes_save", args=[self.recipe.pk]), data
        )

    def _chips(self):
        response = self.client.get(
            reverse("recipe_showcase"), {"row": self.recipe.pk}
        )
        return {c["slug"]: c for c in response.context["rows"][0]["chips"]}

    def test_the_dye_reading_is_offered_unticked(self):
        """This is the difference from private/colors/, and it is the whole
        safety argument. There Confirm is the only button, so a pre-ticked
        suggestion is answering the page's one question. Here Save is about
        the dyes, so a pre-ticked guess would be confirmed by a click aimed at
        something else — and a wrong band is silent."""
        chips = self._chips()

        self.assertTrue(chips["red"]["guessed"])
        self.assertFalse(chips["red"]["on"])

    def test_saving_dyes_without_ticking_leaves_the_bands_unconfirmed(self):
        """Confirmed-with-no-bands prints the colorway in no section at all,
        and it is a state this app has been in before, arrived at by giving
        up. An empty answer here is indistinguishable from nobody looking."""
        self._post()

        self.recipe.refresh_from_db()
        self.assertIsNone(self.recipe.bands_confirmed_at)
        self.assertFalse(self.recipe.color_bands)

    def test_ticking_a_chip_stores_and_confirms_it(self):
        self._post(bands=["red", "pink"])

        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.color_bands, ["red", "pink"])
        self.assertIsNotNone(self.recipe.bands_confirmed_at)

    def test_a_band_that_is_not_a_band_is_dropped(self):
        self._post(bands=["red", "banana"])

        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.color_bands, ["red"])

    def test_bands_come_back_in_rainbow_order(self):
        self._post(bands=["blue", "red"])

        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.color_bands,
                         colorbands.sort_bands(["blue", "red"]))

    def test_a_confirmed_colorway_is_not_quietly_unconfirmed_by_a_dye_save(self):
        """Idempotent: the chips render ticked, so they post back ticked."""
        self._post(bands=["red"])
        stamped = Recipe.objects.get(pk=self.recipe.pk).bands_confirmed_at

        self._post(bands=["red"])

        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.color_bands, ["red"])
        self.assertIsNotNone(self.recipe.bands_confirmed_at)
        self.assertGreaterEqual(self.recipe.bands_confirmed_at, stamped)

    def test_a_confirmed_colorway_gets_no_suggestion(self):
        """Nothing to suggest at somebody who has already ruled."""
        self._post(bands=["red"])

        chips = self._chips()

        self.assertTrue(chips["red"]["on"])
        self.assertFalse(any(c["guessed"] for c in chips.values()))

    def test_a_confirmed_colorway_can_be_cleared_back_to_no_bands(self):
        """Once somebody has ruled, unticking everything is a ruling too —
        the deliberate 'this belongs in no section' answer, kept reachable."""
        self._post(bands=["red"])

        self._post()

        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.color_bands, [])
        self.assertIsNotNone(self.recipe.bands_confirmed_at)

    def test_the_band_chips_are_not_the_dyes_over_again(self):
        """A recipe's dyes are not its colour — they are not blended, and a
        band is a judgement about the scarf. The two share a column, so the
        band group carrying dye hexes would invite exactly that reading."""
        hex_on_file = self.recipe.recipe_dyes.first().dye.hex_color
        Recipe.objects.filter(pk=self.recipe.pk).update(
            color_bands=["red"], bands_confirmed_at=timezone.now()
        )

        html = self.client.get(reverse("recipe_showcase")).content.decode()
        colors_cell = html.split('<div class="bandsummary">')[1].split("</div>")[0]

        self.assertIn("bandtag", colors_cell)
        self.assertNotIn(hex_on_file, colors_cell)

    def test_the_dye_colour_rides_inside_its_own_pill(self):
        """A swatch beside a name is two things to line up by eye, and they
        used to sit in different table cells — so a five-dye recipe asked
        somebody to count across a gap to find out which colour was which."""
        hex_on_file = self.recipe.recipe_dyes.first().dye.hex_color

        html = self.client.get(reverse("recipe_showcase")).content.decode()
        dyes_cell = html.split('<div class="dyes">')[1].split("</div>")[0]

        self.assertIn('class="chip"', dyes_cell)
        self.assertIn(hex_on_file, dyes_cell)
        self.assertNotIn('class="swatches"', html)

    def test_a_dye_with_no_colour_on_file_gets_hatching_never_a_colour(self):
        """A placeholder swatch is a guess somebody then reads off the screen
        as fact — the same reason colorbands prints no band rather than its
        best one."""
        Dye.objects.filter(
            pk=self.recipe.recipe_dyes.first().dye_id
        ).update(hex_color="")

        response = self.client.get(reverse("recipe_showcase"))

        self.assertContains(response, 'class="dyedot unknown"')

    def test_a_closed_row_draws_only_confirmed_bands(self):
        """A dot nobody agreed to is indistinguishable from one somebody did —
        the same reason the reference sheet skips an unconfirmed colorway."""
        response = self.client.get(reverse("recipe_showcase"))
        row = response.context["rows"][0]
        self.assertEqual(row["band_dots"], [])
        self.assertContains(response, "bands unconfirmed")

        self._post(bands=["red"])

        response = self.client.get(reverse("recipe_showcase"))
        row = response.context["rows"][0]
        self.assertEqual([d["slug"] for d in row["band_dots"]], ["red"])
        self.assertNotContains(response, "bands unconfirmed")

    def test_nothing_classifies_on_a_page_that_only_lists(self):
        """The classifier never writes. It fills the form in; a person
        decides — and simply looking at the list is not deciding."""
        self.client.get(reverse("recipe_showcase"))
        self.client.get(reverse("recipe_showcase"), {"row": self.recipe.pk})

        self.recipe.refresh_from_db()
        self.assertIsNone(self.recipe.bands_confirmed_at)
        self.assertFalse(self.recipe.color_bands)
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

    def test_the_form_records_now_and_moves_stock_full_stop(self):
        """One button, one meaning.

        It used to carry an optional back-date folded into a disclosure
        *below* the submit — so the same button either moved stock or didn't,
        and the switch deciding which was under it and closed by default.
        Typing up old sessions has its own door at `private/cards/`, which
        cannot move current stock at all.
        """
        base = self._base("Heavenly - Angel", per_bath=5, on_hand=40)
        product = self._product(base, "Heavenly - Angel - Sage", on_hand=2)

        self.client.post(self.url, {f"baths_{product.pk}": "2"})

        product.refresh_from_db()
        base.refresh_from_db()
        self.assertEqual(product.number_on_hand, 12)
        self.assertEqual(base.number_on_hand, 30)

    def test_a_date_posted_by_hand_changes_nothing(self):
        """The field is gone from the form, so nothing should still be
        reading it — a leftover parser is how a removed option comes back
        through a hand-built POST."""
        base = self._base("Heavenly - Angel", per_bath=5, on_hand=40)
        product = self._product(base, "Heavenly - Angel - Sage", on_hand=2)

        self.client.post(self.url, {
            f"baths_{product.pk}": "2",
            "dyed_on": "2024-06-15",
        })

        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 12)
        log = InventoryLog.objects.get()
        self.assertEqual(
            timezone.localtime(log.created_at).date(), timezone.localdate()
        )
        self.assertNotIn("back-dated", log.notes)

    def test_the_page_sends_old_sessions_to_the_cards(self):
        """Removing the option without saying where it went leaves somebody
        looking for it on a page that no longer has it."""
        base = self._base("Heavenly - Angel")
        self._product(base, "Heavenly - Angel - Sage")

        response = self.client.get(reverse("recipe_detail", args=[self.recipe.pk]))

        self.assertNotContains(response, 'name="dyed_on"')
        self.assertContains(response, reverse("card_backfill_index"))

    def test_the_history_filters_to_one_finished_product(self):
        """There is no per-finished-product page anywhere in this app, so a
        colorway on several blanks otherwise gives one interleaved column and
        "what has this one done" has no answer."""
        base_a = self._base("Heavenly - Angel", per_bath=5)
        base_b = self._base("Homespun - Angel", per_bath=5)
        sage = self._product(base_a, "Heavenly - Angel - Sage")
        moss = self._product(base_b, "Homespun - Angel - Moss")
        self.client.post(self.url, {
            f"baths_{sage.pk}": "1", f"baths_{moss.pk}": "1",
        })

        page = self.client.get(
            reverse("recipe_detail", args=[self.recipe.pk]), {"product": sage.pk}
        )

        names = [log.finished_product_id for log in page.context["logs"]]
        self.assertEqual(set(names), {sage.pk})

    def test_the_figures_follow_the_filter(self):
        """A total that disagrees with the list under it is the page
        contradicting itself — the rule the colour page's pills follow."""
        base_a = self._base("Heavenly - Angel", per_bath=5)
        base_b = self._base("Homespun - Angel", per_bath=5)
        sage = self._product(base_a, "Heavenly - Angel - Sage")
        moss = self._product(base_b, "Homespun - Angel - Moss")
        self.client.post(self.url, {
            f"baths_{sage.pk}": "1", f"baths_{moss.pk}": "2",
        })

        whole = self.client.get(reverse("recipe_detail", args=[self.recipe.pk]))
        one = self.client.get(
            reverse("recipe_detail", args=[self.recipe.pk]), {"product": sage.pk}
        )

        self.assertEqual(whole.context["produced"], 15)
        self.assertEqual(one.context["produced"], 5)
        self.assertEqual(one.context["scope_count"], 1)

    def test_each_chip_counts_what_it_will_show(self):
        """A chip promising 42 that lands on a list of nine is the same
        contradiction one level up."""
        base_a = self._base("Heavenly - Angel", per_bath=5)
        base_b = self._base("Homespun - Angel", per_bath=5)
        sage = self._product(base_a, "Heavenly - Angel - Sage")
        moss = self._product(base_b, "Homespun - Angel - Moss")
        self.client.post(self.url, {f"baths_{sage.pk}": "1"})

        chips = {
            c["product"].pk: c["count"]
            for c in self.client.get(
                reverse("recipe_detail", args=[self.recipe.pk])
            ).context["chips"]
        }

        self.assertEqual(chips[sage.pk], 1)
        self.assertEqual(chips[moss.pk], 0)

    def test_no_chips_when_there_is_only_one_product(self):
        """A filter offering one choice is a row of furniture."""
        base = self._base("Heavenly - Angel")
        self._product(base, "Heavenly - Angel - Sage")

        html = self.client.get(
            reverse("recipe_detail", args=[self.recipe.pk])
        ).content.decode()

        self.assertNotIn('class="chips"', html)

    def test_a_stale_or_junk_product_id_falls_back_to_the_whole_colorway(self):
        """A filter is navigation, and the worst a dead link should do is show
        more than was asked for."""
        base = self._base("Heavenly - Angel", per_bath=5)
        product = self._product(base, "Heavenly - Angel - Sage")
        self.client.post(self.url, {f"baths_{product.pk}": "1"})

        for bad in ("999999", "sage", ""):
            with self.subTest(bad=bad):
                page = self.client.get(
                    reverse("recipe_detail", args=[self.recipe.pk]),
                    {"product": bad},
                )
                self.assertEqual(page.status_code, 200)
                self.assertIsNone(page.context["focus"])
                self.assertEqual(len(page.context["logs"]), 1)

    def test_an_empty_filtered_history_says_which_silence_it_is(self):
        """"Nothing here" and "nothing anywhere on this colorway" are
        different facts, and only one of them has somewhere else to look."""
        base_a = self._base("Heavenly - Angel", per_bath=5)
        base_b = self._base("Homespun - Angel", per_bath=5)
        sage = self._product(base_a, "Heavenly - Angel - Sage")
        moss = self._product(base_b, "Homespun - Angel - Moss")
        self.client.post(self.url, {f"baths_{sage.pk}": "1"})

        page = self.client.get(
            reverse("recipe_detail", args=[self.recipe.pk]), {"product": moss.pk}
        )

        self.assertContains(page, "Nothing recorded for")
        self.assertContains(page, "The rest of the colorway")

    def test_a_chip_click_swaps_only_the_history(self):
        """The chips are under a long page, so following a link meant landing
        back at the top and scrolling down again for every filter."""
        base_a = self._base("Heavenly - Angel", per_bath=5)
        base_b = self._base("Homespun - Angel", per_bath=5)
        sage = self._product(base_a, "Heavenly - Angel - Sage")
        moss = self._product(base_b, "Homespun - Angel - Moss")
        self.client.post(self.url, {
            f"baths_{sage.pk}": "1", f"baths_{moss.pk}": "1",
        })

        response = self.client.get(
            reverse("recipe_history", args=[self.recipe.pk]), {"product": sage.pk}
        )

        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="chips"', html)
        # A fragment, not a page.
        self.assertNotIn("<!doctype", html.lower())
        self.assertNotIn("Record production", html)

    def test_the_figures_ride_out_of_band_with_it(self):
        """They are above the fold and follow the same filter, so leaving
        them behind puts a colorway-wide total over a one-product list —
        invisible until somebody scrolls up."""
        base_a = self._base("Heavenly - Angel", per_bath=5)
        base_b = self._base("Homespun - Angel", per_bath=5)
        sage = self._product(base_a, "Heavenly - Angel - Sage")
        moss = self._product(base_b, "Homespun - Angel - Moss")
        self.client.post(self.url, {
            f"baths_{sage.pk}": "1", f"baths_{moss.pk}": "2",
        })

        html = self.client.get(
            reverse("recipe_history", args=[self.recipe.pk]), {"product": sage.pk}
        ).content.decode()

        self.assertIn('id="figures"', html)
        self.assertIn('id="focus-note"', html)
        self.assertEqual(html.count('hx-swap-oob="true"'), 2)
        self.assertIn("Heavenly - Angel - Sage", html)

    def test_the_chip_pushes_the_page_url_not_the_fragment(self):
        """Pushing the fragment's address would put a URL in the bar that
        renders a bare table on reload."""
        base_a = self._base("Heavenly - Angel")
        base_b = self._base("Homespun - Angel")
        sage = self._product(base_a, "Heavenly - Angel - Sage")
        self._product(base_b, "Homespun - Angel - Moss")

        html = self.client.get(
            reverse("recipe_detail", args=[self.recipe.pk])
        ).content.decode()

        page = reverse("recipe_detail", args=[self.recipe.pk])
        frag = reverse("recipe_history", args=[self.recipe.pk])
        self.assertIn(f'hx-get="{frag}?product={sage.pk}"', html)
        self.assertIn(f'hx-push-url="{page}?product={sage.pk}"', html)

    def test_the_chips_are_still_links_without_htmx(self):
        base_a = self._base("Heavenly - Angel")
        base_b = self._base("Homespun - Angel")
        sage = self._product(base_a, "Heavenly - Angel - Sage")
        self._product(base_b, "Homespun - Angel - Moss")

        html = self.client.get(
            reverse("recipe_detail", args=[self.recipe.pk])
        ).content.decode()

        page = reverse("recipe_detail", args=[self.recipe.pk])
        self.assertIn(f'href="{page}?product={sage.pk}"', html)

    def test_the_page_and_the_fragment_agree_about_a_filter(self):
        """One context builder, because two would drift — and the drift shows
        as a swapped-in view disagreeing with the one a refresh produces."""
        base_a = self._base("Heavenly - Angel", per_bath=5)
        base_b = self._base("Homespun - Angel", per_bath=5)
        sage = self._product(base_a, "Heavenly - Angel - Sage")
        self._product(base_b, "Homespun - Angel - Moss")
        self.client.post(self.url, {f"baths_{sage.pk}": "3"})

        page = self.client.get(
            reverse("recipe_detail", args=[self.recipe.pk]), {"product": sage.pk}
        )
        fragment = self.client.get(
            reverse("recipe_history", args=[self.recipe.pk]), {"product": sage.pk}
        )

        for key in ("produced", "sold", "scope_count", "all_count", "focus"):
            self.assertEqual(
                page.context[key], fragment.context[key], key
            )

    def test_the_fragment_is_staff_only(self):
        self.client.logout()
        response = self.client.get(
            reverse("recipe_history", args=[self.recipe.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

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
class RecipeRowActionTests(TestCase):
    """The bulk editing list's two row actions: flag the oven, retire a colour.

    Both live on an open row of `private/recipes/` because that is the one pass
    somebody makes down the whole catalogue — the person filling in a
    colorway's dyes is the person who knows which box it is made in and
    whether anybody still dyes it.
    """

    def setUp(self):
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)
        self.recipe = make_recipe("Cabernet")
        self.product = make_bathable(self.recipe, "Cabernet Wool", on_hand=0,
                                     par=8, bath=4)

    def _dye_post(self, **extra):
        """What the row's Save submits — every slot, whether or not it is set."""
        data = {f"dye{i}": "" for i in range(1, 6)}
        data.update(extra)
        return data

    # --- the oven flag ----------------------------------------------------

    def test_the_row_saves_the_oven_flag(self):
        self.client.post(
            reverse("recipe_dyes_save", args=[self.recipe.pk]),
            self._dye_post(oven_dyed="on"),
        )

        self.recipe.refresh_from_db()
        self.assertTrue(self.recipe.oven_dyed)

    def test_saving_a_flagged_row_does_not_silently_unflag_it(self):
        """The trap in a checkbox: unticked posts nothing, so a row rendered
        without its current value would clear the flag on the next Save of
        anything else on that row."""
        Recipe.objects.filter(pk=self.recipe.pk).update(oven_dyed=True)

        response = self.client.get(
            reverse("recipe_showcase"), {"row": self.recipe.pk}
        )

        form = response.context["rows"][0]["form"]
        self.assertTrue(form.initial["oven_dyed"])

    def test_the_editor_renders_exactly_one_oven_checkbox(self):
        """`{% for field in form %}` renders every field, and `oven_dyed` is a
        declared attribute while the dye slots are added in `__init__` — so
        Django ordered it first and the row came out with a stray checkbox in
        front of the dye boxes. Two inputs sharing one name is worse than
        untidy: unticking the visible one while the stray stays ticked still
        posts `on`."""
        html = self.client.get(
            reverse("recipe_row", args=[self.recipe.pk]), {"edit": "1"}
        ).content.decode()

        self.assertEqual(html.count('name="oven_dyed"'), 1)

    def test_the_box_can_be_unticked(self):
        Recipe.objects.filter(pk=self.recipe.pk).update(oven_dyed=True)

        self.client.post(
            reverse("recipe_dyes_save", args=[self.recipe.pk]),
            self._dye_post(),
        )

        self.recipe.refresh_from_db()
        self.assertFalse(self.recipe.oven_dyed)

    def test_retiring_deactivates_and_never_deletes(self):
        """History points at this row — inventory logs, production rows,
        resolved sales — and all of it stays readable."""
        self.client.post(reverse("recipe_retire", args=[self.recipe.pk]))

        self.recipe.refresh_from_db()
        self.assertFalse(self.recipe.is_active)
        self.assertTrue(Recipe.objects.filter(pk=self.recipe.pk).exists())

    def test_the_row_collapses_to_a_strip_rather_than_vanishing(self):
        """A row that disappeared is indistinguishable from a click that never
        arrived, which on a list this long is the mistake made twice."""
        response = self.client.post(reverse("recipe_retire", args=[self.recipe.pk]))

        self.assertContains(response, f'id="recipe-row-{self.recipe.pk}"')
        self.assertContains(response, "retired")
        self.assertContains(response, "Undo")

    def test_undo_puts_it_back_and_returns_the_whole_row(self):
        self.client.post(reverse("recipe_retire", args=[self.recipe.pk]))

        response = self.client.post(reverse("recipe_restore", args=[self.recipe.pk]))

        self.recipe.refresh_from_db()
        self.assertTrue(self.recipe.is_active)
        # The whole row, closed — the same shape every other row on the page
        # is in, with its way back into the editor on it. Not the editor
        # itself: undoing a retire says nothing about wanting to edit dyes.
        self.assertContains(response, f'id="recipe-row-{self.recipe.pk}"')
        self.assertContains(response, "Edit dyes")

    def test_a_retired_colorway_stops_being_planned(self):
        """*Retire, don't delete* promises retirement takes something out of
        production planning. That held for a retired product and not for a
        retired recipe — its finished products stay active, so the colorway
        kept being asked for with nothing to say why, and the dye room gets
        sent to make a colour somebody decided to stop making."""
        self.assertIn("Cabernet",
                      {b.recipe_name for b in production.plan_baths(50)})

        self.client.post(reverse("recipe_retire", args=[self.recipe.pk]))

        self.assertNotIn("Cabernet",
                         {b.recipe_name for b in production.plan_baths(50)})

    def test_and_drops_off_the_production_needed_page(self):
        """The two have to agree — one ranks what the other lists."""
        self.client.post(reverse("recipe_retire", args=[self.recipe.pk]))

        response = self.client.get(reverse("production_needed"))

        self.assertNotIn(
            "Cabernet",
            {g["recipe_name"] for g in response.context["groups"]},
        )

    def test_retiring_needs_a_post(self):
        response = self.client.get(reverse("recipe_retire", args=[self.recipe.pk]))

        self.assertEqual(response.status_code, 405)
        self.recipe.refresh_from_db()
        self.assertTrue(self.recipe.is_active)
class AnHtmxEditSendsAFragmentTests(TestCase):
    """A swap is a few lines of HTML, not a page the browser throws away.

    The first version used `hx-select` against the full page, which renders
    everything — the site chrome, both start panels, the whole shell — and
    discards all but the table. That is a page load wearing a swap's clothes.
    The endpoint now returns the fragment itself.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.product = make_bathable(
            make_recipe("Stormy Sea"), "Stormy Silk", on_hand=0, par=20, bath=4
        )
        self.url = reverse("production_sheet_index")
        self.params = {"items": [f"{self.product.pk}:2"]}

    def _get(self, **extra):
        params = dict(self.params)
        params.update(extra)
        return (
            self.client.get(self.url, params),
            self.client.get(self.url, params, HTTP_HX_REQUEST="true"),
        )

    def test_the_fragment_is_much_smaller_than_the_page(self):
        page, fragment = self._get()

        self.assertLess(len(fragment.content), len(page.content) / 2)

    def test_the_fragment_carries_no_page_shell(self):
        _page, fragment = self._get()
        body = fragment.content.decode()

        self.assertNotIn("<!doctype", body.lower())
        self.assertNotIn("Suggest dye baths to me", body)
        self.assertNotIn("I know what to dye", body)

    def test_the_fragment_is_the_swap_target(self):
        """`hx-swap="outerHTML"` on `#sheet` needs the response to be that
        element, not something to fish it out of."""
        _page, fragment = self._get()

        self.assertTrue(fragment.content.decode().lstrip().startswith('<div id="sheet"'))

    def test_the_page_and_the_fragment_agree(self):
        """One renderer: the page includes the same partial, so a swapped
        view cannot disagree with a refreshed one.

        Compared with the CSRF token blanked, since that is per-response and
        is the only thing that legitimately differs between the two.
        """
        page, fragment = self._get()
        strip = lambda body: re.sub(
            r'name="csrfmiddlewaretoken" value="[^"]*"', "", body
        )

        self.assertIn(
            strip(fragment.content.decode()).strip(),
            strip(page.content.decode()),
        )

    def test_an_added_row_comes_back_in_the_fragment(self):
        other = make_bathable(
            make_recipe("Ember"), "Ember Silk", on_hand=40, par=8, bath=5
        )

        _page, fragment = self._get(add=str(other.pk))

        self.assertContains(fragment, "Ember")
        self.assertContains(fragment, "3 baths")

    def test_the_page_still_answers_a_plain_request(self):
        """The fallback is the whole point of the pair — with the script
        blocked every one of these is an ordinary GET."""
        page, _fragment = self._get()

        self.assertContains(page, "Suggest dye baths to me")
        self.assertContains(page, 'id="sheet-list"')
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
