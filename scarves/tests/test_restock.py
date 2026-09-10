"""The display map and the restock walk.

The reasoning behind these is in `docs/claude/restock.md`.
"""
from datetime import date, datetime, time, timedelta
from io import StringIO
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
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
    hang,
    make_close_product,
    make_recipe,
)


def make_board(name="Yarn Pegboard", rows=7, columns=6, capacity=2):
    """A fixture with a price tag where the real board has one.

    The pegs themselves arrive with the fixture — a `post_save` signal
    creates one per grid cell, so a board is usable however it was made. All
    that is left here is marking the reserved cells.
    """
    fixture = DisplayFixture.objects.create(
        name=name, rows=rows, columns=columns, capacity_per_position=capacity
    )
    # Middle two of the top row, as on the wall.
    middle = columns // 2
    DisplayPosition.objects.filter(
        fixture=fixture, row=1, column__in=(middle, middle + 1)
    ).update(reserved_label="Price tag")
    return fixture
class CopyBoardLayoutTests(TestCase):
    """The yarn boards are one pattern repeated per base, so copy it."""

    def setUp(self):
        self.category, _ = RawProductCategory.objects.get_or_create(name="Yarn")
        self.heavenly = RawProduct.objects.create(
            name="Heavenly", category=self.category, price="9.00"
        )
        self.homespun = RawProduct.objects.create(
            name="Homespun", category=self.category, price="9.00"
        )
        self.recipes = [make_recipe(n) for n in ("Aegean", "Ember", "Lichen")]

        self.source = make_board("Heavenly Board", rows=3, columns=3, capacity=2)
        self.source.raw_product = self.heavenly
        self.source.save(update_fields=["raw_product"])
        self.target = make_board("Homespun Board", rows=3, columns=3, capacity=2)
        self.target.raw_product = self.homespun
        self.target.save(update_fields=["raw_product"])

        for i, recipe in enumerate(self.recipes):
            hang(self.source, self._product(self.heavenly, recipe), 2, i + 1)

    def _product(self, blank, recipe):
        return FinishedProduct.objects.create(
            name=f"{recipe.name} {blank.name}", raw_product=blank,
            recipe=recipe, price="30.00",
        )

    def _run(self, **opts):
        out = StringIO()
        call_command(
            "copy_board_layout", "--from", self.source.name,
            "--to", self.target.name, stdout=out, **opts
        )
        return out.getvalue()

    def test_the_same_peg_gets_the_same_colorway_on_the_other_blank(self):
        for recipe in self.recipes:
            self._product(self.homespun, recipe)

        self._run()

        for i, recipe in enumerate(self.recipes):
            peg = self.target.positions.get(row=2, column=i + 1)
            self.assertEqual(peg.finished_product.recipe, recipe)
            self.assertEqual(peg.finished_product.raw_product, self.homespun)

    def test_it_sets_display_capacity_as_it_goes(self):
        product = self._product(self.homespun, self.recipes[0])
        self._run()

        product.refresh_from_db()
        self.assertEqual(product.display_slots, 2)

    def test_a_colorway_the_target_blank_lacks_is_named_not_invented(self):
        """Creating it would mean inventing a price, and a silently created
        row at a guessed price is the expensive kind of helpful."""
        self._product(self.homespun, self.recipes[0])

        output = self._run()

        self.assertIn("Ember", output)
        self.assertIn("Lichen", output)
        self.assertEqual(
            FinishedProduct.objects.filter(raw_product=self.homespun).count(), 1
        )
        self.assertFalse(
            self.target.positions.filter(row=2, column=2).first().finished_product
        )

    def test_work_already_on_the_target_is_left_alone(self):
        """A half-laid-out board is usually somebody's work in progress, and
        overwriting it is the one mistake here the page can't undo."""
        for recipe in self.recipes:
            self._product(self.homespun, recipe)
        mine = self._product(self.homespun, make_recipe("Hand Placed"))
        hang(self.target, mine, 2, 1)

        self._run()

        self.assertEqual(
            self.target.positions.get(row=2, column=1).finished_product, mine
        )
        self.assertEqual(
            self.target.positions.get(row=2, column=2).finished_product.recipe,
            self.recipes[1],
        )

    def test_overwrite_is_available_when_it_is_meant(self):
        for recipe in self.recipes:
            self._product(self.homespun, recipe)
        hang(self.target, self._product(self.homespun, make_recipe("Wrong")), 2, 1)

        self._run(overwrite=True)

        self.assertEqual(
            self.target.positions.get(row=2, column=1).finished_product.recipe,
            self.recipes[0],
        )

    def test_a_dry_run_writes_nothing(self):
        for recipe in self.recipes:
            self._product(self.homespun, recipe)

        output = self._run(dry_run=True)

        self.assertIn("Dry run", output)
        self.assertFalse(
            self.target.positions.filter(finished_product__isnull=False).exists()
        )

    def test_a_target_with_no_blank_is_refused(self):
        """The mapping is "same peg, same colorway, other blank" — without a
        target blank there is nothing to map onto."""
        self.target.raw_product = None
        self.target.save(update_fields=["raw_product"])

        with self.assertRaises(CommandError):
            self._run()

    def test_a_typo_in_a_board_name_lists_the_real_ones(self):
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command("copy_board_layout", "--from", "Heavnly",
                         "--to", self.target.name, stdout=out)
class DisplayMapTests(TestCase):
    """The board as data: what a peg is, and what it says about capacity."""

    def setUp(self):
        self.fixture = make_board()
        self.product = make_close_product("Aegean Sea", on_hand=5, slots=2)

    def test_hanging_a_colorway_sets_its_display_capacity(self):
        """The map is the source and `display_slots` is what everything reads.

        Two numbers that agree until somebody edits one of them is exactly
        the drift this avoids — and the symptom would be the Sunday close
        asking about the wrong products, silently.
        """
        hang(self.fixture, self.product, 2, 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.display_slots, 2)

        hang(self.fixture, self.product, 2, 2)
        self.product.refresh_from_db()
        self.assertEqual(self.product.display_slots, 4)

    def test_a_board_made_anywhere_gets_its_pegs(self):
        """**A fixture with no positions is a board that cannot be used.**

        The grid renders as dashes, the editor offers no dropdowns, and there
        is nothing to hang a colorway on — it looks like a board and does
        nothing. That was survivable while boards only came from
        `seed_display_board`; it stopped being so the moment one was made in
        the admin, which is the obvious way to make one.
        """
        made_plainly = DisplayFixture.objects.create(
            name="Made In The Admin", rows=3, columns=4, capacity_per_position=2
        )
        self.assertEqual(made_plainly.positions.count(), 12)
        self.assertTrue(
            all(cell is not None for row in made_plainly.grid() for cell in row)
        )

    def test_making_a_board_bigger_fills_in_the_new_pegs(self):
        """The normal reason to edit a board."""
        fixture = DisplayFixture.objects.create(name="Growing", rows=2, columns=2)
        self.assertEqual(fixture.positions.count(), 4)

        fixture.rows = 4
        fixture.save()

        self.assertEqual(fixture.positions.count(), 8)

    def test_shrinking_a_board_never_deletes_a_peg(self):
        """One of them may have a colorway on it, and quietly dropping that
        is worse than carrying a row `grid()` never reads."""
        fixture = DisplayFixture.objects.create(name="Shrinking", rows=3, columns=3)
        outlier = fixture.positions.get(row=3, column=3)
        outlier.finished_product = self.product
        outlier.save()

        fixture.rows = 2
        fixture.columns = 2
        fixture.save()

        self.assertTrue(fixture.positions.filter(pk=outlier.pk).exists())
        self.assertEqual(len(fixture.grid()), 2)

    def test_the_editor_offers_a_dropdown_for_every_peg_of_a_new_board(self):
        """The symptom that started this: 42 dashes and nothing to edit."""
        user = User.objects.create_user("newboard", password="pw")
        self.client.force_login(user)
        fresh = DisplayFixture.objects.create(name="Fresh", rows=2, columns=3)

        html = self.client.get(
            reverse("display_map", args=[fresh.pk])
        ).content.decode()
        self.assertEqual(html.count('name="peg_'), 6)

    def test_a_reserved_space_is_not_a_home(self):
        """The price tag takes up board space and must never read as a peg
        nobody has got round to assigning."""
        plate = self.fixture.positions.filter(reserved_label="Price tag").first()
        self.assertIsNotNone(plate)
        self.assertFalse(plate.is_home)

        rows = restock.board(self.fixture)
        kinds = {cell["kind"] for row in rows for cell in row}
        self.assertIn("reserved", kinds)

    def test_the_grid_fills_in_pegs_nobody_has_created(self):
        """A missing row and an unassigned peg are the same empty hook on the
        wall, so the template must not have to tell them apart."""
        rows = self.fixture.grid()
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(len(row) == 6 for row in rows))

    def test_a_colorway_on_no_board_keeps_its_capacity_rather_than_losing_it(self):
        """Zero means "never goes on display", which would drop it off the
        close. "Nobody has mapped this yet" is a different claim."""
        loose = make_close_product("Not Mapped", on_hand=3, slots=2)
        sync_display_slots(loose)
        loose.refresh_from_db()
        self.assertEqual(loose.display_slots, 2)
        self.assertFalse(loose.display_positions.exists())
