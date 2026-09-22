"""Reading a supplier's invoice, and the order number that makes it safe.

The reasoning is in `docs/claude/stock.md` under *Invoices*. The two things
worth pinning here are the two that fail silently: a cost that reads as a
fact, and a delivery booked twice onto a shelf nothing recounts.
"""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .. import invoiceread
from ..models import (
    RawProduct,
    RawProductCategory,
    Supplier,
    SupplierInvoice,
    SupplierInvoiceLine,
)


class InvoiceFlowTests(TestCase):
    """Uploading, correcting and booking one document."""

    def setUp(self):
        self.user = User.objects.create_superuser("inv", "i@example.test", "pw")
        self.client.force_login(self.user)
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.yarn, _ = RawProductCategory.objects.get_or_create(name="Yarn")
        self.veil = RawProduct.objects.create(
            name="Half Circle Veil", category=self.silk,
            price=Decimal("24.99"), number_on_hand=12,
        )
        self.skein = RawProduct.objects.create(
            name="Heavenly", category=self.yarn,
            price=Decimal("8.71"), number_on_hand=40,
        )
        self.supplier = Supplier.objects.create(name="Wool2dye4")

    # --- helpers ---------------------------------------------------------

    def _draft(self, **kwargs):
        kwargs.setdefault("order_number", "W2D-1183")
        return SupplierInvoice.objects.create(**kwargs)

    def _line(self, invoice, product, quantity=10, unit_cost="8.00", description="x"):
        return SupplierInvoiceLine.objects.create(
            invoice=invoice, raw_product=product, quantity=quantity,
            unit_cost=Decimal(unit_cost), description=description,
        )

    def _post(self, invoice, rows, **extra):
        data = {
            "order_number": extra.pop("order_number", invoice.order_number),
            "ordered_on": extra.pop("ordered_on", "2026-09-01"),
            "supplier": extra.pop("supplier", str(self.supplier.pk)),
            "row": [key for key, _ in rows],
        }
        for key, fields in rows:
            for name, value in fields.items():
                data[f"{name}_{key}"] = value
        data.update(extra)
        return self.client.post(reverse("invoice_book", args=[invoice.pk]), data)

    # --- the two claims a booking makes ----------------------------------

    def test_booking_sets_the_replacement_cost_and_adds_the_delivery(self):
        """Cost is written flat; quantity is added. Two different claims."""
        invoice = self._draft()
        line = self._line(invoice, self.veil, quantity=10, unit_cost="22.50")

        self._post(invoice, [(str(line.pk), {
            "book": "1", "product": str(self.veil.pk),
            "quantity": "10", "cost": "26.40", "description": "45in semicircle",
        })])

        self.veil.refresh_from_db()
        self.assertEqual(self.veil.price, Decimal("26.40"))
        self.assertEqual(self.veil.number_on_hand, 22)

        invoice.refresh_from_db()
        self.assertFalse(invoice.is_draft)
        booked = invoice.lines.get()
        self.assertEqual(booked.previous_price, Decimal("24.99"))
        self.assertEqual(booked.line_total, Decimal("264.00"))
        self.assertEqual(booked.description, "45in semicircle")

    def test_a_cheaper_invoice_still_wins(self):
        """Replacement cost is today's price, not the highest ever paid."""
        invoice = self._draft()
        line = self._line(invoice, self.veil)
        self._post(invoice, [(str(line.pk), {
            "book": "1", "product": str(self.veil.pk),
            "quantity": "5", "cost": "19.00",
        })])
        self.veil.refresh_from_db()
        self.assertEqual(self.veil.price, Decimal("19.00"))

    def test_booking_does_not_claim_the_shelf_was_counted(self):
        """A delivery note is a claim about a change; a count is a measurement."""
        invoice = self._draft()
        line = self._line(invoice, self.veil)
        self._post(invoice, [(str(line.pk), {
            "book": "1", "product": str(self.veil.pk),
            "quantity": "5", "cost": "19.00",
        })])
        self.veil.refresh_from_db()
        self.assertIsNone(self.veil.counted_at)

    # --- removing and adding rows ----------------------------------------

    def test_an_unticked_row_is_the_way_to_remove_one(self):
        invoice = self._draft()
        keep = self._line(invoice, self.veil)
        drop = self._line(invoice, self.skein)

        self._post(invoice, [
            (str(keep.pk), {"book": "1", "product": str(self.veil.pk),
                            "quantity": "4", "cost": "25.00"}),
            (str(drop.pk), {"product": str(self.skein.pk),
                            "quantity": "10", "cost": "9.00"}),
        ])

        self.skein.refresh_from_db()
        self.assertEqual(self.skein.number_on_hand, 40)
        self.assertEqual(self.skein.price, Decimal("8.71"))
        invoice.refresh_from_db()
        self.assertEqual([l.raw_product_id for l in invoice.lines.all()], [self.veil.pk])

    def test_a_line_the_reader_missed_can_be_typed_in(self):
        """A reader that misses a line must not be the end of the job."""
        invoice = self._draft()
        line = self._line(invoice, self.veil)

        self._post(invoice, [
            (str(line.pk), {"book": "1", "product": str(self.veil.pk),
                            "quantity": "4", "cost": "25.00"}),
            ("new1", {"book": "1", "product": str(self.skein.pk),
                      "quantity": "25", "cost": "8.99",
                      "description": "Heavenly natural, 25 pack"}),
        ])

        self.skein.refresh_from_db()
        self.assertEqual(self.skein.number_on_hand, 65)
        self.assertEqual(self.skein.price, Decimal("8.99"))

    # --- nothing lands unless every ticked line reads ---------------------

    def test_one_bad_figure_books_nothing(self):
        invoice = self._draft()
        good = self._line(invoice, self.veil)
        bad = self._line(invoice, self.skein)

        response = self._post(invoice, [
            (str(good.pk), {"book": "1", "product": str(self.veil.pk),
                            "quantity": "4", "cost": "25.00"}),
            (str(bad.pk), {"book": "1", "product": str(self.skein.pk),
                           "quantity": "ten", "cost": "8.99"}),
        ])

        self.assertEqual(response.status_code, 200)
        self.veil.refresh_from_db()
        self.skein.refresh_from_db()
        self.assertEqual(self.veil.number_on_hand, 12)
        self.assertEqual(self.skein.number_on_hand, 40)
        self.assertTrue(SupplierInvoice.objects.get(pk=invoice.pk).is_draft)

    def test_a_ticked_row_with_no_blank_is_named_rather_than_skipped(self):
        invoice = self._draft()
        line = self._line(invoice, self.veil, description="Mystery item")
        response = self._post(invoice, [(str(line.pk), {
            "book": "1", "product": "", "quantity": "4", "cost": "25.00",
            "description": "Mystery item",
        })])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mystery item")
        self.assertTrue(SupplierInvoice.objects.get(pk=invoice.pk).is_draft)

    def test_what_was_typed_survives_a_refusal(self):
        """Losing a corrected pass to one fat-fingered digit is the expensive
        failure on this form, exactly as on the bill form."""
        invoice = self._draft()
        good = self._line(invoice, self.veil)
        bad = self._line(invoice, self.skein)
        response = self._post(invoice, [
            (str(good.pk), {"book": "1", "product": str(self.veil.pk),
                            "quantity": "4", "cost": "25.00"}),
            (str(bad.pk), {"book": "1", "product": str(self.skein.pk),
                           "quantity": "ten", "cost": "8.99"}),
        ])
        rows = {row["key"]: row for row in response.context["rows"]}
        self.assertEqual(rows[str(good.pk)]["quantity"], "4")
        self.assertEqual(rows[str(good.pk)]["unit_cost"], "25.00")
        self.assertEqual(rows[str(bad.pk)]["quantity"], "ten")

    # --- the order number -------------------------------------------------

    def test_an_order_number_is_required_to_book(self):
        invoice = self._draft(order_number="")
        line = self._line(invoice, self.veil)
        response = self._post(invoice, [(str(line.pk), {
            "book": "1", "product": str(self.veil.pk),
            "quantity": "4", "cost": "25.00",
        })], order_number="")
        self.assertEqual(response.status_code, 200)
        self.veil.refresh_from_db()
        self.assertEqual(self.veil.number_on_hand, 12)

    def test_the_same_order_twice_is_refused_loudly(self):
        first = self._draft()
        line = self._line(first, self.veil)
        self._post(first, [(str(line.pk), {
            "book": "1", "product": str(self.veil.pk),
            "quantity": "10", "cost": "25.00",
        })])
        self.veil.refresh_from_db()
        self.assertEqual(self.veil.number_on_hand, 22)

        again = self._draft()
        line2 = self._line(again, self.veil)
        response = self._post(again, [(str(line2.pk), {
            "book": "1", "product": str(self.veil.pk),
            "quantity": "10", "cost": "25.00",
        })])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already been ingested")
        self.veil.refresh_from_db()
        self.assertEqual(self.veil.number_on_hand, 22)

    def test_spacing_and_case_do_not_defeat_the_check(self):
        first = self._draft(order_number="W2D-1183")
        line = self._line(first, self.veil)
        self._post(first, [(str(line.pk), {
            "book": "1", "product": str(self.veil.pk),
            "quantity": "1", "cost": "25.00",
        })])

        again = self._draft(order_number="")
        line2 = self._line(again, self.veil)
        response = self._post(again, [(str(line2.pk), {
            "book": "1", "product": str(self.veil.pk),
            "quantity": "1", "cost": "25.00",
        })], order_number="  w2d-1183 ")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(SupplierInvoice.objects.get(pk=again.pk).is_draft)

    def test_a_duplicate_can_still_be_booked_on_purpose(self):
        """Loud, not locked. A page that simply refuses gets worked around by
        typing a -2 on the end, which teaches nobody anything."""
        first = self._draft()
        line = self._line(first, self.veil)
        self._post(first, [(str(line.pk), {
            "book": "1", "product": str(self.veil.pk),
            "quantity": "10", "cost": "25.00",
        })])

        again = self._draft()
        line2 = self._line(again, self.veil)
        self._post(again, [(str(line2.pk), {
            "book": "1", "product": str(self.veil.pk),
            "quantity": "10", "cost": "25.00",
        })], confirm_duplicate="1")

        self.veil.refresh_from_db()
        self.assertEqual(self.veil.number_on_hand, 32)
        self.assertFalse(SupplierInvoice.objects.get(pk=again.pk).is_draft)

    def test_a_draft_is_not_a_clash(self):
        """Only a booked invoice has moved anything, so only a booked one
        warns. Two drafts of the same order are somebody reading it twice."""
        self._draft()
        again = self._draft()
        line = self._line(again, self.veil)
        self._post(again, [(str(line.pk), {
            "book": "1", "product": str(self.veil.pk),
            "quantity": "3", "cost": "25.00",
        })])
        self.veil.refresh_from_db()
        self.assertEqual(self.veil.number_on_hand, 15)

    # --- drafts ------------------------------------------------------------

    def test_a_draft_discards_but_a_booked_invoice_does_not(self):
        draft = self._draft()
        self._line(draft, self.veil)
        self.client.post(reverse("invoice_discard", args=[draft.pk]))
        self.assertFalse(SupplierInvoice.objects.filter(pk=draft.pk).exists())

        booked = self._draft(order_number="W2D-2")
        line = self._line(booked, self.veil)
        self._post(booked, [(str(line.pk), {
            "book": "1", "product": str(self.veil.pk),
            "quantity": "1", "cost": "25.00",
        })])
        self.client.post(reverse("invoice_discard", args=[booked.pk]))
        self.assertTrue(SupplierInvoice.objects.filter(pk=booked.pk).exists())

    def test_booking_twice_from_the_same_draft_does_nothing_the_second_time(self):
        invoice = self._draft()
        line = self._line(invoice, self.veil)
        payload = [(str(line.pk), {
            "book": "1", "product": str(self.veil.pk),
            "quantity": "7", "cost": "25.00",
        })]
        self._post(invoice, payload)
        self._post(invoice, payload)
        self.veil.refresh_from_db()
        self.assertEqual(self.veil.number_on_hand, 19)

    # --- the upload door ---------------------------------------------------

    def test_a_reading_lands_as_a_draft_with_every_figure_in_a_box(self):
        reading = invoiceread.ReadInvoice(
            order_number="w2d-77",
            supplier_name="Wool2dye4 Inc",
            lines=[
                invoiceread.ReadLine(
                    description="Half circle veil 45in",
                    quantity=10, line_total=Decimal("249.90"),
                    unit_cost=Decimal("24.99"), raw_product_id=self.veil.pk,
                ),
                invoiceread.ReadLine(description="Something else", quantity=2),
            ],
        )
        with mock.patch.object(invoiceread, "read", return_value=reading):
            response = self.client.post(
                reverse("invoice_index"),
                {"invoice": SimpleUploadedFile("bill.pdf", b"%PDF-1.4 fake",
                                               content_type="application/pdf")},
                follow=True,
            )

        invoice = SupplierInvoice.objects.get()
        self.assertTrue(invoice.is_draft)
        self.assertEqual(invoice.order_number, "W2D-77")
        self.assertEqual(invoice.supplier, self.supplier)
        self.assertEqual(invoice.lines.count(), 2)
        # Nothing has moved, and the page says the line's own wording.
        self.veil.refresh_from_db()
        self.assertEqual(self.veil.number_on_hand, 12)
        self.assertContains(response, "Half circle veil 45in")
        # The unmatched line is there to be assigned, not silently dropped.
        self.assertContains(response, "Something else")

    def test_text_pasted_out_of_an_email_is_the_other_door(self):
        """Most of these orders are an email before they are ever a file."""
        reading = invoiceread.ReadInvoice(
            order_number="W2D-90",
            lines=[invoiceread.ReadLine(
                description="Heavenly 25 pack", quantity=25,
                line_total=Decimal("217.75"), unit_cost=Decimal("8.71"),
                raw_product_id=self.skein.pk,
            )],
        )
        with mock.patch.object(invoiceread, "read_text", return_value=reading) as reader:
            response = self.client.post(
                reverse("invoice_index"),
                {"pasted": "Order W2D-90\n25 x Heavenly ... 217.75"},
                follow=True,
            )

        reader.assert_called_once()
        invoice = SupplierInvoice.objects.get()
        self.assertTrue(invoice.is_draft)
        self.assertEqual(invoice.order_number, "W2D-90")
        # The paste is kept as the basis, the way a document would be.
        self.assertIn("25 x Heavenly", invoice.pasted_text)
        self.assertContains(response, "Heavenly 25 pack")
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.number_on_hand, 40)

    def test_neither_a_file_nor_text_is_refused_without_making_a_draft(self):
        response = self.client.post(reverse("invoice_index"), {"pasted": "   "},
                                    follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SupplierInvoice.objects.exists())

    def test_a_failed_reading_still_opens_a_form(self):
        """No key, a bad photo, an API having an afternoon: the worst case of
        this feature is the job as it was done before it existed."""
        reading = invoiceread.ReadInvoice(error="No CLAUDE_API_KEY is set")
        with mock.patch.object(invoiceread, "read", return_value=reading):
            response = self.client.post(
                reverse("invoice_index"),
                {"invoice": SimpleUploadedFile("bill.pdf", b"%PDF", content_type="application/pdf")},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No CLAUDE_API_KEY is set")
        self.assertTrue(SupplierInvoice.objects.get().is_draft)
        # Five spare rows, so a document nothing could be read from is still
        # a document somebody can enter.
        self.assertEqual(len(response.context["rows"]), 5)


class OrderedThenReceivedTests(TestCase):
    """Most of these documents are order confirmations, not delivery notes.

    The cost is true the moment the order is placed. The goods are not in the
    building until they are — and `number_on_hand` is what a production run
    claims its blanks from, so putting them on the shelf early sends the dye
    room after yarn that is still in a van.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("ord", "o@example.test", "pw")
        self.client.force_login(self.user)
        self.yarn, _ = RawProductCategory.objects.get_or_create(name="Yarn")
        self.skein = RawProduct.objects.create(
            name="Heavenly", category=self.yarn,
            price=Decimal("8.71"), number_on_hand=40,
        )

    def _draft_with_line(self, order="W2D-500"):
        invoice = SupplierInvoice.objects.create(order_number=order)
        line = SupplierInvoiceLine.objects.create(
            invoice=invoice, raw_product=self.skein, quantity=250,
            unit_cost=Decimal("8.71"), description="Angel DK - 10 x 100g",
        )
        return invoice, line

    def _book(self, invoice, line, arrival):
        return self.client.post(reverse("invoice_book", args=[invoice.pk]), {
            "order_number": invoice.order_number,
            "ordered_on": "2026-09-11",
            "supplier": "",
            "arrival": arrival,
            "row": [str(line.pk)],
            f"book_{line.pk}": "1",
            f"product_{line.pk}": str(self.skein.pk),
            f"quantity_{line.pk}": "250",
            f"cost_{line.pk}": "9.40",
            f"description_{line.pk}": "Angel DK - 10 x 100g",
        })

    def test_on_order_sets_the_cost_and_leaves_the_shelf_alone(self):
        invoice, line = self._draft_with_line()
        self._book(invoice, line, "ordered")

        self.skein.refresh_from_db()
        self.assertEqual(self.skein.price, Decimal("9.40"))
        self.assertEqual(self.skein.number_on_hand, 40)

        invoice.refresh_from_db()
        self.assertFalse(invoice.is_draft)
        self.assertTrue(invoice.is_on_order)
        self.assertIsNone(invoice.received_on)

    def test_arrived_does_both_at_once(self):
        invoice, line = self._draft_with_line()
        self._book(invoice, line, "received")

        self.skein.refresh_from_db()
        self.assertEqual(self.skein.price, Decimal("9.40"))
        self.assertEqual(self.skein.number_on_hand, 290)

        invoice.refresh_from_db()
        self.assertFalse(invoice.is_on_order)
        self.assertIsNotNone(invoice.received_on)

    def test_yarn_on_order_cannot_be_claimed_by_a_production_sheet(self):
        """The bug the two dates exist for: `number_on_hand` is what a run
        takes its blanks from."""
        invoice, line = self._draft_with_line()
        self._book(invoice, line, "ordered")
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.number_on_hand, 40)

    def test_receiving_puts_the_settled_lines_on_the_shelf(self):
        invoice, line = self._draft_with_line()
        self._book(invoice, line, "ordered")

        self.client.post(reverse("invoice_receive", args=[invoice.pk]),
                         {"received_on": "2026-09-20"})

        self.skein.refresh_from_db()
        self.assertEqual(self.skein.number_on_hand, 290)
        invoice.refresh_from_db()
        self.assertEqual(invoice.received_on.isoformat(), "2026-09-20")
        # The cost was already set at order time and is not touched again.
        self.assertEqual(self.skein.price, Decimal("9.40"))

    def test_receiving_twice_moves_nothing(self):
        invoice, line = self._draft_with_line()
        self._book(invoice, line, "ordered")
        self.client.post(reverse("invoice_receive", args=[invoice.pk]), {})
        self.client.post(reverse("invoice_receive", args=[invoice.pk]), {})
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.number_on_hand, 290)

    def test_a_draft_cannot_be_received(self):
        invoice, _line = self._draft_with_line()
        self.client.post(reverse("invoice_receive", args=[invoice.pk]), {})
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.number_on_hand, 40)
        invoice.refresh_from_db()
        self.assertIsNone(invoice.received_on)

    def test_the_card_says_which_state_it_is_in(self):
        invoice, line = self._draft_with_line()
        self._book(invoice, line, "ordered")
        response = self.client.get(reverse("invoice_detail", args=[invoice.pk]))
        self.assertContains(response, "still on order")
        self.assertContains(response, "Put it on the shelf")

        self.client.post(reverse("invoice_receive", args=[invoice.pk]), {})
        response = self.client.get(reverse("invoice_detail", args=[invoice.pk]))
        self.assertContains(response, "on the shelf since")
        self.assertNotContains(response, "Put it on the shelf")

    def test_the_picker_separates_what_is_coming_from_what_arrived(self):
        coming, line = self._draft_with_line("W2D-501")
        self._book(coming, line, "ordered")
        here, line2 = self._draft_with_line("W2D-502")
        self._book(here, line2, "received")

        response = self.client.get(reverse("invoice_index"))
        self.assertEqual(
            [i.order_number for i in response.context["on_order"]], ["W2D-501"]
        )
        self.assertEqual(
            [i.order_number for i in response.context["booked"]], ["W2D-502"]
        )
        self.assertContains(response, "250 units")


class OnOrderIsNotUnderParTests(TestCase):
    """Par is an order signal, not a stock reading.

    Something already on a supplier's van is not under par in any way that
    should make a page flash: a second order will not make the first arrive
    faster. Same subtraction `production.in_flight` makes for baths already
    on a sheet.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("par", "p@example.test", "pw")
        self.client.force_login(self.user)
        self.yarn, _ = RawProductCategory.objects.get_or_create(name="Yarn")
        self.supplier = Supplier.objects.create(name="Wool2dye4", lead_time_days=14)
        self.skein = RawProduct.objects.create(
            name="Heavenly", category=self.yarn, price=Decimal("8.71"),
            number_on_hand=40, par_level=200, supplier=self.supplier,
        )

    def _order(self, quantity=250, ordered_on="2026-09-11", received=False):
        invoice = SupplierInvoice.objects.create(
            order_number=f"W2D-{quantity}-{ordered_on}",
            supplier=self.supplier,
            ordered_on=date.fromisoformat(ordered_on),
            booked_at=timezone.now(),
            received_on=date.fromisoformat(ordered_on) if received else None,
        )
        SupplierInvoiceLine.objects.create(
            invoice=invoice, raw_product=self.skein, quantity=quantity,
            unit_cost=Decimal("8.71"),
        )
        return invoice

    def test_a_shelf_below_par_is_short_with_nothing_on_order(self):
        self.assertEqual(self.skein.raw_shortage, 160)

    def test_an_open_order_covers_the_shortage(self):
        self._order(250)
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.on_order, 250)
        self.assertEqual(self.skein.raw_shortage, 0)

    def test_a_partial_order_only_covers_its_part(self):
        self._order(60)
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.raw_shortage, 100)

    def test_what_arrived_stops_counting_as_on_order(self):
        """Once received it is on the shelf, and counting it twice would
        cover a shortage that is real."""
        self._order(250, received=True)
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.on_order, 0)
        self.assertEqual(self.skein.raw_shortage, 160)

    def test_a_draft_is_not_an_order(self):
        """Nobody has confirmed it, so nothing has been ordered."""
        invoice = SupplierInvoice.objects.create(order_number="W2D-DRAFT")
        SupplierInvoiceLine.objects.create(
            invoice=invoice, raw_product=self.skein, quantity=250,
            unit_cost=Decimal("8.71"),
        )
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.on_order, 0)
        self.assertEqual(self.skein.raw_shortage, 160)

    def test_on_order_never_reaches_the_shelf(self):
        """The line that must not move: `number_on_hand` is what a production
        run claims its blanks from, and yarn in transit cannot start a bath."""
        self._order(250)
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.number_on_hand, 40)

    # --- the risk this takes on -------------------------------------------

    def test_the_page_says_what_is_holding_the_shortage_down(self):
        """A suppressed signal with no visible reason is the par mistake
        wearing a delivery note."""
        self._order(250)
        response = self.client.get(
            reverse("raw_inventory", args=[self.yarn.pk])
        )
        self.assertContains(response, "250 on order")
        self.assertContains(response, "since 11 Sep")

    def test_an_order_past_its_lead_time_says_so(self):
        """Nothing un-suppresses automatically — that would be the app
        deciding. It is said out loud and a person judges it."""
        self._order(250, ordered_on="2026-01-05")
        self.skein.refresh_from_db()
        self.assertTrue(self.skein.order_is_late)
        response = self.client.get(reverse("raw_inventory", args=[self.yarn.pk]))
        self.assertContains(response, "past their usual lead time")

    def test_an_unknown_lead_time_claims_nothing(self):
        """Null means nobody has said, which is not zero — a date derived
        from a guess is the par mistake with a delivery on the end."""
        self.supplier.lead_time_days = None
        self.supplier.save()
        self._order(250, ordered_on="2026-01-05")
        self.skein.refresh_from_db()
        self.assertFalse(self.skein.order_is_late)

    def test_priming_a_page_costs_one_query_rather_than_one_per_row(self):
        self._order(250)
        others = [
            RawProduct.objects.create(
                name=f"Blank {n}", category=self.yarn, price="1.00", par_level=10
            )
            for n in range(12)
        ]
        blanks = [self.skein] + others
        with self.assertNumQueries(1):
            RawProduct.prime_on_order(blanks)
        with self.assertNumQueries(0):
            self.assertEqual([b.on_order for b in blanks], [250] + [0] * 12)


