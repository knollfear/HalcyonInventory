"""`private/produced-since/`: the receipt, and the button that takes one back.

The page exists because the production flow had no receipt. Every other page
that reads these rows consumes them — the label sheet turns them into
stickers, the raw shelf into a reorder date, the planner subtracts them from a
shortage — and none of them ever showed the rows, so recording a dye bath was
a commitment with nothing to read back and no way out. The person who does the
dyeing responded the way anybody would: by not using the form.

Two properties are load-bearing and both are pinned here.

**It reads dyeing and nothing else.** An adjustment is a recount, and a page
meant to answer "did I dye this" must not also be answering "did somebody find
a bag of it in a cupboard". That was the fault the label sheet had, in the same
query.

**A retraction leaves no mark on the page.** Both rows drop out, so reading
the page tells you the app's current belief and nothing about what anybody got
wrong. A struck-through row reading *taken back* is a standing note about a
mistake on a page somebody opens every week, and a correction that leaves one
behind has a price on it — which is exactly the price that gets a wrong number
left unmentioned instead. Nothing is hidden from the *record*: both rows are
in `InventoryLog` for good, linked by `reverses`.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .. import producedsince
from ..forms import ProducedSinceForm
from ..models import (
    FinishedProduct,
    InventoryLog,
    ProductionRun,
    ProductionRunRow,
    RawProduct,
    RawProductCategory,
)
from .helpers import make_bathable, make_product, make_recipe


class ProducedSinceListTests(TestCase):
    """What lands on the page, and what deliberately doesn't."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_bathable(self.recipe, "Half Circle Veil", on_hand=12)
        self.url = reverse("produced_since")

    def _log(self, quantity, log_type=InventoryLog.PRODUCTION, days_ago=1, **kw):
        log = InventoryLog.objects.create(
            finished_product=self.product,
            raw_product=self.product.raw_product,
            log_type=log_type,
            quantity=quantity,
            **kw,
        )
        InventoryLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timedelta(days=days_ago)
        )
        log.refresh_from_db()
        return log

    def test_a_dye_bath_is_listed_with_what_the_app_believes(self):
        self._log(5)

        body = self.client.get(self.url).content.decode()

        self.assertIn(self.product.name, body)
        self.assertIn("+5", body)
        self.assertIn("12", body, "what it thinks is on hand now")

    def test_a_recount_is_not_listed(self):
        """The fault the label sheet had: an adjustment is a recount, not a
        bath, and a page that mixes them answers neither question."""
        self._log(9, log_type=InventoryLog.ADJUSTMENT,
                  source=InventoryLog.SOURCE_SUNDAY_CLOSE)

        record = producedsince.build(timezone.localdate() - timedelta(days=7))

        self.assertTrue(record.is_empty)

    def test_a_sale_is_not_listed(self):
        self._log(-3, log_type=InventoryLog.SALE)

        record = producedsince.build(timezone.localdate() - timedelta(days=7))

        self.assertTrue(record.is_empty)

    def test_entries_group_by_day_newest_first(self):
        self._log(4, days_ago=1)
        self._log(5, days_ago=3)

        record = producedsince.build(timezone.localdate() - timedelta(days=7))

        self.assertEqual([g.units for g in record.groups], [4, 5])
        self.assertEqual(record.units, 9)
        self.assertEqual(record.count, 2)

    def test_two_entries_on_one_day_share_a_group(self):
        self._log(4, days_ago=2)
        self._log(5, days_ago=2)

        record = producedsince.build(timezone.localdate() - timedelta(days=7))

        self.assertEqual(len(record.groups), 1)
        self.assertEqual(record.groups[0].units, 9)

    def test_the_cutoff_excludes_older_entries(self):
        self._log(4, days_ago=2)
        self._log(99, days_ago=40)

        record = producedsince.build(timezone.localdate() - timedelta(days=7))

        self.assertEqual(record.units, 4)

    def test_a_month_only_card_heads_its_own_group(self):
        """Stored on the 1st as sort padding, so the group says the month and
        no more — printing a day nobody wrote down is what `when` exists to
        stop, and grouping is the same rule."""
        log = self._log(6, days_ago=200, date_precision=InventoryLog.MONTH,
                        source=InventoryLog.SOURCE_CARD_BACKFILL)

        record = producedsince.build(timezone.localdate() - timedelta(days=365))

        labels = [g.label for g in record.groups]
        self.assertIn(log.day, labels)
        self.assertNotIn(":", " ".join(labels), "no clock on any group")

    def test_the_default_window_is_used_when_nothing_is_asked_for(self):
        self._log(4, days_ago=2)
        self._log(99, days_ago=ProducedSinceForm.DEFAULT_DAYS + 10)

        body = self.client.get(self.url).content.decode()

        self.assertIn("+4", body)
        self.assertNotIn("+99", body)

    def test_an_unreadable_since_falls_back_rather_than_blanking_the_page(self):
        self._log(4, days_ago=2)

        response = self.client.get(self.url, {"since": "not-a-date"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("+4", response.content.decode())

    def test_the_window_is_in_the_url(self):
        self._log(4, days_ago=20)

        body = self.client.get(
            self.url,
            {"since": (timezone.localdate() - timedelta(days=3)).isoformat()},
        ).content.decode()

        self.assertNotIn("+4", body)


class ProducedSinceCategoryTests(TestCase):
    """Yarn or silk, because a session is usually one or the other."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.recipe = make_recipe("Stormy Sea")
        self.silk = make_product(self.recipe, "Half Circle Veil", with_image=False)
        yarn_category, _ = RawProductCategory.objects.get_or_create(name="Yarn")
        self.yarn_blank = RawProduct.objects.create(
            name="Heavenly", category=yarn_category, price="9.00",
        )
        self.yarn = FinishedProduct.objects.create(
            name="Heavenly — Stormy Sea", raw_product=self.yarn_blank,
            recipe=self.recipe, price="24.00",
        )
        for product, quantity in ((self.silk, 4), (self.yarn, 7)):
            InventoryLog.objects.create(
                finished_product=product, raw_product=product.raw_product,
                log_type=InventoryLog.PRODUCTION, quantity=quantity,
            )
        self.since = timezone.localdate() - timedelta(days=7)

    def test_unfiltered_shows_both(self):
        record = producedsince.build(self.since)

        self.assertEqual(record.units, 11)
        self.assertEqual(dict(record.by_category), {"Silk": 4, "Yarn": 7})

    def test_filtering_to_yarn_drops_the_silk(self):
        record = producedsince.build(
            self.since, category=self.yarn_blank.category
        )

        self.assertEqual(record.units, 7)
        self.assertEqual(dict(record.by_category), {"Yarn": 7})

    def test_the_filter_is_a_query_parameter_named_after_the_field(self):
        body = self.client.get(
            reverse("produced_since"),
            {"category": self.yarn_blank.category.pk, "since": self.since},
        ).content.decode()

        self.assertIn(self.yarn.name, body)
        self.assertNotIn(self.silk.name, body)


class TakeItBackTests(TestCase):
    """The undo: finished down, blanks back, and nothing deleted."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_bathable(
            self.recipe, "Half Circle Veil", on_hand=0, bath=5
        )
        self.raw = self.product.raw_product

    def _record_a_bath(self):
        """Through the page that records one, so the log is a real one."""
        self.client.post(
            reverse("record_dye_bath", args=[self.product.pk]), {"qty": "5"}
        )
        self.product.refresh_from_db()
        self.raw.refresh_from_db()
        return InventoryLog.objects.get(log_type=InventoryLog.PRODUCTION)

    def _take_back(self, log, **extra):
        return self.client.post(
            reverse("produced_since_retract", args=[log.pk]), extra
        )

    def test_recording_a_bath_moves_stock_both_ways(self):
        """The starting position, so the reversal has something to reverse."""
        self._record_a_bath()

        self.assertEqual(self.product.number_on_hand, 5)
        self.assertEqual(self.raw.number_on_hand, 95)

    def test_taking_it_back_puts_both_sides_where_they_were(self):
        log = self._record_a_bath()

        self._take_back(log)

        self.product.refresh_from_db()
        self.raw.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 0)
        self.assertEqual(
            self.raw.number_on_hand, 100,
            "the bath never ran, so the blanks were never wet",
        )

    def test_nothing_is_deleted_and_the_two_rows_are_linked(self):
        log = self._record_a_bath()

        self._take_back(log)

        log.refresh_from_db()
        self.assertEqual(log.quantity, 5, "the original is untouched")
        reversal = log.reversals.get()
        self.assertEqual(reversal.quantity, -5)
        self.assertEqual(reversal.log_type, InventoryLog.ADJUSTMENT)
        self.assertEqual(reversal.source, InventoryLog.SOURCE_PRODUCTION_UNDO)
        self.assertEqual(reversal.finished_product_id, self.product.pk)

    def test_the_reason_typed_in_lands_on_the_record(self):
        log = self._record_a_bath()

        self._take_back(log, reason="entered it twice")

        self.assertIn("entered it twice", log.reversals.get().notes)

    def test_a_double_tap_takes_it_back_once(self):
        """Two requests, one bath. Without the guard the second one takes five
        real scarves off the shelf, silently."""
        log = self._record_a_bath()

        self._take_back(log)
        self._take_back(log)

        self.product.refresh_from_db()
        self.raw.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 0)
        self.assertEqual(self.raw.number_on_hand, 100)
        self.assertEqual(log.reversals.count(), 1)

    def test_a_sale_cannot_be_taken_back_here(self):
        sale = InventoryLog.objects.create(
            finished_product=self.product, log_type=InventoryLog.SALE,
            quantity=-2,
        )

        self.assertEqual(self._take_back(sale).status_code, 404)
        self.assertIsNone(producedsince.retract(sale))

    def test_the_endpoint_is_post_only(self):
        log = self._record_a_bath()

        response = self.client.get(
            reverse("produced_since_retract", args=[log.pk])
        )

        self.assertEqual(response.status_code, 405)

    def test_it_lands_back_on_the_window_it_came_from(self):
        """Taking one entry back must not reset the page and lose her place."""
        log = self._record_a_bath()

        response = self._take_back(log, window="since=2026-01-01")

        self.assertRedirects(
            response, f"{reverse('produced_since')}?since=2026-01-01"
        )

    def test_the_window_field_cannot_send_her_somewhere_else(self):
        """It is a query string, not a `next` URL — the view rebuilds its own
        path around it, so there is nothing here to redirect off-site with."""
        log = self._record_a_bath()

        response = self._take_back(log, window="https://example.test/")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("produced_since")))

    def test_a_sale_between_the_mistake_and_the_undo_degrades_safely(self):
        """Reversed as a delta, never as a restored absolute — and
        `set_on_hand` clamps at zero."""
        log = self._record_a_bath()
        self.product.set_on_hand(2)

        self._take_back(log)

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 0)