class DisplayBoardGapTests(TestCase):
    """A board knows what blank it is for, and says what isn't up."""

    def setUp(self):
        self.fixture = make_board("Shawl Board")
        self.blank = RawProduct.objects.get_or_create(
            name="raw-Shawl A",
            category=RawProductCategory.objects.get_or_create(name="Silk")[0],
            defaults={"price": "5.00"},
        )[0]
        self.fixture.raw_product = self.blank
        self.fixture.save(update_fields=["raw_product"])

    def _colorway(self, name, on_hand=4):
        product = FinishedProduct.objects.create(
            name=name, raw_product=self.blank,
            recipe=make_recipe(f"{name} Recipe"), price="30.00",
        )
        FinishedProduct.objects.filter(pk=product.pk).update(
            number_on_hand=on_hand, display_slots=2
        )
        product.refresh_from_db()
        return product

    def test_the_gap_is_this_blanks_colorways_with_no_home(self):
        """The local question, which the global list cannot express.

        A Shawl with nowhere to hang reads the same on a global list as an
        undyed skein nobody has mapped — and the person standing at the Shawl
        board can only act on one of them.
        """
        up = self._colorway("Hung")
        down = self._colorway("Not Hung")
        hang(self.fixture, up, 2, 1)

        gap = list(restock.unmapped_for(self.fixture))
        self.assertIn(down, gap)
        self.assertNotIn(up, gap)

    def test_another_blanks_colorway_is_not_this_boards_problem(self):
        other_blank = RawProduct.objects.create(
            name="raw-Veil", category=self.blank.category, price="5.00"
        )
        stray = FinishedProduct.objects.create(
            name="Veil Colorway", raw_product=other_blank,
            recipe=make_recipe("Veil Recipe"), price="30.00",
        )
        self.assertNotIn(stray, list(restock.unmapped_for(self.fixture)))

    def test_a_colorway_on_a_second_board_is_not_reported_missing(self):
        """It is displayed. Listing it would be fussing about a thing done."""
        elsewhere = make_board("Overflow Board")
        product = self._colorway("On The Other Board")
        hang(elsewhere, product, 2, 1)

        self.assertNotIn(product, list(restock.unmapped_for(self.fixture)))

    def test_a_colorway_with_no_stock_is_still_reported(self):
        """An empty peg for a colorway you have none of is a decision about
        what to dye. Hiding it answers "what's missing?" with only the half
        somebody can fix this minute."""
        bare = self._colorway("None Made", on_hand=0)
        self.assertIn(bare, list(restock.unmapped_for(self.fixture)))

    def test_a_mixed_board_has_no_gap_report(self):
        """Blank means the board carries no single blank, so there is no such
        question to answer — and inventing one would name colorways that were
        never meant to be there."""
        mixed = make_board("Odds And Ends")
        self.assertEqual(list(restock.unmapped_for(mixed)), [])

    def test_the_gap_is_the_mappers_list_and_nobody_elses(self):
        """**Which colorways belong on a board is somebody's decision.**

        So it is offered on the editor, where that person is sitting with the
        empty pegs in front of them, and shown nowhere else. On the crew's
        board it told the wrong people about work they had no part in, and
        quietly asserted the app knew what ought to be hanging there.
        """
        missing = self._colorway("Nowhere To Hang")
        hang(self.fixture, self._colorway("Fine"), 2, 1)

        crew_board = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        self.assertNotIn(missing.recipe.name, crew_board)

        picker = self.client.get(reverse("restock_index")).content.decode()
        self.assertNotIn(missing.recipe.name, picker)

        user = User.objects.create_user("themapper", password="pw")
        self.client.force_login(user)
        editor = self.client.get(
            reverse("display_map", args=[self.fixture.pk])
        ).content.decode()
        self.assertIn(missing.recipe.name, editor)

    def test_a_board_links_to_every_other_board_and_back_out(self):
        """The stall is walked as one circuit, not board-by-board with a trip
        to a menu in between.

        Plain links rather than a dropdown: a `<select>` that needs a script
        to navigate does nothing on a bad connection, and the names fit.
        """
        other = make_board("Second Board")
        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()

        self.assertIn(reverse("restock_board", args=[other.pk]), html)
        self.assertIn(reverse("restock_index"), html)
        self.assertNotIn("<select name=\"board", html)

    def test_the_board_you_are_on_is_not_a_link_to_itself(self):
        make_board("Second Board")
        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        self.assertIn(f'<span class="here">{self.fixture.name}</span>', html)

    def test_the_picker_says_what_is_waiting_not_what_is_unbuilt(self):
        """**What decides which rack to do next.**

        Colorways with no home answer "what should we build one day", which
        is the least actionable thing on a page whose job is choosing the
        next job. That belongs on the board, beside the empty pegs.
        """
        stocked = self._colorway("Stocked", on_hand=8)
        peg = hang(self.fixture, stocked, 2, 1)
        self._colorway("Homeless Colorway")     # a gap, deliberately ignored

        walk = restock.open_pass(self.fixture)
        restock.record(walk, peg)
        for _ in range(2):
            InventoryLog.objects.create(
                finished_product=stocked, raw_product=stocked.raw_product,
                log_type=InventoryLog.SALE,
                source=InventoryLog.SOURCE_SQUARE_WEBHOOK, quantity=-1,
            )

        status = restock.board_status(self.fixture)
        self.assertEqual(status["bare"], 1)
        self.assertEqual(status["units"], 2)

        html = self.client.get(reverse("restock_index")).content.decode()
        self.assertIn("1 bare", html)
        self.assertNotIn("not on any board", html)

    def test_a_board_that_cannot_be_fixed_is_counted_quietly(self):
        """Walking over there changes nothing about it, so it must not read
        like a board that has been neglected."""
        thin = self._colorway("Nothing Behind It", on_hand=1)
        hang(self.fixture, thin, 2, 1)

        status = restock.board_status(self.fixture)
        self.assertEqual(status["unfillable"], 1)
        self.assertEqual(status["bare"], 0)
        self.assertEqual(status["units"], 0)

    def test_the_pull_list_is_one_trip_for_the_whole_stall(self):
        """Per product, never per peg — a colorway on three pegs is one bag
        to open. Sorted by SKU, which groups by blank, because that is how
        the bags are stood."""
        product = self._colorway("Spread Across Pegs", on_hand=12)
        pegs = [hang(self.fixture, product, 2, c) for c in (1, 2)]
        walk = restock.open_pass(self.fixture)
        for peg in pegs:
            restock.record(walk, peg)
        for _ in range(3):
            InventoryLog.objects.create(
                finished_product=product, raw_product=product.raw_product,
                log_type=InventoryLog.SALE,
                source=InventoryLog.SOURCE_SQUARE_WEBHOOK, quantity=-1,
            )

        pull = restock.pull_list()
        self.assertEqual(len(pull), 1)
        self.assertEqual(pull[0]["product"], product)
        self.assertEqual(pull[0]["boards"], [self.fixture.name])

        html = self.client.get(reverse("restock_index")).content.decode()
        self.assertIn("Bring from backstock", html)
        self.assertIn(product.recipe.name, html)

    def test_a_peg_never_asks_for_more_than_it_holds(self):
        """Five sold over two days still only takes what a peg holds; asking
        for five sends somebody to the bag for three with nowhere to go."""
        product = self._colorway("Sold Loads", on_hand=20)
        peg = hang(self.fixture, product, 2, 1)
        walk = restock.open_pass(self.fixture)
        restock.record(walk, peg)
        for _ in range(5):
            InventoryLog.objects.create(
                finished_product=product, raw_product=product.raw_product,
                log_type=InventoryLog.SALE,
                source=InventoryLog.SOURCE_SQUARE_WEBHOOK, quantity=-1,
            )

        self.assertEqual(restock.board_status(self.fixture)["units"], 2)

    def test_boards_with_bare_pegs_are_offered_first(self):
        """A recommendation by ordering, which somebody can ignore without
        being told off — not a badge that says a board is behind."""
        quiet = make_board("Quiet Board")
        quiet.raw_product = self.blank
        quiet.save(update_fields=["raw_product"])

        busy = self._colorway("Busy", on_hand=8)
        peg = hang(self.fixture, busy, 2, 1)
        walk = restock.open_pass(self.fixture)
        restock.record(walk, peg)
        for _ in range(2):
            InventoryLog.objects.create(
                finished_product=busy, raw_product=busy.raw_product,
                log_type=InventoryLog.SALE,
                source=InventoryLog.SOURCE_SQUARE_WEBHOOK, quantity=-1,
            )

        html = self.client.get(reverse("restock_index")).content.decode()
        self.assertLess(html.index(self.fixture.name), html.index(quiet.name))

    def test_boards_are_told_apart_by_name(self):
        second = make_board("Second Board")
        self.assertNotEqual(self.fixture.pk, second.pk)
        html = self.client.get(reverse("restock_index")).content.decode()
        self.assertIn("Shawl Board", html)
        self.assertIn("Second Board", html)
