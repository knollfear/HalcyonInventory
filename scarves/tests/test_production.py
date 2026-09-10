"""Planning a session: what lands on the sheet, the oven, and what comes back.

The reasoning behind these is in `docs/claude/production.md`.
"""
import re
from datetime import date, datetime, time, timedelta
from django.contrib.auth.models import User
from django.utils import timezone
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from .. import (
    closing, colorbands, crew, fancy, nav, photowalk, production, restock,
    sales, seasonreport, seasons, sheetscan, skus, slowsellers, timesheets,
    weather,
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
    _pdf_text,
    make_bathable,
    make_employee,
    make_recipe,
)


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
class BathsAlreadyInFlightTests(TestCase):
    """A sheet must not re-ask for baths another sheet is already out getting.

    This is the bug the whole "production is not momentary" change exists to
    fix. Dyeing runs one to three days, so the stock a sheet asked for has not
    arrived while that sheet is being worked — the colorway still reads as
    short, and the next sheet asks for it again. In season that is a session
    spent dyeing something twice.
    """

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_bathable(
            self.recipe, "Stormy Silk", on_hand=0, par=8, bath=4
        )

    def _print(self, count=10):
        run = ProductionRun.objects.create()
        ProductionRunRow.objects.bulk_create([
            ProductionRunRow(
                run=run, finished_product=bath.product,
                order=i, quantity=bath.quantity,
            )
            for i, bath in enumerate(production.plan_baths(count), start=1)
        ])
        return run

    def test_a_second_sheet_does_not_repeat_the_first(self):
        self._print()

        self.assertEqual(production.plan_baths(10), [])

    def test_it_asks_only_for_what_the_first_sheet_left_short(self):
        """One bath printed against a shortage of two leaves one to ask for."""
        run = ProductionRun.objects.create()
        ProductionRunRow.objects.create(
            run=run, finished_product=self.product, order=1, quantity=4
        )

        self.assertEqual(len(production.plan_baths(10)), 1)

    def test_an_accepted_bath_is_counted_once_not_twice(self):
        """Once it lands in `number_on_hand`, counting it as in flight too
        would subtract the same bath from the plan twice."""
        run = self._print()
        for row in run.rows.all():
            production.apply_row(row)

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 8)
        self.assertEqual(production.plan_baths(10), [])

    def test_a_cancelled_bath_is_asked_for_again(self):
        """It is never coming, so the colorway is short again — which is the
        whole difference between cancelling and binning a lot."""
        run = self._print()
        for row in run.rows.all():
            production.cancel_row(row)

        self.assertEqual(len(production.plan_baths(10)), 2)

    def test_an_overdue_sheet_releases_its_claim(self):
        """Paper that has been lost must not suppress a colorway forever."""
        run = self._print()
        ProductionRun.objects.filter(pk=run.pk).update(
            created_at=timezone.now() - production.OVERDUE_AFTER - timedelta(days=1)
        )

        self.assertEqual(len(production.plan_baths(10)), 2)

    def test_a_binned_bath_is_not_asked_for_again(self):
        """The blanks are gone, so re-dyeing it needs blanks that no longer
        exist — and the shortage it leaves is answered by the ordinary par
        check on the next sheet, not by this one's claim."""
        run = self._print()
        rows = list(run.rows.all())
        production.apply_row(rows[0], yielded=0)

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 0)
        # The other row is still pending, so it still claims its bath.
        self.assertEqual(len(production.plan_baths(10)), 1)
class OneAnswerToWhatIsShortTests(TestCase):
    """`private/production-needed/` and the sheet answer one question.

    They did not. The page ran its own SQL — `par - number_on_hand`,
    aggregated per recipe — which knew nothing about paper already printed, so
    a sheet covering a colorway's whole shortage left the page still reporting
    it in full. The planner was right and the page a person reads was wrong.

    Same failure this codebase keeps naming: two answers to one question, and
    the disagreement is silent because each side looks perfectly sensible on
    its own.
    """

    def setUp(self):
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)
        self.recipe = make_recipe("Cabernet")
        self.product = make_bathable(self.recipe, "Cabernet Wool",
                                     on_hand=0, par=8, bath=4)

    def _shortage(self):
        response = self.client.get(reverse("production_needed"))
        for group in response.context["groups"]:
            if group["recipe_name"] == "Cabernet":
                return group["total_shortage"]
        return None

    def test_printing_a_sheet_moves_the_shortage(self):
        self.assertEqual(self._shortage(), 8)

        self.client.post(reverse("production_sheet_index"), {"baths": 20})

        # Two baths of four cover the whole shortage, so there is nothing left
        # to ask for — and the page has to say so, because it is what somebody
        # reads before deciding to print another one.
        self.assertIsNone(self._shortage())

    def test_a_partly_covered_colorway_reports_what_is_left(self):
        self.client.post(
            reverse("production_sheet_index"),
            {"items": f"{self.product.pk}:1"},
        )

        self.assertEqual(self._shortage(), 4)

    def test_the_page_says_what_is_already_on_paper(self):
        """Silently subtracting is the same confusion pointing the other way:
        a colorway that went quiet reads as 'nothing needed'."""
        self.client.post(
            reverse("production_sheet_index"),
            {"items": f"{self.product.pk}:1"},
        )

        response = self.client.get(reverse("production_needed"))

        self.assertEqual(response.context["in_flight_total"], 4)
        # Substrings that don't span the template's own line wrapping.
        self.assertContains(response, "on a printed sheet")
        self.assertContains(response, "taken off the shortages below")
        self.assertContains(response, "4 on a sheet")

    def test_the_two_pages_agree_on_what_is_left(self):
        """The property that matters: read the list, ask the picker for that
        many baths, get the ones you were looking at."""
        self.client.post(
            reverse("production_sheet_index"),
            {"items": f"{self.product.pk}:1"},
        )

        page = {
            product.pk
            for group in self.client.get(reverse("production_needed")).context["groups"]
            for product in group["items"]
        }
        planner = {bath.product.pk for bath in production.plan_baths(20)}

        self.assertEqual(page, planner)

    def test_a_cancelled_bath_comes_back_onto_the_page(self):
        """Cancelling hands the claim back, and the page has to hear about it
        for the same reason it had to hear about the print."""
        self.client.post(reverse("production_sheet_index"), {"baths": 20})
        run = ProductionRun.objects.get()
        self.assertIsNone(self._shortage())

        for row in run.rows.all():
            production.cancel_row(row)

        self.assertEqual(self._shortage(), 8)

    def test_the_swap_path_renders_the_same_fields(self):
        """`record_dye_bath` re-renders the row partial, which now reads
        `net_shortage`. A missing attribute renders as an empty cell rather
        than raising, so forgetting to annotate there is silent."""
        response = self.client.post(
            reverse("record_dye_bath", args=[self.product.pk]),
            {"qty": 4},
            HTTP_HX_REQUEST="true",
        )

        html = response.rendered_content
        self.assertIn('class="num shortage"', html)
        # 8 par, 4 just bagged, nothing on paper -> 4 left, and it is printed.
        self.assertRegex(html, r'class="num shortage">\s*4')

    def test_an_oven_colorway_is_still_reported(self):
        """The page reports where the sheet plans: `oven=None`, so both boxes
        are listed and the oven ones are badged rather than dropped."""
        Recipe.objects.filter(pk=self.recipe.pk).update(oven_dyed=True)

        self.assertEqual(self._shortage(), 8)