class WhatTurnedUpTests(TestCase):
    """Ordered 80, received 50 — both true, and the shelf gets 50.

    The alternative was marking 80 received and remembering to correct the
    shelf on another page, which is the step that has to be remembered to be
    correct. The second half is what gets skipped.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("short", "s@example.test", "pw")
        self.client.force_login(self.user)
        self.yarn, _ = RawProductCategory.objects.get_or_create(name="Yarn")
        self.skein = RawProduct.objects.create(
            name="Heavenly", category=self.yarn, price=Decimal("8.71"),
            number_on_hand=10, par_level=200,
        )
        self.invoice = SupplierInvoice.objects.create(
            order_number="W2D-800", ordered_on=date(2026, 9, 11),
            booked_at=timezone.now(),
        )
        self.line = SupplierInvoiceLine.objects.create(
            invoice=self.invoice, raw_product=self.skein, quantity=80,
            unit_cost=Decimal("8.71"), line_total=Decimal("696.80"),
        )

    def _receive(self, **extra):
        data = {"received_on": "2026-09-20"}
        data.update(extra)
        return self.client.post(
            reverse("invoice_receive", args=[self.invoice.pk]), data
        )

    def test_an_untouched_form_puts_the_whole_line_on(self):
        self._receive()
        self.skein.refresh_from_db()
        self.line.refresh_from_db()
        self.assertEqual(self.skein.number_on_hand, 90)
        self.assertIsNone(self.line.received_quantity)
        self.assertEqual(self.line.arrived, 80)

    def test_a_short_delivery_puts_on_what_arrived(self):
        self._receive(**{f"arrived_{self.line.pk}": "50"})
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.number_on_hand, 60)

    def test_both_figures_are_kept(self):
        """What was billed and what reached the shelf are different facts."""
        self._receive(**{f"arrived_{self.line.pk}": "50"})
        self.line.refresh_from_db()
        self.assertEqual(self.line.quantity, 80)
        self.assertEqual(self.line.received_quantity, 50)
        self.assertEqual(self.line.arrived, 50)
        self.assertTrue(self.line.came_up_short)
        # The billed total is untouched: it is what the document says.
        self.assertEqual(self.line.line_total, Decimal("696.80"))

    def test_the_cost_per_unit_is_unaffected(self):
        self._receive(**{f"arrived_{self.line.pk}": "50"})
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.price, Decimal("8.71"))

    def test_nothing_is_still_outstanding_afterwards(self):
        """The 30 that never came is between a person and the supplier. The
        order is closed, so it stops holding the reorder signal down."""
        self._receive(**{f"arrived_{self.line.pk}": "50"})
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.on_order, 0)
        self.assertEqual(self.skein.raw_shortage, 140)

    def test_receiving_none_of_a_line_is_allowed(self):
        self._receive(**{f"arrived_{self.line.pk}": "0"})
        self.skein.refresh_from_db()
        self.line.refresh_from_db()
        self.assertEqual(self.skein.number_on_hand, 10)
        self.assertEqual(self.line.received_quantity, 0)

    def test_one_bad_figure_receives_nothing(self):
        """The bill form's bargain: half a delivery booked is worse than
        none, because the half that landed looks complete."""
        response = self._receive(**{f"arrived_{self.line.pk}": "fifty"})
        self.skein.refresh_from_db()
        self.invoice.refresh_from_db()
        self.assertEqual(self.skein.number_on_hand, 10)
        self.assertIsNone(self.invoice.received_on)
        self.assertIn("adjust=1", response.url)

    def test_the_message_names_what_was_short(self):
        response = self._receive(**{f"arrived_{self.line.pk}": "50"})
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any("50 of 80" in m for m in messages))

    # --- the two ways the form arrives ------------------------------------

    def test_the_boxes_stay_out_of_the_way_until_asked_for(self):
        body = self.client.get(
            reverse("invoice_detail", args=[self.invoice.pk])
        ).content.decode()
        self.assertIn("Some of it didn't turn up", body)
        self.assertNotIn(f'name="arrived_{self.line.pk}"', body)

    def test_a_plain_url_renders_the_same_inputs(self):
        """With the script blocked the link is still a working link."""
        body = self.client.get(
            reverse("invoice_detail", args=[self.invoice.pk]) + "?adjust=1"
        ).content.decode()
        self.assertIn(f'name="arrived_{self.line.pk}"', body)
        self.assertIn('placeholder="80"', body)

    def test_the_fragment_carries_no_page_shell(self):
        response = self.client.get(
            reverse("invoice_receive_lines", args=[self.invoice.pk])
        )
        body = response.content.decode()
        self.assertIn(f'name="arrived_{self.line.pk}"', body)
        self.assertNotIn("<!doctype html>", body.lower())

    def test_the_card_prints_both_figures_once_received(self):
        self._receive(**{f"arrived_{self.line.pk}": "50"})
        body = self.client.get(
            reverse("invoice_detail", args=[self.invoice.pk])
        ).content.decode()
        self.assertIn("80", body)
        self.assertIn("50", body)


class AnOrderCanAlsoNeverArriveTests(TestCase):
    """The third ending, and the one the reorder page depends on.

    An open order holds a blank's shortage down. One that is never coming
    would hold it down for good, silently — a page that looks fine and an
    order nobody places.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("lost", "l@example.test", "pw")
        self.client.force_login(self.user)
        self.yarn, _ = RawProductCategory.objects.get_or_create(name="Yarn")
        self.supplier = Supplier.objects.create(name="Wool2dye4", lead_time_days=14)
        self.skein = RawProduct.objects.create(
            name="Heavenly", category=self.yarn, price=Decimal("8.71"),
            number_on_hand=40, par_level=200, supplier=self.supplier,
        )
        self.invoice = SupplierInvoice.objects.create(
            order_number="W2D-900", supplier=self.supplier,
            ordered_on=date(2026, 9, 11), booked_at=timezone.now(),
        )
        SupplierInvoiceLine.objects.create(
            invoice=self.invoice, raw_product=self.skein, quantity=250,
            unit_cost=Decimal("8.71"), description="Angel DK",
        )

    def _write_off(self, note="never shipped"):
        return self.client.post(
            reverse("invoice_write_off", args=[self.invoice.pk]),
            {"written_off_note": note},
        )

    def test_writing_one_off_hands_the_reorder_signal_back(self):
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.raw_shortage, 0)

        self._write_off()

        self.skein.refresh_from_db()
        self.assertEqual(self.skein.on_order, 0)
        self.assertEqual(self.skein.raw_shortage, 160)

    def test_it_moves_no_stock(self):
        self._write_off()
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.number_on_hand, 40)

    def test_the_costs_stand(self):
        """They were true when the order was placed and are still what the
        supplier charges. `price` is the replacement cost, not a record of
        this document."""
        self._write_off()
        self.skein.refresh_from_db()
        self.assertEqual(self.skein.price, Decimal("8.71"))

    def test_it_is_not_a_received_date(self):
        """Folding it into `received_on` would claim a delivery that never
        happened."""
        self._write_off()
        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.received_on)
        self.assertIsNotNone(self.invoice.written_off_on)
        self.assertEqual(self.invoice.state, "written off")
        self.assertFalse(self.invoice.is_on_order)
        self.assertTrue(self.invoice.is_written_off)

    def test_the_reason_is_kept_in_whatever_words_fit(self):
        self._write_off("back-ordered to March, cancelled")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.written_off_note, "back-ordered to March, cancelled")
        response = self.client.get(reverse("invoice_detail", args=[self.invoice.pk]))
        self.assertContains(response, "back-ordered to March, cancelled")
        self.assertContains(response, "never arrived")

    def test_a_late_parcel_can_still_be_received(self):
        """Turning up three weeks after it was given up on is ordinary, so it
        is the same button rather than an undo first."""
        self._write_off()
        self.client.post(reverse("invoice_receive", args=[self.invoice.pk]),
                         {"received_on": "2026-10-02"})

        self.invoice.refresh_from_db()
        self.skein.refresh_from_db()
        self.assertIsNone(self.invoice.written_off_on)
        self.assertEqual(self.invoice.written_off_note, "")
        self.assertEqual(self.invoice.received_on.isoformat(), "2026-10-02")
        self.assertEqual(self.skein.number_on_hand, 290)
        self.assertEqual(self.invoice.state, "received")

    def test_what_arrived_cannot_be_given_up_on(self):
        self.client.post(reverse("invoice_receive", args=[self.invoice.pk]), {})
        self._write_off()
        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.written_off_on)

    def test_a_draft_is_discarded_not_written_off(self):
        draft = SupplierInvoice.objects.create(order_number="W2D-901")
        self.client.post(reverse("invoice_write_off", args=[draft.pk]),
                         {"written_off_note": "x"})
        draft.refresh_from_db()
        self.assertIsNone(draft.written_off_on)

    def test_the_picker_lists_it_apart_from_what_arrived(self):
        self._write_off()
        response = self.client.get(reverse("invoice_index"))
        self.assertEqual(
            [i.order_number for i in response.context["written_off"]], ["W2D-900"]
        )
        self.assertEqual(response.context["on_order"], [])
        self.assertContains(response, "Never arrived")

    def test_the_message_names_what_went_back_below_par(self):
        """The consequence, said at the moment it happens — this is the whole
        reason the action exists."""
        response = self._write_off()
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any("back below par" in m and "Heavenly" in m for m in messages))


