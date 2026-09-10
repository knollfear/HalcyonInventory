"""Raw stock, passthroughs, fancy veils, and the corrections that heal them.

The reasoning behind these is in `docs/claude/stock.md`.
"""
import base64
import hashlib
import hmac
import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from django.db.models import ProtectedError
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from .. import (
    closing, colorbands, crew, fancy, nav, photowalk, production, restock,
    sales, seasonreport, seasons, sheetscan, skus, slowsellers, timesheets,
    weather,
)
from .. import labels as labelmod
from .. import views as viewsmod
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
    FakeSquareClient,
    FakeSquareResult,
    make_close_product,
    make_product,
    make_recipe,
    make_undyed,
)


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
class RawInventoryBillTests(TestCase):
    """One save for the whole category, because what gets typed in is a bill.

    The page used to post a row at a time — three nudge buttons and a "set"
    box per product, each its own form — so a delivery of nine lines was nine
    round trips and nine page rebuilds with the invoice in somebody's other
    hand. Now it is one form.

    Two columns, because there are two questions. *Received* is a delta: the
    note says twelve arrived and nobody should have to add twelve to the
    current figure in their head. *Counted* is an absolute: the shelf holds
    nine, whatever the app believed — the shape every correction in this app
    takes, because an absolute heals whatever went unrecorded before it.
    """

    def setUp(self):
        self.category = RawProductCategory.objects.create(name="Silk")
        self.raw = RawProduct.objects.create(
            name="8mm Habotai", category=self.category, price="5.00",
            number_on_hand=12,
        )
        self.other = RawProduct.objects.create(
            name="Sash Blank", category=self.category, price="4.00",
            number_on_hand=3,
        )
        User.objects.create_user("staff", "s@example.test", "pw")
        self.client.login(username="staff", password="pw")
        self.url = reverse("raw_inventory", args=[self.category.pk])

    def _post(self, **data):
        return self.client.post(self.url, data)

    def test_one_save_books_every_line(self):
        """The whole point: a bill is one document, not nine round trips."""
        self._post(**{
            f"received_{self.raw.pk}": "12",
            f"received_{self.other.pk}": "6",
        })

        self.raw.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 24)
        self.assertEqual(self.other.number_on_hand, 9)

    def test_a_blank_row_is_left_alone(self):
        """What makes a nine-line bill cheap on a page of forty products."""
        self._post(**{f"received_{self.raw.pk}": "1"})

        self.other.refresh_from_db()
        self.assertEqual(self.other.number_on_hand, 3)

    def test_counted_replaces_rather_than_adds(self):
        self._post(**{f"counted_{self.raw.pk}": "5"})

        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 5)

    def test_counted_zero_empties_the_shelf(self):
        """0 is a real count. The falsy-string trap would read it as blank
        and leave the row untouched, which is the opposite of what was
        said."""
        self._post(**{f"counted_{self.raw.pk}": "0"})

        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 0)

    def test_a_count_wins_over_a_delta_on_the_same_row(self):
        """A count is a measurement; a delivery note is a claim about a
        change."""
        self._post(**{
            f"counted_{self.raw.pk}": "4",
            f"received_{self.raw.pk}": "100",
        })

        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 4)

    def test_a_negative_received_is_a_return_to_the_supplier(self):
        """What the row's old -1 button was for."""
        self._post(**{f"received_{self.raw.pk}": "-3"})

        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 9)

    def test_received_cannot_drive_the_shelf_below_zero(self):
        self._post(**{f"received_{self.raw.pk}": "-99"})

        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 0)

    def test_one_bad_line_books_none_of_them(self):
        """A bill goes in whole or not at all. Half of one booked in is worse
        than none, because the missing half is invisible afterwards."""
        response = self._post(**{
            f"received_{self.raw.pk}": "12",
            f"counted_{self.other.pk}": "twelve",
        })

        self.assertEqual(response.status_code, 200)
        self.raw.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 12)
        self.assertEqual(self.other.number_on_hand, 3)

    def test_a_rejected_bill_comes_back_still_typed_in(self):
        """Losing nine lines to one fat-fingered digit is the expensive
        failure here."""
        response = self._post(**{
            f"received_{self.raw.pk}": "12",
            f"counted_{self.other.pk}": "twelve",
        })

        html = response.content.decode()
        self.assertIn('value="12"', html)
        self.assertIn('value="twelve"', html)

    def test_a_rejected_bill_names_the_line(self):
        """On a category of forty products, 'a line didn't read' without
        saying which one is a hunt."""
        response = self._post(**{f"counted_{self.other.pk}": "-2"})

        self.assertContains(response, "Sash Blank")
        self.assertContains(response, "can&#x27;t be negative", html=False)

    def test_a_line_that_changes_nothing_is_not_an_error(self):
        response = self._post(**{f"counted_{self.raw.pk}": "12"})

        self.assertEqual(response.status_code, 302)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.number_on_hand, 12)

    def test_the_page_offers_both_columns(self):
        response = self.client.get(self.url)

        self.assertContains(response, f'name="received_{self.raw.pk}"')
        self.assertContains(response, f'name="counted_{self.raw.pk}"')

    def test_there_is_one_form_and_one_button(self):
        """The regression this replaced: a submit per row."""
        html = self.client.get(self.url).content.decode()

        self.assertEqual(html.count("<form"), 1)
        self.assertEqual(html.count('type="submit"'), 1)

    def test_a_passthrough_mirror_still_follows_the_raw_count(self):
        """`save()` rather than a queryset `update()`, because a `post_save`
        signal is what keeps a passthrough's finished row in step — and a
        passthrough is one physical pile with one row allowed to count it."""
        finished = FinishedProduct.objects.create(
            name="8mm Habotai", raw_product=self.raw, recipe=None,
            price="9.00", number_on_hand=12,
        )

        self._post(**{f"received_{self.raw.pk}": "5"})

        finished.refresh_from_db()
        self.assertEqual(finished.number_on_hand, 17)
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
class NotMadeInADyeBathTests(TestCase):
    """Fancy veils carry a colorway and still can't be dyed into existence.

    The undyed passthroughs drop off every production list by construction —
    a null recipe fails the dyed-only test every one of those queries makes.
    A fancy veil is an already-dyed scarf with line work added, so it *has* a
    recipe and sails through all of them. Sending somebody to the dye room
    for one is asking for a thing that isn't made there.
    """

    def setUp(self):
        self.category, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.recipe = make_recipe("Aegean Sea")

        self.plain = RawProduct.objects.create(
            name="Half Circle Veil", category=self.category, price="8.00"
        )
        self.fancy = RawProduct.objects.create(
            name="Fancy Veil", category=self.category, price="41.99",
            made_in_a_dye_bath=False,
        )
        self.plain_product = self._short(self.plain, "Aegean Half Circle")
        self.fancy_product = self._short(self.fancy, "Aegean Fancy Veil")

    def _short(self, blank, name):
        product = FinishedProduct.objects.create(
            name=name, raw_product=blank, recipe=self.recipe, price="60.00"
        )
        # Well below par, so every production query would want it.
        FinishedProduct.objects.filter(pk=product.pk).update(
            number_on_hand=0, par=8
        )
        product.refresh_from_db()
        return product

    def test_the_production_sheet_never_asks_for_one(self):
        candidates = production.candidates()
        self.assertIn(self.plain_product, candidates)
        self.assertNotIn(self.fancy_product, candidates)

    def test_the_production_needed_page_never_lists_one(self):
        user = User.objects.create_user("prod", password="pw")
        self.client.force_login(user)
        html = self.client.get(reverse("production_needed")).content.decode()

        self.assertIn(self.plain_product.name, html)
        self.assertNotIn(self.fancy_product.name, html)

    def test_no_kanban_card_is_offered_for_one(self):
        """A card records a dye bath, and no bath ever made this."""
        user = User.objects.create_user("cards", password="pw")
        self.client.force_login(user)
        html = self.client.get(reverse("card_backfill_index")).content.decode()

        self.assertIn(self.plain_product.name, html)
        self.assertNotIn(self.fancy_product.name, html)

    def test_it_is_still_a_real_product_everywhere_else(self):
        """Only *production* is excluded. It has a colorway, it sells, it
        hangs on a board, and the Sunday close still asks about it — none of
        which is production's business."""
        self.assertEqual(self.fancy_product.recipe, self.recipe)
        FinishedProduct.objects.filter(pk=self.fancy_product.pk).update(
            display_slots=2, number_on_hand=1
        )
        self.fancy_product.refresh_from_db()
        self.assertIn(self.fancy_product, list(closing.expected_products()))

    def test_the_default_is_dyed_so_nothing_else_changes(self):
        """Every existing blank keeps its behaviour; only the ones marked
        otherwise drop out."""
        self.assertTrue(self.plain.made_in_a_dye_bath)
        ordinary = RawProduct.objects.create(
            name="Shawl", category=self.category, price="8.00"
        )
        self.assertTrue(ordinary.made_in_a_dye_bath)