class OvenRunTests(TestCase):
    """The oven: a second kind of session, planned to the box rather than to
    the work.

    Some colorways are made in an oven rather than in a pot, and running it
    is an *event* — it heats once, holds `OVEN_TRAYS` trays, and fifteen is
    what makes the heating worth it. Two things follow, and both are the sort
    that fail silently if they come undone:

    **The partition runs both ways.** An oven colorway on a dye-room sheet
    sends somebody to a sink to make a thing that is not made there, which is
    exactly the failure `made_in_a_dye_bath` exists to stop. It is invisible
    on the paper — the row looks like every other row.

    **The gap is stated, never enforced.** A short sheet is somebody's
    decision and the app does not get to refuse it.
    """

    def setUp(self):
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)

        self.pot = make_recipe("Stormy Sea")
        self.oven = make_recipe("Speckled Ember")
        Recipe.objects.filter(pk=self.oven.pk).update(oven_dyed=True)
        self.oven.refresh_from_db()

        self.pot_product = make_bathable(self.pot, "Stormy Silk", on_hand=0, par=8, bath=4)
        self.oven_product = make_bathable(self.oven, "Ember Wool", on_hand=0, par=8, bath=4)

        # One page, one tick — the oven is a checkbox on the picker, not a
        # second URL somebody has to remember because of which appliance
        # they are using.
        self.sheet_url = reverse("production_sheet_index")

    # --- the partition ----------------------------------------------------

    def test_the_dye_room_sheet_never_suggests_an_oven_colorway(self):
        """The silent one. A pot cannot make it, and the row on the paper
        looks like every other row."""
        names = {bath.recipe_name for bath in production.plan_baths(20)}

        self.assertIn("Stormy Sea", names)
        self.assertNotIn("Speckled Ember", names)

    def test_the_oven_run_suggests_only_oven_colorways(self):
        names = {bath.recipe_name for bath in production.plan_baths(20, oven=True)}

        self.assertEqual(names, {"Speckled Ember"})

    def test_the_two_sessions_are_disjoint_rather_than_one_being_a_subset(self):
        """Neither list is the other with something taken off — they are two
        populations, and every dyeable colorway is on exactly one."""
        pot = {b.product.pk for b in production.plan_baths(50)}
        oven = {b.product.pk for b in production.plan_baths(50, oven=True)}

        self.assertEqual(pot & oven, set())
        self.assertEqual(pot | oven, {self.pot_product.pk, self.oven_product.pk})

    def test_printing_an_oven_run_records_that_it_was_one(self):
        """Frozen onto the run, for the reason the bath size is: a reprint
        has to say what the paper said, and the run page reads it to decide
        what may be added later."""
        self.client.post(self.sheet_url, {"baths": 15, "oven": "1"})

        run = ProductionRun.objects.get()
        self.assertTrue(run.oven)
        self.assertEqual(
            {row.finished_product.recipe.name for row in run.rows.all()},
            {"Speckled Ember"},
        )

    def test_a_dye_room_sheet_is_not_marked_as_one(self):
        self.client.post(self.sheet_url, {"baths": 10})

        self.assertFalse(ProductionRun.objects.get().oven)

    def test_the_checkbox_is_actually_on_the_page(self):
        """**The tick has to be tickable**, which is not implied by any of the
        behaviour tests below.

        This shipped once with no checkbox at all. A `@property` named `oven`
        sat below `oven = forms.BooleanField(...)` in the same class body and
        overwrote it, so the metaclass never collected the field — while every
        test here still passed, because the property read the raw POST data
        and the behaviour was therefore right. The feature worked perfectly
        and could not be switched on.
        """
        response = self.client.get(self.sheet_url, {"baths": 10})
        html = response.content.decode()

        self.assertIn("oven", response.context["form"].fields)
        self.assertIn('name="oven"', html)
        self.assertIn("This is an oven run", html)

    def test_the_checkbox_comes_back_ticked_on_an_oven_run(self):
        """Or every re-render silently offers to turn it off again."""
        response = self.client.get(self.sheet_url, {"baths": 15, "oven": "1"})

        self.assertIn("checked", str(response.context["form"]["oven"]))

    def test_every_control_on_the_page_carries_the_tick(self):
        """**The cost of a checkbox instead of a route, pinned.**

        A route carried the session kind in the URL and could not be dropped.
        A tick has to be re-carried by every control that makes a round trip,
        and each miss is silent: the list keeps working and the tray gauge
        simply stops being drawn. On the Print path it is worse — the run is
        stored as a dye-room sheet, and the run page then refuses the oven
        colorways that are actually on it.

        So this walks the rendered page rather than trusting four templates to
        each remember.
        """
        # A second oven colorway, so a remove link still has rows left in it
        # — the single-row case reduces to `?oven=1` and would pass trivially.
        second = make_recipe("Ash Bloom")
        Recipe.objects.filter(pk=second.pk).update(oven_dyed=True)
        make_bathable(second, "Ash Wool", on_hand=0, par=8, bath=4)

        response = self.client.get(self.sheet_url, {"baths": 15, "oven": "1"})
        html = response.content.decode()

        # The list form: covers qty edits, the search's add, and Print, all of
        # which submit or `hx-include` it.
        self.assertIn('<input type="hidden" name="oven" value="1">', html)

        # Every ✕ builds its own address, so each one has to say it itself —
        # including the one that empties the list, which still has to come
        # back as an oven run rather than as a dye-room sheet.
        removes = re.findall(r'<a class="drop"[^>]*?href="\?([^"]*)"', html, re.S)
        self.assertEqual(len(removes), 2, f"expected two remove links, got {removes}")
        for link in removes:
            self.assertIn("oven=1", link)
        self.assertTrue(any("items=" in link for link in removes))

    def test_the_tick_survives_an_edit(self):
        """The round trip that actually happens: change a count, and the
        sheet still has to be an oven run on the other side."""
        response = self.client.get(
            self.sheet_url,
            {"items": f"{self.oven_product.pk}:2", "oven": "1"},
            HTTP_HX_REQUEST="true",
        )

        self.assertTrue(response.context["oven"])
        self.assertIn('name="oven" value="1"', response.content.decode())

    def test_a_dropped_tick_would_store_the_wrong_kind_of_run(self):
        """Why the one above matters, stated as the failure it prevents."""
        self.client.post(self.sheet_url, {"items": f"{self.oven_product.pk}:2"})

        # No tick posted, so this is a dye-room sheet — and the guard on the
        # run page will then refuse the oven colorway sitting on it.
        run = ProductionRun.objects.get()
        self.assertFalse(run.oven)

    def test_each_picker_lists_only_its_own_live_sheets(self):
        """A dye-room sheet is not something anybody is working from at the
        oven — mixing them makes the list longer without making it useful."""
        self.client.post(self.sheet_url, {"baths": 15, "oven": "1"})
        oven_run = ProductionRun.objects.get()

        response = self.client.get(self.sheet_url)

        self.assertNotIn(oven_run, list(response.context["open_runs"]))

    # --- filling the box --------------------------------------------------

    def test_the_page_counts_trays_and_names_the_gap(self):
        """One tray is one bath, so this is a row count. Two baths of the one
        oven colorway leaves thirteen trays empty."""
        response = self.client.get(self.sheet_url, {"baths": 15, "oven": "1"})

        self.assertEqual(response.context["tray_gap"], production.OVEN_TRAYS - 2)
        self.assertContains(response, f"2 of {production.OVEN_TRAYS} trays")

    def test_a_short_sheet_still_prints(self):
        """Stated, never enforced. A session with a reason to run short is a
        session somebody has a reason for, and refusing would be the app
        arguing with a person who can see the calendar."""
        response = self.client.post(self.sheet_url, {"baths": 15, "oven": "1"})

        run = ProductionRun.objects.get()
        self.assertEqual(run.rows.count(), 2)
        self.assertRedirects(response, reverse("production_run_detail", args=[run.pk]))

    def test_top_ups_are_offered_when_the_box_is_short(self):
        """The one place the app suggests making something not below par —
        and it offers, it never adds."""
        spare = make_recipe("Ash Bloom")
        Recipe.objects.filter(pk=spare.pk).update(oven_dyed=True)
        stocked = make_bathable(spare, "Ash Wool", on_hand=99, par=8, bath=4)

        response = self.client.get(self.sheet_url, {"baths": 15, "oven": "1"})

        offered = {product.pk for product, _ in response.context["top_ups"]}
        self.assertIn(stocked.pk, offered)
        # Offered, not added: the list is still just the shortage.
        self.assertEqual(len(response.context["baths"]), 2)

    def test_a_top_up_is_never_a_pot_colorway(self):
        """Filling the oven with something that cannot go in it is the whole
        failure this feature exists to prevent, arriving by the back door."""
        make_bathable(self.pot, "Stormy Wool", on_hand=99, par=8, bath=4)

        response = self.client.get(self.sheet_url, {"baths": 15, "oven": "1"})

        for product, _ in response.context["top_ups"]:
            self.assertTrue(product.recipe.oven_dyed)

    def test_nothing_is_offered_once_the_box_is_full(self):
        """The panel only ever answers a question the page is asking."""
        self.assertEqual(production.top_ups([], 0), [])

    def test_a_top_up_is_not_something_already_on_the_list(self):
        response = self.client.get(self.sheet_url, {"baths": 15, "oven": "1"})

        offered = {product.pk for product, _ in response.context["top_ups"]}
        self.assertNotIn(self.oven_product.pk, offered)

    # --- editing a printed oven sheet -------------------------------------

    def test_a_pot_colorway_can_still_be_added_to_an_oven_run(self):
        """**Advice, not enforcement.** `oven_dyed` is typed by a person from
        a rule with exceptions nobody knows yet, so refusing on it would be
        the app enforcing somebody's provisional data back at them — at the
        exact moment they are saying it is wrong. It is said and allowed."""
        self.client.post(self.sheet_url, {"baths": 15, "oven": "1"})
        run = ProductionRun.objects.get()
        before = run.rows.count()

        response = self.client.post(
            reverse("production_run_add_row", args=[run.pk]),
            {"product": self.pot_product.pk},
            follow=True,
        )

        self.assertEqual(run.rows.count(), before + 1)
        self.assertContains(response, "made in a pot")

    def test_an_oven_colorway_can_still_be_added_to_a_dye_room_sheet(self):
        self.client.post(self.sheet_url, {"baths": 10})
        run = ProductionRun.objects.get()
        before = run.rows.count()

        response = self.client.post(
            reverse("production_run_add_row", args=[run.pk]),
            {"product": self.oven_product.pk},
            follow=True,
        )

        self.assertEqual(run.rows.count(), before + 1)
        self.assertContains(response, "oven colorway")

    def test_what_still_cannot_go_on_a_sheet(self):
        """The line: how a thing comes into being is a fact, which stays
        refused. An undyed skein is ordered, not dyed."""
        plain = make_recipe("Plain")
        passthrough = make_bathable(plain, "Undyed Skein", on_hand=0, par=8, bath=4)
        FinishedProduct.objects.filter(pk=passthrough.pk).update(recipe=None)

        self.client.post(self.sheet_url, {"baths": 15, "oven": "1"})
        run = ProductionRun.objects.get()
        before = run.rows.count()

        self.client.post(
            reverse("production_run_add_row", args=[run.pk]),
            {"product": passthrough.pk},
        )

        self.assertEqual(run.rows.count(), before)

    def test_a_sheet_can_be_an_oven_load_plus_pots_on_the_side(self):
        """One session, one sheet. If the oven is running and two other
        colours want a pot the same afternoon, two sheets for one afternoon is
        overhead for overhead."""
        response = self.client.post(
            self.sheet_url,
            {
                "items": [
                    f"{self.oven_product.pk}:2",
                    f"{self.pot_product.pk}:2",
                ],
                "oven": "1",
            },
        )

        run = ProductionRun.objects.get()
        self.assertEqual(run.rows.count(), 4)
        self.assertRedirects(response, reverse("production_run_detail", args=[run.pk]))

    def test_the_pots_alongside_take_no_tray_space(self):
        """The counting bug this arrangement creates. Fifteen trays plus two
        pots is not seventeen trays — the pots were never in the box, and a
        gauge reading `17 of 15` tells somebody to remove work the oven is not
        holding."""
        response = self.client.get(
            self.sheet_url,
            {
                "items": [
                    f"{self.oven_product.pk}:2",
                    f"{self.pot_product.pk}:3",
                ],
                "oven": "1",
            },
        )

        self.assertEqual(response.context["oven_bath_count"], 2)
        self.assertEqual(response.context["pot_bath_count"], 3)
        self.assertEqual(response.context["tray_gap"], production.OVEN_TRAYS - 2)
        self.assertContains(response, f"2 of {production.OVEN_TRAYS} trays")
        self.assertContains(response, "take no tray space")

    def test_the_run_page_counts_trays_the_same_way(self):
        self.client.post(
            self.sheet_url,
            {
                "items": [
                    f"{self.oven_product.pk}:2",
                    f"{self.pot_product.pk}:3",
                ],
                "oven": "1",
            },
        )
        run = ProductionRun.objects.get()

        response = self.client.get(reverse("production_run_detail", args=[run.pk]))

        self.assertEqual(response.context["live_trays"], 2)
        self.assertEqual(response.context["alongside_baths"], 3)

    def _over_full(self):
        """More trays than the oven holds, across two colours.

        Deliberately not sixteen baths of one colorway: `MAX_PER_ITEM` refuses
        that as a typo, and a real over-full oven is several colours anyway.
        """
        second = make_recipe("Ash Bloom")
        Recipe.objects.filter(pk=second.pk).update(oven_dyed=True)
        other = make_bathable(second, "Ash Wool", on_hand=0, par=8, bath=4)
        return {
            "items": [f"{self.oven_product.pk}:8", f"{other.pk}:8"],
            "oven": "1",
        }

    def test_a_batch_bigger_than_the_oven_still_prints(self):
        """Sixteen trays is somebody deciding, and the app does not get a
        vote — it says the box holds fifteen and prints what was asked for."""
        response = self.client.post(self.sheet_url, self._over_full())

        run = ProductionRun.objects.get()
        self.assertEqual(run.rows.count(), 16)
        self.assertRedirects(response, reverse("production_run_detail", args=[run.pk]))

    def test_and_the_page_says_it_is_over(self):
        response = self.client.get(self.sheet_url, self._over_full())

        self.assertContains(response, "more than the oven holds")
        self.assertContains(response, "Print this sheet")

    def test_the_count_box_cannot_offer_more_than_the_server_takes(self):
        """A client cap looser than the validating one is a number somebody
        can type and then be refused for."""
        response = self.client.get(self.sheet_url, {"baths": 15, "oven": "1"})

        self.assertEqual(response.context["max_per_item"],
                         PickedBathsField.MAX_PER_ITEM)
        self.assertContains(response, f'max="{PickedBathsField.MAX_PER_ITEM}"')

    def test_the_matching_kind_still_adds(self):
        """The guard is narrow. Everything else about this box stays
        permissive — a bath nobody needs is exactly what it is for."""
        spare = make_recipe("Ash Bloom")
        Recipe.objects.filter(pk=spare.pk).update(oven_dyed=True)
        stocked = make_bathable(spare, "Ash Wool", on_hand=99, par=8, bath=4)

        self.client.post(self.sheet_url, {"baths": 15, "oven": "1"})
        run = ProductionRun.objects.get()

        self.client.post(
            reverse("production_run_add_row", args=[run.pk]),
            {"product": stocked.pk},
        )

        self.assertIn(stocked.pk,
                      {row.finished_product_id for row in run.rows.all()})

    def test_a_struck_row_hands_its_tray_back(self):
        self.client.post(self.sheet_url, {"baths": 15, "oven": "1"})
        run = ProductionRun.objects.get()
        row = run.rows.first()

        self.client.post(
            reverse("production_run_strike_row", args=[run.pk, row.pk])
        )
        response = self.client.get(reverse("production_run_detail", args=[run.pk]))

        self.assertEqual(response.context["live_trays"], 1)

    def test_an_oven_sheet_prints(self):
        """Three documents, unchanged — the work sheet's stage boxes carry no
        printed names, so oven stages already fit."""
        self.client.post(self.sheet_url, {"baths": 15, "oven": "1"})
        run = ProductionRun.objects.get()

        response = self.client.get(reverse("production_sheet_pdf", args=[run.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))

    # --- the pages that must not filter ------------------------------------

    def test_production_needed_still_lists_an_oven_colorway(self):
        """It reports what is below par, and an oven colorway being short is
        a real fact. Filtering would leave a shortage nobody can see from
        anywhere — the badge is what keeps the two pages from disagreeing
        silently instead."""
        response = self.client.get(reverse("production_needed"))

        names = {group["recipe_name"] for group in response.context["groups"]}
        self.assertIn("Speckled Ember", names)
        self.assertContains(response, "oven")
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
        self.other = make_bathable(
            make_recipe("Ember"), "Ember Silk", on_hand=0, par=8, bath=4
        )
        self.run = ProductionRun.objects.create()
        # Two baths of one colorway — which is now **one line of eight**, one
        # box, one answer — plus a second colorway so there is something for a
        # partial report to leave behind.
        self.rows = [
            ProductionRunRow.objects.create(
                run=self.run, finished_product=self.product, order=i, quantity=4
            )
            for i in (1, 2)
        ]
        self.other_row = ProductionRunRow.objects.create(
            run=self.run, finished_product=self.other, order=3, quantity=4
        )
        self.line, self.other_line = production.lines_for_run(self.run)
        self.url = reverse("production_run", args=[self.run.token])

    def _report(self, *lines, **extra):
        data = {"done": [str(line.key) for line in lines]}
        data.update(extra)
        return self.client.post(self.url, data)

    def test_it_serves_an_anonymous_get(self):
        """The crew have no accounts, and a login here would mean the sheet
        never gets reported."""
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_bad_token_is_not_a_page(self):
        self.assertEqual(
            self.client.get(reverse("production_run", args=["nope"])).status_code, 404
        )

    def test_ticking_a_line_banks_every_bath_of_it(self):
        """**One tick, one colorway, all of it.** Three baths of Artisan
        Cabernet is one pile of fifteen, so the box beside it means the pile
        arrived — not that one pot of it did."""
        self._report(self.line)

        self.product.refresh_from_db()
        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 8)
        self.assertEqual(self.product.raw_product.number_on_hand, 92)
        # A row per bath underneath, because that is where scrap is answered
        # from and what stops a bath being counted twice.
        self.assertEqual(InventoryLog.objects.count(), 2)

    def test_an_unticked_line_moves_nothing(self):
        """Half a sheet is the normal outcome, not an error state."""
        self._report(self.line)

        self.other_row.refresh_from_db()
        self.assertIsNone(self.other_row.accepted_at)
        self.other.refresh_from_db()
        self.assertEqual(self.other.number_on_hand, 0)

    def test_reporting_the_same_line_twice_applies_it_once(self):
        self._report(self.line)
        self._report(self.line)

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 8)
        self.assertEqual(InventoryLog.objects.count(), 2)

    def test_a_line_remembered_later_still_goes_in(self):
        """Reopening the page and adding one is normal, and must add rather
        than replace."""
        self._report(self.line)
        self._report(self.other_line)

        self.product.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 8)
        self.assertEqual(self.other.number_on_hand, 4)
        self.assertEqual(InventoryLog.objects.count(), 3)

    def test_one_bath_back_does_not_close_the_sheet(self):
        """The rule this replaced said one tick was enough.

        That was right while a bath was an atomic event. Dyeing runs one to
        three days, so a sheet answered once is usually a sheet with most of
        its baths still on a line — and calling it finished there is what put
        the app one to three days ahead of the shelf.
        """
        self._report(self.line)

        self.run.refresh_from_db()
        self.assertTrue(self.run.is_open)
        self.assertEqual(self.run.pending_count, 1)

    def test_the_sheet_closes_when_nothing_is_left_pending(self):
        self._report(self.line)
        self._report(self.other_line)

        self.run.refresh_from_db()
        self.assertTrue(self.run.is_closed)

    def test_a_reply_is_still_recorded_even_though_it_decides_nothing(self):
        self._report(self.line)

        self.run.refresh_from_db()
        self.assertIsNotNone(self.run.submitted_at)

    def test_an_accepted_row_is_shown_not_hidden(self):
        """A row that vanished would read as 'I never ticked that'."""
        self._report(self.line)

        response = self.client.get(self.url)

        self.assertContains(response, "In stock")

    def test_a_short_line_credits_what_came_out_and_consumes_the_blanks(self):
        """The scarves that failed still used their blanks up."""
        self._report(self.line, **{f"yielded-{self.line.key}": "3"})

        self.product.refresh_from_db()
        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 3)
        self.assertEqual(self.product.raw_product.number_on_hand, 92)

    def test_banking_three_baths_at_once_does_not_lose_two_of_them(self):
        """**The bug grouping introduced, and it was silent.**

        Every row of a line points at the same colorway, and `select_related`
        hands each row its own copy of it. Applied in one pass, each read
        `number_on_hand` as it was before any of them ran, added its own
        yield and saved — last write wins, and a line of three baths banked
        five units instead of fifteen. Nothing errored; the run closed, the
        logs were all there, and only the count was wrong.

        It could never happen while a tick was one bath in one request, which
        is exactly why it arrived with the grouping.
        """
        third = ProductionRunRow.objects.create(
            run=self.run, finished_product=self.product, order=4, quantity=4
        )
        line = production.lines_for_run(self.run)[0]
        self.assertEqual(line.rows, [self.rows[0], self.rows[1], third])

        self._report(line)

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 12)
        # And the blanks go once per bath, not once per line.
        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.raw_product.number_on_hand, 88)

    def test_a_short_line_loses_whole_baths_first(self):
        """**Four of eight means one pot failed, not that both came up
        short.** So the shortfall lands on a whole bath — 4 and 0 — which is
        the event somebody describing the session would report, and
        `ProductionRunRow` is the only place a scrap question is answerable
        from. Spreading it evenly would invent a bad afternoon out of one
        ruined lot.
        """
        self._report(self.line, **{f"yielded-{self.line.key}": "4"})

        first, second = [r for r in self.line.rows]
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual([first.yielded, second.yielded], [4, 0])
        self.assertEqual([first.loss, second.loss], [0, 4])
        # Both pots were emptied of blanks either way.
        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.raw_product.number_on_hand, 92)

    def test_a_binned_line_is_a_log_at_zero_not_a_missing_log(self):
        """`applied_log` is the only guard against a row being counted
        twice, so a total loss has to write one like everything else."""
        self._report(self.line, **{f"yielded-{self.line.key}": "0"})

        self.rows[0].refresh_from_db()
        self.product.refresh_from_db()
        self.product.raw_product.refresh_from_db()
        self.assertTrue(self.rows[0].is_accepted)
        self.assertEqual(self.rows[0].yielded, 0)
        self.assertEqual(self.product.number_on_hand, 0)
        self.assertEqual(self.product.raw_product.number_on_hand, 92)
        self.assertEqual(InventoryLog.objects.count(), 2)

    def test_a_tick_with_no_number_still_means_the_whole_line(self):
        """A thumb that never reaches the number box reports what a tick has
        always reported."""
        self._report(self.line)

        self.rows[0].refresh_from_db()
        self.rows[1].refresh_from_db()
        self.assertEqual([self.rows[0].yielded, self.rows[1].yielded], [4, 4])

    def test_a_cancelled_bath_moves_nothing_and_writes_no_log(self):
        production.cancel_row(self.rows[0])

        self.rows[0].refresh_from_db()
        self.product.refresh_from_db()
        self.product.raw_product.refresh_from_db()
        self.assertTrue(self.rows[0].is_cancelled)
        self.assertEqual(self.product.number_on_hand, 0)
        self.assertEqual(self.product.raw_product.number_on_hand, 100)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_cancelling_is_not_the_same_as_binning_a_bath(self):
        """Cancelled means it never ran, so the blanks are still there.

        Binned means it ran and the lot was lost, so they are not. Both end
        with no scarves, and only one of them costs stock — which is why they
        are different states rather than two names for one.
        """
        production.cancel_row(self.rows[0])
        line = production.lines_for_run(self.run)[0]
        self._report(line, **{f"yielded-{line.key}": "0"})

        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.raw_product.number_on_hand, 96)

    def test_an_accepted_row_cannot_then_be_cancelled(self):
        """Stock has moved; taking it back is an adjustment with a reason."""
        self._report(self.line)
        self.rows[0].refresh_from_db()

        self.assertFalse(production.cancel_row(self.rows[0]))

    def test_the_phone_records_who_reported_if_it_knows(self):
        """A record, not a check — the token on the paper is what lets the
        report through."""
        employee = make_employee("Sam", pin="4821")
        self.client.post(reverse("hours_entry"), {
            "employee": employee.pk, "pin": "4821", "hours": "9.5",
            "work_date": timezone.localdate().isoformat(),
        })

        self._report(self.line)

        self.run.refresh_from_db()
        self.assertEqual(self.run.submitted_by, employee)

    def test_an_unknown_phone_still_reports(self):
        self._report(self.line)

        self.run.refresh_from_db()
        self.assertIsNone(self.run.submitted_by)
        self.assertEqual(InventoryLog.objects.count(), 2)

    def test_the_fallback_page_lists_open_sheets(self):
        """For a cracked camera or a photocopied sheet."""
        response = self.client.get(reverse("production_run_index"))

        self.assertContains(response, f"Run {self.run.pk}")

    def test_a_part_reported_sheet_stays_on_the_fallback_list(self):
        """It used to drop off on the first tick.

        That is the wrong list to be taken off: this is the page somebody
        reaches for when the QR won't scan, and a sheet with baths still
        drying is exactly the one they are holding. Reporting the first bath
        of a three-day session would have left the rest with no way back in
        short of typing the code.
        """
        self._report(self.line)

        response = self.client.get(reverse("production_run_index"))

        self.assertContains(response, f"Run {self.run.pk}")

    def test_a_finished_sheet_leaves_the_fallback_list(self):
        self._report(self.line)
        self._report(self.other_line)

        response = self.client.get(reverse("production_run_index"))

        self.assertNotContains(response, f"Run {self.run.pk}")
