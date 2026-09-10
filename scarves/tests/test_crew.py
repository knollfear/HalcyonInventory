"""The booth, the handbook, hours, and the unidentified-sales queue.

The reasoning behind these is in `docs/claude/crew.md`.
"""
import re
import tempfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.files.base import ContentFile
from django.db.models import ProtectedError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
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
from ..views import HOURS_PIN_ATTEMPT_LIMIT, IMAGE_MAX_EDGE
from .helpers import (
    image_size,
    make_employee,
    make_jpeg,
    make_recipe,
)


class PayWeekTests(TestCase):
    """Saturday-to-Friday, which no date library assumes for you.

    Worth pinning hard: getting it wrong still renders seven columns, they're
    just the wrong seven, and the totals belong to a week nobody is paying for.
    """

    def test_a_saturday_is_its_own_week_start(self):
        saturday = date(2026, 8, 1)
        self.assertEqual(saturday.weekday(), 5)
        self.assertEqual(timesheets.week_start(saturday), saturday)

    def test_every_day_of_a_week_maps_to_the_same_saturday(self):
        saturday = date(2026, 8, 1)
        for offset in range(7):
            day = saturday + timedelta(days=offset)
            with self.subTest(day=day):
                self.assertEqual(timesheets.week_start(day), saturday)

    def test_the_next_saturday_starts_a_new_week(self):
        self.assertEqual(
            timesheets.week_start(date(2026, 8, 8)), date(2026, 8, 8)
        )

    def test_a_friday_closes_the_week_it_belongs_to(self):
        self.assertEqual(timesheets.week_end(date(2026, 8, 1)), date(2026, 8, 7))

    def test_the_week_runs_saturday_to_friday(self):
        days = timesheets.week_days(date(2026, 8, 1))
        self.assertEqual(len(days), 7)
        self.assertEqual(days[0].strftime("%A"), "Saturday")
        self.assertEqual(days[-1].strftime("%A"), "Friday")

    def test_a_week_param_is_snapped_to_its_saturday(self):
        """Any day inside the week is a valid way to ask for it."""
        self.assertEqual(
            timesheets.parse_week("2026-08-05", date(2026, 8, 7)), date(2026, 8, 1)
        )

    def test_an_unreadable_week_param_falls_back_to_this_week(self):
        for bad in ["", "not-a-date", "2026-13-45", "08/01/2026", None]:
            with self.subTest(value=bad):
                self.assertEqual(
                    timesheets.parse_week(bad, date(2026, 8, 7)), date(2026, 8, 1)
                )
class HoursFormTests(TestCase):
    """What the public form will and won't accept."""

    def setUp(self):
        self.today = date(2026, 8, 7)
        self.sam = make_employee("Sam", pin="4821")

    def _data(self, **overrides):
        data = {
            "employee": self.sam.pk,
            "pin": "4821",
            "hours": "9.5",
            "work_date": "2026-08-07",
        }
        data.update(overrides)
        return data

    def test_a_good_submission_validates(self):
        form = HoursForm(self._data(), today=self.today)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["hours"], Decimal("9.5"))

    def test_the_wrong_pin_is_rejected(self):
        form = HoursForm(self._data(pin="0000"), today=self.today)
        self.assertFalse(form.is_valid())
        self.assertIn("pin", form.errors)

    def test_another_persons_pin_does_not_work(self):
        """The PIN is checked against the name picked, not against every PIN."""
        make_employee("Alex", pin="1111")
        form = HoursForm(self._data(pin="1111"), today=self.today)
        self.assertFalse(form.is_valid())
        self.assertIn("pin", form.errors)

    def test_a_pin_that_is_not_four_digits_is_rejected(self):
        for bad in ["123", "12345", "abcd", "12 4", ""]:
            with self.subTest(pin=bad):
                form = HoursForm(self._data(pin=bad), today=self.today)
                self.assertFalse(form.is_valid())
                self.assertIn("pin", form.errors)

    def test_an_inactive_employee_is_not_on_the_list(self):
        gone = make_employee("Gone", pin="9999", active=False)
        form = HoursForm(self._data(employee=gone.pk, pin="9999"), today=self.today)
        self.assertFalse(form.is_valid())
        self.assertIn("employee", form.errors)

    def test_a_future_day_is_rejected(self):
        form = HoursForm(self._data(work_date="2026-08-08"), today=self.today)
        self.assertFalse(form.is_valid())
        self.assertIn("work_date", form.errors)

    def test_today_is_accepted(self):
        form = HoursForm(self._data(work_date="2026-08-07"), today=self.today)
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_day_beyond_the_backdate_window_is_rejected(self):
        old = self.today - timedelta(days=HoursForm.MAX_BACKDATE_DAYS + 1)
        form = HoursForm(self._data(work_date=old.isoformat()), today=self.today)
        self.assertFalse(form.is_valid())
        self.assertIn("work_date", form.errors)

    def test_the_edge_of_the_backdate_window_is_accepted(self):
        edge = self.today - timedelta(days=HoursForm.MAX_BACKDATE_DAYS)
        form = HoursForm(self._data(work_date=edge.isoformat()), today=self.today)
        self.assertTrue(form.is_valid(), form.errors)

    def test_hours_outside_the_picker_are_rejected(self):
        """The picker is a whitelist, so a hand-crafted POST can't beat it."""
        for bad in ["0", "-4", "24", "9.33", "999"]:
            with self.subTest(hours=bad):
                form = HoursForm(self._data(hours=bad), today=self.today)
                self.assertFalse(form.is_valid())
                self.assertIn("hours", form.errors)

    def test_the_picker_runs_in_quarter_hours(self):
        values = [v for v, _ in HoursForm.hour_choices() if v]
        self.assertIn("0.25", values)
        self.assertIn("9.5", values)
        self.assertNotIn("9.1", values)

    def test_one_hour_is_not_labelled_hours(self):
        labels = dict(HoursForm.hour_choices())
        self.assertEqual(labels["1"], "1 hour")
        self.assertEqual(labels["9.5"], "9.5 hours")
        # 10 must not come out as "1E+1" and put a hole in the picker.
        self.assertEqual(labels["10"], "10 hours")
class HoursEntryViewTests(TestCase):
    """The public form end to end — no login anywhere in here on purpose."""

    def setUp(self):
        self.sam = make_employee("Sam", pin="4821")
        self.url = reverse("hours_entry")

    def _post(self, **overrides):
        data = {
            "employee": self.sam.pk,
            "pin": "4821",
            "hours": "9.5",
            "work_date": timezone.localdate().isoformat(),
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_the_form_serves_an_anonymous_visitor(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Report your hours")

    def test_a_submission_records_the_hours(self):
        response = self._post()
        self.assertEqual(response.status_code, 302)
        entry = TimeEntry.objects.get()
        self.assertEqual(entry.employee, self.sam)
        self.assertEqual(entry.hours, Decimal("9.5"))

    def test_the_receipt_shows_after_the_redirect(self):
        """Post/redirect/get: the confirmation survives, a refresh doesn't resubmit."""
        response = self.client.post(
            self.url,
            {
                "employee": self.sam.pk,
                "pin": "4821",
                "hours": "9.5",
                "work_date": timezone.localdate().isoformat(),
            },
            follow=True,
        )
        self.assertContains(response, "Got it")
        self.assertContains(response, "Sam")

        # Second GET: the receipt was popped, so a refresh is a clean form.
        again = self.client.get(self.url)
        self.assertNotContains(again, "Got it")
        self.assertEqual(TimeEntry.objects.count(), 1)

    def test_a_wrong_pin_records_nothing(self):
        response = self._post(pin="0000")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TimeEntry.objects.count(), 0)

    def test_reporting_the_same_day_twice_asks_before_replacing(self):
        """The double-tapped Submit. Without this it books the day twice."""
        self._post(hours="9.5")
        self.assertEqual(TimeEntry.objects.count(), 1)

        response = self._post(hours="6")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already reported that day")

        # Still the original figure — nothing was overwritten by the ask.
        self.assertEqual(TimeEntry.objects.get().hours, Decimal("9.5"))

    def test_a_confirmed_replacement_overwrites_rather_than_adding(self):
        self._post(hours="9.5")
        response = self._post(hours="6", confirm_replace="9.50")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TimeEntry.objects.count(), 1)
        self.assertEqual(TimeEntry.objects.get().hours, Decimal("6"))

    def test_a_stale_confirmation_does_not_overwrite(self):
        """The token is the figure being replaced, so a form left open in
        another tab can't confirm away a number it never showed."""
        self._post(hours="9.5")
        response = self._post(hours="6", confirm_replace="3.00")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TimeEntry.objects.get().hours, Decimal("9.5"))

    def test_two_people_can_report_the_same_day(self):
        alex = make_employee("Alex", pin="1111")
        self._post()
        self.client.post(self.url, {
            "employee": alex.pk,
            "pin": "1111",
            "hours": "7",
            "work_date": timezone.localdate().isoformat(),
        })
        self.assertEqual(TimeEntry.objects.count(), 2)

    def test_repeated_wrong_pins_stop_being_answered(self):
        for _ in range(HOURS_PIN_ATTEMPT_LIMIT):
            self._post(pin="0000")

        # Even the right PIN gets nowhere now: the throttle is checked first.
        response = self._post()
        self.assertContains(response, "Too many wrong PINs")
        self.assertEqual(TimeEntry.objects.count(), 0)

    def test_a_correct_pin_clears_the_attempt_count(self):
        for _ in range(HOURS_PIN_ATTEMPT_LIMIT - 1):
            self._post(pin="0000")
        self._post()
        self.assertEqual(self.client.session["hours_pin_attempts"], 0)

    def test_the_form_never_shows_anybody_a_pin(self):
        """It lists names — it must not list the numbers that go with them."""
        html = self.client.get(self.url).content.decode()
        self.assertIn("Sam", html)
        self.assertNotIn("4821", html)
