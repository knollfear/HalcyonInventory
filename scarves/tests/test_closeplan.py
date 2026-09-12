"""`private/production-from-close/`: Sunday night's cards into baths to dye.

The shop's own production loop, which the app spent a long time not modelling.
The crew walk the display on Sunday night, count what is there, and the evening
ends with a stack of kanban cards — one per product whose bag is empty. That
stack *is* the week's work order, and it has been for years.

What the app offered instead was shortages against par. Par was never dialled
in, so that page asks somebody to trust a number she did not choose about a
shelf she walked past twelve hours ago — and it went unused. The close had
already been built and turned out to be the model for how the work actually
happens rather than one more report.

Three properties are load-bearing and all three are pinned here.

**One claim, whoever wrote it.** Par and the close both propose, and they
compete only if both can plan the same colorway. A `ProductionRunRow` is the
single claim, matched on finished product — so a card that lands on any list
is accounted for and drops off the pool, and a colorway already out on a
par-based sheet never enters it.

**One close, several lists.** Five cards onto list A and those five are gone;
ten more onto B; the balance onto C. Nothing is planned twice unless somebody
deliberately puts it back.

**The pool is frozen at the close.** A card is what Sunday found, not what is
true now — dye a bath on Wednesday and a live test would flip, quietly
dropping rows out from under a half-made plan.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .. import closeplan, closing, production
from ..models import (
    CloseRun,
    CloseRunRow,
    FinishedProduct,
    InventoryLog,
    ProductionRun,
    ProductionRunRow,
    RawProduct,
)
from .helpers import (
    make_close_product,
    make_employee,
    make_product,
    make_recipe,
    make_undyed,
)


def answer(run, product, counted):
    """Count one row, the way the close's own page does."""
    row = run.rows.get(finished_product=product)
    return closing.record_count(run, row, counted)


class TheStackTests(TestCase):
    """What counts as a card, and what deliberately doesn't."""

    def setUp(self):
        self.employee = make_employee("Close Walker")

    def _close(self):
        run, _ = closing.run_for_today(employee=self.employee)
        return run

    def test_a_row_counted_at_or_under_its_pegs_is_a_card(self):
        """The app's own words for an empty bag, asked of a physical count."""
        product = make_close_product("Two On The Pegs", on_hand=2, slots=2)
        run = self._close()
        answer(run, product, 2)

        stack = closeplan.cards(run)

        self.assertEqual([card.product for card in stack], [product])

    def test_a_bag_found_under_the_table_is_not_a_card(self):
        """Counted above what the pegs hold means there was backstock after
        all, which is the one answer that takes a product off the stack."""
        product = make_close_product("Bag Behind It", on_hand=0, slots=2)
        run = self._close()
        answer(run, product, 9)

        self.assertEqual(closeplan.cards(run), [])

    def test_a_row_nobody_counted_is_not_a_card(self):
        """A pending row is "nobody looked", never a zero. Manufacturing work
        out of the pegs somebody didn't get to is the mistake
        `production.stockout_baths` refuses to make."""
        make_close_product("Never Asked", on_hand=0, slots=2)
        run = self._close()

        self.assertEqual(closeplan.cards(run), [])

    def test_the_pool_does_not_move_when_stock_does(self):
        """Frozen at the close, deliberately unlike `closing.card_status`.

        That one asks the live number because it is answering "what should be
        in the stack right now". This one asks what Sunday found — a pool that
        flipped when a bath landed would drop rows out from under a half-made
        plan with nothing to say whether a missing card was made, claimed, or
        never there."""
        product = make_close_product("Dyed On Wednesday", on_hand=0, slots=2)
        run = self._close()
        answer(run, product, 0)

        product.set_on_hand(20)

        self.assertEqual([card.product for card in closeplan.cards(run)], [product])

    def test_a_passthrough_tag_is_not_a_dye_list_row(self):
        """It genuinely ran out and the tag is genuinely in somebody's hand.
        It just arrives in a box — putting it on a dye list sends somebody to
        a sink to make a thing that is not made there.

        Reached by the unpredicted-tag search, because that is the only way
        one of these gets onto a close at all: `expected_products` already
        excludes a null recipe, so the predicted list never carries one. The
        tag search is over the whole catalogue on purpose, which is what
        leaves this path open."""
        yarn = make_undyed("Undyed Heavenly", on_hand=0)
        run = self._close()
        row, _ = closing.add_tag(run, yarn)
        closing.record_count(run, row, 0)

        self.assertEqual(closeplan.cards(run), [])

    def test_a_fancy_veil_card_is_not_either(self):
        """Line work on a scarf that already exists — it carries a colorway
        and slips past every dyed-only test, and no bath answers a shortage
        of one."""
        recipe = make_recipe("Stormy Sea")
        blank = RawProduct.objects.create(
            name="Fancy Half Circle", category=make_product(
                make_recipe("Other"), "Filler", with_image=False
            ).raw_product.category,
            price="9.00", made_in_a_dye_bath=False,
        )
        fancy = FinishedProduct.objects.create(
            name="Fancy Half Circle — Stormy Sea", raw_product=blank,
            recipe=recipe, price="40.00", display_slots=2, par=3,
        )
        run = self._close()
        answer(run, fancy, 0)

        self.assertEqual(closeplan.cards(run), [])

    def test_a_retired_colorway_card_is_not_either(self):
        """*Retire, don't delete* says retiring takes something out of
        production planning, and that has to hold for a colorway as well as a
        product — the symptom otherwise is a dye room sent to make a colour
        somebody decided to stop making."""
        product = make_close_product("Discontinued", on_hand=0, slots=2)
        run = self._close()
        answer(run, product, 0)
        self.assertEqual(len(closeplan.cards(run)), 1)

        recipe = product.recipe
        recipe.is_active = False
        recipe.save(update_fields=["is_active"])

        self.assertEqual(closeplan.cards(run), [])