class IdenticalBathsAreOneLineTests(TestCase):
    """Three baths of Artisan Cabernet is one group of fifteen, not three of five.

    **The reporting sheet asks about a pile, and the pile is one pile.** A
    box per bath is three marks for one answer, three chances to tick the
    wrong line, and three lines a photograph has to resolve — while the crew
    are standing in front of fifteen scarves that came out of one colour.

    The blank is the other half of the identity. Artisan Peacock and Noble
    Peacock came out of two different pots and stay two lines, which falls
    out of grouping on `finished_product` — blank × colorway, the axis the
    catalogue is already organised on.

    What does *not* group: the work sheet, whose boxes hold a pot at a point
    in a one-to-three day process, and the rows in the database, where a bath
    stays a bath because that is where scrap is answered from.
    """

    def setUp(self):
        self.run = ProductionRun.objects.create()
        self.cabernet = make_recipe("Cabernet")
        self.peacock = make_recipe("Peacock")
        self.artisan_cabernet = make_bathable(
            self.cabernet, "Artisan", on_hand=0, par=40, bath=5
        )
        self.artisan_peacock = make_bathable(
            self.peacock, "Artisan Two", on_hand=0, par=40, bath=5
        )
        self.noble_peacock = make_bathable(
            self.peacock, "Noble", on_hand=0, par=40, bath=5
        )

    def _row(self, product, order):
        return ProductionRunRow.objects.create(
            run=self.run, finished_product=product, order=order, quantity=5
        )

    def test_three_baths_of_one_colorway_are_one_line_of_fifteen(self):
        for order in (1, 2, 3):
            self._row(self.artisan_cabernet, order)

        lines = production.lines_for_run(self.run)

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].quantity, 15)
        self.assertEqual(lines[0].baths, 3)

    def test_the_blank_is_half_the_identity(self):
        """**Artisan Peacock and Noble Peacock are not matches.** Same
        colour, two different pots, two different things on the shelf."""
        self._row(self.artisan_peacock, 1)
        self._row(self.noble_peacock, 2)

        lines = production.lines_for_run(self.run)

        self.assertEqual(len(lines), 2)
        self.assertEqual([line.quantity for line in lines], [5, 5])

    def test_lines_keep_the_order_the_planner_chose(self):
        """`plan_baths` already clumps a recipe's baths together — one mix
        and one pot serve several loads — and the order between recipes is
        the urgency it decided. Grouping must not re-sort that away."""
        self._row(self.artisan_peacock, 1)
        self._row(self.artisan_cabernet, 2)
        self._row(self.artisan_cabernet, 3)
        self._row(self.noble_peacock, 4)

        lines = production.lines_for_run(self.run)

        self.assertEqual(
            [line.product for line in lines],
            [self.artisan_peacock, self.artisan_cabernet, self.noble_peacock],
        )
        self.assertEqual([line.number for line in lines], [1, 2, 3])

    def test_the_crew_page_shows_one_box_per_colorway(self):
        for order in (1, 2, 3):
            self._row(self.artisan_cabernet, order)
        self._row(self.noble_peacock, 4)

        html = self.client.get(
            reverse("production_run", args=[self.run.token])
        ).content.decode()

        self.assertEqual(html.count('name="done"'), 2)
        self.assertIn("15 × Cabernet", html)
        self.assertIn("5 × Peacock", html)

    def test_the_sheet_still_prints(self):
        """A smoke test with a grouped line on it, because the reporting
        page's geometry is derived from the same constants `sheetscan` reads
        back along."""
        for order in (1, 2, 3):
            self._row(self.artisan_cabernet, order)

        pdf = production.render_sheet(self.run, "http://example.test/x/")

        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_one_code_per_line_not_per_bath(self):
        """The position half of the code used to be what stopped a decoder
        collapsing three identical symbols into one. Grouping removed the
        collision at its source; the position stays as a check on being
        pointed at the right sheet."""
        for order in (1, 2, 3):
            self._row(self.artisan_cabernet, order)
        self._row(self.noble_peacock, 4)

        codes = [production.line_code(l) for l in production.lines_for_run(self.run)]

        self.assertEqual(len(codes), 2)
        self.assertEqual(len(set(codes)), 2)
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
class SheetsAgeOutRatherThanBeingRetiredTests(TestCase):
    """Nothing closes a sheet on the app's own initiative any more.

    Printing a sixth sheet used to close the oldest, on the reasoning that
    five out at once means the reporting loop has already failed. The
    reasoning holds; the remedy was the app guessing. Closing an unanswered
    sheet silently decided its session never happened — and since a bath
    takes one to three days, a sheet with four still drying looks exactly
    like a sheet somebody abandoned.

    What replaced it destroys nothing: an old sheet stops *claiming* its
    baths, so the colorways come back onto new sheets, and it is named on
    the picker for a person to settle.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        make_bathable(make_recipe("Stormy Sea"), "Stormy Silk", on_hand=0, par=80, bath=4)
        self.url = reverse("production_sheet_index")

    def _print(self):
        return self.client.post(self.url, {"baths": "2"})

    def _age(self, run, days):
        ProductionRun.objects.filter(pk=run.pk).update(
            created_at=timezone.now() - timedelta(days=days)
        )

    def test_printing_is_never_refused(self):
        for _ in range(8):
            self._print()

        self.assertEqual(ProductionRun.objects.count(), 8)

    def test_no_sheet_is_closed_behind_anyones_back(self):
        for _ in range(8):
            self._print()

        self.assertEqual(production.closed_runs().count(), 0)
        self.assertFalse(
            ProductionRun.objects.exclude(note="").exists(),
            "nothing should be writing a closing note on its own",
        )

    def test_an_old_sheet_goes_overdue_rather_than_away(self):
        run = ProductionRun.objects.get(pk=self._print_one().pk)
        self._age(run, production.OVERDUE_AFTER.days + 1)

        self.assertIn(run, production.overdue_runs())
        self.assertTrue(production.open_runs().filter(pk=run.pk).exists())

    def test_a_recent_sheet_is_counted_not_overdue(self):
        run = self._print_one()

        self.assertIn(run, production.counted_runs())
        self.assertEqual(production.overdue_runs().count(), 0)

    def test_the_picker_names_an_overdue_sheet(self):
        """Going overdue changes what gets asked for, so it cannot be
        silent — a colorway would quietly start being dyed twice."""
        run = self._print_one()
        self._age(run, production.OVERDUE_AFTER.days + 1)

        response = self.client.get(self.url)

        self.assertContains(response, "open longer than expected")
        self.assertContains(response, f"Run {run.pk}")

    def test_retiring_a_sheet_is_cancelling_what_is_left_on_it(self):
        """There is no run-level retired flag, so closed cannot disagree
        with the rows it is derived from."""
        run = self._print_one()
        for row in run.rows.all():
            production.cancel_row(row)

        run.refresh_from_db()
        self.assertTrue(run.is_closed)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def _print_one(self):
        self._print()
        return ProductionRun.objects.order_by("-pk").first()
class SheetStaysReachableTests(TestCase):
    """A partly-reported sheet is still live, and still openable by its code."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        # **Two colorways, one bath short each.** The sheet groups identical
        # baths onto one line now, so two baths of one colorway would be a
        # single tick and the run would close on the first submit — which is
        # the opposite of what this class is about.
        make_bathable(make_recipe("Stormy Sea"), "Stormy Silk", on_hand=0, par=4, bath=4)
        make_bathable(make_recipe("Ember"), "Ember Silk", on_hand=0, par=4, bath=4)
        self.client.post(reverse("production_sheet_index"), {"baths": "2"})
        self.run = ProductionRun.objects.get()
        self.lines = production.lines_for_run(self.run)
        # Stated rather than assumed: if the planner ever stops giving one
        # bath of each, this fails here instead of somewhere confusing.
        self.assertEqual(len(self.lines), 2)

    def test_a_part_reported_sheet_is_still_reachable_by_its_code(self):
        url = reverse("production_run", args=[self.run.token])
        self.client.post(url, {"done": [str(self.lines[0].key)]})

        self.assertEqual(self.client.get(url).status_code, 200)

        self.client.post(url, {"done": [str(self.lines[1].key)]})
        self.run.refresh_from_db()
        self.assertEqual(self.run.accepted_count, 2)

    def test_it_stays_on_the_working_list_until_every_bath_is_settled(self):
        """It used to drop off on the first tick, which read one answered
        bath as a finished session."""
        url = reverse("production_run", args=[self.run.token])
        self.client.post(url, {"done": [str(self.lines[0].key)]})

        self.assertTrue(production.open_runs().filter(pk=self.run.pk).exists())

        self.client.post(url, {"done": [str(self.lines[1].key)]})

        self.assertFalse(production.open_runs().filter(pk=self.run.pk).exists())