class TimesheetViewTests(TestCase):
    """The weekly sheet: staff-only, and the thing that replaces the mental math."""

    def setUp(self):
        self.user = User.objects.create_superuser("boss", "b@example.test", "pw")
        self.client.force_login(self.user)
        self.sam = make_employee("Sam", pin="4821")
        self.alex = make_employee("Alex", pin="1111")
        self.url = reverse("timesheet")
        # The week of Sat 1 Aug – Fri 7 Aug 2026.
        self.week = date(2026, 8, 1)

    def _entry(self, employee, day, hours, created=None):
        """An entry reported on the day it was worked, unless `created` says
        otherwise.

        Defaulting `created_at` matters: left on `auto_now_add` it takes the
        real wall clock, which drifts away from this class's hardcoded August
        2026 fixtures until every entry looks reported weeks late and picks up
        a "reported Nd later" flag. That turned these tests into a time bomb
        that went off a week after they were written — and only
        `test_an_ordinary_day_is_not_flagged` noticed, because it's the one
        asserting the flag list is *empty*. The rest use `assertIn` and would
        have sailed on with a spurious flag.
        """
        entry = TimeEntry.objects.create(
            employee=employee, work_date=day, hours=Decimal(str(hours))
        )
        if created is None:
            created = timezone.make_aware(datetime.combine(day, time(17, 0)))
        # Both timestamps move together. `updated_at` is auto_now, so leaving
        # it on the wall clock while created_at goes back to 2026 makes
        # `was_revised` (updated_at - created_at > 1s) true for every entry.
        TimeEntry.objects.filter(pk=entry.pk).update(
            created_at=created, updated_at=created
        )
        entry.refresh_from_db()
        return entry

    def test_it_needs_a_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_an_empty_week_says_so_rather_than_erroring(self):
        response = self.client.get(self.url, {"week": "2026-08-01"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nobody reported hours")

    def test_a_week_totals_each_person(self):
        self._entry(self.sam, date(2026, 8, 1), "9.5")
        self._entry(self.sam, date(2026, 8, 2), "6")
        self._entry(self.alex, date(2026, 8, 1), "4.25")

        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]

        totals = {r["employee"].name: r["total"] for r in summary["rows"]}
        self.assertEqual(totals, {"Sam": Decimal("15.5"), "Alex": Decimal("4.25")})
        self.assertEqual(summary["total"], Decimal("19.75"))

    def test_the_neighbouring_weeks_are_excluded(self):
        """The Friday before and the Saturday after both belong elsewhere."""
        self._entry(self.sam, date(2026, 7, 31), "8")   # previous week's Friday
        self._entry(self.sam, date(2026, 8, 1), "5")    # this week's Saturday
        self._entry(self.sam, date(2026, 8, 8), "8")    # next week's Saturday

        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]
        self.assertEqual(summary["total"], Decimal("5"))

    def test_any_day_in_the_week_lands_on_the_same_sheet(self):
        self._entry(self.sam, date(2026, 8, 1), "5")
        for day in ["2026-08-01", "2026-08-04", "2026-08-07"]:
            with self.subTest(week=day):
                summary = self.client.get(self.url, {"week": day}).context["summary"]
                self.assertEqual(summary["start"], date(2026, 8, 1))

    def test_it_defaults_to_the_current_week(self):
        summary = self.client.get(self.url).context["summary"]
        self.assertEqual(
            summary["start"], timesheets.week_start(timezone.localdate())
        )

    def test_every_day_gets_a_column_even_when_nobody_worked_it(self):
        self._entry(self.sam, date(2026, 8, 1), "5")
        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]
        cells = summary["rows"][0]["cells"]
        self.assertEqual(len(cells), 7)
        self.assertEqual(sum(1 for c in cells if c["entry"]), 1)

    def test_a_long_day_is_flagged(self):
        self._entry(self.sam, date(2026, 8, 1), "13")
        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]
        flags = [f for c in summary["rows"][0]["cells"] for f in c["flags"]]
        self.assertIn("long day", flags)

    def test_an_ordinary_day_is_not_flagged(self):
        self._entry(self.sam, date(2026, 8, 1), "9.5")
        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]
        self.assertEqual(
            [f for c in summary["rows"][0]["cells"] for f in c["flags"]], []
        )

    def test_a_long_week_is_flagged(self):
        for offset in range(6):
            self._entry(self.sam, date(2026, 8, 1) + timedelta(days=offset), "10")
        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]
        self.assertIn("long week", summary["rows"][0]["flags"])

    def test_a_figure_reported_long_after_the_fact_is_flagged(self):
        self._entry(
            self.sam, date(2026, 8, 1), "8",
            created=timezone.make_aware(datetime(2026, 8, 20, 12, 0)),
        )
        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]
        flags = [f for c in summary["rows"][0]["cells"] for f in c["flags"]]
        self.assertTrue(any("later" in f for f in flags), flags)

    def test_a_revised_figure_is_flagged(self):
        entry = self._entry(self.sam, date(2026, 8, 1), "8")
        TimeEntry.objects.filter(pk=entry.pk).update(
            updated_at=entry.created_at + timedelta(minutes=5)
        )
        summary = self.client.get(
            self.url, {"week": "2026-08-01"}
        ).context["summary"]
        flags = [f for c in summary["rows"][0]["cells"] for f in c["flags"]]
        self.assertIn("revised", flags)

    def test_a_fresh_entry_is_not_called_revised(self):
        """auto_now and auto_now_add land microseconds apart on create."""
        entry = self._entry(self.sam, date(2026, 8, 1), "8")
        self.assertFalse(entry.was_revised)

    def test_the_sheet_tells_you_where_staff_report_their_hours(self):
        response = self.client.get(self.url)
        self.assertContains(response, reverse("hours_entry"))