class DisplayMapEditorTests(TestCase):
    """Saying what hangs where — a staff desk job, and never a check."""

    def setUp(self):
        self.user = User.objects.create_user("mapper", password="pw")
        self.fixture = make_board("Shawl Board")
        self.blank = RawProduct.objects.get_or_create(
            name="raw-Editor Blank",
            category=RawProductCategory.objects.get_or_create(name="Silk")[0],
            defaults={"price": "5.00"},
        )[0]
        self.fixture.raw_product = self.blank
        self.fixture.save(update_fields=["raw_product"])
        self.product = self._colorway("Aegean Sea")
        # The pegs arrive with the fixture, so this one already exists.
        self.peg = self.fixture.positions.get(row=2, column=1)

    def _colorway(self, name):
        product = FinishedProduct.objects.create(
            name=name, raw_product=self.blank,
            recipe=make_recipe(f"{name} Recipe"), price="30.00",
        )
        FinishedProduct.objects.filter(pk=product.pk).update(number_on_hand=6)
        product.refresh_from_db()
        return product

    def _save(self, **pegs):
        """POST the page as a browser would.

        The board's name and blank share one form and one Save with the grid,
        so they are always on the wire — a test that posted only the pegs
        would be exercising a submission the page cannot produce.
        """
        data = {
            "board-name": self.fixture.name,
            "board-raw_product": self.fixture.raw_product_id or "",
            "board-capacity_per_position": self.fixture.capacity_per_position,
        }
        data.update(pegs)
        return self.client.post(
            reverse("display_map", args=[self.fixture.pk]), data, follow=True
        )

    def test_the_editor_needs_an_account(self):
        """Unlike the walk. Deciding what hangs where is a staff decision made
        sitting down; walking a board is done at the stall by people who have
        no login and shouldn't need one."""
        for name, args in (
            ("display_map_index", []),
            ("display_map", [self.fixture.pk]),
        ):
            response = self.client.get(reverse(name, args=args))
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login", response["Location"])

    def test_saving_the_map_assigns_and_updates_capacity(self):
        self.client.login(username="mapper", password="pw")
        self._save(**{f"peg_{self.peg.pk}": self.product.sku})

        self.peg.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.peg.finished_product, self.product)
        self.assertEqual(self.product.display_slots, 2)

    def test_saving_the_map_is_not_a_check(self):
        """**The whole reason this is a separate page.**

        Assigning a colorway to a peg says something about the map; ticking a
        peg says something about the stock, and only somebody standing in
        front of it can say it. One Save that quietly did both would record
        forty confirmations nobody made — and those confirmations are what
        the restock page's numbers rest on.
        """
        self.client.login(username="mapper", password="pw")
        response = self._save(**{f"peg_{self.peg.pk}": self.product.sku})

        self.assertEqual(RestockPass.objects.count(), 0)
        self.assertEqual(RestockCheck.objects.count(), 0)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.assertIn("no stock moved", response.content.decode())

    def test_clearing_a_peg_empties_it(self):
        self.client.login(username="mapper", password="pw")
        self._save(**{f"peg_{self.peg.pk}": self.product.sku})
        self.peg.refresh_from_db()
        self.assertEqual(self.peg.finished_product, self.product)

        self._save(**{f"peg_{self.peg.pk}": ""})

        self.peg.refresh_from_db()
        self.assertIsNone(self.peg.finished_product)

    def test_a_hand_built_post_cannot_hang_something_the_page_never_offered(self):
        """The scoping to this board's blank is a rule, not a convenience."""
        other_blank = RawProduct.objects.create(
            name="raw-Somewhere Else", category=self.blank.category, price="5.00"
        )
        stray = FinishedProduct.objects.create(
            name="Not Offered", raw_product=other_blank,
            recipe=make_recipe("Stray Recipe"), price="30.00",
        )

        self.client.login(username="mapper", password="pw")
        self._save(**{f"peg_{self.peg.pk}": stray.sku})

        self.peg.refresh_from_db()
        self.assertIsNone(self.peg.finished_product)

    def test_a_stray_already_hanging_stays_on_the_menu(self):
        """A board scoped to one blank can still be carrying something else,
        and a picker that omitted it would drop that assignment the first
        time anybody saved."""
        other_blank = RawProduct.objects.create(
            name="raw-Visiting", category=self.blank.category, price="5.00"
        )
        stray = FinishedProduct.objects.create(
            name="Visiting Colorway", raw_product=other_blank,
            recipe=make_recipe("Visiting Recipe"), price="30.00",
        )
        self.peg.finished_product = stray
        self.peg.save()

        self.client.login(username="mapper", password="pw")
        html = self.client.get(
            reverse("display_map", args=[self.fixture.pk])
        ).content.decode()
        self.assertIn(stray.recipe.name, html)

        # And saving the page unchanged leaves it where it is.
        self._save(**{f"peg_{self.peg.pk}": stray.sku})
        self.peg.refresh_from_db()
        self.assertEqual(self.peg.finished_product, stray)

    def test_the_board_can_be_renamed_and_pointed_at_a_blank(self):
        """Both discovered while looking at the grid — the board is
        mislabelled, or the menu is offering the wrong blank. Sending somebody
        to another screen is how a board keeps a name nobody meant."""
        self.client.login(username="mapper", password="pw")
        self.client.post(reverse("display_map", args=[self.fixture.pk]), {
            "board-name": "Rectangle Veil Board (left wall)",
            "board-raw_product": "",
            "board-capacity_per_position": self.fixture.capacity_per_position,
        })

        self.fixture.refresh_from_db()
        self.assertEqual(self.fixture.name, "Rectangle Veil Board (left wall)")
        self.assertIsNone(self.fixture.raw_product)

    def test_changing_the_hook_size_recalculates_every_colorway_on_it(self):
        """**A hook swap has to reach the colorways, not just the fixture.**

        `display_slots` is computed from capacity, so leaving it stale means
        the Sunday close asks about the wrong products — silently, which is
        the exact failure `sync_display_slots` exists to prevent.
        """
        hang(self.fixture, self.product, 2, 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.display_slots, 2)

        self.client.login(username="mapper", password="pw")
        self.client.post(reverse("display_map", args=[self.fixture.pk]), {
            "board-name": self.fixture.name,
            "board-raw_product": str(self.blank.pk),
            "board-capacity_per_position": "1",
        })

        self.fixture.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.fixture.capacity_per_position, 1)
        self.assertEqual(self.product.display_slots, 1)

    def test_a_bigger_hook_changes_nothing_about_production(self):
        """A ceiling, never a target. Somewhere to put stock is not a reason
        to make more of it — see the northstar in CLAUDE.md."""
        hang(self.fixture, self.product, 2, 1)
        before = self.product.par

        self.fixture.capacity_per_position = 4
        self.fixture.save()

        self.product.refresh_from_db()
        self.assertEqual(self.product.display_slots, 4)
        self.assertEqual(self.product.par, before)

    def test_switching_the_blank_never_assigns_pegs_in_the_same_save(self):
        """**The worst shape a bug here could have.**

        Every blank has an "Aegean Sea". Switch the blank, then pick a
        colorway off the menu still on screen, and you get a *different
        product with an identical label* — nothing on the page looks wrong.

        Telling somebody to save first leaves the hazard for whoever doesn't
        read it. Refusing makes it impossible: the blank is applied, the menus
        come back rebuilt, and the pegs are untouched and said to be.
        """
        other_blank = RawProduct.objects.create(
            name="raw-Other Blank", category=self.blank.category, price="5.00"
        )
        self.client.login(username="mapper", password="pw")

        response = self.client.post(
            reverse("display_map", args=[self.fixture.pk]),
            {
                "board-name": self.fixture.name,
                "board-raw_product": str(other_blank.pk),
                "board-capacity_per_position": self.fixture.capacity_per_position,
                f"peg_{self.peg.pk}": self.product.sku,
            },
            follow=True,
        )

        self.fixture.refresh_from_db()
        self.peg.refresh_from_db()
        self.assertEqual(self.fixture.raw_product, other_blank)
        self.assertIsNone(self.peg.finished_product)
        self.assertIn("left exactly as they were", response.content.decode())

    def test_pegs_save_normally_when_the_blank_is_unchanged(self):
        self.client.login(username="mapper", password="pw")
        self._save(**{f"peg_{self.peg.pk}": self.product.sku})

        self.peg.refresh_from_db()
        self.assertEqual(self.peg.finished_product, self.product)

    def test_a_mixed_board_gets_a_typeahead_and_one_shared_list(self):
        """**The scarf rack is a row per scarf type**, so a per-peg menu would
        carry the whole catalogue — unreadable, and rendered once per peg.

        A `<datalist>` is native type-ahead with no JavaScript, written once
        for the page. The colorway name alone can't tell a Shawl from a
        Rectangle Veil, so the blank is named in the label.
        """
        self.fixture.raw_product = None
        self.fixture.save(update_fields=["raw_product"])
        self.client.login(username="mapper", password="pw")

        html = self.client.get(
            reverse("display_map", args=[self.fixture.pk])
        ).content.decode()

        self.assertEqual(html.count('<datalist id="all-products">'), 1)
        self.assertIn(f'list="all-products" name="peg_{self.peg.pk}"', html)
        self.assertIn(f'{self.blank.name} — {self.product.recipe.name}', html)
        self.assertNotIn(f'<select name="peg_{self.peg.pk}"', html)

    def test_a_typo_in_the_box_is_named_rather_than_dropped(self):
        """A typed box invites a typo, and a peg that silently stayed as it
        was reads exactly like one that saved."""
        self.fixture.raw_product = None
        self.fixture.save(update_fields=["raw_product"])
        self.peg.finished_product = self.product
        self.peg.save()

        self.client.login(username="mapper", password="pw")
        response = self._save(**{f"peg_{self.peg.pk}": "RAWSHA-WRONGG"})

        self.peg.refresh_from_db()
        self.assertEqual(self.peg.finished_product, self.product)
        # The apostrophe is escaped in the rendered page, so match on the
        # part that isn't, plus the value that couldn't be resolved.
        html = response.content.decode()
        self.assertIn("t place", html)
        self.assertIn("RAWSHA-WRONGG", html)

    def test_the_editor_links_to_the_board_as_the_crew_see_it(self):
        """The check on whether the map you typed is the map that's hanging.

        One way only: the crew's board is a `secret/` page with no login, and
        a link back to a staff screen would send whoever tapped it to a
        sign-in form they have no account for.
        """
        self.client.login(username="mapper", password="pw")
        editor = self.client.get(
            reverse("display_map", args=[self.fixture.pk])
        ).content.decode()
        self.assertIn(reverse("restock_board", args=[self.fixture.pk]), editor)

        board = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        self.assertNotIn(reverse("display_map", args=[self.fixture.pk]), board)

    def test_a_reserved_space_has_no_dropdown(self):
        self.client.login(username="mapper", password="pw")
        plate = self.fixture.positions.filter(reserved_label="Price tag").first()
        html = self.client.get(
            reverse("display_map", args=[self.fixture.pk])
        ).content.decode()
        self.assertNotIn(f'name="peg_{plate.pk}"', html)
        self.assertIn("Price tag", html)