class WorkSheetAndReportingSheetTests(TestCase):
    """Three documents in one print, in the order the job happens.

    The reason they are separate is the one-to-three-day process: the sheet
    marked up mid-session and the sheet photographed at the end want opposite
    things, and the working copy carrying nothing scannable is what stops it
    being read as a report of baths that are still on a drying line.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.product = make_bathable(
            make_recipe("Stormy Sea"), "Stormy Silk", on_hand=0, par=8, bath=4
        )
        self.run = ProductionRun.objects.create()
        self.rows = [
            ProductionRunRow.objects.create(
                run=self.run, finished_product=self.product, order=i, quantity=4
            )
            for i in (1, 2)
        ]

    def _pdf(self):
        return production.render_sheet(self.run, "https://x.test/report/")

    def _text(self):
        return _pdf_text(self._pdf())

    def test_it_renders(self):
        self.assertTrue(self._pdf().startswith(b"%PDF"))

    def test_the_working_copy_says_what_it_is(self):
        self.assertIn("WORKING COPY", self._text())

    def test_the_working_copy_names_no_stages(self):
        """The boxes are blank, and that is the decision.

        Printing DYED / DRIED / TAGGED / BAGGED across the top would be the
        app telling somebody how to do a job it doesn't do — the stages are
        hers, they vary with what is in the pot, and a printed name is an
        instruction whether or not it was meant as one. There is a ruled line
        over each column instead, which is an invitation.
        """
        # Scoped to the working copy's own rows. "Bagged" appears legitimately
        # on the *reporting* sheet, where it says what the tick box claims —
        # that is the acceptance wording, not a stage caption.
        work_page = (
            self._text().upper()
            .split("DO NOT PHOTOGRAPH THIS PAGE")[1]
            .split("PRODUCTION SHEET")[0]
        )

        for word in ("DYED", "DRIED", "TAGGED", "BAGGED"):
            self.assertNotIn(word, work_page)
        self.assertFalse(
            hasattr(production, "WORK_STAGES"),
            "the named stage list is gone — the count is what remains",
        )

    def test_the_working_copy_has_one_box_per_bath(self):
        """It was four, and four was a guess.

        The reasoning for a row of them — a bath moves through stages over one
        to three days, and paper holds that better than a phone by a sink —
        is true, and it does not follow that the app should decide how many
        stages there are. It never knew: the count was invented, the boxes
        were left unlabelled *because* nothing could honestly name them, and a
        rule was printed over each column so somebody could name them herself.
        A column nobody asked for, headed by nothing, is four boxes to ignore
        per row.
        """
        self.assertEqual(production.WORK_BOXES, 1)
        self.assertFalse(
            hasattr(production, "_stage_columns"),
            "the column layout went with the columns",
        )

    def test_the_working_copy_says_what_goes_in_each_bath(self):
        """The collection page lists dyes pooled, which is the right shape for
        one walk to the shelf and the wrong shape at the sink: standing over a
        pot the question is what goes in *this* one."""
        dye = self.rows[0].finished_product.recipe.recipe_dyes.first()
        if dye is None:
            self.skipTest("fixture recipe has no dyes")

        self.assertIn(dye.dye.name, self._text())

    def test_a_recipe_with_no_dyes_on_file_says_so_on_the_sheet(self):
        """A blank there reads as "no dyes needed", which is a bath somebody
        starts and cannot finish."""
        recipe = self.rows[0].finished_product.recipe
        recipe.recipe_dyes.all().delete()

        self.assertIn("no dyes on file", self._text())

    def test_a_reprint_does_not_read_the_run_state_back(self):
        """Information flows paper → app.

        A reprint mid-session has to be the same document as the first print.
        The moment a PDF starts hiding rows that have come back, two sheets
        for one run disagree about how many baths are on it — and the one
        that disagrees is the one already in somebody's hand.
        """
        before = self._text().count("Stormy Sea")
        production.apply_row(self.rows[0])
        self.rows[1].refresh_from_db()
        production.cancel_row(self.rows[1])

        self.assertEqual(self._text().count("Stormy Sea"), before)

    def test_a_sheet_with_every_bath_settled_still_prints_them_all(self):
        production.apply_row(self.rows[0])
        production.apply_row(self.rows[1])

        self.assertTrue(self._pdf().startswith(b"%PDF"))
        self.assertIn("Stormy Sea", self._text())

    def test_the_pdf_route_serves_it(self):
        response = self.client.get(
            reverse("production_sheet_pdf", args=[self.run.pk])
        )

        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
class CrewCanSayTheRestIsNotComingTests(TestCase):
    """The code on the paper is enough to call a bath off.

    It was already enough to *accept* one, which moves stock — so requiring a
    staff login to say "this isn't happening", which moves nothing, had the
    permissions backwards. The person reporting the session is also the only
    one who knows the rest isn't coming.
    """

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_bathable(
            self.recipe, "Stormy Silk", on_hand=0, par=20, bath=4
        )
        self.other = make_bathable(
            make_recipe("Ember"), "Ember Silk", on_hand=0, par=20, bath=4
        )
        self.run = ProductionRun.objects.create()
        # Two baths of one colorway and one of another: two lines, so
        # banking the first still leaves something for "the rest".
        self.rows = [
            ProductionRunRow.objects.create(
                run=self.run, finished_product=self.product, order=i, quantity=4
            )
            for i in (1, 2)
        ]
        self.rows.append(ProductionRunRow.objects.create(
            run=self.run, finished_product=self.other, order=3, quantity=4
        ))
        self.lines = production.lines_for_run(self.run)
        self.url = reverse("production_run", args=[self.run.token])

    def test_no_login_is_needed(self):
        self.client.post(self.url, {"cancel": str(self.rows[0].pk)})

        self.rows[0].refresh_from_db()
        self.assertTrue(self.rows[0].is_cancelled)

    def test_calling_one_off_moves_nothing(self):
        self.client.post(self.url, {"cancel": str(self.rows[0].pk)})

        self.product.refresh_from_db()
        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 0)
        self.assertEqual(self.product.raw_product.number_on_hand, 100)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_the_rest_isnt_coming_closes_the_sheet(self):
        self.client.post(self.url, {"cancel_rest": "1"})

        self.run.refresh_from_db()
        self.assertTrue(self.run.is_closed)
        self.assertEqual(self.run.cancelled_count, 3)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_the_rest_leaves_what_was_already_accepted_alone(self):
        """One line banked — both its baths — and the other called off."""
        self.client.post(self.url, {"done": [str(self.lines[0].key)]})

        self.client.post(self.url, {"cancel_rest": "1"})

        self.rows[0].refresh_from_db()
        self.run.refresh_from_db()
        self.assertTrue(self.rows[0].is_accepted)
        self.assertIsNone(self.rows[0].cancelled_at)
        self.assertEqual(self.run.accepted_count, 2)
        self.assertEqual(self.run.cancelled_count, 1)

    def test_the_colorway_is_asked_for_again(self):
        """That is the difference from a bath that ran and was binned.

        Par 20 against a bath of 4 needs five baths of each colorway, so ten
        in all. This sheet holds two of Stormy and one of Ember, and those
        three are subtracted while it is live — leaving seven. Calling the
        sheet off hands all three claims back.
        """
        self.assertEqual(len(production.plan_baths(10)), 7)

        self.client.post(self.url, {"cancel_rest": "1"})

        self.assertEqual(len(production.plan_baths(10)), 10)

    def test_a_cancel_never_banks_whatever_was_ticked(self):
        """A cancel that also accepted a half-entered row would move stock
        this page has no way to take back."""
        self.client.post(self.url, {
            "cancel": str(self.rows[0].pk),
            "done": [str(self.rows[1].pk)],
        })

        self.rows[1].refresh_from_db()
        self.product.refresh_from_db()
        self.assertFalse(self.rows[1].is_accepted)
        self.assertEqual(self.product.number_on_hand, 0)

    def test_a_mis_tap_can_be_undone_without_an_account(self):
        """Same reasoning as the Sunday close's Undo: a fix they cannot make
        is a mistake they have to go and tell somebody about."""
        self.client.post(self.url, {"cancel": str(self.rows[0].pk)})

        self.client.post(self.url, {"uncancel": str(self.rows[0].pk)})

        self.rows[0].refresh_from_db()
        self.assertTrue(self.rows[0].is_pending)

    def test_an_accepted_bath_cannot_be_called_off(self):
        """Stock moved; that is an adjustment with a reason, not a button."""
        self.client.post(self.url, {"done": [str(self.rows[0].pk)]})

        self.client.post(self.url, {"cancel": str(self.rows[0].pk)})

        self.rows[0].refresh_from_db()
        self.assertTrue(self.rows[0].is_accepted)
        self.assertIsNone(self.rows[0].cancelled_at)

    def test_a_row_from_another_sheet_cannot_be_touched(self):
        other = ProductionRun.objects.create()
        stranger = ProductionRunRow.objects.create(
            run=other, finished_product=self.product, order=1, quantity=4
        )

        self.client.post(self.url, {"cancel": str(stranger.pk)})

        stranger.refresh_from_db()
        self.assertIsNone(stranger.cancelled_at)

    def test_an_unreadable_id_does_nothing_rather_than_raising(self):
        response = self.client.post(self.url, {"cancel": "not-a-number"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.run.cancelled_count, 0)

    def test_the_page_offers_it(self):
        response = self.client.get(self.url)

        self.assertContains(response, "not coming")
        self.assertContains(response, "The rest isn't coming")

    def test_a_finished_sheet_stops_offering_the_rest(self):
        self.client.post(self.url, {"cancel_rest": "1"})

        response = self.client.get(self.url)

        self.assertNotContains(response, "The rest isn't coming")
        self.assertContains(response, "undo")

    def test_cancelling_records_who_replied(self):
        """A cancel is a reply too — somebody picked the phone up."""
        self.client.post(self.url, {"cancel": str(self.rows[0].pk)})

        self.run.refresh_from_db()
        self.assertIsNotNone(self.run.submitted_at)
class FancyAtProductionTests(TestCase):
    """Routing a bath's output to fancy at the moment it is made.

    The conversion page records the other route — a plain scarf already in
    stock that had line work added later — which has to be noticed and
    leaves the plain side overstated until it is. Deciding it here is better
    evidence and nearly free: the scarf is never counted as plain at all.
    """

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_bathable(
            self.recipe, "Half Circle Veil", on_hand=0, par=8, bath=5
        )
        self.plain_blank = self.product.raw_product
        # The fancy counterpart: same colorway, a blank no bath makes.
        self.fancy_blank = RawProduct.objects.create(
            name="Fancy Half Circle Veil",
            category=self.plain_blank.category,
            price=self.plain_blank.price,
            made_in_a_dye_bath=False,
            number_per_dye_bath=5,
        )
        self.fancy_product = FinishedProduct.objects.create(
            name="Fancy Half Circle Veil - Stormy Sea",
            raw_product=self.fancy_blank,
            recipe=self.recipe,
            price=self.product.price,
            par=0,
        )
        self.plain_blank.fancy_counterpart = self.fancy_blank
        self.plain_blank.save(update_fields=["fancy_counterpart"])

        self.run = ProductionRun.objects.create()
        self.row = ProductionRunRow.objects.create(
            run=self.run, finished_product=self.product, order=1, quantity=5
        )
        self.url = reverse("production_run", args=[self.run.token])

    def _report(self, **extra):
        data = {"done": [str(self.row.pk)]}
        data.update(extra)
        return self.client.post(self.url, data)

    def test_the_counterpart_is_found_one_to_one(self):
        self.assertEqual(self.row.fancy_target, self.fancy_product)

    def test_four_plain_and_one_fancy(self):
        """The worked case: a bath of five, one delivered fancy."""
        self._report(**{
            f"yielded-{self.row.pk}": "5", f"fancy-{self.row.pk}": "1",
        })

        self.product.refresh_from_db()
        self.fancy_product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 4)
        self.assertEqual(self.fancy_product.number_on_hand, 1)

    def test_the_blanks_are_consumed_the_same_either_way(self):
        """A fancy veil is a plain scarf with line work, so the bath still
        ate five blanks off the shelf."""
        self._report(**{
            f"yielded-{self.row.pk}": "5", f"fancy-{self.row.pk}": "1",
        })

        self.plain_blank.refresh_from_db()
        self.assertEqual(self.plain_blank.number_on_hand, 95)

    def test_fancy_is_a_subset_so_nothing_reads_as_lost(self):
        self._report(**{
            f"yielded-{self.row.pk}": "5", f"fancy-{self.row.pk}": "1",
        })

        self.row.refresh_from_db()
        self.assertEqual(self.row.yielded, 5)
        self.assertEqual(self.row.fancy_yield, 1)
        self.assertEqual(self.row.loss, 0)

    def test_a_short_bath_and_a_fancy_one_are_separate_questions(self):
        """Four of five came out, and one of those four is fancy."""
        self._report(**{
            f"yielded-{self.row.pk}": "4", f"fancy-{self.row.pk}": "1",
        })

        self.row.refresh_from_db()
        self.product.refresh_from_db()
        self.fancy_product.refresh_from_db()
        self.assertEqual(self.row.loss, 1)
        self.assertEqual(self.product.number_on_hand, 3)
        self.assertEqual(self.fancy_product.number_on_hand, 1)

    def test_fancy_cannot_exceed_what_came_out(self):
        self._report(**{
            f"yielded-{self.row.pk}": "2", f"fancy-{self.row.pk}": "5",
        })

        self.row.refresh_from_db()
        self.fancy_product.refresh_from_db()
        self.assertEqual(self.row.fancy_yield, 2)
        self.assertEqual(self.fancy_product.number_on_hand, 2)

    def test_a_whole_bath_can_go_out_fancy(self):
        """And the plain log is still written, at zero, because it is what
        `applied_log` points at and so what stops a second submission."""
        self._report(**{
            f"yielded-{self.row.pk}": "5", f"fancy-{self.row.pk}": "5",
        })

        self.row.refresh_from_db()
        self.product.refresh_from_db()
        self.assertTrue(self.row.is_accepted)
        self.assertEqual(self.product.number_on_hand, 0)
        self.assertEqual(
            InventoryLog.objects.filter(finished_product=self.product).count(), 1
        )

    def test_the_fancy_units_are_production_not_a_conversion(self):
        """Nothing was converted — this scarf was never plain."""
        self._report(**{
            f"yielded-{self.row.pk}": "5", f"fancy-{self.row.pk}": "1",
        })

        log = InventoryLog.objects.get(finished_product=self.fancy_product)
        self.assertEqual(log.log_type, InventoryLog.PRODUCTION)
        self.assertEqual(log.source, InventoryLog.SOURCE_PRODUCTION_SHEET)
        self.assertEqual(log.quantity, 1)

    def test_reporting_twice_routes_it_once(self):
        self._report(**{
            f"yielded-{self.row.pk}": "5", f"fancy-{self.row.pk}": "1",
        })
        self._report(**{
            f"yielded-{self.row.pk}": "5", f"fancy-{self.row.pk}": "1",
        })

        self.fancy_product.refresh_from_db()
        self.assertEqual(self.fancy_product.number_on_hand, 1)

    def test_no_fancy_box_where_there_is_no_counterpart(self):
        """Every yarn row, and most silk — and nothing had to check for
        silk to get there."""
        yarn = make_bathable(
            make_recipe("Aegean"), "Heavenly Yarn", on_hand=0, par=8, bath=4
        )
        row = ProductionRunRow.objects.create(
            run=self.run, finished_product=yarn, order=2, quantity=4
        )

        self.assertIsNone(row.fancy_target)

        response = self.client.get(self.url)
        self.assertNotContains(response, f'name="fancy-{row.pk}"')

    def test_a_stray_fancy_count_on_such_a_row_is_ignored(self):
        """There is nowhere for it to go, so it must not silently vanish
        into the plain count either."""
        yarn = make_bathable(
            make_recipe("Aegean"), "Heavenly Yarn", on_hand=0, par=8, bath=4
        )
        row = ProductionRunRow.objects.create(
            run=self.run, finished_product=yarn, order=2, quantity=4
        )

        self.client.post(self.url, {
            "done": [str(row.pk)], f"yielded-{row.pk}": "4", f"fancy-{row.pk}": "2",
        })

        row.refresh_from_db()
        yarn.refresh_from_db()
        self.assertEqual(row.fancy_yield, 0)
        self.assertEqual(yarn.number_on_hand, 4)

    def test_the_fancy_box_is_offered_where_there_is_one(self):
        response = self.client.get(self.url)

        self.assertContains(response, f'name="fancy-{self.row.pk}"')
        self.assertContains(response, "Fancy Half Circle Veil")

    def test_a_plain_report_still_works_untouched(self):
        """No fancy field posted at all — the overwhelmingly common case."""
        self._report()

        self.row.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.row.fancy_yield, 0)
        self.assertEqual(self.product.number_on_hand, 5)
class PrintSubmitsTheListItselfTests(TestCase):
    """One form. Print posts the same inputs the counts are typed into.

    There used to be two — a GET list and a separate POST carrying the
    server's copy of it — so a count typed into a box but not yet synced
    never reached Print. An "Update the list" button existed purely to close
    that gap, and with htmx it was never used, because changing a count
    already refreshed.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.product = make_bathable(
            make_recipe("Stormy Sea"), "Stormy Silk", on_hand=0, par=40, bath=4
        )
        self.url = reverse("production_sheet_index")

    def test_an_unsynced_count_still_reaches_the_print(self):
        """The gap the button existed to cover, now closed by the form."""
        self.client.post(self.url, {
            "items": [f"{self.product.pk}:2"],
            f"qty-{self.product.pk}": "5",
        })

        self.assertEqual(ProductionRun.objects.get().rows.count(), 5)

    def test_the_update_button_is_gone(self):
        response = self.client.get(self.url, {"items": [f"{self.product.pk}:2"]})

        self.assertNotContains(response, "Update the list")

    def test_print_belongs_to_the_list_form(self):
        response = self.client.get(self.url, {"items": [f"{self.product.pk}:2"]})

        self.assertContains(response, 'form="sheet-list" formmethod="post"')

    def test_the_sheet_carries_exactly_one_copy_of_the_list(self):
        """The print form used to hold a second copy, and two copies are two
        things to fall out of step.

        Checked on the fragment rather than the page: the search form keeps
        its own hidden copy on purpose, so a "Find" with the script blocked
        doesn't lose the list. That one is never read on the htmx path, which
        includes `#sheet-list` instead.
        """
        response = self.client.get(
            self.url, {"items": [f"{self.product.pk}:2"]}, HTTP_HX_REQUEST="true"
        )

        self.assertEqual(
            response.content.decode().count(f'value="{self.product.pk}:2"'), 1
        )

    def test_the_list_form_stays_a_get(self):
        """Enter in a count box, and the add button's no-script fallback,
        must refresh rather than print a sheet nobody asked for."""
        response = self.client.get(self.url, {"items": [f"{self.product.pk}:2"]})

        self.assertContains(response, '<form method="get" id="sheet-list">')

    def test_adding_by_name_never_prints(self):
        self.client.get(self.url, {
            "items": [f"{self.product.pk}:2"], "add": str(self.product.pk),
        })

        self.assertEqual(ProductionRun.objects.count(), 0)

    def test_the_csrf_token_stays_out_of_the_swap(self):
        """It rides in the list form so Print can post it, but a swap and the
        URL it pushes have no business carrying a session token."""
        response = self.client.get(self.url, {"items": [f"{self.product.pk}:2"]})

        self.assertContains(response, 'hx-params="not csrfmiddlewaretoken"')