class TimeEntryModelTests(TestCase):
    def setUp(self):
        self.sam = make_employee("Sam", pin="4821")

    def test_one_entry_per_person_per_day_is_enforced_by_the_database(self):
        from django.db import IntegrityError

        TimeEntry.objects.create(
            employee=self.sam, work_date=date(2026, 8, 1), hours=Decimal("8")
        )
        with self.assertRaises(IntegrityError):
            TimeEntry.objects.create(
                employee=self.sam, work_date=date(2026, 8, 1), hours=Decimal("6")
            )

    def test_an_employee_with_hours_cannot_be_deleted_out_from_under_them(self):
        from django.db.models import ProtectedError

        TimeEntry.objects.create(
            employee=self.sam, work_date=date(2026, 8, 1), hours=Decimal("8")
        )
        with self.assertRaises(ProtectedError):
            self.sam.delete()

    def test_reported_late_by_counts_from_the_day_worked(self):
        entry = TimeEntry.objects.create(
            employee=self.sam, work_date=timezone.localdate() - timedelta(days=3),
            hours=Decimal("8"),
        )
        self.assertEqual(entry.reported_late_by, 3)

    def test_same_day_reporting_is_not_late(self):
        entry = TimeEntry.objects.create(
            employee=self.sam, work_date=timezone.localdate(), hours=Decimal("8")
        )
        self.assertEqual(entry.reported_late_by, 0)
class BoothPhotoFormTests(TestCase):
    """The crew's form. PIN, and the one rule worth refusing a photo over."""

    def setUp(self):
        self.employee = Employee.objects.create(name="Robin", pin="4821")

    def _data(self, **overrides):
        data = {
            "employee": self.employee.pk,
            "pin": "4821",
            "reason": BoothPhoto.REASON_SHARE,
        }
        data.update(overrides)
        return data

    def _files(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return {"photo": SimpleUploadedFile(
            "booth.jpg", make_jpeg((60, 40)), content_type="image/jpeg"
        )}

    def _form(self, **overrides):
        from ..forms import BoothPhotoForm
        return BoothPhotoForm(self._data(**overrides), self._files())

    def test_the_wrong_pin_is_rejected(self):
        form = self._form(pin="0000")
        self.assertFalse(form.is_valid())
        self.assertIn("pin", form.errors)

    def test_a_person_in_the_photo_needs_their_own_yes(self):
        """The sender's tick is the sender's permission. It is not the
        permission of the person in the picture, and the form won't pretend it
        is."""
        form = self._form(share_instagram=True, people_in_photo=True)

        self.assertFalse(form.is_valid())
        self.assertIn("people_agreed", form.errors)

    def test_a_person_in_the_photo_is_fine_when_nothing_is_ticked(self):
        """Sending it with no destination is a legitimate answer — 'here, your
        call' — and must not be blocked."""
        self.assertTrue(self._form(people_in_photo=True).is_valid())

    def test_the_barcode_prefix_is_trimmed_to_what_it_means(self):
        form = self._form(
            reason=BoothPhoto.REASON_UNIDENTIFIED, sku_prefix="infi-aeg"
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["sku_prefix"], "INFIAE"[:6])

    def test_an_unidentified_report_defaults_to_now(self):
        """Reported straight after the sale is the normal case, and the moment
        it was sent is what the ±15 minute match looks for."""
        form = self._form(reason=BoothPhoto.REASON_UNIDENTIFIED)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNotNone(form.cleaned_data["sold_at"])

    def test_a_sale_in_the_future_is_refused(self):
        future = (timezone.localtime() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        form = self._form(reason=BoothPhoto.REASON_UNIDENTIFIED, sold_at=future)

        self.assertFalse(form.is_valid())
        self.assertIn("sold_at", form.errors)
class BoothPhotoViewTests(TestCase):
    """The page itself: no login, PIN in the page, and only the half of the
    form that the reason applies to is kept."""

    def setUp(self):
        self.employee = Employee.objects.create(name="Robin", pin="4821")
        self.url = reverse("booth_photo")

    def _send(self, **overrides):
        from django.core.files.uploadedfile import SimpleUploadedFile
        data = {
            "employee": self.employee.pk,
            "pin": "4821",
            "reason": BoothPhoto.REASON_SHARE,
            "photo": SimpleUploadedFile(
                "booth.jpg", make_jpeg((60, 40)), content_type="image/jpeg"
            ),
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_it_serves_an_anonymous_get(self):
        """A login here would lock out exactly the people it is for, and the
        only symptom would be that nobody ever reports."""
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_share_photo_is_stored_with_its_permissions(self):
        self._send(share_website=True, caption="Best hat all weekend", tag="@someone")

        photo = BoothPhoto.objects.get()
        self.assertTrue(photo.share_website)
        self.assertFalse(photo.share_instagram)
        self.assertEqual(photo.caption, "Best hat all weekend")
        self.assertEqual(photo.employee, self.employee)
        self.assertTrue(photo.image.name.startswith("booth/"))

    def test_a_sale_report_keeps_no_sharing_permission(self):
        """Both halves always submit. A report that changed reason mid-thought
        must not leave a permission to post it behind."""
        self._send(
            reason=BoothPhoto.REASON_UNIDENTIFIED,
            share_instagram=True,
            caption="ignore me",
            sku_prefix="INFI",
        )

        photo = BoothPhoto.objects.get()
        self.assertFalse(photo.share_instagram)
        self.assertEqual(photo.caption, "")
        self.assertEqual(photo.sku_prefix, "INFI")

    def test_the_photo_is_downscaled_on_the_way_in(self):
        self._send()

        photo = BoothPhoto.objects.get()
        with photo.image.open("rb") as fh:
            self.assertLessEqual(max(image_size(fh.read())), IMAGE_MAX_EDGE)

    def test_a_bad_pin_saves_nothing(self):
        self._send(pin="1111")

        self.assertEqual(BoothPhoto.objects.count(), 0)
class ShareableTests(TestCase):
    """`shareable` is what the gallery reads, so it has to be the whole rule
    rather than the two destination ticks."""

    def _photo(self, **kwargs):
        employee = Employee.objects.create(name=f"E{BoothPhoto.objects.count()}", pin="1234")
        return BoothPhoto(employee=employee, reason=BoothPhoto.REASON_SHARE, **kwargs)

    def test_nothing_ticked_is_not_shareable(self):
        self.assertFalse(self._photo().shareable)

    def test_a_destination_alone_is_enough_with_nobody_in_shot(self):
        self.assertTrue(self._photo(share_website=True).shareable)

    def test_a_person_in_shot_without_their_yes_is_not_shareable(self):
        self.assertFalse(
            self._photo(share_website=True, people_in_photo=True).shareable
        )

    def test_a_person_in_shot_who_agreed_is_shareable(self):
        self.assertTrue(
            self._photo(
                share_website=True, people_in_photo=True, people_agreed=True
            ).shareable
        )
class UnmatchedSaleReviewTests(TestCase):
    """Pairing sales with photos, and what resolving one actually does."""

    def setUp(self):
        self.user = User.objects.create_superuser("owner", "o@example.test", "pw")
        self.client.force_login(self.user)
        self.employee = Employee.objects.create(name="Robin", pin="4821")
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.raw = RawProduct.objects.create(
            name="Infinity", category=self.silk, price="5.00"
        )
        self.product = FinishedProduct.objects.create(
            name="Aegean Infinity", raw_product=self.raw,
            recipe=make_recipe("Aegean Sea"), price="30.00", number_on_hand=4,
        )
        self.sold_at = timezone.now() - timedelta(hours=3)
        self.sale = UnmatchedSale.objects.create(
            order_id="ORDER-1", line_uid="L1", name="Scarf",
            quantity=1, amount_cents=3000, sold_at=self.sold_at,
        )
        self.url = reverse("unmatched_sales")

    def _report(self, minutes_off=0, prefix="", image=True):
        photo = BoothPhoto(
            employee=self.employee,
            reason=BoothPhoto.REASON_UNIDENTIFIED,
            sold_at=self.sold_at + timedelta(minutes=minutes_off),
            sku_prefix=prefix,
        )
        if image:
            photo.image.save("r.jpg", ContentFile(make_jpeg((40, 30))), save=False)
        photo.save()
        return photo

    def _day(self):
        return timezone.localtime(self.sold_at).date().isoformat()

    def _rows(self):
        return self.client.get(f"{self.url}?day={self._day()}").context["rows"]

    def test_a_photo_within_the_window_is_offered_against_the_sale(self):
        report = self._report(minutes_off=7)

        self.assertEqual([r.pk for r in self._rows()[0]["reports"]], [report.pk])

    def test_a_photo_well_outside_the_window_is_not(self):
        self._report(minutes_off=45)

        response = self.client.get(f"{self.url}?day={self._day()}")
        self.assertEqual(response.context["rows"][0]["reports"], [])
        # Not dropped either: a report with no sale beside it is the
        # interesting case, so it stays on the page.
        self.assertEqual(len(response.context["orphans"]), 1)

    def test_a_reported_barcode_narrows_the_products_offered(self):
        other = RawProduct.objects.create(name="Sash Belt", category=self.silk, price="5")
        FinishedProduct.objects.create(
            name="Aegean Belt", raw_product=other,
            recipe=Recipe.objects.get(name="Aegean Sea"), price="20.00",
        )
        self._report(minutes_off=2, prefix=self.product.sku[:6])

        row = self._rows()[0]
        self.assertTrue(row["narrowed"])
        self.assertEqual([p.pk for p in row["options"]], [self.product.pk])

    def test_with_no_barcode_reported_the_whole_catalogue_is_offered(self):
        self._report(minutes_off=2, prefix="")

        row = self._rows()[0]
        self.assertFalse(row["narrowed"])
        self.assertEqual(len(row["options"]), FinishedProduct.objects.count())

    def _resolve(self, **extra):
        data = {"product_id": self.product.pk, "day": self._day()}
        data.update(extra)
        return self.client.post(
            reverse("resolve_unmatched_sale", args=[self.sale.pk]), data
        )

    def test_matching_takes_the_scarf_out_of_stock(self):
        self._resolve()

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 3)

    def test_the_log_row_is_dated_when_it_sold_not_when_it_was_matched(self):
        """Otherwise the sale lands on the day someone got round to the queue,
        which is a day nothing happened."""
        self._resolve()

        log = InventoryLog.objects.get(log_type=InventoryLog.SALE)
        self.assertEqual(log.created_at, self.sold_at)
        self.assertEqual(log.sale_reference, "ORDER-1")
        self.assertIn("by hand", log.notes)

    def test_matching_twice_only_sells_it_once(self):
        """A double-submitted review screen must not take two scarves off."""
        self._resolve()
        self._resolve()

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 3)
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_dismissing_closes_the_row_without_touching_stock(self):
        """Not everything Square couldn't place was a scarf — a queue that
        can't be emptied stops being read."""
        self.client.post(
            reverse("resolve_unmatched_sale", args=[self.sale.pk]),
            {"dismiss": "1", "dismissed_reason": "tip jar", "day": self._day()},
        )

        self.sale.refresh_from_db()
        self.product.refresh_from_db()
        self.assertFalse(self.sale.is_open)
        self.assertEqual(self.sale.dismissed_reason, "tip jar")
        self.assertEqual(self.product.number_on_hand, 4)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_the_photo_can_be_filed_against_the_product(self):
        """The scarf nobody could name now has a picture, so next time it's
        identifiable."""
        report = self._report(minutes_off=3)
        self._resolve(report_id=report.pk, file_photo="1")

        image = self.product.images.get()
        self.assertEqual(image.image.name, report.image.name)

    def test_the_photo_is_not_filed_unless_asked(self):
        report = self._report(minutes_off=3)
        self._resolve(report_id=report.pk)

        self.assertEqual(self.product.images.count(), 0)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.resolved_photo, report)

    def test_a_resolved_photo_leaves_the_queue(self):
        report = self._report(minutes_off=3)
        self._resolve(report_id=report.pk)

        second = UnmatchedSale.objects.create(
            order_id="ORDER-2", line_uid="L1", name="Scarf",
            quantity=1, sold_at=self.sold_at,
        )
        rows = self._rows()
        self.assertEqual([r["sale"].pk for r in rows], [second.pk])
        self.assertEqual(rows[0]["reports"], [])