class TheOrderTests(TestCase):
    """Empty pegs and last-one-hanging lead; then what it sold this season."""

    def setUp(self):
        self.employee = make_employee("Close Walker")

    def test_gone_and_down_to_one_come_before_everything_else(self):
        gone = make_close_product("Gone", on_hand=0, slots=4)
        last = make_close_product("Last One", on_hand=1, slots=4)
        some = make_close_product("Three Left", on_hand=3, slots=4)
        run, _ = closing.run_for_today(employee=self.employee)
        answer(run, gone, 0)
        answer(run, last, 1)
        answer(run, some, 3)

        stack = closeplan.cards(run)

        self.assertEqual(
            [card.is_critical for card in stack], [True, True, False],
            "critical first, whatever anything sold",
        )
        self.assertEqual(stack[-1].product, some)

    def test_the_band_is_counted_not_the_share_of_the_pegs(self):
        """Two on a four-peg hook is an empty bag and is not critical; one on
        a one-peg hook is. The band is about the shelf, not the fraction."""
        wide = make_close_product("Wide Hook", on_hand=2, slots=4)
        narrow = make_close_product("One Peg", on_hand=1, slots=1)
        run, _ = closing.run_for_today(employee=self.employee)
        answer(run, wide, 2)
        answer(run, narrow, 1)

        by_product = {c.product: c for c in closeplan.cards(run)}

        self.assertFalse(by_product[wide].is_critical)
        self.assertTrue(by_product[narrow].is_critical)

    def test_both_numbers_the_order_is_built_on_are_on_the_card(self):
        """A ranking nobody can check by looking is a ranking they have to
        trust — the same reason production-needed prints what it sold."""
        product = make_close_product("Checkable", on_hand=0, slots=2)
        run, _ = closing.run_for_today(employee=self.employee)
        answer(run, product, 0)

        card = closeplan.cards(run)[0]

        self.assertEqual(card.counted, 0)
        self.assertEqual(card.sold, 0)