class RememberedWordingTests(TestCase):
    """`Machine Hemmed 8mm Habotai Scarves 21" x 76" Circle` is an Infinity.

    There is no deriving that — not from the name, not from the fibre. It is
    a fact somebody knows, it only has to be said once, and once said it
    outranks any reading of the same line.
    """

    HABOTAI = 'Machine Hemmed 8mm Habotai Scarves 21" x 76" Circle'

    def setUp(self):
        self.user = User.objects.create_superuser("rem", "r@example.test", "pw")
        self.client.force_login(self.user)
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.infinity = RawProduct.objects.create(
            name="Infinity", category=self.silk, price=Decimal("15.37"),
        )
        self.veil = RawProduct.objects.create(
            name="Half Circle Veil", category=self.silk, price=Decimal("24.99"),
        )

    def _book(self, invoice, product, description):
        line = SupplierInvoiceLine.objects.create(
            invoice=invoice, raw_product=product, quantity=4,
            unit_cost=Decimal("15.00"), description=description,
        )
        return self.client.post(reverse("invoice_book", args=[invoice.pk]), {
            "order_number": invoice.order_number,
            "ordered_on": "2026-09-01",
            "supplier": "",
            "row": [str(line.pk)],
            f"book_{line.pk}": "1",
            f"product_{line.pk}": str(product.pk),
            f"quantity_{line.pk}": "4",
            f"cost_{line.pk}": "15.00",
            f"description_{line.pk}": description,
        })

    def test_confirming_a_line_is_what_records_the_wording(self):
        """Nobody types it in. The pick was being made anyway."""
        invoice = SupplierInvoice.objects.create(order_number="A1")
        self._book(invoice, self.infinity, self.HABOTAI)
        self.infinity.refresh_from_db()
        self.assertEqual(self.infinity.invoice_description, self.HABOTAI)

    def test_a_remembered_wording_beats_the_reading(self):
        self.infinity.invoice_description = self.HABOTAI
        self.infinity.save()
        payload = {
            "order_number": "A2", "supplier": "", "ordered_on": "",
            "lines": [{
                "description": self.HABOTAI, "quantity": 10,
                "line_total": "153.70", "unit_price": None,
                # The reading got it wrong, which is the whole point.
                "blank_id": self.veil.pk,
            }],
        }
        reading = invoiceread._to_reading(payload, [self.infinity, self.veil])
        self.assertEqual(reading.lines[0].raw_product_id, self.infinity.pk)
        self.assertTrue(reading.lines[0].remembered)

    def test_curly_quotes_and_spacing_do_not_defeat_it(self):
        """The same order as a PDF and as an email is the case this hits."""
        self.infinity.invoice_description = self.HABOTAI
        self.infinity.save()
        emailed = 'machine hemmed  8mm habotai scarves 21\u201d x 76\u201d circle'
        self.assertEqual(
            invoiceread.known_match(emailed, [self.infinity, self.veil]),
            self.infinity.pk,
        )

    def test_a_wording_belongs_to_one_blank_so_a_mismatch_heals(self):
        """Confirm it against the wrong blank, then put it right: the wording
        moves rather than leaving two blanks both claiming the line."""
        first = SupplierInvoice.objects.create(order_number="B1")
        self._book(first, self.veil, self.HABOTAI)
        self.veil.refresh_from_db()
        self.assertEqual(self.veil.invoice_description, self.HABOTAI)

        second = SupplierInvoice.objects.create(order_number="B2")
        self._book(second, self.infinity, self.HABOTAI)

        self.veil.refresh_from_db()
        self.infinity.refresh_from_db()
        self.assertEqual(self.infinity.invoice_description, self.HABOTAI)
        self.assertEqual(self.veil.invoice_description, "")
        self.assertEqual(
            invoiceread.known_match(self.HABOTAI, [self.infinity, self.veil]),
            self.infinity.pk,
        )

    def test_a_blank_description_does_not_forget_what_was_known(self):
        """Clearing the box means "nothing to say", not "forget"."""
        self.infinity.invoice_description = self.HABOTAI
        self.infinity.save()
        invoice = SupplierInvoice.objects.create(order_number="C1")
        self._book(invoice, self.infinity, "")
        self.infinity.refresh_from_db()
        self.assertEqual(self.infinity.invoice_description, self.HABOTAI)

    def test_nothing_is_matched_by_a_near_miss(self):
        """Exact or nothing. A wrong match arrives looking unremarkable and
        gets confirmed, which is worse than the dropdown it saved."""
        self.infinity.invoice_description = self.HABOTAI
        self.infinity.save()
        for text in ("Machine Hemmed 8mm Habotai Scarves", "8mm Habotai", ""):
            with self.subTest(text=text):
                self.assertIsNone(
                    invoiceread.known_match(text, [self.infinity, self.veil])
                )

    def test_the_page_says_which_matches_were_remembered(self):
        self.infinity.invoice_description = self.HABOTAI
        self.infinity.save()
        invoice = SupplierInvoice.objects.create(order_number="D1")
        SupplierInvoiceLine.objects.create(
            invoice=invoice, raw_product=self.infinity, quantity=4,
            unit_cost=Decimal("15.00"), description=self.HABOTAI,
        )
        response = self.client.get(reverse("invoice_detail", args=[invoice.pk]))
        self.assertContains(response, "remembered from a past invoice")