class UnmatchedSalePaceTests(TestCase):
    """Working the queue at pace: a hundred lines, read and clicked.

    Two costs were in the way and both were structural rather than
    incidental. The page asked the same catalogue-sized question once per
    row, and every dismissal was a full navigation that rebuilt all of it.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("owner", "o@example.test", "pw")
        self.client.force_login(self.user)
        self.employee = Employee.objects.create(name="Robin", pin="4821")
        silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        raw = RawProduct.objects.create(name="Infinity", category=silk, price="5.00")
        for colour in ("Aegean Sea", "Ember", "Stormy"):
            FinishedProduct.objects.create(
                name=f"{colour} Infinity", raw_product=raw,
                recipe=make_recipe(colour), price="30.00", number_on_hand=4,
            )
        self.sold_at = timezone.now() - timedelta(hours=3)
        self.sale = UnmatchedSale.objects.create(
            order_id="ORDER-1", line_uid="L1", name="Scarf",
            quantity=1, amount_cents=3000, sold_at=self.sold_at,
        )
        self.url = reverse("unmatched_sales")
        self.day = timezone.localtime(self.sold_at).date().isoformat()

    def _more_sales(self, count):
        for n in range(2, count + 2):
            UnmatchedSale.objects.create(
                order_id=f"ORDER-{n}", line_uid="L1", name="Scarf",
                quantity=1, amount_cents=3000, sold_at=self.sold_at,
            )

    def test_the_catalogue_is_asked_for_once_however_many_rows(self):
        """The common row has no photo and therefore no reported barcode, so
        every one of them asks the identical question — the whole active
        catalogue. A day with ten was running ten copies of the biggest query
        on the page, and paying it again on every dismissal's redirect."""
        with CaptureQueriesContext(connection) as one_row:
            self.client.get(self.url, {"day": self.day})

        self._more_sales(8)

        with CaptureQueriesContext(connection) as nine_rows:
            self.client.get(self.url, {"day": self.day})

        self.assertEqual(len(nine_rows), len(one_row))

    def test_rows_that_really_differ_still_get_their_own_list(self):
        """The cache is keyed on the reported prefixes and nothing else, so
        narrowing still narrows — this must not become one list for every
        row."""
        photo = BoothPhoto(
            employee=self.employee,
            reason=BoothPhoto.REASON_UNIDENTIFIED,
            sold_at=self.sold_at,
            sku_prefix="AEGEAN",
        )
        photo.save()
        FinishedProduct.objects.filter(name="Aegean Sea Infinity").update(
            sku="AEGEAN-INFIN"
        )
        # Same day, well outside the photo's fifteen minutes, so this one has
        # nothing reported against it and asks the wide question.
        UnmatchedSale.objects.create(
            order_id="ORDER-2", line_uid="L1", name="Scarf",
            quantity=1, amount_cents=3000,
            sold_at=self.sold_at + timedelta(minutes=40),
        )

        rows = self.client.get(
            self.url, {"day": self.day}
        ).context["rows"]

        by_narrowed = {r["narrowed"]: r for r in rows}
        self.assertIn(True, by_narrowed)
        self.assertIn(False, by_narrowed)
        self.assertEqual(len(by_narrowed[True]["options"]), 1)
        self.assertEqual(len(by_narrowed[False]["options"]), 3)

    def _dismiss(self, sale, htmx=True, **extra):
        data = {"dismiss": "1", "day": self.day}
        data.update(extra)
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        return self.client.post(
            reverse("resolve_unmatched_sale", args=[sale.pk]), data, **headers
        )

    def test_an_htmx_dismiss_swaps_one_row_instead_of_rebuilding_the_page(self):
        response = self._dismiss(self.sale)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(f'id="sale-{self.sale.pk}"', html)
        self.assertIn("Dismissed", html)
        # The thing the whole change is about: no other row, and no
        # catalogue-sized <select>, comes back with it.
        self.assertNotIn("which scarf was it?", html)
        self.sale.refresh_from_db()
        self.assertIsNotNone(self.sale.dismissed_at)

    def test_the_reason_typed_beside_it_is_kept(self):
        self._dismiss(self.sale, dismissed_reason="tip jar")

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.dismissed_reason, "tip jar")

    def test_the_header_count_comes_back_with_it(self):
        """Out-of-band, because a header reading '2 open in total' over one
        row is the page contradicting itself."""
        self._more_sales(1)

        html = self._dismiss(self.sale).content.decode()

        self.assertIn('id="open-total"', html)
        self.assertIn('hx-swap-oob="true"', html)
        self.assertIn("1 open in total", html)

    def test_a_photo_left_behind_by_a_dismissal_comes_back_as_an_orphan(self):
        """Dismissing can *make* an orphan: the photo that was sitting beside
        that sale now has nothing to be beside."""
        photo = BoothPhoto(
            employee=self.employee,
            reason=BoothPhoto.REASON_UNIDENTIFIED,
            sold_at=self.sold_at,
            note="blue one",
        )
        photo.save()

        html = self._dismiss(self.sale).content.decode()

        self.assertIn('id="orphans"', html)
        self.assertIn("Photos with no sale beside them", html)
        self.assertIn("blue one", html)

    def test_dismissing_the_same_line_twice_says_where_things_stand(self):
        """A double-click at pace, or a stale tab. The page is already in the
        state they were asking for, so it says so rather than erroring."""
        self._dismiss(self.sale)

        response = self._dismiss(self.sale)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Dismissed", response.content.decode())

    def test_without_htmx_it_still_posts_and_redirects(self):
        """Script blocked, and the button is the ordinary submit it always
        was."""
        response = self._dismiss(self.sale, htmx=False)

        self.assertRedirects(response, f"{self.url}?day={self.day}")
        self.sale.refresh_from_db()
        self.assertIsNotNone(self.sale.dismissed_at)

    def test_the_page_wires_the_button_to_the_swap(self):
        html = self.client.get(self.url, {"day": self.day}).content.decode()

        self.assertIn(
            f'hx-post="{reverse("resolve_unmatched_sale", args=[self.sale.pk])}"',
            html,
        )
        self.assertIn(f'hx-target="#sale-{self.sale.pk}"', html)
        self.assertIn('hx-swap="outerHTML"', html)
        self.assertIn("htmx.org", html)

    def test_matching_a_product_is_still_a_navigation(self):
        """It moves stock, and the sentence saying what moved is worth a
        page."""
        product = FinishedProduct.objects.get(name="Ember Infinity")

        response = self.client.post(
            reverse("resolve_unmatched_sale", args=[self.sale.pk]),
            {"product_id": product.pk, "day": self.day},
            HTTP_HX_REQUEST="true",
        )

        self.assertRedirects(response, f"{self.url}?day={self.day}")
        product.refresh_from_db()
        self.assertEqual(product.number_on_hand, 3)
