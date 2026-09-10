"""The Sunday close: the app's empty bags against the tags in hand.

The reasoning behind these is in `docs/claude/close.md`.
"""
import csv
import re
import tempfile
from datetime import date, datetime, time, timedelta
from django.core.management import call_command
from django.utils import timezone
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
    make_close_product,
    make_product,
)


def count_post(row, total):
    """The POST a person's thumb produces for one row.

    Answers up to the display's capacity are buttons; anything past it went
    in the box, which is the shape the form is built in and the shape a test
    has to send if it is testing the page rather than `closing`.
    """
    if total <= (row.display_slots or 0):
        return {f"counted_{row.pk}": str(total)}
    return {f"counted_{row.pk}": "more", f"more_{row.pk}": str(total)}
def on_table(product, category_name):
    """Move a product's blank onto another table.

    Category is which table at the stall — the one axis a close is physically
    walked on — and `make_product` puts everything on Silk, so this is how a
    test gets a yarn board to walk.
    """
    category, _ = RawProductCategory.objects.get_or_create(name=category_name)
    RawProduct.objects.filter(pk=product.raw_product_id).update(category=category)
    product.refresh_from_db()
    return product
class SundayCloseTests(TestCase):
    """What the close asks about, and what each answer is allowed to move.

    The expensive failures here are all silent. A count filed as the wrong
    kind of disagreement corrupts the only number the page produces; an
    adjustment applied twice takes stock the shelf still has; a closed day
    that still accepts answers rewrites a record somebody already read.

    And the most expensive of all is the one this was rebuilt to stop: a tag
    in hand read as "we have none", which writes off the units still hanging
    on the display and turns the app into a backstock tracker.
    """

    def setUp(self):
        self.employee = Employee.objects.create(name="Close Tester", pin="4321")

    # --- what lands on the list -------------------------------------------

    def test_the_list_is_the_empty_bags_not_the_empty_shelves(self):
        """The pivot, stated as a query.

        A product with two on the pegs and nothing behind them has an empty
        bag and a tag in somebody's hand, and it is *not* at zero. Waiting
        for zero means waiting until the display has been sold down to
        nothing, which is both later and rarer than the thing the tag says.
        """
        bag_empty = make_close_product("All On Display", on_hand=2, slots=2)
        bag_has_some = make_close_product("Bag Behind It", on_hand=5, slots=2)
        bare = make_close_product("Nothing At All", on_hand=0, slots=2)

        expected = list(closing.expected_products())
        self.assertIn(bag_empty, expected)
        self.assertIn(bare, expected)
        self.assertNotIn(bag_has_some, expected)

    def test_a_product_that_never_goes_on_display_is_never_asked_about(self):
        """No display, no tag, nothing to disagree with."""
        product = make_close_product("Boxed Away", on_hand=0, slots=0)
        self.assertNotIn(product, list(closing.expected_products()))

    def test_par_no_longer_decides_what_gets_audited(self):
        """It used to, and it was the wrong instrument.

        Par is a production number. A colorway with par 0 is one nobody plans
        to make again — which says nothing about whether it is on the pegs
        this weekend with a tag in the bag behind it.
        """
        product = make_close_product("No Par, Still Out There", on_hand=1, par=0)
        self.assertIn(product, list(closing.expected_products()))

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
        make_close_product("Bag Empty", on_hand=0)
        first, created_first = closing.run_for_today(employee=self.employee)
        second, created_second = closing.run_for_today(employee=self.employee)

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(CloseRun.objects.count(), 1)

    def test_a_product_whose_bag_empties_later_joins_the_open_run(self):
        """A close started at noon still has to ask about the four o'clock sale."""
        make_close_product("Empty At Noon", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        self.assertEqual(run.rows.count(), 1)

        afternoon = make_close_product("Empty At Four", on_hand=1)
        closing.sync_expected(run)

        self.assertEqual(run.rows.count(), 2)
        self.assertIn(afternoon, [row.finished_product for row in run.rows.all()])

    def test_a_row_freezes_the_display_it_was_asked_about(self):
        """The buttons said "0, 1 or 2" because that is what the pegs held.

        Re-reading capacity later would read back a display that has since
        been rebuilt, and the answer on file would stop meaning what the
        person meant by it. Same reasoning as the production sheet freezing
        its bath size.
        """
        product = make_close_product("Rebuilt Later", on_hand=1, slots=2)
        run, _ = closing.run_for_today()
        row = run.rows.get()
        self.assertEqual(row.display_slots, 2)

        FinishedProduct.objects.filter(pk=product.pk).update(display_slots=6)
        closing.sync_expected(run)
        row.refresh_from_db()

        self.assertEqual(row.display_slots, 2)

    def test_syncing_never_rewrites_a_row_that_was_already_answered(self):
        """The frozen `on_hand_before` is what the disagreement was measured
        against — re-reading it would read back the number this close fixed."""
        product = make_close_product("Answered", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get()
        closing.record_count(run, row, 4)

        closing.sync_expected(run)
        row.refresh_from_db()

        self.assertEqual(row.on_hand_before, 0)
        self.assertEqual(row.counted, 4)
        self.assertEqual(run.rows.count(), 1)

    # --- the one answer ----------------------------------------------------

    def test_counting_the_display_does_not_write_off_what_is_hanging_there(self):
        """**The whole reason this was rebuilt.**

        Two on the pegs, bag empty, tag in hand. The old close read that tag
        as "we have none" and adjusted to zero — deleting two real skeins.
        Do that all season and stock moving from bag to peg reads as sales:
        the app becomes a backstock tracker, and every rack the shop adds
        bills itself as demand and gets paid for in dyeing.
        """
        product = make_close_product("Two On The Pegs", on_hand=2, slots=2)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        closing.record_count(run, row, 2)
        product.refresh_from_db()
        row.refresh_from_db()

        self.assertEqual(product.number_on_hand, 2)
        self.assertEqual(row.outcome, CloseRunRow.CONFIRMED)
        self.assertIsNone(row.applied_log)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_counting_more_than_the_app_had_trues_it_up_and_tags_the_source(self):
        product = make_close_product("Undercounted", on_hand=1, slots=2)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        closing.record_count(run, row, 6)
        product.refresh_from_db()
        row.refresh_from_db()

        self.assertEqual(product.number_on_hand, 6)
        self.assertEqual(row.outcome, CloseRunRow.MISSING)
        self.assertEqual(row.counted, 6)

        log = row.applied_log
        self.assertIsNotNone(log)
        self.assertEqual(log.quantity, 5)
        self.assertEqual(log.log_type, InventoryLog.ADJUSTMENT)
        self.assertEqual(log.source, InventoryLog.SOURCE_SUNDAY_CLOSE)

    def test_a_predicted_row_can_come_out_as_an_overcount(self):
        """Direction is the sign of the delta, not what was in whose hand.

        The app says two are on the pegs, the pegs have one. That is stock
        that left without registering — the dead-webhook shape — and it
        arrives on a row the close predicted, which the old outcome scheme
        could not express.
        """
        product = make_close_product("Peg Has One", on_hand=2, slots=2)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        closing.record_count(run, row, 1)
        product.refresh_from_db()
        row.refresh_from_db()

        self.assertEqual(product.number_on_hand, 1)
        self.assertEqual(row.outcome, CloseRunRow.EXTRA)
        self.assertEqual(row.applied_log.quantity, -1)
        self.assertFalse(row.added_by_tag)

    def test_a_count_of_zero_is_a_real_answer(self):
        """Display empty and bag empty. Recorded, and moves nothing if the app
        already agreed."""
        product = make_close_product("Gone Entirely", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        closing.record_count(run, row, 0)
        row.refresh_from_db()
        product.refresh_from_db()

        self.assertEqual(row.outcome, CloseRunRow.CONFIRMED)
        self.assertEqual(row.counted, 0)
        self.assertIsNone(row.applied_log)
        self.assertEqual(product.number_on_hand, 0)

    def test_an_unpredicted_tag_only_adds_a_row_and_moves_nothing(self):
        """It used to adjust straight to zero on the strength of the tag.

        Wrong twice over: the tag says the bag is empty, not the shelf, and
        there are still units on the display waiting to be counted. Now the
        tag puts the product on the list and the count settles it, like
        everything else.
        """
        product = make_close_product("Tag In Hand", on_hand=3, slots=2)
        run, _ = closing.run_for_today()
        self.assertEqual(run.rows.count(), 0)   # bag not empty, so unpredicted

        row, created = closing.add_tag(run, product)
        product.refresh_from_db()

        self.assertTrue(created)
        self.assertTrue(row.added_by_tag)
        self.assertEqual(row.outcome, CloseRunRow.PENDING)
        self.assertEqual(row.on_hand_before, 3)
        self.assertEqual(row.display_slots, 2)
        self.assertEqual(product.number_on_hand, 3)
        self.assertIsNone(row.applied_log)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_an_unpredicted_tag_counted_low_is_the_overcount_it_looks_like(self):
        product = make_close_product("Really Was Over", on_hand=3, slots=2)
        run, _ = closing.run_for_today()
        row, _ = closing.add_tag(run, product)

        closing.record_count(run, row, 1)
        product.refresh_from_db()
        row.refresh_from_db()

        self.assertEqual(product.number_on_hand, 1)
        self.assertEqual(row.outcome, CloseRunRow.EXTRA)
        self.assertEqual(row.applied_log.quantity, -2)

    def test_an_unpredicted_tag_can_turn_out_to_agree(self):
        """The guess the old code had to make, no longer guessed.

        A product can be off the list and still be right — and filing that as
        an overcount would put a fault into the one number this page produces,
        in the direction that reads as "the till is losing sales".
        """
        product = make_close_product("Off The List But Right", on_hand=3, slots=2)
        run, _ = closing.run_for_today()
        row, _ = closing.add_tag(run, product)

        closing.record_count(run, row, 3)
        row.refresh_from_db()

        self.assertEqual(row.outcome, CloseRunRow.CONFIRMED)
        self.assertIsNone(row.applied_log)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_a_passthrough_count_writes_to_the_raw_row(self):
        """One pile, one count. Writing the mirror would snap back on save."""
        category, _ = RawProductCategory.objects.get_or_create(name="Yarn")
        raw = RawProduct.objects.create(
            name="Undyed DK", category=category, price="12.00", number_on_hand=6
        )
        passthrough = FinishedProduct.objects.create(
            name="Undyed DK", raw_product=raw, recipe=None, price="18.00"
        )
        run, _ = closing.run_for_today()
        row, _ = closing.add_tag(run, passthrough)

        closing.record_count(run, row, 2)
        raw.refresh_from_db()
        passthrough.refresh_from_db()

        self.assertEqual(raw.number_on_hand, 2)
        self.assertEqual(passthrough.number_on_hand, 2)

    # --- applying twice ----------------------------------------------------

    def test_a_row_that_moved_stock_is_never_applied_again(self):
        """The page gets reopened and the button gets double-tapped."""
        product = make_close_product("Double Tap", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        closing.record_count(run, row, 4)
        closing.record_count(run, row, 9)
        product.refresh_from_db()

        self.assertEqual(product.number_on_hand, 4)
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_the_same_tag_added_twice_is_one_row(self):
        product = make_close_product("Scanned Twice", on_hand=4, slots=2)
        run, _ = closing.run_for_today()

        closing.add_tag(run, product)
        row, created = closing.add_tag(run, product)

        self.assertFalse(created)
        self.assertEqual(run.rows.filter(finished_product=product).count(), 1)

    def test_an_agreed_row_can_be_re_answered_but_a_moved_one_cannot(self):
        """A bag found under the table at seven, confirmed gone at four.

        An agreement moved no stock, so it is still open to correction all
        evening and the new number is what moves the shelf. A row that
        already moved stock is not re-answerable — putting that back is an
        explicit Undo, which writes the compensating entry a retyped number
        has no way to express.
        """
        product = make_close_product("Found Under The Table", on_hand=2, slots=2)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        closing.record_count(run, row, 2)
        row.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.CONFIRMED)

        closing.record_count(run, row, 8)
        row.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.MISSING)
        self.assertEqual(product.number_on_hand, 8)

        closing.record_count(run, row, 1)
        row.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.MISSING)
        self.assertEqual(product.number_on_hand, 8)

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

        closing.record_count(run, row, 7)
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

        make_close_product("Empty Tomorrow", on_hand=0)
        self.assertEqual(closing.sync_expected(run), [])
        self.assertEqual(run.rows.count(), 0)

    # --- the tally ---------------------------------------------------------

    def test_the_tally_counts_failures_and_keeps_the_directions_apart(self):
        """Never a net figure: a bad intake would cancel out a dead webhook."""
        under = make_close_product("Under", on_hand=0)
        agreed = make_close_product("Agreed", on_hand=2, slots=2)
        over = make_close_product("Over", on_hand=2, slots=2)
        run, _ = closing.run_for_today()

        closing.record_count(run, run.rows.get(finished_product=agreed), 2)
        closing.record_count(run, run.rows.get(finished_product=under), 3)
        closing.record_count(run, run.rows.get(finished_product=over), 0)

        tally = closing.tally(run)
        self.assertEqual(tally["missing"], 1)
        self.assertEqual(tally["extra"], 1)
        self.assertEqual(tally["confirmed"], 1)
        self.assertEqual(tally["disagreements"], 2)
        self.assertEqual(tally["under_units"], 3)
        self.assertEqual(tally["over_units"], 2)
        self.assertNotIn("rate", tally)

    def test_predicted_and_unpredicted_is_a_different_axis_from_over_and_under(self):
        """They used to be the same one, and it put the wrong thing in the
        only number this page produces."""
        listed = make_close_product("Listed", on_hand=2, slots=2)
        walked_up = make_close_product("Walked Up", on_hand=5, slots=2)
        run, _ = closing.run_for_today()

        closing.record_count(run, run.rows.get(finished_product=listed), 0)
        tag_row, _ = closing.add_tag(run, walked_up)
        closing.record_count(run, tag_row, 5)

        tally = closing.tally(run)
        self.assertEqual(tally["expected"], 1)
        self.assertEqual(tally["unpredicted"], 1)
        self.assertEqual(tally["extra"], 1)        # the *listed* one
        self.assertEqual(tally["confirmed"], 1)    # the unpredicted one

    def test_the_tally_never_counts_displays_left_short(self):
        """Capacity is not a target, and a number here would be acted on.

        A hook that holds four exists precisely so three is allowed to be
        enough. Counting the gap would put display size back on the path to
        production, which is the coupling this whole rebuild removes.
        """
        make_close_product("Thin On The Pegs", on_hand=2, slots=6)
        run, _ = closing.run_for_today()
        closing.record_count(run, run.rows.get(), 2)

        tally = closing.tally(run)
        for key in tally:
            self.assertNotIn("hole", key)
            self.assertNotIn("short", key)
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
        self.assertRedirects(
            response, reverse("close_run", args=[run.token]) + "?mode=count"
        )
        self.assertEqual(run.employee, self.employee)
        self.assertEqual(run.rows.count(), 1)

    def test_the_buttons_only_run_as_high_as_the_display_holds(self):
        """The range is the instruction, not decoration.

        Holding the tag means the bag is empty, so the answer cannot exceed
        what is hanging up. A count that runs past the last button is itself
        the news that there was a bag after all, which is what the box is for.
        """
        make_close_product("Three Pegs", on_hand=1, slots=3)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        html = self.client.get(
            reverse("close_run", args=[run.token])
        ).content.decode()

        for n in range(4):
            self.assertIn(f'name="counted_{row.pk}" value="{n}"', html)
        self.assertNotIn(f'name="counted_{row.pk}" value="4"', html)
        self.assertIn(f'name="counted_{row.pk}" value="more"', html)
        self.assertIn(f"more_{row.pk}", html)

    def test_a_product_whose_bag_empties_since_appears_on_the_very_next_load(self):
        """The four o'clock sale has to be on the seven o'clock page.

        Not the one after it. The close gets worked in passes across an
        evening, and a row that arrives one request late is one nobody is
        asked about while they are standing in front of the tags — the list
        reads as complete and the scarf is simply missing from it.

        This is a regression test for a prefetch cache: the rows were being
        read into memory before `sync_expected` added to them.
        """
        make_close_product("Out At Four", on_hand=0)
        run, _ = closing.run_for_today()
        self.client.get(reverse("close_run", args=[run.token]))

        later = make_close_product("Out At Seven", on_hand=1)
        html = self.client.get(
            reverse("close_run", args=[run.token])
        ).content.decode()

        row = run.rows.get(finished_product=later)
        self.assertIn(f"counted_{row.pk}", html)
        self.assertIn(later.name, html)

    def test_a_row_added_between_page_and_submit_is_still_accepted(self):
        """The form is rebuilt from the rows as they are at POST time.

        Otherwise a product that joined the run while somebody was counting
        would have its field silently dropped on validation — the number gets
        typed, the page comes back, and nothing says it wasn't saved.
        """
        make_close_product("Already There", on_hand=0)
        run, _ = closing.run_for_today()
        late = make_close_product("Joined Late", on_hand=1, slots=2)
        closing.sync_expected(run)
        late_row = run.rows.get(finished_product=late)

        self.client.post(
            reverse("close_run", args=[run.token]), count_post(late_row, 5)
        )

        late_row.refresh_from_db()
        late.refresh_from_db()
        self.assertEqual(late_row.counted, 5)
        self.assertEqual(late.number_on_hand, 5)

    def test_an_agreed_row_comes_back_with_its_answer_showing(self):
        """A blank row reads as one nobody has reached.

        The list is worked in passes across an evening, so an answer that
        vanished off the page is how a product gets counted twice or skipped.
        """
        make_close_product("Answered At Four", on_hand=2, slots=2)
        run, _ = closing.run_for_today()
        row = run.rows.get()
        url = reverse("close_run", args=[run.token])

        self.client.post(url, count_post(row, 2))
        # The counting list with the drawer open: a bare URL opens the card
        # lists once anything has been counted, and an answered row is off
        # the counting list until it is asked for. Both of those are stronger
        # answers to what this test guards — a row that is absent and counted
        # in a header cannot be misread as one nobody reached — but the
        # answer still has to be shown back on the buttons that took it.
        html = self.client.get(
            url, {"mode": "count", "answered": "1"}
        ).content.decode()

        checked = re.search(
            rf'<input type="radio" name="counted_{row.pk}" value="(\d+|more)"[^>]*checked',
            html,
        )
        self.assertIsNotNone(checked, "the answer given at four is not shown back")
        self.assertEqual(checked.group(1), "2")

    def test_a_bag_found_at_seven_corrects_what_was_counted_at_four(self):
        """Confirmed at four, a bag of them found at seven.

        This is the whole reason an agreed row stays un-frozen: counting one
        at what the app already believed moves no stock, so it can be
        answered again and the new number is what moves the shelf.

        It lands as `missing`, and the label repays a careful reading: the
        tag was in hand at four, so "no tag" isn't literally what happened.
        What the outcome records is the *direction* — the app was under,
        which is the stock-arrived-unrecorded end of the pipeline — and the
        direction is what is being counted.
        """
        product = make_close_product("Lost Bag", on_hand=2, slots=2)
        run, _ = closing.run_for_today()
        row = run.rows.get()
        url = reverse("close_run", args=[run.token])

        self.client.post(url, count_post(row, 2))
        row.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.CONFIRMED)
        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 2)

        self.client.post(url, count_post(row, 8))
        row.refresh_from_db()
        product.refresh_from_db()

        self.assertEqual(row.outcome, CloseRunRow.MISSING)
        self.assertEqual(row.counted, 8)
        self.assertEqual(product.number_on_hand, 8)
        self.assertEqual(row.applied_log.quantity, 6)
        self.assertEqual(row.applied_log.source, InventoryLog.SOURCE_SUNDAY_CLOSE)
        self.assertEqual(closing.tally(run)["missing"], 1)
        self.assertEqual(closing.tally(run)["confirmed"], 0)

    def test_tapping_more_without_a_number_says_so_and_keeps_the_page(self):
        """A dropped answer is the failure to avoid: re-rendered, not
        redirected, so the numbers already typed survive the message."""
        make_close_product("Half Answered", on_hand=1, slots=2)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        response = self.client.post(reverse("close_run", args=[run.token]), {
            f"counted_{row.pk}": "more",
        })

        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.PENDING)
        self.assertIn("type how many", response.content.decode().lower())

    def test_a_number_typed_in_the_box_without_the_tap_is_still_taken(self):
        """A thumb on a phone. Rejecting it for the missing tap loses a real
        count and tells the person their answer was wrong when it wasn't."""
        product = make_close_product("Typed Only", on_hand=1, slots=2)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        self.client.post(reverse("close_run", args=[run.token]), {
            f"more_{row.pk}": "9",
        })

        row.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(row.counted, 9)
        self.assertEqual(product.number_on_hand, 9)

    def test_undo_puts_a_miscount_back_without_an_account(self):
        """The mis-tap an employee has to be able to fix themselves.

        Needing a staff login here means the person who made the mistake goes
        and tells somebody, and the cost of that conversation is what gets a
        wrong count left unmentioned. So the client below is deliberately not
        logged in.
        """
        product = make_close_product("Fat Fingered", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()
        url = reverse("close_run", args=[run.token])

        self.client.post(url, count_post(row, 50))
        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 50)

        self.client.post(reverse("close_undo", args=[run.token, row.pk]))

        row.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 0)
        self.assertEqual(row.outcome, CloseRunRow.PENDING)
        self.assertIsNone(row.counted)
        self.assertIsNone(row.applied_log)
        self.assertEqual(closing.tally(run)["disagreements"], 0)

    def test_undo_writes_a_compensating_entry_and_erases_nothing(self):
        """History is added to, never rewritten.

        The ledger has to be able to say "this happened and was put back",
        because that is what did happen. Deleting the first entry would make
        the log claim the stock never moved.
        """
        product = make_close_product("Put Back", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()
        url = reverse("close_run", args=[run.token])

        self.client.post(url, count_post(row, 7))
        original = InventoryLog.objects.get()

        self.client.post(reverse("close_undo", args=[run.token, row.pk]))

        self.assertTrue(InventoryLog.objects.filter(pk=original.pk).exists())
        self.assertEqual(InventoryLog.objects.count(), 2)
        quantities = sorted(l.quantity for l in InventoryLog.objects.all())
        self.assertEqual(quantities, [-7, 7])
        self.assertEqual(
            {l.source for l in InventoryLog.objects.all()},
            {InventoryLog.SOURCE_SUNDAY_CLOSE},
        )
        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 0)

    def test_undoing_an_unpredicted_tag_restores_the_stock_and_the_row_goes(self):
        """A row the close invented; undone, it leaves nothing to be pending
        about, and anyone genuinely holding the tag can add it again."""
        product = make_close_product("Walked Up", on_hand=4, slots=2)
        run, _ = closing.run_for_today()
        self.client.post(reverse("close_add_tag", args=[run.token]), {
            "product_id": product.pk,
        })
        row = run.rows.get(finished_product=product)
        self.client.post(reverse("close_run", args=[run.token]), count_post(row, 1))
        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 1)

        self.client.post(reverse("close_undo", args=[run.token, row.pk]))

        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 4)
        self.assertEqual(run.rows.filter(finished_product=product).count(), 0)
        self.assertEqual(closing.tally(run)["extra"], 0)
        self.assertEqual(InventoryLog.objects.count(), 2)

    def test_undoing_a_predicted_row_leaves_it_on_the_list(self):
        """The other half of the same button. A predicted row was asked about
        by the close, so taking the answer back returns it to be answered —
        deleting it would drop a question nobody has answered."""
        product = make_close_product("Predicted", on_hand=2, slots=2)
        run, _ = closing.run_for_today()
        row = run.rows.get()
        self.client.post(reverse("close_run", args=[run.token]), count_post(row, 9))

        self.client.post(reverse("close_undo", args=[run.token, row.pk]))

        row.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.PENDING)
        self.assertEqual(product.number_on_hand, 2)

    def test_undo_reverses_a_delta_so_a_sale_in_between_survives(self):
        """A webhook can land between the mistake and the noticing.

        Restoring a remembered absolute would put back a number that was
        already out of date. The inverse delta plus the clamp lands on the
        right answer.
        """
        product = make_close_product("Sold Meanwhile", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        closing.record_count(run, row, 6)
        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 6)

        product.set_on_hand(5)          # a sale lands
        closing.undo(run, row)

        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 0)

    def test_undo_twice_is_not_an_error(self):
        """A double tap on a slow connection is the normal case."""
        product = make_close_product("Double Tapped Undo", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()
        self.client.post(
            reverse("close_run", args=[run.token]), count_post(row, 3)
        )

        self.client.post(reverse("close_undo", args=[run.token, row.pk]))
        second = self.client.post(reverse("close_undo", args=[run.token, row.pk]))

        self.assertEqual(second.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 0)
        self.assertEqual(InventoryLog.objects.count(), 2)

    def test_undo_is_refused_on_a_finished_day(self):
        """Same boundary as everything else — yesterday is a record."""
        product = make_close_product("Yesterdays Mistake", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()
        closing.record_count(run, row, 9)
        CloseRun.objects.filter(pk=run.pk).update(
            day=timezone.localdate() - timedelta(days=1)
        )

        self.client.post(reverse("close_undo", args=[run.token, row.pk]))

        row.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(row.outcome, CloseRunRow.MISSING)
        self.assertEqual(product.number_on_hand, 9)
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_the_undo_button_is_on_the_page_for_a_settled_row(self):
        """Findable without knowing it exists — the whole point."""
        make_close_product("Needs An Out", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()
        url = reverse("close_run", args=[run.token])
        self.client.post(url, count_post(row, 5))

        # Undo lives on the counting list, behind the answered drawer — a
        # bare URL opens the card lists, and the counting list shows what is
        # left rather than what is done.
        html = self.client.get(
            url, {"mode": "count", "answered": "1"}
        ).content.decode()
        self.assertIn(reverse("close_undo", args=[run.token, row.pk]), html)
        self.assertIn("Undo", html)

    def test_a_count_posted_for_a_row_that_already_moved_is_refused(self):
        """The same rule, enforced where a hand-built POST would arrive."""
        product = make_close_product("Already Moved", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()
        url = reverse("close_run", args=[run.token])
        self.client.post(url, count_post(row, 5))

        self.client.post(url, count_post(row, 8))

        row.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(row.counted, 5)
        self.assertEqual(product.number_on_hand, 5)
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_a_blank_count_is_left_for_later_rather_than_read_as_zero(self):
        product = make_close_product("Not Counted Yet", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()

        self.client.post(reverse("close_run", args=[run.token]), {
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

        self.client.post(reverse("close_run", args=[run.token]), count_post(row, 6))

        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 6)
        self.assertEqual(
            InventoryLog.objects.get().source, InventoryLog.SOURCE_SUNDAY_CLOSE
        )

    def test_adding_a_tag_through_the_page_moves_no_stock(self):
        product = make_close_product("Held", on_hand=4, slots=2)
        run, _ = closing.run_for_today()

        response = self.client.post(reverse("close_add_tag", args=[run.token]), {
            "product_id": product.pk,
        })

        self.assertRedirects(
            response, reverse("close_run", args=[run.token]) + "?mode=count"
        )
        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 4)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.assertTrue(run.rows.get(finished_product=product).added_by_tag)

    def test_a_stale_tab_cannot_write_to_a_finished_day(self):
        """The van is unpacked by now — this is a bookmark, not a person
        standing in front of the tags."""
        product = make_close_product("Yesterdays", on_hand=0)
        run, _ = closing.run_for_today()
        row = run.rows.get()
        CloseRun.objects.filter(pk=run.pk).update(
            day=timezone.localdate() - timedelta(days=1)
        )

        self.client.post(reverse("close_run", args=[run.token]), count_post(row, 9))
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
class CloseAnsweredDrawerTests(TestCase):
    """Answered rows come off the counting list, and come back on request.

    What somebody is looking for on this page is the next thing they have not
    checked. On a list twenty-three long a settled row sitting between two
    unsettled ones has to be read in order to be skipped, which is a cost
    paid on every pass down the pile — and a close is worked in several
    passes across an evening.

    A reveal rather than a mode. Nothing carries `?answered=1` onward, so it
    evaporates on the next submit or link — the same inversion the restock
    board's `?bare=1` makes, and for the same reason: the focused list is
    what the page is for, and a stale reveal quietly puts the long list back.
    """

    def setUp(self):
        self.employee = Employee.objects.create(name="Closer", pin="1234")

    def test_an_answered_row_is_off_the_counting_list(self):
        make_close_product("Counted Already", on_hand=0)
        make_close_product("Still Waiting", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get(finished_product__name="Counted Already")
        url = reverse("close_run", args=[run.token])
        self.client.post(url, count_post(row, 1))

        html = self.client.get(url, {"mode": "count"}).content.decode()
        self.assertIn("Still Waiting", html)
        self.assertNotIn("Counted Already", html)

    def test_the_drawer_says_how_many_are_behind_it(self):
        """A row that is simply absent, with nothing saying so, reads as a
        row the close never asked about."""
        make_close_product("Counted Already", on_hand=0)
        make_close_product("Still Waiting", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get(finished_product__name="Counted Already")
        url = reverse("close_run", args=[row.run.token])
        self.client.post(url, count_post(row, 1))

        html = self.client.get(url, {"mode": "count"}).content.decode()
        self.assertIn("already answered", html)
        self.assertIn("left to count", html)

    def test_the_drawer_brings_them_back(self):
        make_close_product("Counted Already", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get()
        url = reverse("close_run", args=[run.token])
        self.client.post(url, count_post(row, 1))

        html = self.client.get(
            url, {"mode": "count", "answered": "1"}
        ).content.decode()
        self.assertIn("Counted Already", html)
        self.assertIn(reverse("close_undo", args=[run.token, row.pk]), html)

    def test_hiding_a_row_does_not_unanswer_it(self):
        """The hidden row simply isn't in the POST, and an absent row is one
        nobody answered — which is the same rule a half-worked close relies
        on. It must not read as an answer of any kind."""
        make_close_product("Counted Already", on_hand=0)
        other = make_close_product("Still Waiting", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        settled = run.rows.get(finished_product__name="Counted Already")
        waiting = run.rows.get(finished_product=other)
        url = reverse("close_run", args=[run.token])
        self.client.post(url, count_post(settled, 1))

        self.client.post(url, count_post(waiting, 2))

        settled.refresh_from_db()
        self.assertEqual(settled.counted, 1)
        self.assertEqual(settled.outcome, CloseRunRow.MISSING)

    def test_the_reveal_does_not_survive_a_submit(self):
        """It evaporates, unlike the mode. A reveal that followed somebody
        round would put the long list back without being asked for."""
        make_close_product("Counted Already", on_hand=0)
        make_close_product("Still Waiting", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        waiting = run.rows.get(finished_product__name="Still Waiting")
        url = reverse("close_run", args=[run.token])

        response = self.client.post(
            url + "?mode=count&answered=1", count_post(waiting, 1)
        )

        self.assertRedirects(response, url + "?mode=count")

    def test_a_rejected_answer_is_never_the_thing_that_got_hidden(self):
        """'more' tapped with no number, on a row opened from the drawer. A
        form that came back with its error hidden reads as saved."""
        # Counted at exactly what the app believed: answered, so it is in
        # the drawer, but it moved no stock so it is still correctable — the
        # bag-found-at-seven case, which is the reason to open the drawer at
        # all.
        make_close_product("Fumbled", on_hand=1, slots=2)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get()
        url = reverse("close_run", args=[run.token])
        self.client.post(url, count_post(row, 1))
        self.assertEqual(run.rows.get().outcome, CloseRunRow.CONFIRMED)

        response = self.client.post(url + "?mode=count&answered=1", {
            f"counted_{row.pk}": "more",
        })

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Fumbled", html)
        self.assertIn("type how many there are altogether", html)

    def test_a_finished_day_hides_nothing(self):
        """All record, nothing answerable — there is no work left to focus
        on, so there is nothing to put behind a drawer."""
        make_close_product("Yesterdays Answer", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get()
        url = reverse("close_run", args=[run.token])
        self.client.post(url, count_post(row, 3))
        CloseRun.objects.filter(pk=run.pk).update(
            day=timezone.localdate() - timedelta(days=1)
        )

        html = self.client.get(url, {"mode": "count"}).content.decode()
        self.assertIn("Yesterdays Answer", html)
class CloseTagSearchTests(TestCase):
    """The unpredicted-tag search, swapped in rather than navigated to.

    This was a plain form submit on purpose and the reason is worth keeping:
    the close runs in a field on one bar, and a search that silently does
    nothing is worse than one that visibly reloads. So the round trip stays
    visible — an indicator while it runs, a sentence when it fails — and it
    is still submit-only, never a request per keystroke.

    What changed is the cost the original reasoning did not weigh: the box
    sits under twenty-odd rows, and a full navigation throws the scroll
    position away, so every search was paid for with a scrub back down the
    page.
    """

    def setUp(self):
        self.employee = Employee.objects.create(name="Closer", pin="1234")
        self.product = make_close_product("Sought After", on_hand=9, slots=2)
        self.run, _ = closing.run_for_today(employee=self.employee)

    def test_the_fragment_needs_no_login(self):
        """secret/ means unlisted, not gated — and the crew have no
        accounts."""
        response = self.client.get(
            reverse("close_tag_search", args=[self.run.token]), {"q": "Sought"}
        )
        self.assertEqual(response.status_code, 200)

    def test_it_returns_the_matches_as_add_buttons(self):
        response = self.client.get(
            reverse("close_tag_search", args=[self.run.token]), {"q": "Sought"}
        )
        html = response.content.decode()
        self.assertIn("Sought After", html)
        self.assertIn(reverse("close_add_tag", args=[self.run.token]), html)
        self.assertIn(f'value="{self.product.pk}"', html)

    def test_it_carries_this_run_and_no_other(self):
        """The token scopes the fragment, so a swapped-in button cannot post
        a tag onto somebody else's close."""
        other = CloseRun.objects.create(day=timezone.localdate() - timedelta(days=3))

        html = self.client.get(
            reverse("close_tag_search", args=[self.run.token]), {"q": "Sought"}
        ).content.decode()

        self.assertNotIn(reverse("close_add_tag", args=[other.token]), html)

    def test_an_empty_query_returns_nothing_rather_than_everything(self):
        html = self.client.get(
            reverse("close_tag_search", args=[self.run.token])
        ).content.decode()
        self.assertNotIn("Sought After", html)

    def test_the_page_renders_the_same_partial_it_swaps_in(self):
        """One copy of the markup. Two would drift, and the way that shows is
        the swapped version quietly posting somewhere the inline one
        doesn't."""
        html = self.client.get(
            reverse("close_run", args=[self.run.token]),
            {"mode": "count", "q": "Sought"},
        ).content.decode()

        self.assertIn('id="tag-results"', html)
        self.assertIn("Sought After", html)
        self.assertIn(reverse("close_add_tag", args=[self.run.token]), html)

    def test_the_form_still_works_without_htmx(self):
        """Script blocked, and it is the ordinary GET it always was — landing
        back on the counting list rather than the default reading."""
        html = self.client.get(
            reverse("close_run", args=[self.run.token]), {"mode": "count"}
        ).content.decode()

        self.assertIn('method="get"', html)
        self.assertIn('name="mode" value="count"', html)
        self.assertIn(
            f'hx-get="{reverse("close_tag_search", args=[self.run.token])}"', html
        )

    def test_a_dropped_request_has_somewhere_to_say_so(self):
        """The whole of what the page reload gave for free. A search that
        didn't arrive must not read as a search that found nothing — on this
        page that reads as 'the scarf isn't in the app'."""
        html = self.client.get(
            reverse("close_run", args=[self.run.token]), {"mode": "count"}
        ).content.decode()

        self.assertIn('id="tag-failed"', html)
        self.assertIn("htmx:sendError", html)
        self.assertIn("htmx:responseError", html)
class CloseCardListTests(TestCase):
    """What the close leaves in somebody's hand, once the counting is done.

    A different question from the one the evening is worked on, asked of the
    same rows. Not *which way was the app wrong* — that is the counting
    list's business and the history page's output — but *which tags should be
    in the stack now*, which is one live test applied to every row alike:
    `number_on_hand <= display_slots`, the app saying the bag is empty.

    Deliberately blind to whether a row was predicted. Where the evening's
    work came from is not what should be in the stack at the end of it, and
    reading the stack through that split asks somebody to do the subtraction
    in their head while holding forty cards.

    A row nobody counted is neither card nor not-card but a third pile: work
    remaining. Its number is the app's belief rather than tonight's finding,
    and an unchecked claim in a list whose whole job is to be checked would
    make the stack agree with itself.
    """

    def setUp(self):
        self.employee = Employee.objects.create(name="Closer", pin="1234")

    def _cards(self, run):
        rows = list(run.rows.select_related("finished_product"))
        return closing.card_status(rows)

    def test_a_count_that_finds_a_bag_moves_the_row_out_of_the_cards(self):
        """The whole point of counting: the tag was wrong and goes back.

        Predicted, so a tag was in hand — and the count found four behind a
        display that holds two. There is a bag after all.
        """
        make_close_product("Found A Bag", on_hand=1, slots=2)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get()

        self.client.post(reverse("close_run", args=[run.token]), count_post(row, 4))

        cards, no_cards, uncounted = self._cards(run)
        self.assertEqual(cards, [])
        self.assertEqual([r.pk for r in no_cards], [row.pk])
        self.assertEqual(uncounted, [])

    def test_an_unpredicted_tag_counted_low_is_a_card_like_any_other(self):
        """The split the lists refuse to make.

        This row arrived because somebody was holding a tag the close never
        asked about, and the count agreed the bag was empty. In the stack it
        is a card; nothing about where it came from changes that.
        """
        product = make_close_product("Turned Up", on_hand=5, slots=2)
        run, _ = closing.run_for_today(employee=self.employee)
        self.client.post(reverse("close_add_tag", args=[run.token]), {
            "product_id": product.pk,
        })
        row = run.rows.get(finished_product=product)

        self.client.post(reverse("close_run", args=[run.token]), count_post(row, 2))

        cards, no_cards, uncounted = self._cards(run)
        self.assertEqual([r.pk for r in cards], [row.pk])
        self.assertEqual(no_cards, [])
        self.assertEqual(uncounted, [])

    def test_a_row_sitting_exactly_on_the_display_is_a_card(self):
        """Every unit is hanging up, so the bag is empty — same `<=` the
        close opened the evening by predicting with."""
        make_close_product("Exactly Full", on_hand=0, slots=2)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get()

        self.client.post(reverse("close_run", args=[run.token]), count_post(row, 2))

        cards, no_cards, uncounted = self._cards(run)
        self.assertEqual([r.pk for r in cards], [row.pk])
        self.assertEqual(no_cards, [])
        self.assertEqual(uncounted, [])

    def test_an_uncounted_row_is_neither_card_nor_not_card(self):
        """Work remaining, kept out of both stacks.

        The number on it is the app's belief rather than tonight's finding,
        so filing it under a card either way would put an unchecked claim in
        a list whose whole job is to be checked.
        """
        make_close_product("Never Got To It", on_hand=1, slots=2)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get()

        cards, no_cards, uncounted = self._cards(run)
        self.assertEqual(cards, [])
        self.assertEqual(no_cards, [])
        self.assertEqual([r.pk for r in uncounted], [row.pk])

    def test_the_uncounted_are_on_the_page_as_their_own_list(self):
        """Dropped instead, and the two card lists read as complete when
        they are short — which is the silence this page exists to break."""
        make_close_product("Never Got To It", on_hand=1, slots=2)
        run, _ = closing.run_for_today(employee=self.employee)

        html = self.client.get(
            reverse("close_run", args=[run.token]), {"mode": "cards"}
        ).content.decode()
        self.assertIn("Still to count", html)
        self.assertIn("Never Got To It", html)
        self.assertIn("1 still to count", html)

    def test_the_frozen_display_is_what_decides_a_card(self):
        """A board rebuilt on Monday must not change which tags were in a
        hand on Sunday — the same reason the row froze the number."""
        product = make_close_product("Rebuilt Peg", on_hand=2, slots=2)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get()
        self.client.post(reverse("close_run", args=[run.token]), count_post(row, 2))

        FinishedProduct.objects.filter(pk=product.pk).update(display_slots=1)

        cards, no_cards, uncounted = self._cards(run)
        self.assertEqual([r.pk for r in cards], [row.pk])
        self.assertEqual(no_cards, [])
        self.assertEqual(uncounted, [])
class CloseModeTests(TestCase):
    """Which of the two readings a URL gets, and what carries it.

    Counting is the evening's work; the cards are what it leaves behind. The
    mode is in the query string so a reading is a link somebody can send, and
    every action on the counting half redirects back carrying it — a close is
    worked in several passes across an evening, and dropping into the summary
    after each submit would cost a tap back every time.
    """

    def setUp(self):
        self.employee = Employee.objects.create(name="Closer", pin="1234")

    def test_an_untouched_close_opens_on_the_counting_list(self):
        make_close_product("Nothing Done", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)

        html = self.client.get(
            reverse("close_run", args=[run.token])
        ).content.decode()
        self.assertIn("Count what you've got", html)

    def test_once_something_is_counted_a_bare_url_opens_the_cards(self):
        """The moment the page's usefulness changes hands. Before the first
        submit there is no stack to check; after it, the stack is the
        question — which is what a link off the history page is following."""
        make_close_product("Counted One", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get()
        url = reverse("close_run", args=[run.token])
        self.client.post(url, count_post(row, 1))

        html = self.client.get(url).content.decode()
        self.assertIn("Cards you should have", html)
        self.assertNotIn("Count what you've got", html)

    def test_a_finished_day_opens_on_the_cards(self):
        """Nothing on it can be counted any more, so it is read rather than
        worked."""
        make_close_product("Yesterdays Stack", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        CloseRun.objects.filter(pk=run.pk).update(
            day=timezone.localdate() - timedelta(days=1)
        )

        html = self.client.get(
            reverse("close_run", args=[run.token])
        ).content.decode()
        self.assertIn("Cards you should have", html)

    def test_saving_counts_comes_back_to_the_counting_list(self):
        """Worked in passes, so a submit mid-pile stays where the pile is."""
        make_close_product("Mid Pile", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get()

        response = self.client.post(
            reverse("close_run", args=[run.token]), count_post(row, 1)
        )

        self.assertRedirects(
            response, reverse("close_run", args=[run.token]) + "?mode=count"
        )

    def test_undo_comes_back_to_the_counting_list(self):
        make_close_product("Mis Tapped", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get()
        url = reverse("close_run", args=[run.token])
        self.client.post(url, count_post(row, 5))

        response = self.client.post(
            reverse("close_undo", args=[run.token, row.pk])
        )

        self.assertRedirects(response, url + "?mode=count")

    def test_the_tag_search_keeps_the_counting_list(self):
        """A GET off the page has to carry the mode, or looking for a tag
        drops somebody into the summary."""
        make_close_product("Searchable", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)
        row = run.rows.get()
        url = reverse("close_run", args=[run.token])
        self.client.post(url, count_post(row, 1))

        html = self.client.get(url, {"mode": "count"}).content.decode()
        self.assertIn('name="mode" value="count"', html)

    def test_an_unreadable_mode_falls_back_to_the_default(self):
        """A hand-edited or stale link degrades rather than 500s."""
        make_close_product("Junk Mode", on_hand=0)
        run, _ = closing.run_for_today(employee=self.employee)

        html = self.client.get(
            reverse("close_run", args=[run.token]), {"mode": "sideways"}
        ).content.decode()
        self.assertIn("Count what you've got", html)
class CloseAddTagSwapTests(TestCase):
    """Adding an unpredicted tag without losing the half-filled form above.

    This was a full POST and navigation, and the argument for it was that the
    row has to visibly appear or the same tag gets added three times. What it
    did not weigh is the cost on the page it is on: late in a long evening the
    counting form above is part-answered, and a redirect throws all of that
    away and lands somebody at the top of a page they were at the bottom of.

    So the row is swapped in beside the search that found it, nothing above is
    re-rendered, and the double-add is handled where it always really was —
    `closing.add_tag` hands back the row that exists.
    """

    def setUp(self):
        self.employee = Employee.objects.create(name="Closer", pin="1234")
        self.listed = make_close_product("On The List", on_hand=0)
        self.tagged = make_close_product("Sought After", on_hand=9, slots=2)
        self.run, _ = closing.run_for_today(employee=self.employee)
        self.url = reverse("close_run", args=[self.run.token])
        self.add_url = reverse("close_add_tag", args=[self.run.token])

    def listed_row(self):
        return self.run.rows.get(finished_product=self.listed)

    def add(self, product, **extra):
        return self.client.post(
            self.add_url, {"product_id": product.pk}, HTTP_HX_REQUEST="true", **extra
        )

    def test_the_row_comes_back_instead_of_a_redirect(self):
        response = self.add(self.tagged)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        row = self.run.rows.get(finished_product=self.tagged)
        self.assertIn(f'name="counted_{row.pk}"', html)
        self.assertIn("Sought After", html)

    def test_the_swapped_row_still_submits_with_the_rest(self):
        """It lands outside the counting form, so it belongs to it by
        `form=` — otherwise the answer is typed and then goes nowhere."""
        html = self.add(self.tagged).content.decode()

        self.assertIn('form="count-form"', html)

    def test_nothing_above_is_re_rendered_so_nothing_above_is_lost(self):
        """The whole point. A response carrying the other rows would be a
        page load wearing a swap's clothes, and it would take the answers
        typed into them with it."""
        html = self.add(self.tagged).content.decode()

        self.assertNotIn("On The List", html)

    def test_an_answer_typed_but_not_saved_survives_adding_a_tag(self):
        """The failure this replaces, stated as the sequence that produced
        it: type an answer, add a tag, save. The answer has to still be
        there — and it is, because the swap never touched it."""
        listed_row = self.run.rows.get(finished_product=self.listed)
        self.add(self.tagged)
        added_row = self.run.rows.get(finished_product=self.tagged)

        payload = count_post(listed_row, 1)
        payload.update(count_post(added_row, 4))
        self.client.post(self.url, payload)

        listed_row.refresh_from_db()
        added_row.refresh_from_db()
        self.assertEqual(listed_row.counted, 1)
        self.assertEqual(added_row.counted, 4)

    def test_a_second_tap_adds_nothing_and_says_so(self):
        """A repeated tap is normal — no page load proved anything the first
        time — so the guard is that adding is idempotent, not that the page
        navigated."""
        self.add(self.tagged)
        html = self.add(self.tagged).content.decode()

        self.assertEqual(self.run.rows.filter(finished_product=self.tagged).count(), 1)
        self.assertIn("already on this close", html)
        row = self.run.rows.get(finished_product=self.tagged)
        self.assertNotIn(f'name="counted_{row.pk}"', html)

    def test_the_numbers_above_the_list_come_back_out_of_band(self):
        """A line reading "1 left to count" over two rows is the page
        contradicting itself, and it is above the fold — so it rides along
        rather than waiting for a reload nobody is going to do."""
        html = self.add(self.tagged).content.decode()

        self.assertIn('id="count-tables"', html)
        self.assertIn('id="count-left"', html)
        self.assertIn("hx-swap-oob", html)

    def test_one_save_button_serves_the_whole_form(self):
        """Two of them read as two forms, and never were: every count field
        carries `form=`, so a row swapped in beside the search has always
        submitted with the rest. The one that is left follows the screen."""
        html = self.client.get(self.url, {"mode": "count"}).content.decode()

        self.assertEqual(html.count("Save these counts"), 1)
        self.assertIn('class="savebar always"', html)
        self.assertIn('form="count-form"', html)

    def test_the_save_bar_goes_when_there_is_nothing_to_save(self):
        """A button over an empty list is furniture. `always` is what keeps
        it while the counting list has rows; without it the CSS drops the bar
        until a tag is added."""
        self.client.post(self.url, count_post(self.listed_row(), 2))

        html = self.client.get(self.url, {"mode": "count"}).content.decode()

        self.assertIn('class="savebar"', html)
        self.assertNotIn("savebar always", html)

    def test_the_counting_form_is_there_even_with_nothing_to_count(self):
        """A `form=` attribute naming a form that isn't on the page names
        nothing, and "counted the list, now working through tags" is an
        ordinary state to be in."""
        self.client.post(self.url, count_post(self.listed_row(), 2))

        html = self.client.get(self.url, {"mode": "count"}).content.decode()

        self.assertNotIn("counted_", html.split('id="added-rows"')[0].split(
            'id="count-form"')[1])
        self.assertIn('id="count-form"', html)
        self.assertIn('id="added-rows"', html)

    def test_without_the_script_it_is_the_post_and_redirect_it_always_was(self):
        response = self.client.post(self.add_url, {"product_id": self.tagged.pk})

        self.assertRedirects(response, self.url + "?mode=count")
        self.assertTrue(self.run.rows.filter(finished_product=self.tagged).exists())

    def test_a_dropped_add_has_somewhere_to_say_so(self):
        """Same rule as the search: a request that never arrived must not
        look like nothing happening, and this one has a row riding on it."""
        html = self.client.get(
            self.url, {"mode": "count", "q": "Sought"}
        ).content.decode()

        self.assertIn('id="add-failed"', html)
        self.assertIn(f'hx-post="{self.add_url}"', html)
class CloseCategoryFilterTests(TestCase):
    """Which table is on screen, and what the filter is not allowed to do.

    A close is walked one table at a time — the yarn boards are one circuit
    and the silk racks another — so the counting list narrows to the table
    somebody is standing at. The whole risk of that is a page reading as
    finished while a table's worth of rows has never been asked about, so
    every test below is really about the same thing: the filter changes the
    reading and never the run.
    """

    def setUp(self):
        self.employee = Employee.objects.create(name="Walker", pin="1234")

    def open_close(self, yarn=True):
        make_close_product("Silk Sash", on_hand=0)
        if yarn:
            on_table(make_close_product("Yarn Skein", on_hand=0), "Yarn")
        run, _ = closing.run_for_today(employee=self.employee)
        return run, reverse("close_run", args=[run.token])

    def test_the_counting_list_narrows_to_one_table(self):
        run, url = self.open_close()

        html = self.client.get(
            url, {"mode": "count", "category": "Yarn"}
        ).content.decode()

        self.assertIn("Yarn Skein", html)
        self.assertNotIn("Silk Sash", html)

    def test_the_hidden_table_is_still_on_the_run_and_still_counted(self):
        """The pill nobody selected is the standing evidence of what is left.

        Hiding rows on a page whose whole job is to be complete is only safe
        because the other table's count stays on screen — and because the row
        is still on the run, so `counts()` reads its absence from the POST as
        "nobody touched it", which is what a half-worked close already means.
        """
        run, url = self.open_close()

        response = self.client.get(url, {"mode": "count", "category": "Yarn"})

        self.assertEqual(run.rows.count(), 2)
        self.assertEqual(
            {p["label"]: p["count"] for p in response.context["category_pills"]},
            {"All": 2, "Silk": 1, "Yarn": 1},
        )
        self.assertEqual(response.context["hidden_count"], 1)
        self.assertIn("hidden, not done", response.content.decode())

    def test_the_pills_count_what_is_left_rather_than_what_was_asked(self):
        """A pill reading 1 over an empty list is the page contradicting
        itself, so the number follows the reading it sits above."""
        run, url = self.open_close()
        yarn_row = run.rows.get(finished_product__name="Yarn Skein")
        self.client.post(url, count_post(yarn_row, 1))

        response = self.client.get(url, {"mode": "count"})

        self.assertEqual(
            {p["label"]: p["count"] for p in response.context["category_pills"]},
            {"All": 1, "Silk": 1, "Yarn": 0},
        )

    def test_the_cards_reading_narrows_too_and_counts_cards(self):
        """The stack is checked at the table it belongs to, so the same
        filter follows into the second reading — counting cards there, not
        rows left."""
        run, url = self.open_close()
        for row in run.rows.all():
            self.client.post(url, count_post(row, 1))

        response = self.client.get(url, {"mode": "cards", "category": "Yarn"})

        self.assertEqual([r.finished_product.name for r in response.context["cards"]],
                         ["Yarn Skein"])
        self.assertEqual(
            {p["label"]: p["count"] for p in response.context["category_pills"]},
            {"All": 2, "Silk": 1, "Yarn": 1},
        )

    def test_saving_counts_comes_back_to_the_same_table(self):
        """Worked in several passes, so a submit at the yarn boards must not
        land back on a list that starts with the silk."""
        run, url = self.open_close()
        row = run.rows.get(finished_product__name="Yarn Skein")

        response = self.client.post(
            url + "?mode=count&category=Yarn", count_post(row, 1)
        )

        self.assertRedirects(response, url + "?mode=count&category=Yarn")

    def test_undo_comes_back_to_the_same_table(self):
        """Undo posts to its own URL, so the table rides in the form or it is
        lost."""
        run, url = self.open_close()
        row = run.rows.get(finished_product__name="Yarn Skein")
        self.client.post(url, count_post(row, 5))

        response = self.client.post(
            reverse("close_undo", args=[run.token, row.pk]), {"category": "Yarn"}
        )

        self.assertRedirects(response, url + "?mode=count&category=Yarn")

    def test_a_tag_from_another_table_stays_where_you_are(self):
        """The search is over the whole catalogue, so a silk scarf found at
        the yarn boards is an ordinary thing to add — and being made to
        switch tables to answer a tag you are holding is the app arguing
        with the person who found it.

        A hand-added row is on every table's list for that reason: it isn't
        on a table, it is in somebody's hand.
        """
        run, url = self.open_close()
        stray = make_close_product("Stray Silk", on_hand=9, slots=2)

        response = self.client.post(
            reverse("close_add_tag", args=[run.token]),
            {"product_id": stray.pk, "category": "Yarn"},
        )

        self.assertRedirects(response, url + "?mode=count&category=Yarn")
        html = self.client.get(
            url, {"mode": "count", "category": "Yarn"}
        ).content.decode()
        self.assertIn("Stray Silk", html)
        self.assertIn("Yarn Skein", html)
        self.assertNotIn("Silk Sash", html)

    def test_an_unknown_table_shows_every_row(self):
        """A renamed category or a hand-edited link degrades to the whole
        list, never to an error or to a shorter one — a filter is
        navigation."""
        run, url = self.open_close()

        response = self.client.get(url, {"mode": "count", "category": "Denim"})

        self.assertIsNone(response.context["category"])
        self.assertEqual(response.context["hidden_count"], 0)
        self.assertContains(response, "Yarn Skein")
        self.assertContains(response, "Silk Sash")

    def test_one_table_draws_no_filter_at_all(self):
        """A filter offering one choice is furniture."""
        run, url = self.open_close(yarn=False)

        response = self.client.get(url, {"mode": "count"})

        self.assertEqual(response.context["category_pills"], [])
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
        # One peg each, so Stormy (2 on hand) still reads as having a bag
        # behind it and Ember (1) does not. That mix is the realistic one:
        # the crew come back with tags for both, and only one of them was
        # predicted.
        self.products = [
            make_close_product("Stormy", on_hand=2, slots=1),
            make_close_product("Ember", on_hand=1, slots=1),
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

        payload = {}
        for row in run.rows.all():
            payload.update(count_post(row, 0))
        self.client.post(reverse("close_run", args=[run.token]), payload)

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
            row, _ = closing.add_tag(run, product)
            # Everything sold, so the display is bare and the bag with it.
            closing.record_count(run, row, 0)

        call_command("import_square_sales", self._csv(), verbosity=0)

        for product in self.products:
            product.refresh_from_db()
            self.assertEqual(product.number_on_hand, 0)

        movement = sum(log.quantity for log in InventoryLog.objects.all())
        self.assertEqual(movement, -6)          # twice the -3 that really left