class ClaimsTests(TestCase):
    """One claim, matched on finished product, whoever wrote it."""

    def setUp(self):
        self.employee = make_employee("Close Walker")
        self.a = make_close_product("Colour A", on_hand=0, slots=2)
        self.b = make_close_product("Colour B", on_hand=0, slots=2)
        self.run, _ = closing.run_for_today(employee=self.employee)
        answer(self.run, self.a, 0)
        answer(self.run, self.b, 0)

    def _pool(self):
        pool, _ = closeplan.partition(closeplan.cards(self.run))
        return [card.product for card in pool]

    def test_a_card_on_a_list_comes_off_the_pool(self):
        self.assertEqual(set(self._pool()), {self.a, self.b})

        closeplan.make_list(self.run, [(self.a, 1)])

        self.assertEqual(self._pool(), [self.b])

    def test_a_card_on_a_par_based_sheet_never_enters_the_pool(self):
        """However it got onto a list, it is accounted for. The two signals
        don't compete because neither of them is the claim."""
        sheet = ProductionRun.objects.create()
        ProductionRunRow.objects.create(
            run=sheet, finished_product=self.a, order=1, quantity=4
        )

        self.assertEqual(self._pool(), [self.b])

    def test_a_reported_bath_keeps_its_claim(self):
        """Deliberately unlike `production.in_flight`, which counts pending
        rows only. There the question is "how much is still out being dyed";
        here it is "is this card dealt with", and a card she made on Friday is
        the most dealt-with a card gets."""
        run = closeplan.make_list(self.run, [(self.a, 1)])
        production.apply_row(run.rows.get())

        self.assertEqual(self._pool(), [self.b])

    def test_calling_a_bath_off_puts_the_card_back(self):
        """The bath never ran, so the card goes back in the pool — the same
        promise the crew's *not coming* button relies on."""
        run = closeplan.make_list(self.run, [(self.a, 1)])
        self.assertEqual(self._pool(), [self.b])

        production.cancel_row(run.rows.get())

        self.assertEqual(set(self._pool()), {self.a, self.b})

    def test_a_list_from_before_this_close_does_not_claim(self):
        """A card that came up again on Sunday came up again for a reason —
        the shelf was counted and it was still empty."""
        old = ProductionRun.objects.create()
        ProductionRunRow.objects.create(
            run=old, finished_product=self.a, order=1, quantity=4
        )
        ProductionRun.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=9)
        )

        self.assertEqual(set(self._pool()), {self.a, self.b})

    def test_a_claimed_card_is_listed_rather_than_dropped(self):
        """A card missing with nothing said reads exactly like a card that
        was never in the stack."""
        run = closeplan.make_list(self.run, [(self.a, 1)])

        _, listed = closeplan.partition(closeplan.cards(self.run))

        self.assertEqual([card.product for card in listed], [self.a])
        self.assertEqual([row.run_id for row in listed[0].claimed_by], [run.pk])

    def test_adding_a_claimed_card_back_deliberately_is_allowed(self):
        """"Nothing gets planned twice unless she added it" is a statement
        about the default, not a refusal."""
        closeplan.make_list(self.run, [(self.a, 1)])

        second = closeplan.make_list(self.run, [(self.a, 1)])

        self.assertEqual(second.rows.count(), 1)


class FiveThenTenThenTheBalanceTests(TestCase):
    """The worked case, in the words it was asked for."""

    def setUp(self):
        self.employee = make_employee("Close Walker")
        self.products = [
            make_close_product(f"Colour {n:02d}", on_hand=0, slots=2)
            for n in range(20)
        ]
        self.close, _ = closing.run_for_today(employee=self.employee)
        for product in self.products:
            answer(self.close, product, 0)

    def _pool(self):
        pool, _ = closeplan.partition(closeplan.cards(self.close))
        return pool

    def test_five_off_then_ten_then_the_balance(self):
        self.assertEqual(len(self._pool()), 20)

        first_five = [(card.product, 1) for card in self._pool()[:5]]
        list_a = closeplan.make_list(self.close, first_five)
        self.assertEqual(len(self._pool()), 15)

        next_ten = [(card.product, 1) for card in self._pool()[:10]]
        list_b = closeplan.make_list(self.close, next_ten)
        self.assertEqual(len(self._pool()), 5)

        balance = [(card.product, 1) for card in self._pool()]
        list_c = closeplan.make_list(self.close, balance)
        self.assertEqual(self._pool(), [])

        # Nothing planned twice: twenty cards, twenty baths, no product on two
        # lists.
        rows = ProductionRunRow.objects.filter(
            run__in=[list_a.pk, list_b.pk, list_c.pk]
        )
        self.assertEqual(rows.count(), 20)
        self.assertEqual(
            len({row.finished_product_id for row in rows}), 20
        )

    def test_the_lists_are_named_in_the_order_they_were_made(self):
        a = closeplan.make_list(self.close, [(self.products[0], 1)])
        b = closeplan.make_list(self.close, [(self.products[1], 1)])

        self.assertEqual(
            [run.pk for run in closeplan.lists_for(self.close)], [a.pk, b.pk]
        )