class TheSheetAndTheListAgreeTests(TestCase):
    """The picker orders the same way the page you read it on does.

    Somebody reads production-needed, then asks the picker for the first N
    baths. If the two sort differently they get N baths that are not the ones
    they were looking at, and nothing on either page says so.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        # Sells well, only slightly short.
        self.mover = make_bathable(
            make_recipe("Ember"), "Heavenly", on_hand=4, par=12, bath=4
        )
        # Sells nothing, completely out — what par ranks first.
        self.dud = make_bathable(
            make_recipe("Wasteland"), "Homespun", on_hand=0, par=12, bath=4
        )
        self._sell(self.mover, 40)

    def _sell(self, product, units):
        sale = Sale.objects.create(
            order_id=f"o{product.pk}", sold_at=timezone.now(),
            source=Sale.SOURCE_SQUARE_API,
        )
        SaleLine.objects.create(
            sale=sale, line_key=f"k{product.pk}", sold_at=timezone.now(),
            item_name=product.raw_product.name, price_point=product.recipe.name,
            quantity=units, finished_product=product,
            raw_product=product.raw_product, source=Sale.SOURCE_SQUARE_API,
        )

    def test_the_sheet_leads_with_the_seller_by_default(self):
        baths = production.plan_baths(10)

        self.assertEqual(baths[0].recipe_name, "Ember")

    def test_the_first_bath_matches_the_first_row_of_the_list(self):
        """The actual defect this fixes: two pages, one question."""
        listed = self.client.get(reverse("production_needed")).context["groups"]
        baths = production.plan_baths(10)

        self.assertEqual(baths[0].recipe_name, listed[0]["recipe_name"])

    def test_par_ordering_is_still_available_and_differs(self):
        baths = production.plan_baths(10, order=production.ORDER_PAR)

        self.assertEqual(baths[0].recipe_name, "Wasteland")

    def test_the_picker_passes_the_choice_through(self):
        response = self.client.get(
            reverse("production_sheet_index"), {"baths": "10", "order": "par"})

        self.assertEqual(
            response.context["baths"][0].recipe_name, "Wasteland")

    def test_an_empty_shelf_still_leads_among_equal_sellers(self):
        """Sales are the primary key, not the only one — a colorway a
        customer cannot buy still leads its peers."""
        empty = make_bathable(
            make_recipe("Aegean"), "Artisan", on_hand=0, par=12, bath=4
        )
        stocked = make_bathable(
            make_recipe("Rosy"), "Noble", on_hand=4, par=12, bath=4
        )
        self._sell(empty, 5)
        self._sell(stocked, 5)

        names = [b.recipe_name for b in production.plan_baths(20)]

        self.assertLess(names.index("Aegean"), names.index("Rosy"))

    def test_a_colour_nobody_buys_does_not_jump_the_queue_for_being_empty(self):
        names = [b.recipe_name for b in production.plan_baths(20)]

        self.assertLess(names.index("Ember"), names.index("Wasteland"))
class ProductionNeededRanksOnSalesTests(TestCase):
    """What sells outranks what merely crossed par.

    Par was never dialled in and reads as a uniform remnant, so ordering this
    list by shortage ranks it on a number nobody chose — a colorway that sold
    three all season above one that sold forty, purely for crossing an
    arbitrary line first. Sales are measured, so until par means something
    they are the better claim on a dye pot.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.url = reverse("production_needed")
        # Sells well, only just short.
        self.mover = make_bathable(
            make_recipe("Ember"), "Heavenly", on_hand=7, par=8, bath=4
        )
        # Sells nothing, miles short — the one the old order put first.
        self.dud = make_bathable(
            make_recipe("Wasteland"), "Homespun", on_hand=0, par=8, bath=4
        )

    def _sell(self, product, units):
        sale = Sale.objects.create(
            order_id=f"o{product.pk}", sold_at=timezone.now(),
            source=Sale.SOURCE_SQUARE_API,
        )
        SaleLine.objects.create(
            sale=sale, line_key=f"k{product.pk}", sold_at=timezone.now(),
            item_name=product.raw_product.name, price_point=product.recipe.name,
            quantity=units, finished_product=product,
            raw_product=product.raw_product, source=Sale.SOURCE_SQUARE_API,
        )

    def _order(self, **params):
        groups = self.client.get(self.url, params).context["groups"]
        return [g["recipe_name"] for g in groups]

    def test_the_seller_leads_by_default(self):
        self._sell(self.mover, 40)

        self.assertEqual(self._order()[0], "Ember")

    def test_the_old_ordering_is_still_one_click_away(self):
        self._sell(self.mover, 40)

        self.assertEqual(self._order(sort="shortage")[0], "Wasteland")

    def test_neither_ordering_drops_anything(self):
        """A sort changes what is read first, never what exists."""
        self._sell(self.mover, 40)

        self.assertEqual(set(self._order()), set(self._order(sort="shortage")))

    def test_the_count_is_pooled_across_blanks(self):
        """A recipe is dyed as a colorway, so it is ranked as one."""
        other = make_bathable(
            self.mover.recipe, "Artisan", on_hand=1, par=8, bath=4
        )
        self._sell(self.mover, 10)
        self._sell(other, 15)

        groups = {g["recipe_name"]: g for g in
                  self.client.get(self.url).context["groups"]}

        self.assertEqual(groups["Ember"]["units_sold"], 25)

    def test_the_number_is_shown_so_the_ranking_can_be_checked(self):
        self._sell(self.mover, 40)

        response = self.client.get(self.url)

        self.assertContains(response, "sold 40")

    def test_it_reads_the_same_figure_the_slow_sellers_page_reports(self):
        """One answer to 'what sold' — two would let the page that orders by
        it disagree with the page that reports it."""
        self._sell(self.mover, 12)
        rng = slowsellers.season_range({})

        groups = {g["recipe_name"]: g for g in
                  self.client.get(self.url).context["groups"]}

        self.assertEqual(
            groups["Ember"]["units_sold"],
            slowsellers.sold_by_recipe(rng)[self.mover.recipe_id],
        )

    def test_a_colorway_that_sold_nothing_is_still_listed(self):
        """Ranked last, not hidden — it may simply be new, and this page is
        not where that gets decided."""
        self._sell(self.mover, 40)

        self.assertIn("Wasteland", self._order())