class TheUndoLeavesNoMarkTests(TestCase):
    """Reading the page must not tell anybody what somebody got wrong.

    This is the whole reason the button gets used. A mistake somebody cannot
    fix themselves is a mistake they have to go and confess — and a fix that
    leaves a struck-through row on a page opened every week is only half a
    fix, because the confession is still sitting there. The record keeps
    everything; the page keeps none of it.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_bathable(
            self.recipe, "Half Circle Veil", on_hand=0, bath=5
        )
        self.url = reverse("produced_since")

    def _record_and_take_back(self):
        """The real flow, redirects followed.

        Following them matters: each step's flash message is consumed by the
        page it lands on, which is where it belongs — narrated once, to the
        person who just asked for it, and then gone. What the tests below
        then load is the page as anybody else finds it.
        """
        self.client.post(
            reverse("record_dye_bath", args=[self.product.pk]), {"qty": "5"},
            follow=True,
        )
        log = InventoryLog.objects.get(log_type=InventoryLog.PRODUCTION)
        self.client.post(
            reverse("produced_since_retract", args=[log.pk]), follow=True
        )
        return log

    def test_the_retracted_entry_is_gone_from_the_page(self):
        self._record_and_take_back()

        body = self.client.get(self.url).content.decode()

        self.assertNotIn("+5", body)

    def test_the_compensating_row_is_not_on_the_page_either(self):
        """It is an ADJUSTMENT, which this page never reads — so the pair
        cancels out completely rather than showing up as a −5."""
        self._record_and_take_back()

        body = self.client.get(self.url).content.decode()

        self.assertNotIn("-5", body)
        self.assertNotIn("−5", body)

    def test_the_totals_say_nothing_about_it(self):
        self._record_and_take_back()

        record = producedsince.build(timezone.localdate() - timedelta(days=7))

        self.assertTrue(record.is_empty)
        self.assertEqual(record.units, 0)
        self.assertEqual(record.count, 0)

    def test_no_word_for_it_appears_anywhere_on_the_page(self):
        self._record_and_take_back()

        body = self.client.get(self.url).content.decode().lower()

        for word in ("taken back", "retracted", "undone", "reversed",
                     "cancelled", "correction"):
            self.assertNotIn(word, body, f"the page must not say {word!r}")

    def test_the_record_still_has_both_rows(self):
        """Nothing is hidden from the database, which is the whole basis for
        hiding it from the page."""
        log = self._record_and_take_back()

        self.assertEqual(
            InventoryLog.objects.filter(finished_product=self.product).count(), 2
        )
        self.assertEqual(log.reversals.get().reverses_id, log.pk)


class HistoryOnlyEntriesTests(TestCase):
    """A kanban card typed up wrong moves no stock in either direction."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_bathable(
            self.recipe, "Half Circle Veil", on_hand=7, bath=5
        )

    def _card_entry(self):
        return InventoryLog.objects.create(
            finished_product=self.product,
            raw_product=self.product.raw_product,
            log_type=InventoryLog.PRODUCTION,
            source=InventoryLog.SOURCE_CARD_BACKFILL,
            quantity=10,
            notes="History only; stock unchanged.",
        )

    def test_a_card_entry_is_marked_as_history_only(self):
        self._card_entry()

        record = producedsince.build(timezone.localdate() - timedelta(days=7))

        self.assertFalse(record.groups[0].rows[0].moved_stock)

    def test_taking_one_back_moves_nothing(self):
        """It never moved stock, so reversing it must not either — an Undo on
        a two-year-old card would otherwise quietly take ten scarves off
        today's shelf."""
        log = self._card_entry()

        self.client.post(reverse("produced_since_retract", args=[log.pk]))

        self.product.refresh_from_db()
        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 7)
        self.assertEqual(self.product.raw_product.number_on_hand, 100)

    def test_it_still_writes_the_compensating_row(self):
        """So `reversals` stays the single answer to 'has this been taken
        back', rather than needing a second mechanism for these."""
        log = self._card_entry()

        self.client.post(reverse("produced_since_retract", args=[log.pk]))

        reversal = log.reversals.get()
        self.assertEqual(reversal.quantity, 0)
        self.assertIn("History only", reversal.notes)

    def test_and_it_leaves_the_page(self):
        log = self._card_entry()

        self.client.post(reverse("produced_since_retract", args=[log.pk]))

        record = producedsince.build(timezone.localdate() - timedelta(days=7))
        self.assertTrue(record.is_empty)