class MakingTheListTests(TestCase):
    """Rows, bath sizes, and how "I made more" is said."""

    def setUp(self):
        self.employee = make_employee("Close Walker")
        self.product = make_close_product("Cabernet Veil", on_hand=0, slots=2)
        RawProduct.objects.filter(pk=self.product.raw_product_id).update(
            number_per_dye_bath=5, number_on_hand=100
        )
        self.product.refresh_from_db()
        self.close, _ = closing.run_for_today(employee=self.employee)
        answer(self.close, self.product, 0)

    def test_three_baths_are_three_rows(self):
        """Every end state downstream is per bath: three baths where one pot
        failed is 5, 5, 0, which a single row of fifteen could not say."""
        run = closeplan.make_list(self.close, [(self.product, 3)])

        self.assertEqual(run.rows.count(), 3)
        self.assertEqual(
            [row.quantity for row in run.rows.all()], [5, 5, 5]
        )
        self.assertEqual([row.order for row in run.rows.all()], [1, 2, 3])

    def test_the_bath_size_is_frozen_onto_the_row(self):
        """The same promise the printed sheet makes — edit the bath size next
        week and this row still means what it said."""
        run = closeplan.make_list(self.close, [(self.product, 1)])
        RawProduct.objects.filter(pk=self.product.raw_product_id).update(
            number_per_dye_bath=99
        )

        self.assertEqual(run.rows.get().quantity, 5)

    def test_the_list_records_which_close_it_came_from(self):
        run = closeplan.make_list(self.close, [(self.product, 1)])

        self.assertEqual(run.close_run_id, self.close.pk)
        self.assertEqual(list(self.close.production_runs.all()), [run])

    def test_another_bath_appends_a_row(self):
        """"If I made more, let me say so." The list said one bath and the
        session ran two."""
        run = closeplan.make_list(self.close, [(self.product, 1)])

        closeplan.add_bath(run, self.product)

        self.assertEqual(run.rows.count(), 2)
        self.assertEqual([row.order for row in run.rows.all()], [1, 2])

    def test_two_baths_of_one_colorway_are_one_question_to_answer(self):
        """`lines_for` folds them back: the crew are standing in front of one
        pile of ten, not two piles of five."""
        run = closeplan.make_list(self.close, [(self.product, 2)])

        lines = production.lines_for_run(run)

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].quantity, 10)
        self.assertEqual(lines[0].baths, 2)


class ParsingPicksTests(TestCase):
    """Blank means not this one; a bad number refuses rather than guessing."""

    def setUp(self):
        self.employee = make_employee("Close Walker")
        self.a = make_close_product("Colour A", on_hand=0, slots=2)
        self.b = make_close_product("Colour B", on_hand=0, slots=2)
        self.close, _ = closing.run_for_today(employee=self.employee)
        answer(self.close, self.a, 0)
        answer(self.close, self.b, 0)
        self.pool, _ = closeplan.partition(closeplan.cards(self.close))

    def _parse(self, data):
        return closeplan.parse_picks(data, self.pool)

    def test_an_untouched_box_is_not_a_pick(self):
        picks, problems = self._parse({f"baths_{self.a.pk}": "2"})

        self.assertEqual(picks, [(self.a, 2)])
        self.assertEqual(problems, [])

    def test_zero_is_not_a_pick_either(self):
        picks, problems = self._parse({
            f"baths_{self.a.pk}": "0", f"baths_{self.b.pk}": "1",
        })

        self.assertEqual(picks, [(self.b, 1)])
        self.assertEqual(problems, [])

    def test_a_number_that_isnt_one_is_refused(self):
        picks, problems = self._parse({f"baths_{self.a.pk}": "two"})

        self.assertEqual(picks, [])
        self.assertEqual(len(problems), 1)

    def test_a_slip_of_a_digit_is_refused(self):
        """Two digits in this box is almost always a number somebody meant to
        delete half of, and nothing real is on the other side of the line."""
        picks, problems = self._parse({
            f"baths_{self.a.pk}": str(closeplan.MAX_BATHS_PER_CARD + 5)
        })

        self.assertEqual(picks, [])
        self.assertEqual(len(problems), 1)

    def test_a_key_for_something_not_on_offer_is_ignored(self):
        """This form is a list of cards with a number beside each, so a key
        naming anything else is not a pick somebody made on this page."""
        other = make_product(make_recipe("Elsewhere"), "Elsewhere", with_image=False)

        picks, problems = self._parse({f"baths_{other.pk}": "3"})

        self.assertEqual(picks, [])
        self.assertEqual(problems, [])