class DismissAllLikeThisTests(TestCase):
    """One click for every line like this one, on this day.

    Two keys, and they are not two precisions of one idea — they apply to
    different populations, which is the whole reason the button names which
    one it used:

    * **A Square variation id** means Square has a catalog object this app
      doesn't know. That is the unsynced-variation case and the group *most*
      likely to be real scarves, so a bulk dismissal there is the expensive
      click on the page.
    * **A bare name with no id** is a hand-keyed custom amount, and that is
      the group that genuinely never was a scarf.

    Day-scoped, so everything the button claims is on the screen it was
    clicked from and the count beside it can be checked by looking.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("owner", "o@example.test", "pw")
        self.client.force_login(self.user)
        self.sold_at = timezone.now().replace(hour=12, minute=0) - timedelta(days=1)
        self.day = timezone.localtime(self.sold_at).date().isoformat()
        self.url = reverse("unmatched_sales")

    def _sale(self, name, order, variation="", minutes=0, sold_at=None):
        return UnmatchedSale.objects.create(
            order_id=order, line_uid="L1", name=name,
            square_variation_id=variation, quantity=1, amount_cents=1000,
            sold_at=(sold_at or self.sold_at) + timedelta(minutes=minutes),
        )

    def _dismiss_all(self, sale, htmx=True):
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        return self.client.post(
            reverse("resolve_unmatched_sale", args=[sale.pk]),
            {"dismiss_all": "1", "day": self.day},
            **headers,
        )

    def test_a_name_takes_every_other_line_with_that_name(self):
        first = self._sale("Custom Amount", "O-1")
        second = self._sale("Custom Amount", "O-2", minutes=20)
        keep = self._sale("Scarf", "O-3", minutes=40)

        self._dismiss_all(first)

        for sale in (first, second):
            sale.refresh_from_db()
            self.assertIsNotNone(sale.dismissed_at, sale.order_id)
        keep.refresh_from_db()
        self.assertIsNone(keep.dismissed_at)

    def test_a_name_never_sweeps_up_a_line_carrying_a_square_item(self):
        """The one that would be silent. Without scoping the name match to
        lines with no variation id, dismissing every 'Custom Amount' takes
        the dangerous group along with the safe one — real sales written off
        by the button that promised the harmless ones."""
        hand_keyed = self._sale("Custom Amount", "O-1")
        real = self._sale("Custom Amount", "O-2", variation="SQVAR1", minutes=20)

        self._dismiss_all(hand_keyed)

        real.refresh_from_db()
        self.assertIsNone(real.dismissed_at)

    def test_a_square_item_takes_its_own_group_whatever_they_are_called(self):
        """Square's own identity beats the label, which is why it is the
        preferred key when there is one."""
        first = self._sale("Silk Scarf", "O-1", variation="SQVAR1")
        renamed = self._sale("Scarf", "O-2", variation="SQVAR1", minutes=20)
        other = self._sale("Silk Scarf", "O-3", variation="SQVAR2", minutes=40)

        self._dismiss_all(first)

        renamed.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNotNone(renamed.dismissed_at)
        self.assertIsNone(other.dismissed_at)

    def test_it_stops_at_the_day(self):
        """Everything the button claims is on the page it was clicked from."""
        today = self._sale("Custom Amount", "O-1")
        yesterday = self._sale(
            "Custom Amount", "O-2", sold_at=self.sold_at - timedelta(days=1)
        )

        self._dismiss_all(today)

        yesterday.refresh_from_db()
        self.assertIsNone(yesterday.dismissed_at)

    def test_every_row_it_took_leaves_the_page_in_the_one_response(self):
        """A row left behind reads as one the button missed."""
        first = self._sale("Custom Amount", "O-1")
        second = self._sale("Custom Amount", "O-2", minutes=20)

        html = self._dismiss_all(first).content.decode()

        self.assertIn(f'id="sale-{first.pk}"', html)
        self.assertIn(f'id="sale-{second.pk}"', html)
        # The clicked row is the swap target; the rest are found by id.
        self.assertEqual(html.count('hx-swap-oob="true"'), 3)

    def test_the_reason_typed_beside_it_covers_the_group(self):
        first = self._sale("Custom Amount", "O-1")
        second = self._sale("Custom Amount", "O-2", minutes=20)

        self.client.post(
            reverse("resolve_unmatched_sale", args=[first.pk]),
            {"dismiss_all": "1", "day": self.day, "dismissed_reason": "tip jar"},
            HTTP_HX_REQUEST="true",
        )

        second.refresh_from_db()
        self.assertEqual(second.dismissed_reason, "tip jar")

    def test_the_button_says_how_many_and_on_what_key(self):
        self._sale("Custom Amount", "O-1")
        self._sale("Custom Amount", "O-2", minutes=20)
        self._sale("Silk Scarf", "O-3", variation="SQVAR1", minutes=40)
        self._sale("Renamed", "O-4", variation="SQVAR1", minutes=60)

        html = self.client.get(self.url, {"day": self.day}).content.decode()

        self.assertIn("Dismiss all 2 named", html)
        self.assertIn("Dismiss all 2 with this Square item", html)

    def test_a_line_standing_only_for_itself_gets_no_button(self):
        """'Dismiss all 1 like this' is the button beside it wearing a longer
        label."""
        self._sale("One Off", "O-1")

        html = self.client.get(self.url, {"day": self.day}).content.decode()

        self.assertNotIn("Dismiss all", html)

    def test_a_line_with_no_name_and_no_item_gets_no_button(self):
        """'All like this' would mean 'all the nameless ones', which is a
        grab bag rather than a group."""
        self._sale("", "O-1")
        self._sale("", "O-2", minutes=20)

        html = self.client.get(self.url, {"day": self.day}).content.decode()

        self.assertNotIn("Dismiss all", html)

    def test_the_count_costs_no_extra_queries(self):
        """Counted off the list already in memory. A query per row is the
        mistake this page just had removed."""
        self._sale("Custom Amount", "O-1")
        with CaptureQueriesContext(connection) as one:
            self.client.get(self.url, {"day": self.day})

        for n in range(2, 8):
            self._sale("Custom Amount", f"O-{n}", minutes=n * 5)

        with CaptureQueriesContext(connection) as seven:
            self.client.get(self.url, {"day": self.day})

        self.assertEqual(len(seven), len(one))

    def test_without_htmx_it_posts_redirects_and_says_the_count(self):
        first = self._sale("Custom Amount", "O-1")
        self._sale("Custom Amount", "O-2", minutes=20)

        # Followed rather than asserted-then-fetched: `assertRedirects` walks
        # the redirect itself, and that request is the one that consumes the
        # message being checked for.
        response = self.client.post(
            reverse("resolve_unmatched_sale", args=[first.pk]),
            {"dismiss_all": "1", "day": self.day},
            follow=True,
        )

        self.assertEqual(
            response.redirect_chain[-1][0], f"{self.url}?day={self.day}"
        )
        self.assertContains(response, "Dismissed 2 lines")
class CrewCookieTests(TestCase):
    """Remembering name and PIN on the two `secret/` pages.

    The friction this removes is the whole reason the PIN is acceptable at
    all: a four-digit challenge at the moment a scarf sells is enough to mean
    the photo never gets sent. So the cases worth pinning are the ones where
    remembering could quietly do harm — a stale PIN, a departed employee, a
    forged cookie, and above all a cookie that starts standing in for the
    check instead of just typing it.
    """

    def setUp(self):
        self.sam = make_employee("Sam", pin="4821")
        self.hours_url = reverse("hours_entry")
        self.booth_url = reverse("booth_photo")

    def _report_hours(self, **overrides):
        data = {
            "employee": self.sam.pk,
            "pin": "4821",
            "hours": "9.5",
            "work_date": timezone.localdate().isoformat(),
        }
        data.update(overrides)
        return self.client.post(self.hours_url, data)

    def _send_photo(self, **overrides):
        from django.core.files.uploadedfile import SimpleUploadedFile
        data = {
            "employee": self.sam.pk,
            "pin": "4821",
            "reason": BoothPhoto.REASON_SHARE,
            "photo": SimpleUploadedFile(
                "booth.jpg", make_jpeg((60, 40)), content_type="image/jpeg"
            ),
        }
        data.update(overrides)
        return self.client.post(self.booth_url, data)

    def _prefilled(self, url):
        """`(employee pk, pin)` the form at `url` opens with."""
        form = self.client.get(url).context["form"]
        return form.initial.get("employee"), form.initial.get("pin")

    # --- the point of the whole thing ----------------------------------

    def test_reporting_hours_leaves_the_form_filled_in_next_time(self):
        self._report_hours()

        self.assertEqual(self._prefilled(self.hours_url), (self.sam.pk, "4821"))

    def test_one_cookie_covers_both_pages(self):
        """Somebody who has just sent a photo shouldn't have to re-introduce
        themselves to the hours form."""
        self._send_photo()

        self.assertEqual(self._prefilled(self.hours_url), (self.sam.pk, "4821"))

    def test_nothing_is_remembered_before_a_first_submission(self):
        self.assertEqual(self._prefilled(self.booth_url), (None, None))

    # --- it fills the form in; it never stands in for the PIN ----------

    def test_the_cookie_does_not_authorise_a_submission(self):
        """The cookie types for you. It is not a credential, and a POST
        carrying it with the wrong PIN has to fail exactly as before —
        otherwise a found phone submits with no check anywhere."""
        self._report_hours()
        TimeEntry.objects.all().delete()

        self._send_photo(pin="1111")

        self.assertEqual(BoothPhoto.objects.count(), 0)

    def test_a_missing_pin_is_still_a_missing_pin(self):
        self._report_hours()
        TimeEntry.objects.all().delete()

        self._report_hours(pin="")

        self.assertEqual(TimeEntry.objects.count(), 0)

    def test_a_rejected_pin_is_never_remembered(self):
        """Remembering a wrong answer is worse than remembering nothing: the
        page opens looking ready and rejects whatever is submitted."""
        self._report_hours(pin="1111")

        self.assertEqual(self._prefilled(self.hours_url), (None, None))

    # --- a cookie can outlive the facts in it --------------------------

    def test_a_changed_pin_keeps_the_name_and_drops_the_pin(self):
        """The name is still right, so the page still knows who this is and
        asks for the one thing that actually changed."""
        self._report_hours()
        Employee.objects.filter(pk=self.sam.pk).update(pin="9999")

        self.assertEqual(self._prefilled(self.hours_url), (self.sam.pk, None))

    def test_someone_who_has_left_is_forgotten_entirely(self):
        self._report_hours()
        Employee.objects.filter(pk=self.sam.pk).update(is_active=False)

        self.assertEqual(self._prefilled(self.hours_url), (None, None))

    def test_a_forged_cookie_is_ignored_rather_than_trusted(self):
        self.client.cookies[crew.COOKIE] = f"{self.sam.pk}:4821"

        self.assertEqual(self._prefilled(self.hours_url), (None, None))

    # --- not you? ------------------------------------------------------

    def test_the_not_you_link_forgets_both_pages(self):
        """Personal phones make this rare, not never — phones get lent."""
        self._report_hours()

        response = self.client.get(f"{self.hours_url}?{crew.FORGET}=1")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._prefilled(self.booth_url), (None, None))

    def test_the_page_says_the_name_was_filled_in_for_you(self):
        """A pre-filled name nobody mentions is how one person's hours get
        filed under another."""
        self._report_hours()

        response = self.client.get(self.hours_url)

        self.assertContains(response, "Not you?")
        self.assertContains(response, f"{crew.FORGET}=1")
class BoothSignedInTests(TestCase):
    """A staff login shouldn't be asked to prove itself twice.

    The crew path is unchanged and tested elsewhere; what matters here is
    that the name and PIN come off the page for someone already
    authenticated, and that removing them doesn't quietly remove the
    attribution `BoothPhoto` depends on.
    """

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.SimpleUploadedFile = SimpleUploadedFile
        self.user = User.objects.create_user("owner", password="pw")
        self.employee = Employee.objects.create(name="Robin", pin="4821", user=self.user)
        self.url = reverse("booth_photo")

    def _photo(self):
        return self.SimpleUploadedFile(
            "booth.jpg", make_jpeg((60, 40)), content_type="image/jpeg"
        )

    def test_a_linked_login_is_asked_for_neither(self):
        self.client.force_login(self.user)

        form = self.client.get(self.url).context["form"]

        self.assertNotIn("employee", form.fields)
        self.assertNotIn("pin", form.fields)

    def test_the_photo_is_still_attributed(self):
        """`BoothPhoto.employee` is required on purpose — a sharing
        permission nobody can attribute isn't a permission."""
        self.client.force_login(self.user)

        self.client.post(self.url, {
            "reason": BoothPhoto.REASON_SHARE,
            "photo": self._photo(),
            "share_website": True,
        })

        photo = BoothPhoto.objects.get()
        self.assertEqual(photo.employee, self.employee)
        self.assertTrue(photo.share_website)

    def test_an_unlinked_login_is_asked_for_neither_either(self):
        """An unlinked login used to fall back to the name picker.

        The old reasoning was that the app genuinely doesn't know which
        employee this is and guessing would put someone else's name on a
        permission. The first half is still true; the conclusion was wrong.
        What it produced was a signed-in person being shown a list of their
        colleagues and asked which one they were — the exact paperwork a
        login is supposed to settle.

        `crew.employee_for` doesn't guess. It makes a row for the person who
        is demonstrably signed in, so there is no longer an unlinked case to
        fall back from.
        """
        other = User.objects.create_user("stranger", password="pw")
        self.client.force_login(other)

        form = self.client.get(self.url).context["form"]

        self.assertNotIn("employee", form.fields)
        self.assertNotIn("pin", form.fields, "the login already proved more than a PIN")

    def test_an_unlinked_login_sends_as_itself(self):
        """And the photo is attributed to the new row, not to a picked name.

        This is the safety property the old fallback was reaching for, and it
        holds more firmly now: there is no picker to mis-tap, so a permission
        can only ever carry the name of whoever was actually signed in.
        """
        other = User.objects.create_user("stranger", password="pw")
        self.client.force_login(other)

        self.client.post(self.url, {
            "reason": BoothPhoto.REASON_SHARE,
            "photo": self._photo(),
        })

        photo = BoothPhoto.objects.get()
        self.assertEqual(photo.employee.name, "stranger")
        self.assertEqual(photo.employee.user, other)
        self.assertNotEqual(photo.employee, self.employee)

    def test_the_crew_are_unaffected(self):
        """Anonymous is still name plus PIN, and a wrong PIN still stops."""
        form = self.client.get(self.url).context["form"]
        self.assertIn("employee", form.fields)
        self.assertIn("pin", form.fields)

        self.client.post(self.url, {
            "employee": self.employee.pk,
            "pin": "1111",
            "reason": BoothPhoto.REASON_SHARE,
            "photo": self._photo(),
        })
        self.assertEqual(BoothPhoto.objects.count(), 0)

    def test_a_signed_in_post_cannot_smuggle_a_pin_field(self):
        """The fields are removed, not hidden — so a hand-built POST has
        nothing to fill in."""
        self.client.force_login(self.user)
        other = Employee.objects.create(name="Someone Else", pin="1234")

        self.client.post(self.url, {
            "employee": other.pk,
            "pin": "1234",
            "reason": BoothPhoto.REASON_SHARE,
            "photo": self._photo(),
        })

        self.assertEqual(BoothPhoto.objects.get().employee, self.employee)

    def test_signing_in_writes_no_crew_cookie(self):
        """There's no PIN to remember, and the login outlives a cookie."""
        self.client.force_login(self.user)

        self.client.post(self.url, {
            "reason": BoothPhoto.REASON_SHARE,
            "photo": self._photo(),
        })

        self.assertNotIn(crew.COOKIE, self.client.cookies)