class RestockWalkTests(TestCase):
    """Filling the board, and saying so. The promise, not the audit."""

    def setUp(self):
        self.employee = Employee.objects.create(name="Restocker", pin="2468")
        self.fixture = make_board()

    def test_the_expected_fill_is_what_the_app_thinks_can_go_out(self):
        product = make_close_product("Plenty", on_hand=9, slots=2)
        position = hang(self.fixture, product, 2, 1)
        self.assertEqual(restock.expected_fill(position), 2)

        thin = make_close_product("Thin", on_hand=1, slots=2)
        self.assertEqual(restock.expected_fill(hang(self.fixture, thin, 2, 2)), 1)

        none_left = make_close_product("None Left", on_hand=0, slots=2)
        self.assertEqual(restock.expected_fill(hang(self.fixture, none_left, 2, 3)), 0)

    def test_a_colorway_on_several_pegs_fills_greedily(self):
        """What a person does: fill the first peg, then the next, stop when
        the bag runs out. Spreading would ask for a gap on every peg of a
        colorway instead of a gap on the last one."""
        product = make_close_product("Three Pegs", on_hand=3, slots=2)
        first = hang(self.fixture, product, 3, 1)
        second = hang(self.fixture, product, 3, 2)
        third = hang(self.fixture, product, 3, 3)
        product.refresh_from_db()
        self.assertEqual(product.display_slots, 6)

        self.assertEqual(restock.expected_fill(first), 2)
        self.assertEqual(restock.expected_fill(second), 1)
        self.assertEqual(restock.expected_fill(third), 0)

    def test_confirming_a_peg_the_app_knows_is_empty_is_a_completed_job(self):
        """**The distinction the whole page turns on.**

        A peg with nothing to put on it is not a failure to be reported — the
        app already knew, the walk confirmed it, and the person did their job.
        Treating it as an exception would make the ordinary evening read as a
        list of problems.
        """
        product = make_close_product("Genuinely Out", on_hand=0, slots=2)
        position = hang(self.fixture, product, 2, 1)
        walk = restock.open_pass(self.fixture, employee=self.employee)

        check = restock.record(walk, position)

        self.assertEqual(check.result, RestockCheck.AS_PREDICTED)
        self.assertEqual(check.expected, 0)
        self.assertIsNone(check.applied_log)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_a_peg_that_would_not_fill_writes_the_adjustment(self):
        """The worked case: app says 3, one on the peg, nothing in the bag."""
        product = make_close_product("Overcounted", on_hand=3, slots=2)
        position = hang(self.fixture, product, 2, 1)
        walk = restock.open_pass(self.fixture, employee=self.employee)

        check = restock.record(walk, position, counted=1)
        product.refresh_from_db()

        self.assertEqual(check.result, RestockCheck.SHORT)
        self.assertEqual(product.number_on_hand, 1)
        self.assertEqual(check.applied_log.quantity, -2)
        self.assertEqual(check.applied_log.source, InventoryLog.SOURCE_RESTOCK)

    def test_a_peg_that_filled_when_the_app_said_it_could_not(self):
        """Worth the quick look: predicting gaps wrongly is how a colorway
        quietly stops being offered."""
        product = make_close_product("Undercounted", on_hand=1, slots=2)
        position = hang(self.fixture, product, 2, 1)
        walk = restock.open_pass(self.fixture, employee=self.employee)

        check = restock.record(walk, position, counted=6)
        product.refresh_from_db()

        self.assertEqual(check.result, RestockCheck.OVER)
        self.assertEqual(product.number_on_hand, 6)
        self.assertEqual(check.applied_log.quantity, 5)

    def test_the_direction_is_the_delta_not_a_button(self):
        """A button naming the direction could disagree with the number typed
        under it, and then one of them is wrong with nothing to say which."""
        product = make_close_product("Agrees", on_hand=2, slots=2)
        position = hang(self.fixture, product, 2, 1)
        walk = restock.open_pass(self.fixture, employee=self.employee)

        check = restock.record(walk, position, counted=2)

        self.assertEqual(check.result, RestockCheck.AS_PREDICTED)
        self.assertIsNone(check.applied_log)

    def test_one_colorway_on_three_pegs_is_corrected_once(self):
        """Stock is per product, never per peg. Three adjustments for one
        discovery would take the shelf down three times."""
        product = make_close_product("Spread Out", on_hand=6, slots=2)
        pegs = [hang(self.fixture, product, 3, c) for c in (1, 2, 3)]
        walk = restock.open_pass(self.fixture, employee=self.employee)

        restock.record(walk, pegs[0], counted=2)
        restock.record(walk, pegs[1], counted=2)
        restock.record(walk, pegs[2], counted=2)
        product.refresh_from_db()

        self.assertEqual(product.number_on_hand, 2)
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_a_peg_that_moved_stock_is_never_applied_again(self):
        """The page is reopened, the button is double-tapped, and somebody
        walks the same row twice to be sure. All three are normal."""
        product = make_close_product("Double Tapped", on_hand=4, slots=2)
        position = hang(self.fixture, product, 2, 1)
        walk = restock.open_pass(self.fixture, employee=self.employee)

        restock.record(walk, position, counted=1)
        restock.record(walk, position, counted=0)
        product.refresh_from_db()

        self.assertEqual(product.number_on_hand, 1)
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_a_reserved_position_is_never_asked_about(self):
        plate = self.fixture.positions.filter(reserved_label="Price tag").first()
        walk = restock.open_pass(self.fixture)
        self.assertIsNone(restock.record(walk, plate))
        self.assertEqual(walk.checks.count(), 0)

    def test_every_pass_is_its_own_promise(self):
        """Not day-scoped like a close. A restock happens at open, at close
        and at the end of every shift, and each one is a completed task."""
        product = make_close_product("Walked Twice", on_hand=4, slots=2)
        position = hang(self.fixture, product, 2, 1)

        morning = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(morning, position)
        evening = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(evening, position)

        self.assertEqual(RestockPass.objects.count(), 2)
        self.assertEqual(morning.checks.count(), 1)
        self.assertEqual(evening.checks.count(), 1)

    def test_answering_every_peg_records_a_full_check(self):
        """**Two jobs were hiding in one page.**

        A full check is the board walked end to end, expected at open and at
        close. What it buys is not a score: afterwards every peg has a fresh
        baseline, so everything the board predicts is current.
        """
        pegs = [
            hang(self.fixture, make_close_product(f"Peg {n}", on_hand=4), 2, n)
            for n in (1, 2, 3)
        ]
        walk = restock.open_pass(self.fixture, employee=self.employee)

        for peg in pegs[:2]:
            restock.record(walk, peg)
        self.assertFalse(restock.close_pass(walk))
        walk.refresh_from_db()
        self.assertFalse(walk.is_full)

        restock.record(walk, pegs[2])
        self.assertTrue(restock.close_pass(walk))
        walk.refresh_from_db()
        self.assertTrue(walk.is_full)
        self.assertEqual(restock.last_full_check(self.fixture), walk)

    def test_a_partial_pass_is_a_completed_piece_of_work(self):
        """Completeness is recognised; incompleteness is never penalised.

        Nine pegs at four o'clock is work done, not a failed full check, and
        nothing records it as a shortfall.
        """
        pegs = [
            hang(self.fixture, make_close_product(f"Half {n}", on_hand=4), 2, n)
            for n in (1, 2, 3)
        ]
        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, pegs[0])
        restock.close_pass(walk)

        summary = restock.summary(walk)
        self.assertEqual(summary["checked"], 1)
        self.assertFalse(summary["is_full"])
        self.assertIsNone(restock.last_full_check(self.fixture))
        for key in summary:
            self.assertNotIn("missed", key)
            self.assertNotIn("outstanding", key)

    def test_a_full_check_stays_full_when_the_board_grows(self):
        """It covered everything at the time, and has to keep saying so."""
        peg = hang(self.fixture, make_close_product("Only One", on_hand=4), 2, 1)
        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, peg)
        self.assertTrue(restock.close_pass(walk))

        hang(self.fixture, make_close_product("Hung Later", on_hand=4), 2, 2)
        walk.refresh_from_db()
        self.assertTrue(walk.is_full)

    def test_the_summary_reports_work_not_a_score(self):
        product = make_close_product("Summarised", on_hand=4, slots=2)
        position = hang(self.fixture, product, 2, 1)
        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)

        summary = restock.summary(walk)
        self.assertEqual(summary["checked"], 1)
        self.assertEqual(summary["as_predicted"], 1)
        for key in summary:
            self.assertNotIn("rate", key)
            self.assertNotIn("percent", key)