class NameOffALineTests(TestCase):
    """`Angel DK - 10 x 100g SKEINS` is how many came in a box.

    It ends up on reference sheets, barcode labels and the Square till, where
    the pack size is noise on all three — and wrong the day the supplier
    changes it.
    """

    def test_the_pack_clause_comes_off_the_name(self):
        for line, expected in [
            ("Angel DK - 10 x 100g SKEINS", "Angel DK"),
            ("Superwash Merino Zebra DK - 10 x 100g SKEINS",
             "Superwash Merino Zebra DK"),
            ("Tibetan 3ply - 5 x 100g SKEINS", "Tibetan 3ply"),
            ("Desert Silk/Baby Camel Sock - 10 x 100g SKEINS",
             "Desert Silk/Baby Camel Sock"),
        ]:
            with self.subTest(line=line):
                self.assertEqual(invoiceread.product_name(line), expected)

    def test_only_the_last_segment_goes(self):
        """The fibre is part of what the thing is; the pack is not."""
        self.assertEqual(
            invoiceread.product_name(
                "Pure Luxury - 50% Silk 50% Yak 4ply - 10 x 50g SKEINS"
            ),
            "Pure Luxury - 50% Silk 50% Yak 4ply",
        )

    def test_a_size_is_not_a_pack(self):
        """The case that must survive: an `x` and two numbers, but it is the
        measurement that distinguishes this blank from every other habotai."""
        name = 'Machine Hemmed 8mm Habotai Scarves 21" x 76" Circle'
        self.assertEqual(invoiceread.product_name(name), name)

    def test_a_line_with_no_pack_is_left_alone(self):
        self.assertEqual(invoiceread.product_name("Yarn Bowl"), "Yarn Bowl")

    def test_a_line_that_is_only_a_pack_keeps_something(self):
        """An empty box is worse than a wordy one."""
        self.assertEqual(
            invoiceread.product_name("10 x 100g SKEINS"), "10 x 100g SKEINS"
        )

    def test_the_link_trims_the_name_and_keeps_the_wording_exact(self):
        """The wording is a key — it has to match the next invoice character
        for character — so only the name is tidied."""
        user = User.objects.create_superuser("nm", "n@example.test", "pw")
        self.client.force_login(user)
        invoice = SupplierInvoice.objects.create(order_number="W2D-N1")
        SupplierInvoiceLine.objects.create(
            invoice=invoice, raw_product=None, quantity=10,
            unit_cost=Decimal("6.98"),
            description="Superwash Merino Zebra DK - 10 x 100g SKEINS",
        )
        body = self.client.get(
            reverse("invoice_detail", args=[invoice.pk])
        ).content.decode()
        self.assertIn("name=Superwash%20Merino%20Zebra%20DK&", body)
        self.assertIn(
            "invoice_description=Superwash%20Merino%20Zebra%20DK%20-%2010%20x%20100g%20SKEINS",
            body,
        )