class PaperOrNotTests(TestCase):
    """One door, decided once, stored on the run."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.employee = make_employee("Close Walker")
        self.product = make_close_product("Cabernet Veil", on_hand=0, slots=2)
        self.close, _ = closing.run_for_today(employee=self.employee)
        answer(self.close, self.product, 0)
        self.url = reverse("production_from_close")

    def _make(self, **extra):
        data = {"close": self.close.pk, f"baths_{self.product.pk}": "1"}
        data.update(extra)
        self.client.post(self.url, data)
        return ProductionRun.objects.latest("pk")

    def test_paper_is_the_default(self):
        """Every sheet made before this existed was paper, so that is what an
        unanswered field has to mean."""
        self.assertTrue(self._make().is_on_paper)

    def test_no_paper_is_recorded(self):
        run = self._make(reporting=ProductionRun.DIRECT)

        self.assertFalse(run.is_on_paper)
        self.assertEqual(run.reporting, ProductionRun.DIRECT)

    def test_a_paper_list_leads_with_the_printout(self):
        run = self._make()

        body = self.client.get(
            reverse("production_run_detail", args=[run.pk])
        ).content.decode()

        self.assertIn("Print the sheet", body)
        self.assertIn("say what you made without printing", body)

    def test_a_direct_list_leads_with_the_report(self):
        run = self._make(reporting=ProductionRun.DIRECT)

        body = self.client.get(
            reverse("production_run_detail", args=[run.pk])
        ).content.decode()

        self.assertIn("Say what you made", body)

    def test_either_way_the_reporting_page_is_the_same_one(self):
        """The modes differ in which door the page offers, never in what the
        reporting flow is — same rows, same end states, same `accept_line`."""
        run = self._make(reporting=ProductionRun.DIRECT)

        response = self.client.get(
            reverse("production_run", args=[run.token])
        )

        self.assertEqual(response.status_code, 200)


class ThePageTests(TestCase):
    """The picker, end to end."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.employee = make_employee("Close Walker")
        self.url = reverse("production_from_close")

    def test_with_no_close_at_all_it_says_so(self):
        body = self.client.get(self.url).content.decode()

        self.assertIn("No close has been done yet", body)

    def test_it_plans_from_the_latest_close_by_default(self):
        product = make_close_product("Cabernet Veil", on_hand=0, slots=2)
        close, _ = closing.run_for_today(employee=self.employee)
        answer(close, product, 0)

        body = self.client.get(self.url).content.decode()

        self.assertIn(product.name, body)

    def test_an_older_close_is_query_string_state(self):
        product = make_close_product("Last Week", on_hand=0, slots=2)
        old = CloseRun.objects.create(
            day=timezone.localdate() - timedelta(days=7), employee=self.employee
        )
        CloseRunRow.objects.create(
            run=old, finished_product=product, on_hand_before=0,
            display_slots=2, outcome=CloseRunRow.CONFIRMED, counted=0,
            decided_at=timezone.now(),
        )
        closing.run_for_today(employee=self.employee)

        body = self.client.get(self.url, {"close": old.pk}).content.decode()

        self.assertIn(product.name, body)

    def test_making_a_list_lands_on_it(self):
        product = make_close_product("Cabernet Veil", on_hand=0, slots=2)
        close, _ = closing.run_for_today(employee=self.employee)
        answer(close, product, 0)

        response = self.client.post(self.url, {
            "close": close.pk, f"baths_{product.pk}": "2",
        })

        run = ProductionRun.objects.latest("pk")
        self.assertRedirects(
            response, reverse("production_run_detail", args=[run.pk])
        )
        self.assertEqual(run.rows.count(), 2)

    def test_an_empty_submit_makes_nothing(self):
        product = make_close_product("Cabernet Veil", on_hand=0, slots=2)
        close, _ = closing.run_for_today(employee=self.employee)
        answer(close, product, 0)

        self.client.post(self.url, {"close": close.pk})

        self.assertEqual(ProductionRun.objects.count(), 0)

    def test_a_part_walked_close_says_what_it_is_missing(self):
        """A pending row is "nobody looked", so a plan off a half-worked close
        is short by however many pegs got skipped. Said out loud, because a
        list reading as complete when a table was never counted is the silence
        the close itself exists to break."""
        answered = make_close_product("Counted", on_hand=0, slots=2)
        make_close_product("Never Asked", on_hand=0, slots=2)
        close, _ = closing.run_for_today(employee=self.employee)
        answer(close, answered, 0)

        body = self.client.get(self.url).content.decode()

        self.assertIn("was never counted", body)

    def test_the_page_says_where_a_claimed_card_went(self):
        product = make_close_product("Cabernet Veil", on_hand=0, slots=2)
        close, _ = closing.run_for_today(employee=self.employee)
        answer(close, product, 0)
        run = closeplan.make_list(close, [(product, 1)])

        body = self.client.get(self.url).content.decode()

        self.assertIn("Already accounted for", body)
        self.assertIn(reverse("production_run_detail", args=[run.pk]), body)