class AddingAColorwayByNameTests(TestCase):
    """Clicking a search result puts it on the list.

    This broke once already and the way it broke is the thing to guard: the
    click handler wrote into a client-side table that a later rework deleted,
    so the button did nothing and the network panel stayed silent. Adding is
    now an ordinary submit into the list form, which cannot go quiet — either
    the page navigates or nothing was clicked.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.stormy = make_bathable(
            make_recipe("Stormy Sea"), "Stormy Silk", on_hand=0, par=8, bath=4
        )
        self.ember = make_bathable(
            make_recipe("Ember"), "Ember Silk", on_hand=40, par=8, bath=5
        )
        self.url = reverse("production_sheet_index")

    def test_adding_to_an_empty_page(self):
        """The form has to exist before there is a list, or the first row
        can never be added."""
        response = self.client.get(self.url, {"add": str(self.ember.pk)})

        self.assertContains(response, "Ember")
        self.assertContains(response, "1 bath")

    def test_adding_to_a_list_that_already_has_rows(self):
        response = self.client.get(self.url, {
            "items": [f"{self.stormy.pk}:2"],
            f"qty-{self.stormy.pk}": "2",
            "add": str(self.ember.pk),
        })

        self.assertContains(response, "Stormy Sea")
        self.assertContains(response, "Ember")
        self.assertContains(response, "3 baths")

    def test_adding_one_already_on_the_list_bumps_it(self):
        """And this is the case the old wire format got wrong: a second
        `items` entry was summed and then overwritten by the count box's
        older number, so the add silently did nothing."""
        response = self.client.get(self.url, {
            "items": [f"{self.stormy.pk}:2"],
            f"qty-{self.stormy.pk}": "2",
            "add": str(self.stormy.pk),
        })

        self.assertContains(response, "3 baths")

    def test_an_edit_and_an_add_in_one_submit_both_land(self):
        response = self.client.get(self.url, {
            "items": [f"{self.stormy.pk}:2"],
            f"qty-{self.stormy.pk}": "5",
            "add": str(self.ember.pk),
        })

        self.assertContains(response, "6 baths")

    def test_an_added_colorway_reaches_the_printed_sheet(self):
        self.client.post(self.url, {
            "items": [f"{self.stormy.pk}:1"],
            "add": str(self.ember.pk),
        })

        run = ProductionRun.objects.get()
        names = {r.finished_product.recipe.name for r in run.rows.all()}
        self.assertEqual(names, {"Stormy Sea", "Ember"})

    def test_something_that_cannot_be_dyed_is_refused_by_name_too(self):
        passthrough = make_bathable(None, "Undyed Skein", on_hand=5, par=8, bath=4)

        self.client.post(self.url, {"add": str(passthrough.pk)})

        self.assertEqual(ProductionRun.objects.count(), 0)

    def test_an_unreadable_add_does_nothing_rather_than_raising(self):
        response = self.client.get(self.url, {"add": "not-a-number"})

        self.assertEqual(response.status_code, 200)

    def test_the_results_submit_into_the_list_form(self):
        """The wiring that replaced the handler: without `form=` the button
        sits outside the list form and posts nothing."""
        response = self.client.get(
            reverse("product_search"), {"q": "Ember", "mode": "plan"}
        )

        self.assertContains(response, 'form="sheet-list"')
        self.assertContains(response, 'name="add"')

    def test_the_list_form_is_rendered_even_when_empty(self):
        response = self.client.get(self.url)

        self.assertContains(response, 'id="sheet-list"')

    def test_the_search_renders_inline_with_no_script(self):
        response = self.client.get(self.url, {"q": "Ember"})

        self.assertContains(response, "Ember")
        self.assertContains(response, 'name="add"')

    def test_searching_does_not_lose_the_list(self):
        response = self.client.get(self.url, {
            "items": [f"{self.stormy.pk}:2"], "q": "Ember",
        })

        self.assertContains(response, "Stormy Sea")
        self.assertContains(response, "2 baths")
class TheListIsEditableHoweverItWasSeededTests(TestCase):
    """One list, two ways to fill it, and every row editable either way.

    The version this replaced had two modes: a suggested list you could only
    look at, and a picked list you could edit — with a read-only preview of
    one sitting underneath the editable copy of the other. A suggestion you
    cannot change is a suggestion somebody works around on paper.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.short = make_bathable(
            make_recipe("Stormy Sea"), "Stormy Silk", on_hand=0, par=8, bath=4
        )
        self.also = make_bathable(
            make_recipe("Ember"), "Ember Silk", on_hand=0, par=10, bath=5
        )
        self.url = reverse("production_sheet_index")

    def test_a_suggestion_comes_back_as_an_editable_list(self):
        response = self.client.get(self.url, {"baths": "20"})

        # A count box and a remove link per row, not a read-only preview.
        self.assertContains(response, f'name="qty-{self.short.pk}"')
        self.assertContains(response, "Take this off the list")

    def test_a_suggested_row_can_be_removed(self):
        """The ✕ is a plain link to the list without that row, so it works
        with the script blocked."""
        response = self.client.get(
            self.url,
            {"items": [f"{self.short.pk}:2", f"{self.also.pk}:2"]},
        )
        self.assertContains(response, "Ember")

        # The link the page rendered for dropping Ember.
        response = self.client.get(self.url, {"items": [f"{self.short.pk}:2"]})

        self.assertNotContains(response, "Ember")
        self.assertContains(response, "Stormy Sea")

    def test_a_suggested_count_can_be_edited(self):
        response = self.client.get(self.url, {
            "items": [f"{self.short.pk}:2"],
            f"qty-{self.short.pk}": "5",
        })

        self.assertContains(response, "5 baths")

    def test_editing_to_zero_drops_the_row(self):
        """Typing it away has to mean the same as the ✕, not a bath of
        nothing."""
        response = self.client.get(self.url, {
            "items": [f"{self.short.pk}:2", f"{self.also.pk}:2"],
            f"qty-{self.short.pk}": "0",
        })

        self.assertNotContains(response, "Stormy Sea")
        self.assertContains(response, "Ember")

    def test_an_edit_is_what_gets_printed(self):
        self.client.post(self.url, {
            "items": [f"{self.short.pk}:2"],
            f"qty-{self.short.pk}": "3",
        })

        self.assertEqual(ProductionRun.objects.get().rows.count(), 3)

    def test_the_collection_plan_follows_the_edit(self):
        """Every edit is a round trip precisely so this can't go stale — a
        short dye list sends somebody to the shelf for the wrong things."""
        two = self.client.get(self.url, {"items": [f"{self.short.pk}:2"]})
        five = self.client.get(self.url, {
            "items": [f"{self.short.pk}:2"], f"qty-{self.short.pk}": "5",
        })

        self.assertContains(two, "2 baths")
        self.assertContains(five, "5 baths")

    def test_there_is_only_one_table_of_rows(self):
        """The wonk being fixed: an editable list and a read-only preview of
        the same thing, one under the other."""
        response = self.client.get(self.url, {"baths": "20"})

        self.assertEqual(response.content.decode().count("<tbody>"), 1)

    def test_an_old_bare_baths_link_still_works(self):
        """`?baths=20` predates the list and must still resolve — now
        editable rather than as something to look at."""
        response = self.client.get(self.url, {"baths": "20"})

        self.assertContains(response, "Stormy Sea")
        self.assertContains(response, "Print this sheet")

    def test_a_bare_page_asks_nothing_and_says_nothing(self):
        response = self.client.get(self.url)

        self.assertNotContains(response, "Print this sheet")
        self.assertNotContains(response, "Nothing is below par")