class UnitCostTests(TestCase):
    """The pack is what the document states; what one costs is derived."""

    def test_the_pack_divides(self):
        self.assertEqual(
            invoiceread.unit_cost(Decimal("87.10"), 10), Decimal("8.71")
        )

    def test_the_printed_unit_price_is_the_fallback_only(self):
        self.assertEqual(
            invoiceread.unit_cost(None, 10, Decimal("8.75")), Decimal("8.75")
        )
        # A stated price loses to the division, because the division is what
        # the invoice actually charged.
        self.assertEqual(
            invoiceread.unit_cost(Decimal("87.10"), 10, Decimal("8.75")),
            Decimal("8.71"),
        )

    def test_nothing_readable_stays_nothing(self):
        self.assertIsNone(invoiceread.unit_cost(None, None))
        self.assertIsNone(invoiceread.unit_cost(Decimal("10.00"), 0))


class ReadingIsAdviceTests(TestCase):
    """What comes back from the model is narrowed before it is believed."""

    def setUp(self):
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.veil = RawProduct.objects.create(
            name="Half Circle Veil", category=self.silk, price=Decimal("24.99")
        )

    def test_a_blank_id_the_catalogue_does_not_have_is_dropped(self):
        payload = {
            "order_number": "X1", "supplier": "", "ordered_on": "",
            "lines": [
                {"description": "a", "quantity": 1, "line_total": "5.00",
                 "unit_price": None, "blank_id": 999999},
                {"description": "b", "quantity": 1, "line_total": "5.00",
                 "unit_price": None, "blank_id": self.veil.pk},
            ],
        }
        reading = invoiceread._to_reading(payload, [self.veil])
        self.assertIsNone(reading.lines[0].raw_product_id)
        self.assertEqual(reading.lines[1].raw_product_id, self.veil.pk)

    def test_an_unreadable_date_is_no_date_rather_than_today(self):
        payload = {"order_number": "", "supplier": "", "ordered_on": "soon",
                   "lines": []}
        self.assertIsNone(invoiceread._to_reading(payload, []).ordered_on)

    def test_an_empty_paste_is_not_an_api_call(self):
        self.assertEqual(invoiceread.read_text("  ").error, "Nothing was pasted in.")

    def test_no_key_reads_nothing_and_says_so(self):
        with self.settings(CLAUDE_API_KEY=""):
            reading = invoiceread.read(b"x", content_type="application/pdf")
        self.assertIn("CLAUDE_API_KEY", reading.error)
        self.assertEqual(reading.lines, [])