class TheTwoSignalsDoNotCompeteTests(TestCase):
    """The par planner and the close planner, on one claim.

    This is the answer to "those signals seem to compete". They do not,
    because neither of them is the claim: a `ProductionRunRow` is, matched on
    finished product, whoever wrote it.
    """

    def setUp(self):
        self.employee = make_employee("Close Walker")
        self.product = make_close_product("Cabernet Veil", on_hand=0, slots=2, par=8)
        RawProduct.objects.filter(pk=self.product.raw_product_id).update(
            number_per_dye_bath=4, number_on_hand=100
        )
        self.product.refresh_from_db()
        self.close, _ = closing.run_for_today(employee=self.employee)
        answer(self.close, self.product, 0)

    def test_a_close_list_stops_the_par_planner_asking_again(self):
        """`in_flight` subtracts pending rows whichever page wrote them, so a
        list made off the cards takes the colorway off the sheet picker."""
        self.assertIn(
            self.product, production.candidates(),
            "below par and sold out, so the planner wants it",
        )

        closeplan.make_list(self.close, [(self.product, 4)])

        self.assertNotIn(self.product, production.candidates())

    def test_a_par_sheet_stops_the_cards_asking_again(self):
        sheet = ProductionRun.objects.create()
        ProductionRunRow.objects.create(
            run=sheet, finished_product=self.product, order=1, quantity=4
        )

        pool, listed = closeplan.partition(closeplan.cards(self.close))

        self.assertEqual(pool, [])
        self.assertEqual([card.product for card in listed], [self.product])


class WhatTheListRecordsTests(TestCase):
    """Friday night: which of the cards did I make."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.employee = make_employee("Close Walker")
        self.product = make_close_product("Cabernet Veil", on_hand=0, slots=2)
        RawProduct.objects.filter(pk=self.product.raw_product_id).update(
            number_per_dye_bath=5, number_on_hand=100
        )
        self.product.refresh_from_db()
        self.close, _ = closing.run_for_today(employee=self.employee)
        answer(self.close, self.product, 0)
        self.run = closeplan.make_list(
            self.close, [(self.product, 1)], reporting=ProductionRun.DIRECT
        )

    def test_saying_she_made_it_moves_stock(self):
        self.client.post(
            reverse("production_run", args=[self.run.token]),
            {"done": [str(production.lines_for_run(self.run)[0].key)]},
        )

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 5)

    def test_and_it_shows_up_on_produced_since(self):
        """The receipt, so a list recorded is a list she can read back and
        take back."""
        self.client.post(
            reverse("production_run", args=[self.run.token]),
            {"done": [str(production.lines_for_run(self.run)[0].key)]},
        )

        body = self.client.get(reverse("produced_since")).content.decode()

        self.assertIn(self.product.name, body)
        self.assertIn("+5", body)

    def test_another_bath_is_one_post_from_the_row(self):
        row = self.run.rows.get()

        self.client.post(
            reverse("production_run_add_bath", args=[self.run.pk, row.pk])
        )

        self.assertEqual(self.run.rows.count(), 2)

    def test_the_taken_back_bath_leaves_no_trace_of_the_close_either(self):
        """The retraction rule holds whatever wrote the entry."""
        self.client.post(
            reverse("production_run", args=[self.run.token]),
            {"done": [str(production.lines_for_run(self.run)[0].key)]},
        )
        log = InventoryLog.objects.get(log_type=InventoryLog.PRODUCTION)

        self.client.post(
            reverse("produced_since_retract", args=[log.pk]), follow=True
        )

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 0)
        self.assertNotIn(
            "+5", self.client.get(reverse("produced_since")).content.decode()
        )