class HandPickedSheetTests(TestCase):
    """Creating a run from colorways somebody chose, not from par.

    Par is not the only reason to dye — an order taken at the stall, a colour
    worth trying, room beside a pot already being heated. And a planner that
    can only answer "what is below par" cannot plan a session at all when
    nothing is short, which is exactly when there is time for one.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        # Deliberately *above* par: no shortage query would ever offer these.
        self.stormy = make_bathable(
            make_recipe("Stormy Sea"), "Stormy Silk", on_hand=40, par=8, bath=4
        )
        self.ember = make_bathable(
            make_recipe("Ember"), "Ember Silk", on_hand=40, par=8, bath=5
        )
        self.url = reverse("production_sheet_index")

    def _pick(self, *pairs, **extra):
        data = {"items": [f"{p.pk}:{n}" for p, n in pairs]}
        data.update(extra)
        return data

    def test_the_shortage_planner_offers_nothing_here(self):
        """The premise: everything is above par."""
        self.assertEqual(production.plan_baths(10), [])

    def test_a_sheet_can_be_printed_when_nothing_is_short(self):
        response = self.client.post(self.url, self._pick((self.stormy, 2)))

        run = ProductionRun.objects.get()
        self.assertEqual(run.rows.count(), 2)
        self.assertRedirects(
            response, reverse("production_run_detail", args=[run.pk])
        )

    def test_the_count_is_baths_not_scarves(self):
        """Two baths of a blank yielding four is two rows of four, because a
        row is a bath and a bath is what somebody physically does."""
        self.client.post(self.url, self._pick((self.stormy, 2)))

        rows = list(ProductionRun.objects.get().rows.all())
        self.assertEqual([r.quantity for r in rows], [4, 4])

    def test_baths_of_one_colorway_stay_together(self):
        """One mix and one pot serve several loads."""
        self.client.post(
            self.url, self._pick((self.stormy, 2), (self.ember, 1), (self.stormy, 1))
        )

        names = [
            r.finished_product.recipe.name
            for r in ProductionRun.objects.get().rows.all()
        ]
        self.assertEqual(names, ["Stormy Sea"] * 3 + ["Ember"])

    def test_the_preview_shows_what_was_picked(self):
        response = self.client.get(self.url, self._pick((self.ember, 2)))

        self.assertContains(response, "Ember")
        self.assertContains(response, "2 baths")

    def test_a_pick_wins_over_a_suggestion(self):
        """A suggestion seeds the list; once it is a list, it is the list."""
        make_bathable(make_recipe("Rosy"), "Rosy Silk", on_hand=0, par=8, bath=4)

        response = self.client.get(
            self.url, self._pick((self.ember, 1), **{"baths": "20"})
        )

        self.assertContains(response, "Ember")
        self.assertNotContains(response, "Rosy")

    def test_a_pick_is_a_link_somebody_can_send(self):
        """State in the query string, like every other picker here."""
        response = self.client.get(f"{self.url}?items={self.stormy.pk}:3")

        self.assertContains(response, "3 baths")

    def test_an_empty_pick_is_refused_rather_than_printing_nothing(self):
        self.client.post(self.url, {"items": []})

        self.assertEqual(ProductionRun.objects.count(), 0)

    def test_too_many_baths_of_one_colorway_is_refused(self):
        """A typo guard, not a policy: two digits in that box is almost
        always a number somebody meant to delete half of."""
        self.client.post(
            self.url,
            self._pick((self.stormy, PickedBathsField.MAX_PER_ITEM + 1)),
        )

        self.assertEqual(ProductionRun.objects.count(), 0)

    def test_more_baths_than_a_session_holds_is_refused(self):
        """Distinct from the per-colorway cap: each of these is individually
        fine and the sheet as a whole is not.

        How many colorways that takes is derived rather than assumed — it used
        to be a hardcoded four, which only reached the total cap while
        `MAX_PER_ITEM` was 20 and silently stopped testing anything when it
        moved.
        """
        per = PickedBathsField.MAX_PER_ITEM
        needed = ProductionSheetForm.MAX_BATHS // per + 1
        picks = [(self.stormy, per), (self.ember, per)]
        for i in range(needed - len(picks)):
            name = f"Filler {i}"
            picks.append((
                make_bathable(make_recipe(name), f"{name} Silk",
                              on_hand=40, par=8, bath=4),
                per,
            ))
        total = sum(n for _, n in picks)
        self.assertGreater(total, ProductionSheetForm.MAX_BATHS)

        self.client.post(self.url, self._pick(*picks))

        self.assertEqual(ProductionRun.objects.count(), 0)

    def test_the_two_caps_are_different_rules(self):
        """Guarding the test above: if MAX_PER_ITEM ever reaches MAX_BATHS,
        the total cap becomes unreachable and stops being tested."""
        self.assertLess(
            PickedBathsField.MAX_PER_ITEM, ProductionSheetForm.MAX_BATHS
        )

    def test_an_undyed_passthrough_cannot_be_picked(self):
        """You order those, you don't dye them."""
        passthrough = make_bathable(
            None, "Undyed Skein", on_hand=5, par=8, bath=4
        )

        self.client.post(self.url, self._pick((passthrough, 1)))

        self.assertEqual(ProductionRun.objects.count(), 0)

    def test_nothing_about_the_shortage_path_changed(self):
        """Every bookmark and every test predating the dataset field omits
        it, and must still ask the shortage question."""
        short = make_bathable(
            make_recipe("Rosy"), "Rosy Silk", on_hand=0, par=8, bath=4
        )

        self.client.post(self.url, {"baths": "2"})

        rows = list(ProductionRun.objects.get().rows.all())
        self.assertTrue(all(r.finished_product == short for r in rows))

    def test_the_search_offers_a_colorway_that_is_not_short(self):
        response = self.client.get(
            reverse("product_search"), {"q": "Ember", "mode": "plan"}
        )

        self.assertContains(response, "Ember")
        self.assertContains(response, f'value="{self.ember.pk}"')

    def test_the_search_greys_out_what_is_not_made_in_a_bath(self):
        """Shown rather than hidden: dropping it silently means somebody
        searches, doesn't see it, and never learns why."""
        make_bathable(None, "Undyed Skein", on_hand=5, par=8, bath=4)

        response = self.client.get(
            reverse("product_search"), {"q": "Undyed", "mode": "plan"}
        )

        self.assertContains(response, "disabled")
        self.assertContains(response, "ordered, not dyed")

    def test_picking_is_not_filtered_on_what_is_already_in_flight(self):
        """`plan_baths` subtracts live sheets; a pick is somebody deciding,
        and asking for it twice may be exactly what was meant."""
        self.client.post(self.url, self._pick((self.stormy, 1)))
        self.client.post(self.url, self._pick((self.stormy, 1)))

        self.assertEqual(ProductionRun.objects.count(), 2)
class SheetEditorTests(TestCase):
    """A plan somebody can edit.

    The planner only knows about shortages against par, and there are real
    reasons to dye something that isn't one — an order taken at the stall,
    room left in a pot already being heated. A plan that can't be edited gets
    worked around on paper, and then the paper and the app disagree about
    what the session was.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_bathable(
            self.recipe, "Stormy Silk", on_hand=0, par=8, bath=4
        )
        self.other = make_bathable(
            make_recipe("Ember"), "Ember Silk", on_hand=20, par=8, bath=5
        )
        self.run = ProductionRun.objects.create()
        self.row = ProductionRunRow.objects.create(
            run=self.run, finished_product=self.product, order=1, quantity=4
        )

    def test_a_bath_can_be_added_that_the_planner_would_never_pick(self):
        """`self.other` is above par, so no shortage query would offer it."""
        self.client.post(
            reverse("production_run_add_row", args=[self.run.pk]),
            {"product": self.other.pk},
        )

        self.assertEqual(self.run.rows.count(), 2)
        added = self.run.rows.order_by("-order").first()
        self.assertEqual(added.finished_product, self.other)

    def test_an_added_bath_takes_the_bath_size_as_its_quantity(self):
        self.client.post(
            reverse("production_run_add_row", args=[self.run.pk]),
            {"product": self.other.pk},
        )

        self.assertEqual(self.run.rows.order_by("-order").first().quantity, 5)

    def test_an_added_bath_is_appended_not_slotted_in(self):
        """`order` is the position on a printed sheet, so renumbering would
        make an existing printout disagree about which row is which."""
        self.client.post(
            reverse("production_run_add_row", args=[self.run.pk]),
            {"product": self.other.pk},
        )

        self.assertEqual(
            [r.order for r in self.run.rows.order_by("order")], [1, 2]
        )

    def test_striking_a_row_cancels_it_rather_than_deleting_it(self):
        """The run is a record — a deleted row would leave a line on a
        printed sheet the app has never heard of."""
        self.client.post(
            reverse("production_run_strike_row", args=[self.run.pk, self.row.pk])
        )

        self.row.refresh_from_db()
        self.assertTrue(self.row.is_cancelled)
        self.assertEqual(self.run.rows.count(), 1)

    def test_striking_moves_no_stock(self):
        self.client.post(
            reverse("production_run_strike_row", args=[self.run.pk, self.row.pk])
        )

        self.product.refresh_from_db()
        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 0)
        self.assertEqual(self.product.raw_product.number_on_hand, 100)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_an_accepted_row_cannot_be_struck(self):
        production.apply_row(self.row)

        self.client.post(
            reverse("production_run_strike_row", args=[self.run.pk, self.row.pk])
        )

        self.row.refresh_from_db()
        self.assertIsNone(self.row.cancelled_at)

    def test_a_row_from_another_sheet_cannot_be_struck(self):
        other_run = ProductionRun.objects.create()
        stranger = ProductionRunRow.objects.create(
            run=other_run, finished_product=self.product, order=1, quantity=4
        )

        response = self.client.post(
            reverse("production_run_strike_row", args=[self.run.pk, stranger.pk])
        )

        self.assertEqual(response.status_code, 404)
        stranger.refresh_from_db()
        self.assertIsNone(stranger.cancelled_at)

    def test_the_search_renders_inline_with_no_script(self):
        """Same partial the fragment returns, so the two cannot drift into
        the swapped copy posting somewhere the inline one doesn't."""
        response = self.client.get(
            reverse("production_run_detail", args=[self.run.pk]), {"q": "Ember"}
        )

        self.assertContains(response, "Ember")
        self.assertContains(
            response, reverse("production_run_add_row", args=[self.run.pk])
        )

    def test_the_fragment_serves_the_same_rows(self):
        response = self.client.get(
            reverse("product_search"),
            {"q": "Ember", "mode": "sheet", "run": self.run.pk},
        )

        self.assertContains(response, "Ember")
        self.assertContains(
            response, reverse("production_run_add_row", args=[self.run.pk])
        )

    def test_editing_needs_a_login(self):
        self.client.logout()

        self.client.post(
            reverse("production_run_add_row", args=[self.run.pk]),
            {"product": self.other.pk},
        )

        self.assertEqual(self.run.rows.count(), 1)