class BoothReasonHalvesTests(TestCase):
    """Only the half the reason applies to is shown.

    Cosmetic by design — the view already stores only the matching half — so
    what's pinned is that the rule is on the page at all, and that it's the
    kind that still works when the network doesn't.
    """

    def test_each_half_is_addressable_and_hidden_by_the_other_reason(self):
        response = self.client.get(reverse("booth_photo"))
        body = response.content.decode()

        self.assertIn('class="half share"', body)
        self.assertIn('class="half unidentified"', body)
        self.assertIn(
            'form:has(input[name="reason"][value="share"]:checked) .half.unidentified',
            body,
        )

    def test_the_toggle_needs_no_request(self):
        """A stall has one bar of signal; a toggle that needs the network is
        a toggle that sometimes doesn't happen."""
        body = self.client.get(reverse("booth_photo")).content.decode()

        self.assertNotIn("hx-get", body)
        self.assertNotIn("hx-post", body)
@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class CrewHandbookTests(TestCase):
    """The handbook, and the pass it hands out at the bottom.

    The page has an unusual shape for this app: a gate that is deliberately
    not protecting the thing behind it. A faire pass is a barcode and a
    photograph, and anyone who can reach this URL is getting one anyway — so
    the name and PIN are there to pick *whose* PDF comes back, and the tick
    box is there to ask somebody to look at the page. Neither is a lock, and
    the tests below are mostly about the ways that could quietly stop being
    true.

    The one failure that actually costs something is a crew member standing
    at the gate unable to get their pass, so the cases that matter most are
    the ones where the page refuses: a missed tick must not throw away the
    read, and a missing PDF must say who to ask.
    """

    def setUp(self):
        self.sam = make_employee("Sam", pin="4821")
        self.url = reverse("crew_handbook")

    def _attach_pass(self, employee=None, content=b"%PDF-1.4 fake pass"):
        employee = employee or self.sam
        employee.pass_pdf.save("pass.pdf", ContentFile(content), save=True)
        return employee

    def _unlock(self, **overrides):
        data = {"employee": self.sam.pk, "pin": "4821"}
        data.update(overrides)
        return self.client.post(self.url, data)

    def _ask_for_pass(self, **overrides):
        data = {
            "employee": self.sam.pk,
            "pin": "4821",
            "read_it": "on",
            "want_pass": "1",
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    # --- the gate -------------------------------------------------------

    def test_anonymous_get_is_served(self):
        """A `secret/` page must not redirect to a login.

        The crew have no accounts. A login here locks out exactly the people
        the page is for, and the only symptom is that nobody ever reads it.
        """
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["unlocked"])

    def test_locked_page_withholds_the_handbook(self):
        """Not a security boundary — but the gate has to actually gate.

        If the text renders behind the form, the button at the bottom is no
        longer at the bottom of anything and the tick means nothing.
        """
        body = self.client.get(self.url).content.decode()
        self.assertNotIn("The pay week runs Saturday to Friday", body)

    def test_wrong_pin_does_not_unlock(self):
        response = self._unlock(pin="0000")
        self.assertFalse(response.context["unlocked"])
        self.assertTrue(response.context["form"].has_error("pin"))

    def test_correct_pin_unlocks(self):
        response = self._unlock()
        self.assertTrue(response.context["unlocked"])
        self.assertIn(
            "The pay week runs Saturday to Friday", response.content.decode()
        )

    def test_inactive_employee_is_not_offered(self):
        gone = make_employee("Gone", pin="1111", active=False)
        response = self.client.get(self.url)
        self.assertNotIn(gone, response.context["form"].fields["employee"].queryset)

    # --- the tick box ---------------------------------------------------

    def test_pass_requires_the_tick(self):
        self._attach_pass()
        response = self._ask_for_pass(read_it="")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].has_error("read_it"))

    def test_a_missed_tick_does_not_relock_the_page(self):
        """The expensive version of this is losing somebody's place.

        They have just read the page. Bouncing them back to the name-and-PIN
        form to do it again is how a two-tap fix becomes a page nobody
        finishes.
        """
        self._attach_pass()
        response = self._ask_for_pass(read_it="")
        self.assertTrue(response.context["unlocked"])
        self.assertIn(
            "The pay week runs Saturday to Friday", response.content.decode()
        )

    def test_bad_pin_on_the_pass_request_does_relock(self):
        """A name we can't place is the one case that must go back to the top.

        Everything below depends on knowing who this is, so there is nothing
        useful to re-render.
        """
        self._attach_pass()
        response = self._ask_for_pass(pin="0000")
        self.assertFalse(response.context["unlocked"])

    # --- the pass -------------------------------------------------------

    def test_ticking_downloads_the_pass(self):
        self._attach_pass(content=b"%PDF-1.4 sam")
        response = self._ask_for_pass()
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 sam")

    def test_the_pass_can_be_fetched_again(self):
        """Lost passes are the reason the page stays reachable.

        Nothing is recorded when one is handed out, so nothing can decide a
        second request is a duplicate — which is the intended behaviour, not
        an oversight.
        """
        self._attach_pass()
        first = self._ask_for_pass()
        second = self._ask_for_pass()
        self.assertEqual(
            b"".join(first.streaming_content), b"".join(second.streaming_content)
        )

    def test_you_get_your_own_pass(self):
        """The PIN's actual job: not a lock, but the right file.

        Its failure mode is somebody walking off with another person's face
        on their pass, which is discovered at the gate by a stranger.
        """
        self._attach_pass(content=b"%PDF-1.4 sam")
        alex = make_employee("Alex", pin="9090")
        self._attach_pass(alex, content=b"%PDF-1.4 alex")

        response = self.client.post(self.url, {
            "employee": alex.pk, "pin": "9090",
            "read_it": "on", "want_pass": "1",
        })
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 alex")

    def test_no_pass_on_file_names_a_person_to_contact(self):
        """A dead button is the failure this replaces.

        Nothing the reader can do will make the page work, so it has to stop
        them waiting on it.
        """
        response = self._ask_for_pass()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["no_pass"])
        self.assertIn("contact Michael", response.content.decode())

    def test_no_pass_still_leaves_the_page_readable(self):
        self._ask_for_pass()
        response = self._ask_for_pass()
        self.assertTrue(response.context["unlocked"])

    # --- remembering, which is not authorising --------------------------

    def test_unlocking_remembers_the_phone(self):
        self._unlock()
        form = self.client.get(self.url).context["form"]
        self.assertEqual(form.initial.get("employee"), self.sam.pk)
        self.assertEqual(form.initial.get("pin"), "4821")

    def test_the_cookie_does_not_stand_in_for_the_pin(self):
        """Same guarantee as the hours and booth forms.

        A remembered PIN types the field in. It has never authorised
        anything, and a page that hands out a file is exactly where that
        would start to look tempting.
        """
        self._attach_pass()
        self._unlock()          # writes the cookie
        response = self._ask_for_pass(pin="0000")
        self.assertFalse(response.context["unlocked"])
        self.assertTrue(response.context["form"].has_error("pin"))

    def test_forget_link_clears_the_prefill(self):
        self._unlock()
        self.client.get(self.url, {crew.FORGET: "1"})
        form = self.client.get(self.url).context["form"]
        self.assertIsNone(form.initial.get("employee"))

    # --- nothing is recorded --------------------------------------------

    def test_reading_records_nothing(self):
        """There is deliberately no read-receipt.

        The tick is a speed bump, not evidence. Storing it would invite it to
        be used as evidence later, which a checkbox on an unauthenticated
        page cannot support.
        """
        self._attach_pass()
        self._ask_for_pass()
        self.sam.refresh_from_db()
        self.assertEqual(self.sam.notes, "")