class FancyConversionTests(TestCase):
    """Recording that plain scarves had line work added, by colorway."""

    def setUp(self):
        self.user = User.objects.create_user("fancier", password="pw")
        self.category, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.recipe = make_recipe("Aegean Sea")
        self.plain_blank = RawProduct.objects.create(
            name="Half Circle Veil", category=self.category, price="8.00"
        )
        self.fancy_blank = RawProduct.objects.create(
            name="Fancy Veil", category=self.category, price="41.99",
            made_in_a_dye_bath=False,
        )
        self.plain = self._product(self.plain_blank, "Aegean Half Circle", 6)
        self.fancy = self._product(self.fancy_blank, "Aegean Fancy Veil", 0)

    def _product(self, blank, name, on_hand):
        product = FinishedProduct.objects.create(
            name=name, raw_product=blank, recipe=self.recipe, price="60.00"
        )
        FinishedProduct.objects.filter(pk=product.pk).update(
            number_on_hand=on_hand, par=0
        )
        product.refresh_from_db()
        return product

    def test_converting_moves_both_sides_and_says_so_on_both(self):
        target, shortfall = fancy.convert(self.plain, self.fancy_blank, 2)

        self.plain.refresh_from_db()
        self.fancy.refresh_from_db()
        self.assertEqual(target, self.fancy)
        self.assertEqual(shortfall, 0)
        self.assertEqual(self.plain.number_on_hand, 4)
        self.assertEqual(self.fancy.number_on_hand, 2)

        logs = InventoryLog.objects.filter(
            source=InventoryLog.SOURCE_FANCY_CONVERSION
        )
        self.assertEqual(logs.count(), 2)
        self.assertEqual(sorted(l.quantity for l in logs), [-2, 2])
        # Two rows because they are two products, and every other stock
        # movement in this app is per product.
        self.assertEqual(
            {l.finished_product for l in logs}, {self.plain, self.fancy}
        )

    def test_fancying_more_than_the_app_believed_is_allowed_and_reported(self):
        """**The app was already wrong, and this is the evidence.**

        Five really did get line work put on them. Refusing would protect a
        number that was wrong before anybody touched it and lose the only
        thing that says so — so the plain side floors at zero, the fancy side
        gets all five, and the discrepancy comes back to be reported.
        """
        target, shortfall = fancy.convert(self.plain, self.fancy_blank, 9)

        self.plain.refresh_from_db()
        self.fancy.refresh_from_db()
        self.assertEqual(shortfall, 3)
        self.assertEqual(self.plain.number_on_hand, 0)
        self.assertEqual(self.fancy.number_on_hand, 9)

    def test_the_page_reports_that_shortfall_rather_than_hiding_it(self):
        self.client.login(username="fancier", password="pw")
        response = self.client.post(reverse("fancy_convert"), {
            "source": self.plain.pk,
            "blank": self.fancy_blank.pk,
            "quantity": "9",
        }, follow=True)

        html = response.content.decode()
        self.assertIn("under by 3", html)
        self.assertIn("Worth a count", html)

    def test_a_colorway_with_no_fancy_counterpart_moves_nothing(self):
        """The colorway has to exist on both blanks — inventing the target
        would create a product nobody priced."""
        lonely = self._product(self.plain_blank, "No Fancy Version", 4)
        FinishedProduct.objects.filter(pk=self.fancy.pk).delete()

        self.client.login(username="fancier", password="pw")
        response = self.client.post(reverse("fancy_convert"), {
            "source": lonely.pk,
            "blank": self.fancy_blank.pk,
            "quantity": "2",
        }, follow=True)

        lonely.refresh_from_db()
        self.assertEqual(lonely.number_on_hand, 4)
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.assertIn("has to exist on both blanks", response.content.decode())

    def test_only_plain_products_with_stock_and_a_counterpart_are_offered(self):
        empty = self._product(self.plain_blank, "None Left", 0)
        offered = list(fancy.convertible())

        self.assertIn(self.plain, offered)
        self.assertNotIn(empty, offered)
        # A fancy veil is not a thing you convert *from*.
        self.assertNotIn(self.fancy, offered)

    def test_the_conversions_are_the_fancy_production_record(self):
        """**They had to come from somewhere.**

        Fancy supply can't be planned, but every fancy veil that exists was
        converted from something — so the conversion rows *are* the
        production history, and `source` makes counting them a query rather
        than a guess.
        """
        fancy.convert(self.plain, self.fancy_blank, 2)
        fancy.convert(self.plain, self.fancy_blank, 1)

        made = InventoryLog.objects.filter(
            source=InventoryLog.SOURCE_FANCY_CONVERSION,
            finished_product__raw_product=self.fancy_blank,
            quantity__gt=0,
        )
        self.assertEqual(sum(l.quantity for l in made), 3)

    def test_the_page_is_staff_only(self):
        response = self.client.get(reverse("fancy_convert"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])
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

        from .. import production, views
        from ..management.commands import fake_sale, import_square_sales

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
class PassthroughFromUnidentifiedSaleTests(TestCase):
    """Turning a bought-and-sold thing the app never knew into a product.

    The bits and bobs are ordered and resold with no dye bath and no colorway,
    so nothing ever created the row and every sale of one landed in the queue.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("owner", "o@example.test", "pw")
        self.client.force_login(self.user)
        self.notions, _ = RawProductCategory.objects.get_or_create(name="Notions")
        self.sold_at = timezone.now() - timedelta(hours=2)
        self.sale = UnmatchedSale.objects.create(
            order_id="ORDER-N1", line_uid="L1", name="Yarn Bowl",
            square_variation_id="SQVAR1", quantity=1, amount_cents=4500,
            sold_at=self.sold_at,
        )

    def _resolve(self, sale, **extra):
        payload = {"create_product": "1", "category_id": self.notions.pk}
        payload.update(extra)
        return self.client.post(
            reverse("resolve_unmatched_sale", args=[sale.pk]), payload
        )

    def test_tracking_a_sale_creates_the_pile_and_the_sellable_row(self):
        self._resolve(self.sale)

        product = FinishedProduct.objects.get(square_variation_id="SQVAR1")
        self.assertEqual(product.name, "Yarn Bowl")
        self.assertIsNone(product.recipe_id)          # the passthrough marker
        self.assertTrue(product.is_passthrough)
        self.assertEqual(product.raw_product.category, self.notions)
        self.assertTrue(product.sku)                  # printable straight away

    def test_the_price_is_what_square_charged_per_unit_not_the_line_total(self):
        sale = UnmatchedSale.objects.create(
            order_id="ORDER-N2", line_uid="L1", name="Stitch Markers",
            square_variation_id="SQVAR2", quantity=3, amount_cents=2100,
            sold_at=self.sold_at,
        )

        self._resolve(sale)

        product = FinishedProduct.objects.get(square_variation_id="SQVAR2")
        self.assertEqual(product.price, Decimal("7.00"))

    def test_a_notion_is_never_marked_as_the_fancy_veils_are(self):
        """`made_in_a_dye_bath=False` is the *fancy* marker, not this one.

        It reads like the right flag and is the wrong one: `fancy_blanks()`
        filters on exactly it, so setting it here would offer a yarn bowl on
        the conversion page as a thing a silk scarf could be turned into.
        """
        self._resolve(self.sale)

        raw = RawProduct.objects.get(name="Yarn Bowl")
        self.assertTrue(raw.made_in_a_dye_bath)
        self.assertNotIn(raw, list(fancy.fancy_blanks()))

    def test_it_never_reaches_the_dye_room(self):
        """The null recipe is what does this, by construction rather than by
        every production query remembering to exclude a notion."""
        self._resolve(self.sale)

        product = FinishedProduct.objects.get(square_variation_id="SQVAR1")
        for oven in (False, True):
            self.assertNotIn(
                product.pk,
                [c.pk for c in production.candidates(oven=oven)],
            )

    def test_the_sale_is_resolved_and_the_stock_actually_moves(self):
        """A passthrough's count lives on the *raw* row and the finished row
        mirrors it, so a direct write here would be re-derived on save and
        snap back — the sale reading as though it never happened."""
        self._resolve(self.sale)
        self.sale.refresh_from_db()
        product = FinishedProduct.objects.get(square_variation_id="SQVAR1")

        self.assertEqual(self.sale.resolved_product_id, product.pk)
        self.assertIsNotNone(self.sale.resolved_at)
        # Started at zero, so a sale of one floors there rather than going
        # negative — and crucially the raw row is what was written.
        self.assertEqual(product.raw_product.number_on_hand, 0)
        self.assertEqual(product.number_on_hand, 0)

    def test_the_log_is_dated_when_it_sold_not_when_it_was_tidied_up(self):
        self._resolve(self.sale)

        log = InventoryLog.objects.get(log_type=InventoryLog.SALE)
        self.assertEqual(log.sale_reference, "ORDER-N1")
        self.assertEqual(log.created_at, self.sold_at)

    def test_a_second_line_of_the_same_item_reuses_the_product(self):
        """The queue holds one row per order line, so the same notion comes up
        again and again. A second product would split the count with the
        first, silently."""
        self._resolve(self.sale)
        again = UnmatchedSale.objects.create(
            order_id="ORDER-N9", line_uid="L1", name="Yarn Bowl",
            square_variation_id="SQVAR1", quantity=1, amount_cents=4500,
            sold_at=self.sold_at,
        )

        self._resolve(again)

        self.assertEqual(
            FinishedProduct.objects.filter(square_variation_id="SQVAR1").count(), 1
        )
        again.refresh_from_db()
        self.assertIsNotNone(again.resolved_at)

    def test_without_a_category_nothing_is_created(self):
        response = self._resolve(self.sale, category_id="")

        self.assertEqual(FinishedProduct.objects.count(), 0)
        self.sale.refresh_from_db()
        self.assertIsNone(self.sale.resolved_at)
        self.assertEqual(response.status_code, 302)

    def test_the_variation_id_is_what_stops_it_coming_back(self):
        """The whole payoff: `square_webhook` matches a line's
        `catalog_object_id` against this field, so the next sale identifies
        itself instead of landing in the queue."""
        self._resolve(self.sale)

        self.assertTrue(
            FinishedProduct.objects.filter(
                square_variation_id=self.sale.square_variation_id
            ).exists()
        )