class CancelAllButNeverAcceptAllTests(TestCase):
    """Direction decides which bulk actions are allowed.

    Cancelling gives a claim back: nothing moves, and the colorways return to
    the next sheet. Accepting puts stock on the books for piles nobody
    looked at, and every one it gets wrong is then wrong on the pegs, at the
    close and in Square. So one of them is a button and the other has to cost
    a mark per row — the same bargain the restock board makes by having no
    "check all".
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_bathable(
            self.recipe, "Stormy Silk", on_hand=0, par=8, bath=4
        )
        self.run = ProductionRun.objects.create()
        self.rows = [
            ProductionRunRow.objects.create(
                run=self.run, finished_product=self.product, order=i, quantity=4
            )
            for i in (1, 2)
        ]
        self.url = reverse("production_run_cancel_remaining", args=[self.run.pk])

    def test_it_cancels_what_nobody_answered_for(self):
        self.client.post(self.url)

        self.run.refresh_from_db()
        self.assertEqual(self.run.cancelled_count, 2)
        self.assertTrue(self.run.is_closed)

    def test_it_moves_no_stock(self):
        self.client.post(self.url)

        self.product.refresh_from_db()
        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 0)
        self.assertEqual(self.product.raw_product.number_on_hand, 100)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_it_leaves_an_accepted_bath_alone(self):
        """Stock has moved on that row; taking it back is an adjustment."""
        production.apply_row(self.rows[0])

        self.client.post(self.url)

        self.rows[0].refresh_from_db()
        self.assertTrue(self.rows[0].is_accepted)
        self.assertIsNone(self.rows[0].cancelled_at)

    def test_the_cancelled_colorway_is_asked_for_again(self):
        self.client.post(self.url)

        self.assertEqual(len(production.plan_baths(10)), 2)

    def test_there_is_no_accept_all(self):
        """Pinned rather than merely absent, because it is exactly the
        convenience somebody reasonable asks for after a long session."""
        with self.assertRaises(NoReverseMatch):
            reverse("production_run_accept_remaining", args=[self.run.pk])

    def test_it_needs_a_login(self):
        self.client.logout()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 302)
        self.run.refresh_from_db()
        self.assertEqual(self.run.cancelled_count, 0)
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

    def test_a_run_does_not_delete(self):
        """It used to, on the reasoning that a run was scaffolding and the
        `InventoryLog` was the record.

        That held while a bath was an atomic event. It stopped holding once
        the sheet had to carry a one-to-three-day session: the ledger only
        records what *entered* inventory, so a bath that was cancelled and a
        bath that was never printed leave the same trace there — none. The
        rows are the only account of what the session was asked to do and
        what it found.
        """
        self.client.post(
            reverse("admin:scarves_productionrun_delete", args=[self.run.pk]),
            {"post": "yes"},
        )

        self.assertEqual(ProductionRun.objects.count(), 1)
        self.assertEqual(ProductionRunRow.objects.count(), 2)

    def test_a_run_cannot_be_typed_in_by_hand(self):
        """One would have a token on no paper and rows nobody was asked
        to dye."""
        response = self.client.get(reverse("admin:scarves_productionrun_add"))

        self.assertEqual(response.status_code, 403)

    def test_retiring_a_sheet_is_cancelling_it_not_deleting_it(self):
        for row in self.run.rows.all():
            production.cancel_row(row)

        self.run.refresh_from_db()
        self.assertTrue(self.run.is_closed)
        self.assertEqual(self.run.rows.count(), 2)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_the_list_says_how_much_was_reported(self):
        """How far a session got, at a glance, across every sheet."""
        production.apply_row(self.rows[0])

        response = self.client.get(reverse("admin:scarves_productionrun_changelist"))

        self.assertContains(response, "1 of 2")

    def test_the_token_can_be_edited(self):
        """It used to be read-only, on the reasoning that it is printed on
        paper and encoded in a QR so rewriting it orphans the sheet. That cost
        is real and it is not a reason to refuse — orphaning the paper is
        exactly what you want when a printed code has got out, and a rule that
        only stops the deliberate case is not a guard."""
        from scarves.admin import ProductionRunAdmin
        from django.contrib.admin.sites import site

        admin_obj = ProductionRunAdmin(ProductionRun, site)
        self.assertNotIn("token", admin_obj.get_readonly_fields(None, self.run))

    def test_revoking_shuts_the_crews_door_and_keeps_the_record(self):
        """The other way to kill a printed code, and the one to use when
        somebody may still be holding the paper: the token stays on the
        record and the page says what happened, where a rewritten token
        leaves them hunting for a character they think they mistyped."""
        self.run.revoked_at = timezone.now()
        self.run.save(update_fields=["revoked_at"])

        response = self.client.get(
            reverse("production_run", args=[self.run.token])
        )

        self.assertEqual(response.status_code, 410)
        self.assertContains(response, "revoked", status_code=410)
        self.assertTrue(ProductionRun.objects.filter(pk=self.run.pk).exists())

    def test_revoking_does_not_touch_the_work(self):
        """Revoking says nothing about what is pending, so the planner must go
        on subtracting a revoked sheet's baths — otherwise shutting a door
        quietly re-asks for a session somebody is still in the middle of."""
        before = self.run.pending_count

        self.run.revoked_at = timezone.now()
        self.run.save(update_fields=["revoked_at"])
        self.run.refresh_from_db()

        self.assertEqual(self.run.pending_count, before)
        self.assertTrue(self.run.counts_against_the_plan)

    def test_rows_are_not_editable_from_here(self):
        from scarves.admin import ProductionRunRowInline
        from django.contrib.admin.sites import site

        inline = ProductionRunRowInline(ProductionRun, site)
        self.assertFalse(inline.has_add_permission(None))
        self.assertFalse(inline.can_delete)
class StockoutAddsABathTests(TestCase):
    """A Sunday-night zero asks for one more bath. Nothing else does.

    **The one demand signal in the planner, and it is an event rather than an
    estimate.** A `CloseRunRow` answered at zero is the record that a product
    was sold out on a Sunday night — a physical count, on a named date — so
    `n = 1` is allowed to decide something, because nothing is inferring a
    rate from it.

    The rule it replaced went the other way and could not survive its own
    data: *skip a bath because this only sold one all season*. Measured on the
    live catalogue, 76 products qualified and **none of them** survived a 95%
    bound on their own sales figure, because long cover requires a low count
    by construction. Absence of sales over a handful of trading days supports
    almost no inference; a counted zero is not absence, it is a measurement.

    It is also denominated in the only unit that exists. A bath is atomic —
    there is no half bath, because the labour makes one not worth doing — and
    adding exactly `bath_size` to the target adds exactly one bath, since
    `ceil((n + b) / b) == ceil(n / b) + 1` for every n. A rule that had to be
    rounded into baths would be claiming a precision with nowhere to land.
    """

    def setUp(self):
        self.recipe = make_recipe("Cabernet")
        # Sitting exactly at par. Nothing but a stockout can put this on a
        # sheet, which is the case the rule exists for: par being adequate on
        # paper is precisely what selling out disproves.
        self.product = make_bathable(
            self.recipe, "Heavenly", on_hand=8, par=8, bath=5
        )

    def _close(self, product, counted, day=None):
        """One close carrying one answered row."""
        run = CloseRun.objects.create(day=day or timezone.localdate())
        CloseRunRow.objects.create(
            run=run,
            finished_product=product,
            on_hand_before=product.number_on_hand,
            display_slots=product.display_slots,
            counted=counted,
        )
        return run

    def _baths_for(self, product):
        """How many baths of this the planner would put on a sheet."""
        return sum(
            1 for bath in production.plan_baths(50, include_overshoot=True)
            if bath.product.pk == product.pk
        )

    # ---------------------------------------------------------------- fires

    def test_a_counted_zero_puts_a_product_at_par_on_the_sheet(self):
        """Par said it was fine. The shelf said otherwise, and the shelf wins.

        This is the whole reason the SQL prefilter had to widen: a product at
        or above par fails `number_on_hand < par` and would never reach the
        arithmetic that adds the bath.
        """
        self._close(self.product, counted=0)

        self.assertEqual(self._baths_for(self.product), 1)

    def test_it_adds_exactly_one_bath_to_a_product_already_short(self):
        short = make_bathable(
            make_recipe("Lilac Garden"), "Homespun", on_hand=0, par=8, bath=4
        )
        before = self._baths_for(short)
        self._close(short, counted=0)

        self.assertEqual(self._baths_for(short), before + 1)

    def test_the_bath_is_never_written_into_par(self):
        """Par stays a deliberate human decision; the bonus rides the ask."""
        self._close(self.product, counted=0)
        production.plan_baths(50, include_overshoot=True)
        self.product.refresh_from_db()

        self.assertEqual(self.product.par, 8)

    # ------------------------------------------------------------ stays out

    def test_a_row_counted_at_one_proposes_nothing(self):
        """One was sellable and nobody bought it — that is not unmet demand.

        The sharpest line in the rule, and the reason it stayed quiet on the
        shop's best-selling colorway the week it was written: all four blanks
        of it counted 1, not 0.
        """
        self._close(self.product, counted=1)

        self.assertEqual(self._baths_for(self.product), 0)

    def test_a_pending_row_proposes_nothing(self):
        """Nobody looked. That is never a zero.

        The close is routinely worked in passes and left part-done, so
        reading an unanswered row as a stockout would manufacture baths out
        of the pile somebody did not get to.
        """
        self._close(self.product, counted=None)

        self.assertEqual(self._baths_for(self.product), 0)

    def test_only_the_latest_close_proposes(self):
        """Each Sunday supersedes the last, which is what stops it compounding."""
        self._close(self.product, counted=0,
                    day=timezone.localdate() - timedelta(days=7))
        self._close(self.product, counted=4)

        self.assertEqual(self._baths_for(self.product), 0)

    def test_a_bath_already_on_paper_is_not_asked_for_twice(self):
        """`in_flight` nets it off, the same as any other claim on the plan."""
        self._close(self.product, counted=0)
        run = ProductionRun.objects.create()
        ProductionRunRow.objects.create(
            run=run, finished_product=self.product, order=1,
            quantity=self.product.bath_size,
        )

        self.assertEqual(self._baths_for(self.product), 0)
class SoldOnThisBlankIsOnTheRowTests(TestCase):
    """Pooled ranking is right, and blind in one specific way.

    A bath is planned in colorway units, so the sheet ranks on what the
    *colorway* sold across every blank it is dyed on. That is correct and
    stays. What it cannot see is a single blank of a hot colour sitting on a
    full shelf with no sales of its own — it rides up the list on its
    siblings.

    Measured on the live catalogue that is one or two rows in the first
    twenty: too few to justify a rule, too many to leave unsaid. So both
    numbers ride on the row and a person strikes it, which the sheet has
    always allowed. Nothing filters on them.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff2", password="pw"))
        self.recipe = make_recipe("Forest Fire")
        self.mover = make_bathable(self.recipe, "Homespun", on_hand=1, par=8, bath=4)
        self.stocked = make_bathable(self.recipe, "Artisan", on_hand=7, par=8, bath=4)

    def _sell(self, product, units):
        sale = Sale.objects.create(
            order_id=f"so{product.pk}", sold_at=timezone.now(),
            source=Sale.SOURCE_SQUARE_API,
        )
        SaleLine.objects.create(
            sale=sale, line_key=f"sk{product.pk}", sold_at=timezone.now(),
            item_name=product.raw_product.name, price_point=product.recipe.name,
            quantity=units, finished_product=product,
            raw_product=product.raw_product, source=Sale.SOURCE_SQUARE_API,
        )

    def test_the_row_carries_what_that_blank_sold_not_the_colorway_total(self):
        self._sell(self.mover, 13)

        rows = {p.pk: p for group in
                self.client.get(reverse("production_needed")).context["groups"]
                for p in group["items"]}

        self.assertEqual(rows[self.mover.pk].sold_here, 13)
        # The number that makes a stocked blank visible: it rode up here on
        # its colorway's 13, having sold none of its own.
        self.assertEqual(rows[self.stocked.pk].sold_here, 0)

    def test_the_sheet_picker_carries_it_too(self):
        """The picker builds its rows by hand, so it needs its own pin.

        The sheet's rows come from the posted list rather than from
        `candidates()`, so nothing attaches `sold_here` for them on that path.
        A key the view forgets renders as an empty cell rather than raising —
        the failure mode this codebase keeps naming — and an empty `Sold`
        column looks exactly like a blank that genuinely sold none.
        """
        self._sell(self.mover, 13)

        response = self.client.get(
            reverse("production_sheet_index"),
            {"items": f"{self.stocked.pk}:1"},
        )

        self.assertEqual(
            [row["sold_here"] for row in response.context["rows"]], [0]
        )

    def test_the_stocked_blank_is_still_listed(self):
        """Printed, never filtered — the ✕ is a person's call, not the app's."""
        self._sell(self.mover, 13)

        rows = {p.pk for group in
                self.client.get(reverse("production_needed")).context["groups"]
                for p in group["items"]}

        self.assertIn(self.stocked.pk, rows)