class EmployeeForLoginTests(TestCase):
    """Resolving a signed-in user to an `Employee`, creating one if needed.

    The rule this enforces is small and worth stating plainly: **a signed-in
    person is never asked who they are.** Not on the booth form, not on the
    close, not on the handbook. The case that used to break it was a login
    with no `Employee` row, which fell back to the name picker — and the
    symptom was being shown your colleagues' names and asked which one you
    were, on a page you had already authenticated for.
    """

    def test_anonymous_resolves_to_nobody(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertIsNone(crew.employee_for(AnonymousUser()))
        self.assertIsNone(crew.employee_for(None))

    def test_a_linked_login_resolves_to_its_row(self):
        employee = make_employee("Robin", pin="1111")
        user = User.objects.create_user("robin", password="pw")
        employee.user = user
        employee.save()

        self.assertEqual(crew.employee_for(user), employee)

    def test_an_unlinked_login_gets_a_row_named_after_it(self):
        user = User.objects.create_user("stranger", password="pw")

        employee = crew.employee_for(user)

        self.assertEqual(employee.name, "stranger")
        self.assertEqual(employee.user, user)

    def test_the_created_row_has_no_pin(self):
        """These people sign in through Django, so a PIN would be a second
        credential for the same person that nobody has been told."""
        user = User.objects.create_user("stranger", password="pw")

        self.assertEqual(crew.employee_for(user).pin, "")

    def test_a_pinless_row_cannot_walk_in_through_the_pin_door(self):
        """Blank is not a PIN that happens to be easy to guess.

        `clean_pin` wants four digits, so there is nothing submittable that
        matches a blank one — the row exists to be linked and paid, not to be
        picked off a list at the stall.
        """
        user = User.objects.create_user("stranger", password="pw")
        employee = crew.employee_for(user)

        form = CrewHandbookForm(data={"employee": employee.pk, "pin": ""})

        self.assertFalse(form.is_valid())
        self.assertTrue(form.has_error("pin"))

    def test_a_pinless_row_is_kept_off_the_name_picker(self):
        """Picking it is a dead end, so it isn't offered."""
        user = User.objects.create_user("stranger", password="pw")
        created = crew.employee_for(user)
        crew_member = make_employee("Sam", pin="4821")

        offered = CrewHandbookForm().fields["employee"].queryset

        self.assertIn(crew_member, offered)
        self.assertNotIn(created, offered)

    def test_resolving_twice_does_not_make_two_rows(self):
        user = User.objects.create_user("stranger", password="pw")

        first = crew.employee_for(user)
        second = crew.employee_for(user)

        self.assertEqual(first, second)
        self.assertEqual(Employee.objects.filter(name="stranger").count(), 1)

    def test_a_same_named_row_is_linked_rather_than_duplicated(self):
        """The one guess allowed here, and it is a narrow one.

        An exact username against an exact employee name. The alternatives
        are an IntegrityError on a unique name, or a second row for somebody
        who already has one.
        """
        existing = make_employee("stranger", pin="2222")
        user = User.objects.create_user("stranger", password="pw")

        resolved = crew.employee_for(user)

        self.assertEqual(resolved, existing)
        self.assertEqual(Employee.objects.filter(name="stranger").count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.user, user)
        self.assertEqual(existing.pin, "2222", "an existing PIN is left alone")

    def test_an_inactive_link_still_resolves(self):
        """Retiring somebody doesn't stop them being who they are.

        Pages that shouldn't serve a retired employee filter on `is_active`
        themselves; resolving identity is a different question from deciding
        access, and conflating them here would silently hand a retired person
        the name picker instead.
        """
        employee = make_employee("Gone", pin="1111", active=False)
        user = User.objects.create_user("gone", password="pw")
        employee.user = user
        employee.save()

        self.assertEqual(crew.employee_for(user), employee)
@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class CrewHandbookSignedInTests(TestCase):
    """The handbook for somebody with a staff login.

    A login already answers the only question the gate asks, so the gate
    isn't shown: the page opens on the text. The tick box stays, because it
    is asking something the login can't answer — whether this person has read
    the page.
    """

    def setUp(self):
        self.url = reverse("crew_handbook")
        self.user = User.objects.create_user("michael", password="pw")

    def test_a_login_opens_straight_onto_the_handbook(self):
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertTrue(response.context["unlocked"])
        self.assertIn(
            "The pay week runs Saturday to Friday", response.content.decode()
        )

    def test_a_login_is_asked_for_neither_name_nor_pin(self):
        self.client.force_login(self.user)

        form = self.client.get(self.url).context["form"]

        self.assertNotIn("employee", form.fields)
        self.assertNotIn("pin", form.fields)
        self.assertEqual(form.signed_in_as.name, "michael")

    def test_a_login_still_has_to_tick_the_box(self):
        """The one thing authentication cannot assert on somebody's behalf."""
        self.client.force_login(self.user)

        response = self.client.post(self.url, {"want_pass": "1"})

        self.assertTrue(response.context["form"].has_error("read_it"))
        self.assertTrue(response.context["unlocked"], "and doesn't lose their place")

    def test_a_login_gets_its_own_pass(self):
        employee = crew.employee_for(self.user)
        employee.pass_pdf.save("pass.pdf", ContentFile(b"%PDF-1.4 michael"), save=True)
        self.client.force_login(self.user)

        response = self.client.post(self.url, {"read_it": "on", "want_pass": "1"})

        self.assertEqual(
            b"".join(response.streaming_content), b"%PDF-1.4 michael"
        )

    def test_a_login_with_no_pass_is_told_who_to_ask(self):
        self.client.force_login(self.user)

        response = self.client.post(self.url, {"read_it": "on", "want_pass": "1"})

        self.assertTrue(response.context["no_pass"])
        self.assertIn("contact Michael", response.content.decode())