class RestockPageTests(TestCase):
    """The walk on a phone: no JavaScript, one form, partial saves."""

    def setUp(self):
        self.employee = Employee.objects.create(name="Page Walker", pin="1357")
        self.fixture = make_board()
        self.product = make_close_product("Aegean Sea", on_hand=1, slots=2)
        self.position = hang(self.fixture, self.product, 2, 1)

    def _sign(self, **extra):
        return {"employee": self.employee.pk, "pin": "1357", **extra}

    def test_the_board_opens_for_someone_with_no_account(self):
        """secret/ means unlisted, not logged in. A redirect here locks out
        exactly the people it was built for."""
        self.assertEqual(self.client.get(reverse("restock_index")).status_code, 200)
        self.assertEqual(
            self.client.get(
                reverse("restock_board", args=[self.fixture.pk])
            ).status_code,
            200,
        )

    def test_the_page_carries_no_javascript(self):
        """The tick is CSS, not a script and not an htmx round-trip. A tap
        that silently fails to reach the server is a peg somebody believes
        they reported."""
        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        self.assertNotIn("hx-post", html)
        self.assertNotIn("<script", html.split("{% endblock %}")[0].replace(
            '<script src="https://unpkg.com/htmx.org@1.9.12"></script>', ""
        ))

    def test_the_app_says_which_pegs_it_thinks_you_cannot_fill(self):
        """So nobody walks off to look for a colorway there was never any of
        — and it comes from the app's numbers, so the card in hand is still
        an independent witness at the close."""
        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        self.assertIn('class="cell short"', html)
        # The toggle is neutral — it presupposes nothing about the bag, since
        # the one question behind it asks for the total either way.
        self.assertIn("count it", html)

    def test_ticking_a_peg_records_the_walk_and_moves_nothing(self):
        self.client.post(
            reverse("restock_board", args=[self.fixture.pk]),
            self._sign(**{f"done_{self.position.pk}": "1"}),
        )

        walk = RestockPass.objects.get()
        self.assertEqual(walk.employee, self.employee)
        self.assertEqual(walk.checks.get().result, RestockCheck.AS_PREDICTED)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_a_tile_states_the_bag_not_the_total(self):
        """**A total is not checkable by one observation.**

        Standing at the board you can read the peg, and you can read the bag.
        Adding them is a third act nobody does, so a printed total is a claim
        that cannot be falsified where it is displayed. `on_hand -
        display_slots` can be — it is what should be left the moment the peg
        is full.
        """
        product = make_close_product("Deep Bag", on_hand=5, slots=2)
        hang(self.fixture, product, 3, 1)
        product.refresh_from_db()
        self.assertEqual(product.backstock, 3)

        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()

        self.assertIn("bag 3", html)
        self.assertNotIn("5 on hand", html)

    def test_a_peg_that_can_be_filled_reports_an_empty_bag(self):
        """Two on hand, two pegs' worth of capacity: fill it and nothing is
        left over. Saying "bag 0" is what makes the next tap mean something."""
        product = make_close_product("Exactly Enough", on_hand=2, slots=2)
        hang(self.fixture, product, 3, 2)

        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        self.assertIn("bag 0", html)

    def test_a_peg_shows_what_sold_since_it_was_last_filled(self):
        """**Read off the ledger, not forecast.**

        Every sale is already a timestamped row, so "two went out since you
        filled this" is a fact — and one somebody falsifies by looking at the
        peg, which is the only kind of prediction worth printing here.
        """
        product = make_close_product("Sold Two", on_hand=6, slots=2)
        position = hang(self.fixture, product, 3, 1)

        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)
        InventoryLog.objects.create(
            finished_product=product,
            raw_product=product.raw_product,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-2,
        )

        cell = self._cell_for(position)
        self.assertEqual(cell["sold"], 2)
        self.assertTrue(cell["needs_refill"])

        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        self.assertIn("+2", html)
        self.assertIn('class="cell needs"', html)

    def test_the_badge_is_bounded_by_the_peg(self):
        """**The reported bug.** Avocado sat on a two-skein hook with nothing
        in the bag and asked for eleven — every sale since the peg was last
        tapped ten days earlier, spanning two weekends and a bath that went
        out and sold again in between.

        A hook holding two can never need eleven. Sales are a fact about the
        peg; they are not the work, and the badge is the work.
        """
        product = make_close_product("Avocado", on_hand=0, slots=2)
        position = hang(self.fixture, product, 3, 1)

        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)
        product.number_on_hand = 0
        product.save()
        for units in (2, 1, 1, 3, 1, 1, 1, 1):
            InventoryLog.objects.create(
                finished_product=product,
                raw_product=product.raw_product,
                log_type=InventoryLog.SALE,
                source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
                quantity=-units,
            )

        cell = self._cell_for(position)
        self.assertEqual(cell["sold"], 11)
        self.assertEqual(cell["put_out"], 0)
        self.assertFalse(cell["needs_refill"])

        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        self.assertNotIn("+11", html)

    def test_the_badge_is_bounded_by_the_bag(self):
        """**We can't put out two if the bag has one.**

        `fill` is derived from `number_on_hand`, so the peg bound and the bag
        bound normally agree. They come apart when the app believes more is
        hanging up than it believes it owns — a close or a bulk adjustment
        writing the total down with no sale to explain it. Then the pegs' want
        outruns what is behind them and somebody is sent to a bag that cannot
        answer.
        """
        product = make_close_product("Written Down", on_hand=4, slots=2)
        position = hang(self.fixture, product, 3, 1)

        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)
        self.assertEqual(walk.checks.get(position=position).expected, 2)

        # Counted at the close: one on the peg, one in the bag, and no sale
        # anywhere to say where the other two went.
        product.number_on_hand = 2
        product.save()
        InventoryLog.objects.create(
            finished_product=product,
            raw_product=product.raw_product,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-1,
        )

        cell = self._cell_for(position)
        self.assertEqual(cell["on_peg"], 1)
        self.assertEqual(cell["put_out"], 1)

    def test_a_colorway_on_two_pegs_drains_its_sales_once(self):
        """Pastel Rainbow hangs on two pegs of the veil rack and sold two.
        The badge is per peg and the sales are per product, so subtracting
        the colorway's whole count from each peg asked for four back off two
        sales — and the same again on every other multi-peg colorway."""
        product = make_close_product("Pastel Rainbow", on_hand=4, slots=4)
        first = hang(self.fixture, product, 3, 1)
        second = hang(self.fixture, product, 3, 2)

        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, first)
        restock.record(walk, second)
        InventoryLog.objects.create(
            finished_product=product,
            raw_product=product.raw_product,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-2,
        )

        cells = [self._cell_for(first), self._cell_for(second)]
        self.assertEqual([c["sold"] for c in cells], [2, 2])
        self.assertEqual(sum(c["put_out"] for c in cells), 2)

    def test_a_sale_before_the_last_walk_is_already_accounted_for(self):
        """The peg was filled *after* it sold, so there is nothing to put
        back. A prediction that counted it would send somebody to the bag for
        skeins that are already hanging up."""
        product = make_close_product("Sold Then Filled", on_hand=6, slots=2)
        position = hang(self.fixture, product, 3, 1)

        InventoryLog.objects.create(
            finished_product=product,
            raw_product=product.raw_product,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-2,
        )
        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)

        cell = self._cell_for(position)
        self.assertEqual(cell["sold"], 0)
        self.assertFalse(cell["needs_refill"])

    def test_a_peg_nobody_has_walked_predicts_nothing(self):
        """No baseline is not the same as nothing sold, and a quiet tile has
        to mean the app checked — never that it had no idea."""
        product = make_close_product("Never Walked", on_hand=6, slots=2)
        position = hang(self.fixture, product, 3, 2)

        cell = self._cell_for(position)
        self.assertIsNone(cell["sold"])
        self.assertFalse(cell["needs_refill"])

    def test_needing_skeins_and_being_unfillable_are_different_signals(self):
        """One is work — go to the bag. The other is somebody else's decision
        about what gets dyed, and nothing at the board fixes it.

        Unfillable means the bag is empty, not that the peg won't end up
        full. Those two used to be the same test because a short peg could
        never be work; see the next one.
        """
        unfillable = make_close_product("Nothing Left", on_hand=0, slots=2)
        position = hang(self.fixture, unfillable, 3, 3)

        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)
        InventoryLog.objects.create(
            finished_product=unfillable,
            raw_product=unfillable.raw_product,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-1,
        )

        cell = self._cell_for(position)
        self.assertTrue(cell["short"])
        self.assertEqual(cell["sold"], 1)
        # Sold, but there is nothing to put back — so not styled as work.
        self.assertFalse(cell["put_out"])
        self.assertFalse(cell["needs_refill"])

    def test_a_peg_can_be_work_and_still_not_fill(self):
        """**Nothing on the peg, one in the bag: put it out and move on.**

        That is a job, and the board used to call it a non-job — `short` won
        the tile colour outright, so a peg with a skein waiting for it came
        out amber under a caption saying nothing you do here fixes it. Work
        and won't-fill are independent facts and only one of them is an
        instruction; the tile's `1/2` carries the other.
        """
        product = make_close_product("Ochre", on_hand=1, slots=2)
        position = hang(self.fixture, product, 3, 4)

        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)
        InventoryLog.objects.create(
            finished_product=product,
            raw_product=product.raw_product,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-1,
        )

        cell = self._cell_for(position)
        self.assertTrue(cell["short"])
        self.assertEqual(cell["on_peg"], 0)
        self.assertEqual(cell["put_out"], 1)
        self.assertTrue(cell["needs_refill"])

        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        self.assertIn('class="cell needs"', html)

        # And it stops asking the moment the job is done. Put the skein out,
        # tap the tile: nothing is in the bag, one is on the peg, and the peg
        # goes back to amber — still won't fill, but there is no longer
        # anything to carry. A tile that stayed blue would send somebody to an
        # empty bag on the next pass, which is the failure this whole change
        # is about, wearing the opposite coat.
        second = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(second, position)

        cell = self._cell_for(position)
        self.assertEqual(cell["on_peg"], 1)
        self.assertEqual(cell["put_out"], 0)
        self.assertFalse(cell["needs_refill"])
        self.assertTrue(cell["short"])

    def _drain(self, name="Ran Dry", row=3, column=1):
        """A peg the app reckons has run bare with stock still behind it.

        Two went out at the last walk, two have sold since — and there is
        still yarn in the bag that could be on it.
        """
        product = make_close_product(name, on_hand=8, slots=2)
        position = hang(self.fixture, product, row, column)
        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)

        for _ in range(2):
            InventoryLog.objects.create(
                finished_product=product,
                raw_product=product.raw_product,
                log_type=InventoryLog.SALE,
                source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
                quantity=-1,
            )
        return position

    def test_a_peg_reckoned_bare_says_so_and_not_for_how_long(self):
        """**The one thing on this board worth hurrying to — as a state.**

        The elapsed time is a different claim: on the one page in the app
        that deliberately scores nothing, "empty 6 hours" is a number with
        the walker's name beside it, and it mostly measures how long since
        anybody came round with the phone rather than yarn sitting unsold.
        """
        cell = self._cell_for(self._drain())
        self.assertIsNotNone(cell["bare_since"])
        self.assertEqual(cell["put_out"], 2)

        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        # **The count wins the badge and bare is the colour.** "empty" used
        # to replace the number, which left the tile reading `empty` over
        # `2/2` and dropped the one figure somebody carries to the bag.
        self.assertIn('class="badge bare">+2</span>', html)
        self.assertNotRegex(html, r"\+2\s*·\s*\d")

    def test_the_tile_is_the_board_as_it_is_not_as_it_will_be(self):
        """**Ticking the box is what says the top-up happened.**

        Both figures used to be after-states: `fill/capacity` for the peg and
        `on_hand - display_slots` for the bag. Between them the tile described
        a board as it would be once somebody had done the work — an event
        nobody had said occurred — and `2/2` over `+1` said full and
        put-one-out in the same breath. One sold, one on the peg, more in the
        bag is situation normal, and the tile has to read that way.
        """
        product = make_close_product("Soft Tan", on_hand=7, slots=2)
        position = hang(self.fixture, product, 4, 2)

        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)
        InventoryLog.objects.create(
            finished_product=product,
            raw_product=product.raw_product,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-1,
        )

        cell = self._cell_for(position)
        self.assertEqual(cell["on_peg"], 1)
        self.assertEqual(cell["put_out"], 1)
        self.assertEqual(cell["bag_now"], 6)
        # The after-state is still derivable and is deliberately not printed.
        # If these were equal the test would pass without proving anything.
        self.assertEqual(cell["backstock"], 5)

        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        self.assertIn(">1/2</span>", html)
        self.assertIn("bag 6", html)
        self.assertNotIn("bag 5", html)

    def test_the_pull_list_never_asks_for_more_than_the_bag_has(self):
        """**The pull list is the armful, so it must not overstate.**

        It read `min(sold, capacity)` — the peg bound on its own, with the bag
        and the colorway missing — so it survived the tile being fixed and
        went on asking for skeins that are not behind the display. It is
        checked by opening the bag, which is the one place an overstatement
        gets found out with a walk already made.
        """
        product = make_close_product("One Left", on_hand=1, slots=2)
        position = hang(self.fixture, product, 3, 5)

        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)
        for _ in range(8):
            InventoryLog.objects.create(
                finished_product=product,
                raw_product=product.raw_product,
                log_type=InventoryLog.SALE,
                source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
                quantity=-1,
            )

        cell = self._cell_for(position)
        self.assertEqual(cell["sold"], 8)
        self.assertEqual(cell["put_out"], 1)

        # Eight sold, a peg that holds two, and one skein in the world.
        entry = next(e for e in restock.pull_list() if e["product"] == product)
        self.assertEqual(entry["units"], 1)
        self.assertEqual(restock.board_status(self.fixture)["units"], 1)

    def test_the_pull_list_counts_a_colorway_once_across_its_pegs(self):
        """Two pegs, two sold, two to carry — not two per peg. The tile and
        the pull list have to agree, because somebody reads the list, fills
        the bag, and walks to the board expecting it to run out exactly."""
        product = make_close_product("Two Pegs", on_hand=4, slots=4)
        first = hang(self.fixture, product, 3, 6)
        second = hang(self.fixture, product, 4, 1)

        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, first)
        restock.record(walk, second)
        InventoryLog.objects.create(
            finished_product=product,
            raw_product=product.raw_product,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-2,
        )

        entry = next(e for e in restock.pull_list() if e["product"] == product)
        self.assertEqual(entry["units"], 2)
        self.assertEqual(
            entry["units"],
            self._cell_for(first)["put_out"] + self._cell_for(second)["put_out"],
        )

    def test_a_bare_peg_still_says_how_many_to_carry(self):
        """`empty` over `2/2` is a contradiction to read — the fraction is
        what the peg holds when the job is done, not what is on it now — and
        it cost the walker the only number they act on.

        Red also stopped being an alarm when the badge became the work:
        `_drained_at` fires whenever sales since the last walk reach what went
        out, which with walks ten days apart is just "this colorway sold
        through". It was on 18 of the 39 pegs of the Artisan wall at once. So
        it is a hint about which peg to do first, carried as a colour.
        """
        cell = self._cell_for(self._drain())
        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()

        self.assertIsNotNone(cell["bare_since"])
        # Not `assertNotIn("empty")`: the word is also an unassigned peg's
        # class and is in the lead. The claim is about the badge.
        self.assertNotIn('class="badge bare">empty', html)
        self.assertIn('class="badge bare">+2</span>', html)
        self.assertTrue(cell["needs_refill"])

    def test_typing_bare_1_calls_the_elapsed_time_forth(self):
        """Kept for curiosity and demonstration, reachable only by typing."""
        self._drain()

        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk]) + "?bare=1"
        ).content.decode()
        self.assertRegex(html, r"\+2\s*·\s*\d+\s*(minute|hour|day|week)")

    def test_the_elapsed_time_does_not_follow_you_around(self):
        """**The inversion that keeps it away from the crew.**

        `?photos=1` is a mode and every link carries it, so a circuit stays
        in it. This is the opposite: a link sent mid-walk or a bookmark taken
        during a demo is exactly how a stopwatch ends up in front of the
        people it is not for, so nothing on the page passes it on and neither
        does the save.
        """
        self._drain()
        url = reverse("restock_board", args=[self.fixture.pk])

        html = self.client.get(url + "?bare=1").content.decode()
        self.assertNotIn("bare=1", html)

        response = self.client.post(
            url + "?bare=1",
            self._sign(**{f"done_{self.position.pk}": "1"}),
        )
        self.assertNotIn("bare=1", response["Location"])

    def test_the_picker_names_bare_pegs_without_timing_them(self):
        """Same split on the board list: the count is work available, the
        elapsed time is only there if somebody asked."""
        self._drain()

        html = self.client.get(reverse("restock_index")).content.decode()
        self.assertIn("1 bare", html)
        self.assertNotIn("longest", html)

        html = self.client.get(
            reverse("restock_index") + "?bare=1"
        ).content.decode()
        self.assertIn("longest", html)

    def test_a_half_sold_peg_is_not_called_bare(self):
        """One of two gone is a peg to top up, not one to hurry to."""
        product = make_close_product("Half Gone", on_hand=8, slots=2)
        position = hang(self.fixture, product, 3, 2)
        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)

        InventoryLog.objects.create(
            finished_product=product,
            raw_product=product.raw_product,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-1,
        )

        cell = self._cell_for(position)
        self.assertIsNone(cell["bare_since"])
        self.assertEqual(cell["sold"], 1)

    def test_a_bare_peg_with_nothing_to_refill_it_is_not_hurried_about(self):
        """Nothing done at the board fixes it — it belongs to whoever decides
        what gets dyed, and shouting about it here would be shouting at the
        wrong person.

        Note the setup: **nothing on the peg and nothing behind it.** This
        test used to leave one in the bag, which made its own premise false —
        there was something to put out, and it was reading as unfixable only
        because the peg could not be filled to the top. See the next one.
        """
        product = make_close_product("Nothing Behind It", on_hand=1, slots=2)
        position = hang(self.fixture, product, 3, 3)
        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)

        InventoryLog.objects.create(
            finished_product=product,
            raw_product=product.raw_product,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-1,
        )
        product.number_on_hand = 0
        product.save()

        cell = self._cell_for(position)
        self.assertTrue(cell["short"])
        self.assertEqual(cell["put_out"], 0)
        self.assertFalse(cell["bare"])

    def test_no_scarf_out_is_a_bare_spot_even_if_the_peg_wont_fill(self):
        """**Bare is "nothing out", not "can't be filled."**

        Those were one test — `not short` — and they are two questions. A
        hook holding two with one on it is fine: there is something there, a
        customer can see it and buy it. A spot with nothing on it is bare
        even when the bag can only make it one of two, and three pegs on the
        Artisan wall were in exactly that state reading as ordinary work.

        At capacity one this is the whole board: a veil rack spot that sold
        its scarf is empty, and 24 of 42 of them were.
        """
        product = make_close_product("Ochre", on_hand=1, slots=2)
        position = hang(self.fixture, product, 3, 4)
        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)
        InventoryLog.objects.create(
            finished_product=product,
            raw_product=product.raw_product,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-1,
        )

        cell = self._cell_for(position)
        self.assertTrue(cell["short"])
        self.assertEqual(cell["on_peg"], 0)
        self.assertEqual(cell["put_out"], 1)
        self.assertTrue(cell["bare"])

        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        self.assertIn('class="badge bare">+1</span>', html)

    def test_one_of_two_on_the_peg_is_not_bare(self):
        """There is a scarf out and somebody can buy it. It wants topping up
        — blue — and that is a different urgency from a spot with none."""
        product = make_close_product("Soft Tan", on_hand=7, slots=2)
        position = hang(self.fixture, product, 4, 3)
        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)
        InventoryLog.objects.create(
            finished_product=product,
            raw_product=product.raw_product,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-1,
        )

        cell = self._cell_for(position)
        self.assertEqual(cell["on_peg"], 1)
        self.assertEqual(cell["put_out"], 1)
        self.assertFalse(cell["bare"])

    def test_a_peg_bare_since_before_the_last_walk_still_counts_as_bare(self):
        """**The pegs bare the longest were the ones reporting as not bare.**

        `bare_since` is a moment, and there is no moment to name when the peg
        was already empty when somebody last walked it — `_drained_at` has no
        sale to point at and returns `None`. Reading the state off that
        timestamp dropped exactly those pegs, which is backwards. So the
        state is its own field and the timestamp is only the `?bare=1` half.
        """
        product = make_close_product("Long Bare", on_hand=4, slots=2)
        position = hang(self.fixture, product, 4, 4)
        product.number_on_hand = 0
        product.save()
        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, position)
        # Nothing went out at that walk, so nothing can have sold off it.
        self.assertEqual(walk.checks.get(position=position).expected, 0)

        # A bath arrives afterwards and goes in the bag.
        product.number_on_hand = 4
        product.save()

        cell = self._cell_for(position)
        self.assertEqual(cell["on_peg"], 0)
        self.assertEqual(cell["put_out"], 2)
        self.assertTrue(cell["bare"])
        self.assertIsNone(cell["bare_since"])
        self.assertEqual(restock.board_status(self.fixture)["bare"], 1)

    def test_a_peg_nobody_answered_keeps_counting_its_sales(self):
        """**Submitting a pass must not clear a peg it didn't cover.**

        The baseline is per position, so a peg somebody walked past goes on
        counting sales from the last time it was really filled. Clearing it
        would be the app claiming work that nobody did — and the badge is the
        only thing that would have sent somebody back to it.
        """
        answered = make_close_product("Answered", on_hand=8, slots=2)
        skipped = make_close_product("Walked Past", on_hand=8, slots=2)
        answered_peg = hang(self.fixture, answered, 4, 1)
        skipped_peg = hang(self.fixture, skipped, 4, 2)

        first = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(first, answered_peg)
        restock.record(first, skipped_peg)

        for _ in range(2):
            InventoryLog.objects.create(
                finished_product=skipped,
                raw_product=skipped.raw_product,
                log_type=InventoryLog.SALE,
                source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
                quantity=-1,
            )
        self.assertEqual(self._cell_for(skipped_peg)["sold"], 2)
        self.assertIsNotNone(self._cell_for(skipped_peg)["bare_since"])

        # A later pass that answers the other peg and walks past this one.
        self.client.post(
            reverse("restock_board", args=[self.fixture.pk]),
            self._sign(**{f"done_{answered_peg.pk}": "1"}),
        )

        cell = self._cell_for(skipped_peg)
        self.assertEqual(cell["sold"], 2)
        self.assertIsNotNone(cell["bare_since"])
        self.assertEqual(self._cell_for(answered_peg)["sold"], 0)

    def test_answering_a_peg_is_what_clears_it(self):
        """The other half: the badge has to go when the work is really done,
        or it stops meaning anything and gets ignored."""
        product = make_close_product("Refilled", on_hand=8, slots=2)
        peg = hang(self.fixture, product, 4, 3)
        first = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(first, peg)
        InventoryLog.objects.create(
            finished_product=product,
            raw_product=product.raw_product,
            log_type=InventoryLog.SALE,
            source=InventoryLog.SOURCE_SQUARE_WEBHOOK,
            quantity=-1,
        )
        self.assertEqual(self._cell_for(peg)["sold"], 1)

        self.client.post(
            reverse("restock_board", args=[self.fixture.pk]),
            self._sign(**{f"done_{peg.pk}": "1"}),
        )
        self.assertEqual(self._cell_for(peg)["sold"], 0)

    def test_a_partial_walk_is_accepted_whole_and_never_scored(self):
        """**This is not a task master.**

        Refusing a partial walk loses the answers somebody really gave, or
        buys manufactured ones from a person tapping through a validator —
        which is more expensive than the peg nobody looked at. And nothing
        counts walks or reports completeness: five passes in five minutes is a
        good afternoon, not a problem.
        """
        others = [
            hang(self.fixture, make_close_product(f"Skipped {n}", on_hand=4), 4, n)
            for n in (1, 2, 3)
        ]

        response = self.client.post(
            reverse("restock_board", args=[self.fixture.pk]),
            self._sign(**{f"done_{self.position.pk}": "1"}),
            follow=True,
        )
        html = response.content.decode()

        walk = RestockPass.objects.get()
        self.assertEqual(walk.checks.count(), 1)
        self.assertFalse(walk.checks.filter(position__in=others).exists())
        self.assertFalse(walk.is_full)
        self.assertIn("1 peg confirmed", html)
        for scolding in ("still to do", "remaining", "incomplete", "missed"):
            self.assertNotIn(scolding, html.lower())

    def _cell_for(self, position):
        for row in restock.board(self.fixture):
            for cell in row:
                if cell.get("position") == position:
                    return cell
        raise AssertionError("position not on the board")

    def test_the_empty_bag_buttons_cover_every_possible_answer(self):
        """**The finding that will actually happen, on one tap.**

        Nobody counts a bag of twelve reliably and nothing here asks them to.
        An empty bag is different in kind — noticed without counting, constant,
        and exact — and with nothing behind the display the total is whatever
        got put out. So every answer is a button, up to and including a full
        peg, which is the case that used to need typing.

        "Couldn't fill it" is not a second thing: you cannot fail to fill a
        peg unless the bag ran out.
        """
        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        pk = self.position.pk

        for n in (0, 1, 2):
            self.assertIn(f'name="count_{pk}" value="{n}"', html)
        self.assertNotIn(f'name="count_{pk}" value="3"', html)
        self.assertIn("How many have you got altogether", html)

    def test_a_full_peg_with_an_empty_bag_is_one_tap(self):
        """The path that used to be the worst one for the likeliest event.

        The app thinks there are three behind the display, the peg fills, and
        the bag is bare. Two got out, so two is the total.
        """
        product = make_close_product("Bag Was Bare", on_hand=5, slots=2)
        position = hang(self.fixture, product, 3, 1)
        product.refresh_from_db()
        self.assertEqual(product.backstock, 3)

        self.client.post(
            reverse("restock_board", args=[self.fixture.pk]),
            self._sign(**{f"count_{position.pk}": "2"}),
        )

        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 2)
        check = RestockPass.objects.get().checks.get(position=position)
        self.assertEqual(check.result, RestockCheck.SHORT)
        self.assertEqual(check.applied_log.quantity, -3)

    def test_a_bounded_button_records_the_shortfall(self):
        product = make_close_product("Wouldn't Fill", on_hand=3, slots=2)
        position = hang(self.fixture, product, 3, 2)

        self.client.post(
            reverse("restock_board", args=[self.fixture.pk]),
            self._sign(**{f"count_{position.pk}": "1"}),
        )

        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 1)
        check = RestockPass.objects.get().checks.get(position=position)
        self.assertEqual(check.result, RestockCheck.SHORT)

    def test_both_controls_mean_the_total_and_the_app_splits_it(self):
        """**One question: how many have you got altogether.**

        The app fills the pegs first and puts the remainder in the bag,
        because that is what a person does with an armful of skeins — and it
        is the only split that assumes nothing about what was already up.
        """
        self.client.post(
            reverse("restock_board", args=[self.fixture.pk]),
            self._sign(**{f"more_{self.position.pk}": "9"}),
        )

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 9)
        check = RestockPass.objects.get().checks.get()
        self.assertEqual(check.result, RestockCheck.OVER)

    def test_a_peg_the_app_thinks_is_full_and_is_actually_bare(self):
        """**The worst case, and the one the board cannot predict.**

        The app says two on the peg and nothing behind it. The peg is bare.
        Two left without registering — a swapped sale, a hand-keyed line, a
        webhook that stopped delivering — and *nothing on the tile hints at
        it*: no badge, no amber, because the app has no reason to doubt
        itself. Only somebody looking at the peg can find it, which is the
        whole argument for walking the board rather than reading it.

        So the recording has to be trivial, and it is: zero is on the buttons.
        """
        product = make_close_product("Believed Full", on_hand=2, slots=2)
        peg = hang(self.fixture, product, 4, 6)

        walk = restock.open_pass(self.fixture, employee=self.employee)
        restock.record(walk, peg)

        quiet = self._cell_for(peg)
        self.assertEqual(quiet["expected"], 2)
        self.assertEqual(quiet["backstock"], 0)
        self.assertFalse(quiet["short"])
        self.assertEqual(quiet["sold"], 0)      # nothing to draw the eye
        self.assertIsNone(quiet["bare_since"])

        self.client.post(
            reverse("restock_board", args=[self.fixture.pk]),
            self._sign(**{f"count_{peg.pk}": "0"}),
        )

        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 0)
        check = RestockPass.objects.filter(fixture=self.fixture).first().checks.get(
            position=peg
        )
        self.assertEqual(check.result, RestockCheck.SHORT)
        self.assertEqual(check.applied_log.quantity, -2)
        self.assertEqual(check.applied_log.source, InventoryLog.SOURCE_RESTOCK)

    def test_zero_is_always_on_the_buttons(self):
        """A peg the app believes is full still offers zero, because that is
        exactly the peg where zero is the surprising and important answer."""
        product = make_close_product("Looks Fine", on_hand=2, slots=2)
        peg = hang(self.fixture, product, 5, 1)

        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        self.assertIn(f'name="count_{peg.pk}" value="0"', html)

    def test_a_half_full_peg_topped_up_from_a_find_is_not_counted_twice(self):
        """**The bug the "how many in the bag" wording caused.**

        Coffee and Roses: the app says 1 of 2 with an empty bag. You had one,
        you found one more, so you have two and the peg now fills. The one you
        found went *onto the peg* — the bag is still empty.

        Asking "how many in the bag" and adding the display's capacity on
        assumed the peg started full and reported three. Asking the total
        reports two, which is how many there are.
        """
        product = make_close_product("Coffee And Roses", on_hand=1, slots=2)
        position = hang(self.fixture, product, 4, 5)

        self.client.post(
            reverse("restock_board", args=[self.fixture.pk]),
            self._sign(**{f"count_{position.pk}": "2"}),
        )

        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 2)
        self.assertEqual(product.backstock, 0)
        check = RestockPass.objects.get().checks.get(position=position)
        self.assertEqual(check.result, RestockCheck.OVER)
        self.assertEqual(check.applied_log.quantity, 1)

    def test_a_peg_the_app_calls_empty_is_not_asked_if_the_bag_is_empty(self):
        """The panel must not restate the prediction as if it were the finding.

        A tile reading `bag 0` already claims the bag is empty. Leading its
        exception with "bag's empty" asks somebody to confirm the thing that
        is written above it, and buries the only news there is — that they
        found some.
        """
        full_bag = make_close_product("Says Empty", on_hand=2, slots=2)
        position = hang(self.fixture, full_bag, 3, 4)
        full_bag.refresh_from_db()
        self.assertEqual(full_bag.backstock, 0)

        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        panel = html.split(f'id="exc_{position.pk}"')[1].split("</div>\n                  </div>")[0]

        # One question, phrased without presupposing anything about the bag.
        self.assertIn("How many have you got altogether", panel)
        self.assertNotIn("did you find in it", panel)

    def test_a_typed_count_beats_a_tick(self):
        """Somebody who ticked the tile and then found the peg wouldn't fill
        meant the number — typing one is the more deliberate act."""
        self.client.post(
            reverse("restock_board", args=[self.fixture.pk]),
            self._sign(**{
                f"done_{self.position.pk}": "1",
                f"count_{self.position.pk}": "0",
            }),
        )

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 0)
        self.assertEqual(RestockPass.objects.get().checks.get().counted, 0)

    def test_an_untouched_peg_is_not_walked_yet_rather_than_empty(self):
        other = make_close_product("Untouched", on_hand=4, slots=2)
        other_position = hang(self.fixture, other, 2, 2)

        self.client.post(
            reverse("restock_board", args=[self.fixture.pk]),
            self._sign(**{f"done_{self.position.pk}": "1"}),
        )

        other.refresh_from_db()
        self.assertEqual(other.number_on_hand, 4)
        self.assertFalse(
            RestockPass.objects.get().checks.filter(
                position=other_position
            ).exists()
        )

    def test_a_wrong_pin_records_no_promise(self):
        """The name is what turns a page of ticks into something somebody
        said. Without it there is nothing to record."""
        self.client.post(
            reverse("restock_board", args=[self.fixture.pk]),
            {
                "employee": self.employee.pk,
                "pin": "0000",
                f"done_{self.position.pk}": "1",
            },
        )
        self.assertEqual(RestockPass.objects.count(), 0)

    def test_a_walk_with_nothing_ticked_opens_no_pass(self):
        """An empty submit is somebody who hasn't started, not a board with
        nothing on it."""
        self.client.post(
            reverse("restock_board", args=[self.fixture.pk]), self._sign()
        )
        self.assertEqual(RestockPass.objects.count(), 0)

    def test_no_crew_facing_page_says_what_ought_to_be_hanging(self):
        """Neither the picker nor the board. Deciding what belongs on a board
        is the mapper's job, and telling the crew about it is the app
        claiming responsibility nobody gave it."""
        blank = self.product.raw_product
        self.fixture.raw_product = blank
        self.fixture.save(update_fields=["raw_product"])
        homeless = FinishedProduct.objects.create(
            name="Homeless", raw_product=blank,
            recipe=make_recipe("Homeless Recipe"), price="30.00",
        )

        for name, args in (("restock_index", []),
                           ("restock_board", [self.fixture.pk])):
            html = self.client.get(reverse(name, args=args)).content.decode()
            self.assertNotIn(homeless.recipe.name, html, name)

    def test_the_board_reads_as_names_until_photos_are_asked_for(self):
        """Text is the mode the board opens in, whatever the catalogue has.

        The questions a walk asks are words and numbers, and photo mode used
        to arrive by accident — wherever a product happened to have a picture
        — which made one board half one thing and half the other.
        """
        FinishedProductImage.objects.create(
            finished_product=self.product,
            image_url="https://example.test/aegean.jpg",
        )
        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk])
        ).content.decode()
        self.assertIn(self.product.recipe.name, html)
        self.assertNotIn("https://example.test/aegean.jpg", html)

        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk]), {"photos": "1"}
        ).content.decode()
        self.assertIn("https://example.test/aegean.jpg", html)

    def test_photo_mode_still_falls_back_to_text_without_a_picture(self):
        """Half the catalogue has no photo, and a grey box names nothing."""
        html = self.client.get(
            reverse("restock_board", args=[self.fixture.pk]), {"photos": "1"}
        ).content.decode()
        self.assertIn(self.product.recipe.name, html)

    def test_saving_a_walk_in_photo_mode_comes_back_in_photo_mode(self):
        """The mode rides in the URL, so the redirect has to carry it.

        A save that dropped the walk back to names would be the app undoing a
        choice mid-circuit, which on a phone reads as the tap having gone
        somewhere unexpected.
        """
        response = self.client.post(
            reverse("restock_board", args=[self.fixture.pk]) + "?photos=1",
            self._sign(**{f"count_{self.position.pk}": "1"}),
        )
        self.assertEqual(
            response["Location"],
            reverse("restock_board", args=[self.fixture.pk]) + "?photos=1",
        )