class BlanksGoBackTests(TestCase):
    """How many blanks a retraction can vouch for, in the two odd cases."""

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_bathable(
            self.recipe, "Half Circle Veil", on_hand=0, bath=5
        )
        self.raw = self.product.raw_product
        self.run = ProductionRun.objects.create()
        from .. import production
        # The blanks come off the shelf here, when the run is made.
        self.row = production.open_rows(self.run, [(self.product, 5)])[0]

    def test_a_short_bath_returns_the_whole_bath_s_blanks(self):
        """The whole bath's blanks were claimed when the run was planned, and
        only the yield ever reached the finished side. So undoing it has to
        put five back, not the three that survived."""
        from .. import production

        log = production.apply_row(self.row, yielded=3)
        self.product.refresh_from_db()
        self.raw.refresh_from_db()
        self.assertEqual((self.product.number_on_hand, self.raw.number_on_hand),
                         (3, 95))

        producedsince.retract(log)

        self.product.refresh_from_db()
        self.raw.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 0)
        self.assertEqual(self.raw.number_on_hand, 100)

    def test_the_sheet_row_keeps_its_log(self):
        """`applied_log` is what stops a re-scanned sheet dyeing the same bath
        twice on paper. Handing that guard back for a tidier-looking row would
        trade a silent double-count for a cosmetic one."""
        from .. import production

        log = production.apply_row(self.row)

        producedsince.retract(log)

        self.row.refresh_from_db()
        self.assertEqual(self.row.applied_log_id, log.pk)
        self.assertTrue(self.row.is_accepted)

    def test_a_fancy_sibling_returns_no_blanks_of_its_own(self):
        """A bath of five reported as four plain and one fancy writes two
        entries against one pot. Restoring blanks on both would put the bath
        back twice."""
        from .. import production

        fancy_blank = RawProduct.objects.create(
            name="Fancy Half Circle Veil", category=self.raw.category,
            price=self.raw.price, made_in_a_dye_bath=False,
            number_per_dye_bath=5,
        )
        fancy_product = FinishedProduct.objects.create(
            name="Fancy Half Circle Veil — Stormy Sea",
            raw_product=fancy_blank, recipe=self.recipe, price="40.00", par=0,
        )
        self.raw.fancy_counterpart = fancy_blank
        self.raw.save(update_fields=["fancy_counterpart"])

        production.apply_row(self.row, yielded=5, fancy=1)
        fancy_log = InventoryLog.objects.get(finished_product=fancy_product)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 95)

        self.assertEqual(producedsince.blanks_consumed(fancy_log), 0)

        producedsince.retract(fancy_log)

        fancy_product.refresh_from_db()
        self.raw.refresh_from_db()
        self.assertEqual(fancy_product.number_on_hand, 0, "the finished side reverses")
        self.assertEqual(
            self.raw.number_on_hand, 95,
            "the plain entry is still accounting for the pot",
        )


class ProducedSinceIsNotAReportTests(TestCase):
    """It reads like one and it writes, so it is filed with what it reads."""

    def test_it_is_a_production_page(self):
        from .. import views

        self.assertEqual(
            views.produced_since_view.page_meta["category"], "Production"
        )

    def test_it_is_on_the_staff_map(self):
        self.client.force_login(User.objects.create_user("staff", password="pw"))

        body = self.client.get(reverse("index")).content.decode()

        self.assertIn("Produced Since", body)
