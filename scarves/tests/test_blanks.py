"""Making a blank, and changing what one is.

There was no door at all before this: `RawProduct` is not in the admin and no
page created one, so a new blank meant a shell or the Square sync. The
reasoning is in `docs/claude/stock.md`.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .helpers import make_recipe
from ..models import (
    FinishedProduct,
    RawProduct,
    RawProductCategory,
    Supplier,
    SupplierInvoice,
)


class BlankEditorTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("blank", "b@example.test", "pw")
        self.client.force_login(self.user)
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.supplier = Supplier.objects.create(name="Wool2dye4")

    def _post(self, **overrides):
        data = {
            "name": "Habotai Circle",
            "category": str(self.silk.pk),
            "is_active": "on",
            "price": "12.50",
            "number_per_dye_bath": "4",
            "made_in_a_dye_bath": "True",
            "par_level": "100",
            "finished_par_default": "8",
            "display_slots_default": "2",
        }
        data.update(overrides)
        return self.client.post(reverse("blank_new"), data)

    # --- making one -------------------------------------------------------

    def test_a_blank_can_be_made_at_all(self):
        self._post()
        product = RawProduct.objects.get(name="Habotai Circle")
        self.assertEqual(product.category, self.silk)
        self.assertEqual(product.price, Decimal("12.50"))

    def test_an_opening_count_is_the_only_stock_this_form_sets(self):
        self._post(opening_count="25")
        self.assertEqual(RawProduct.objects.get(name="Habotai Circle").number_on_hand, 25)

    def test_the_shelf_is_not_editable_once_the_blank_exists(self):
        """Counting belongs to raw-inventory, where the columns say which
        question is being answered. A second door writing the same number is
        how two of them end up disagreeing."""
        self._post(opening_count="25")
        product = RawProduct.objects.get(name="Habotai Circle")

        response = self.client.get(reverse("blank_edit", args=[product.pk]))
        self.assertNotContains(response, "id_opening_count")

        self.client.post(reverse("blank_edit", args=[product.pk]), {
            "name": product.name, "category": str(self.silk.pk), "is_active": "on",
            "price": "13.00", "number_per_dye_bath": "4", "made_in_a_dye_bath": "True",
            "par_level": "100", "finished_par_default": "8",
            "display_slots_default": "2", "opening_count": "999",
        })
        product.refresh_from_db()
        self.assertEqual(product.price, Decimal("13.00"))
        self.assertEqual(product.number_on_hand, 25)

    def test_no_cost_is_a_gap_rather_than_a_refusal(self):
        """"I don't know what this costs yet" is most of a first pass, and
        `price` is not nullable — so the gap is taken and reported."""
        self._post(price="")
        product = RawProduct.objects.get(name="Habotai Circle")
        self.assertEqual(product.price, Decimal("0"))
        response = self.client.get(reverse("blank_index"))
        self.assertContains(response, "No cost on file")
        self.assertContains(response, "Habotai Circle")

    def test_retiring_is_a_checkbox_and_keeps_the_row(self):
        self._post()
        product = RawProduct.objects.get(name="Habotai Circle")
        self.client.post(reverse("blank_edit", args=[product.pk]), {
            "name": product.name, "category": str(self.silk.pk),
            "price": "12.50", "number_per_dye_bath": "4",
            "made_in_a_dye_bath": "True", "par_level": "100",
            "finished_par_default": "8", "display_slots_default": "2",
        })
        product.refresh_from_db()
        self.assertFalse(product.is_active)
        # Still findable, so nobody makes a second copy of it.
        self.assertContains(self.client.get(reverse("blank_index")), "Habotai Circle")

    # --- the rules the model already had ----------------------------------

    def test_a_fancying_cost_belongs_on_a_fancy_blank(self):
        """`price` is what a supplier charges and a fancy veil has no
        supplier. Storing the whole cost in one field is what let three fancy
        blanks drift $8.96 to $17.30 apart."""
        response = self._post(fancying_cost="9.00")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(RawProduct.objects.filter(name="Habotai Circle").exists())
        self.assertContains(response, "Line work costs belong on a made-here blank")

    def test_a_fancy_blank_takes_one(self):
        self._post(name="Fancy Veil", made_in_a_dye_bath="False", fancying_cost="9.00")
        product = RawProduct.objects.get(name="Fancy Veil")
        self.assertFalse(product.made_in_a_dye_bath)
        self.assertEqual(product.fancying_cost, Decimal("9.00"))

    def test_a_blank_is_never_offered_as_its_own_fancy_version(self):
        """Structural rather than validated: the dropdown offers made-here
        blanks and excludes this one, so the rule cannot be reached to be
        broken. A `clean()` branch for it would never run."""
        from ..forms import RawProductForm
        self._post(name="Fancy Veil", made_in_a_dye_bath="False")
        fancy = RawProduct.objects.get(name="Fancy Veil")
        self.assertNotIn(
            "fancy_counterpart", RawProductForm(instance=fancy).fields
        )

        self._post(name="Veil")
        plain = RawProduct.objects.get(name="Veil")
        offered = RawProductForm(instance=plain).fields["fancy_counterpart"].queryset
        self.assertNotIn(plain, offered)
        self.assertIn(fancy, offered)

    # --- booking one into existence off an invoice -------------------------

    def test_an_invoice_line_prefills_the_form(self):
        """A line the catalogue has never heard of gets a door, not a dead
        end — and nothing is retyped on the way through it."""
        response = self.client.get(reverse("blank_new"), {
            "name": 'Machine Hemmed 8mm Habotai Scarves 21" x 76" Circle',
            "price": "15.37",
            "supplier": str(self.supplier.pk),
            "invoice_description": 'Machine Hemmed 8mm Habotai Scarves 21" x 76" Circle',
        })
        body = response.content.decode()
        self.assertIn("Machine Hemmed 8mm Habotai Scarves", body)
        self.assertIn("15.37", body)

    def test_creating_from_a_line_remembers_the_wording(self):
        """Creating the blank *is* the confirmation, so it counts as one —
        the same rule that keeps `invoice_description` un-typeable."""
        wording = 'Machine Hemmed 8mm Habotai Scarves 21" x 76" Circle'
        self._post(name="Infinity", invoice_description=wording)
        product = RawProduct.objects.get(name="Infinity")
        self.assertEqual(product.invoice_description, wording)

        from .. import invoiceread
        self.assertEqual(
            invoiceread.known_match(wording, [product]), product.pk
        )

    def test_the_wording_is_shown_but_never_offered_as_a_box(self):
        wording = "Angel DK - 10 x 100g SKEINS"
        self._post(name="Heavenly", invoice_description=wording)
        product = RawProduct.objects.get(name="Heavenly")
        response = self.client.get(reverse("blank_edit", args=[product.pk]))
        self.assertContains(response, wording)
        self.assertNotContains(response, "id_invoice_description")

    def test_an_invoice_review_offers_the_door_on_an_unmatched_line(self):
        from ..models import SupplierInvoiceLine
        invoice = SupplierInvoice.objects.create(order_number="W2D-1")
        SupplierInvoiceLine.objects.create(
            invoice=invoice, raw_product=None, quantity=10,
            unit_cost=Decimal("6.98"), description="Pima Cotton DK - 10 x 100g",
        )
        response = self.client.get(reverse("invoice_detail", args=[invoice.pk]))
        self.assertContains(response, "new blank from this line")
        self.assertContains(response, reverse("blank_new"))


class OriginLabelTests(TestCase):
    """One flag, and the label that made it mean two things.

    `made_in_a_dye_bath` is the fancy marker and nothing else. Every undyed
    yarn in the catalogue has it **True** — those blanks are dyed into
    colorways *and* separately sold as they arrive, which is a colorway with
    no recipe rather than anything about the blank.

    Asked as "a dye bath can produce this", the honest answer for a yarn sold
    undyed looks like *no*. Answering it that way would mark the yarn fancy,
    drop it off every production list, and route its baths to a blank that
    does not exist.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("orig", "o@example.test", "pw")
        self.client.force_login(self.user)
        self.yarn, _ = RawProductCategory.objects.get_or_create(name="Yarn")

    def _form(self, instance=None):
        from ..forms import RawProductForm
        return RawProductForm(instance=instance) if instance else RawProductForm()

    def test_the_question_is_where_it_comes_from_not_whether_it_is_dyed(self):
        body = self.client.get(reverse("blank_new")).content.decode()
        self.assertIn("Bought from a supplier", body)
        self.assertIn("Made here from another blank", body)
        self.assertNotIn("dye bath can produce this", body)

    def test_bought_is_the_default_because_nearly_everything_is(self):
        product = RawProduct.objects.create(
            name="Superwash Merino Zebra DK", category=self.yarn, price="8.99"
        )
        self.assertTrue(product.made_in_a_dye_bath)
        form = self._form()
        self.assertEqual(form.fields["made_in_a_dye_bath"].initial, "True")

    def test_an_undyed_yarn_stays_bought(self):
        """The case that prompted this. A yarn sold exactly as it arrives is
        still bought from a supplier, and still dyed into colorways."""
        response = self.client.post(reverse("blank_new"), {
            "name": "Superwash Merino Zebra DK", "category": str(self.yarn.pk),
            "is_active": "on", "price": "8.99", "number_per_dye_bath": "5",
            "made_in_a_dye_bath": "True", "par_level": "100",
            "finished_par_default": "8", "display_slots_default": "2",
        })
        self.assertEqual(response.status_code, 302)
        product = RawProduct.objects.get(name="Superwash Merino Zebra DK")
        self.assertTrue(product.made_in_a_dye_bath)
        # And the picker does not label it as something made here.
        body = self.client.get(reverse("blank_index")).content.decode()
        self.assertNotIn("made here, not bought", body)

    def test_the_picker_says_made_here_rather_than_no_bath_makes_this(self):
        RawProduct.objects.create(
            name="Fancy Veil", category=self.yarn, price="41.99",
            made_in_a_dye_bath=False,
        )
        body = self.client.get(reverse("blank_index")).content.decode()
        self.assertIn("made here, not bought", body)
        self.assertNotIn("no bath makes this", body)

    # --- the chicken and the egg ------------------------------------------

    def test_the_fancy_version_is_made_rather_than_chosen(self):
        """Asking somebody to create the fancy blank first and then come back
        to point at it was asking them to do the bookkeeping in the right
        order. Ticking the box is the whole decision."""
        body = self.client.get(reverse("blank_new")).content.decode()
        self.assertIn("It also comes in a fancy version", body)
        # No dropdown to be confused by: there is nothing to pick from.
        self.assertNotIn("id_fancy_counterpart", body)

        self.client.post(reverse("blank_new"), {
            "name": "Half Circle Veil", "category": str(self.yarn.pk),
            "is_active": "on", "price": "24.99", "number_per_dye_bath": "4",
            "made_in_a_dye_bath": "True", "has_fancy_version": "on",
            "fancying_cost": "", "par_level": "100",
            "finished_par_default": "8", "display_slots_default": "2",
        })

        plain = RawProduct.objects.get(name="Half Circle Veil")
        fancy = plain.fancy_counterpart
        self.assertIsNotNone(fancy)
        self.assertEqual(fancy.name, "Fancy Half Circle Veil")
        self.assertFalse(fancy.made_in_a_dye_bath)
        self.assertEqual(fancy.category, plain.category)
        # It asks for nothing and proposes nothing, which is why making it
        # eagerly is safe.
        self.assertEqual(fancy.par_level, 0)
        self.assertEqual(fancy.number_on_hand, 0)
        self.assertEqual(fancy.raw_shortage, 0)
        # No supplier price: a fancy blank has no supplier.
        self.assertEqual(fancy.price, Decimal("0"))

    def test_making_the_fancy_version_is_idempotent(self):
        from ..blanks import ensure_fancy_counterpart
        plain = RawProduct.objects.create(
            name="Veil", category=self.yarn, price="24.99"
        )
        first = ensure_fancy_counterpart(plain)
        second = ensure_fancy_counterpart(plain)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(RawProduct.objects.filter(name="Fancy Veil").count(), 1)

    def test_unticking_never_deletes_the_fancy_blank(self):
        """It is a row with history. Retiring is how something goes away."""
        from ..blanks import ensure_fancy_counterpart
        plain = RawProduct.objects.create(
            name="Veil", category=self.yarn, price="24.99"
        )
        fancy = ensure_fancy_counterpart(plain)
        self.client.post(reverse("blank_edit", args=[plain.pk]), {
            "name": "Veil", "category": str(self.yarn.pk), "is_active": "on",
            "price": "24.99", "number_per_dye_bath": "4",
            "made_in_a_dye_bath": "True", "par_level": "100",
            "finished_par_default": "8", "display_slots_default": "2",
        })
        self.assertTrue(RawProduct.objects.filter(pk=fancy.pk).exists())

    def test_the_dropdown_only_appears_for_an_unlinked_fancy_blank(self):
        """Its remaining job is repairing a link, so it is offered only when
        it can do something — an empty select is what read as broken."""
        plain = RawProduct.objects.create(
            name="Veil", category=self.yarn, price="24.99"
        )
        self.assertNotIn("fancy_counterpart", self._form(instance=plain).fields)

        RawProduct.objects.create(
            name="Fancy Shawl", category=self.yarn, price="0",
            made_in_a_dye_bath=False,
        )
        self.assertIn("fancy_counterpart", self._form(instance=plain).fields)

    def test_a_fancy_blank_is_not_asked_for_a_fancy_version_of_its_own(self):
        """A veil becomes a fancy veil; a fancy veil becomes nothing."""
        fancy = RawProduct.objects.create(
            name="Fancy Veil", category=self.yarn, price="41.99",
            made_in_a_dye_bath=False,
        )
        self.assertNotIn("fancy_counterpart", self._form(instance=fancy).fields)
        body = self.client.get(reverse("blank_edit", args=[fancy.pk])).content.decode()
        self.assertNotIn("id_fancy_counterpart", body)


class SoldUndyedTests(TestCase):
    """How an undyed yarn gets added, which had no answer but a command.

    One physical pile, two rows: the blank is the pile, and a colorway with
    no recipe is the thing Square sells.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("undyed", "u@example.test", "pw")
        self.client.force_login(self.user)
        self.yarn, _ = RawProductCategory.objects.get_or_create(name="Yarn")

    def _post(self, **overrides):
        data = {
            "name": "Superwash Merino Zebra DK", "category": str(self.yarn.pk),
            "is_active": "on", "price": "6.98", "suggested_price": "18.00",
            "number_per_dye_bath": "5", "made_in_a_dye_bath": "True",
            "sold_undyed": "on", "par_level": "100",
            "finished_par_default": "8", "display_slots_default": "2",
        }
        data.update(overrides)
        return self.client.post(reverse("blank_new"), data)

    def test_ticking_it_makes_the_sellable_row(self):
        self._post()
        blank = RawProduct.objects.get(name="Superwash Merino Zebra DK")
        undyed = FinishedProduct.objects.get(raw_product=blank, recipe__isnull=True)
        self.assertEqual(undyed.name, blank.name)
        self.assertEqual(undyed.price, Decimal("18.00"))
        # The par that matters lives on the blank; one here reads nowhere.
        self.assertEqual(undyed.par, 0)

    def test_the_blank_stays_bought_and_dyeable(self):
        """Sold undyed is not a statement about the blank — it is still
        bought from a supplier and still dyed into colorways."""
        self._post()
        blank = RawProduct.objects.get(name="Superwash Merino Zebra DK")
        self.assertTrue(blank.made_in_a_dye_bath)

    def test_no_suggested_price_lands_at_a_conspicuous_dollar(self):
        """Zero would sync happily and ring up free with a queue behind it."""
        self._post(suggested_price="")
        blank = RawProduct.objects.get(name="Superwash Merino Zebra DK")
        undyed = FinishedProduct.objects.get(raw_product=blank, recipe__isnull=True)
        self.assertEqual(undyed.price, Decimal("1.00"))

    def test_a_deliberate_zero_is_honoured(self):
        """Null and zero are different: a giveaway is a real product."""
        self._post(suggested_price="0")
        blank = RawProduct.objects.get(name="Superwash Merino Zebra DK")
        undyed = FinishedProduct.objects.get(raw_product=blank, recipe__isnull=True)
        self.assertEqual(undyed.price, Decimal("0"))

    def test_it_is_idempotent_and_survives_colorways(self):
        """A yarn both sold undyed and dyed into colorways is the normal
        case, and its colorways must not stop the undyed row being found."""
        from ..blanks import ensure_undyed_product
        self._post()
        blank = RawProduct.objects.get(name="Superwash Merino Zebra DK")
        first = FinishedProduct.objects.get(raw_product=blank, recipe__isnull=True)
        self.assertEqual(ensure_undyed_product(blank).pk, first.pk)
        self.assertEqual(
            FinishedProduct.objects.filter(raw_product=blank, recipe__isnull=True).count(),
            1,
        )

    def test_the_box_remembers_it_is_already_sold_undyed(self):
        self._post()
        blank = RawProduct.objects.get(name="Superwash Merino Zebra DK")
        from ..forms import RawProductForm
        self.assertTrue(RawProductForm(instance=blank).initial["sold_undyed"])

    def test_the_count_is_mirrored_onto_the_sellable_row(self):
        """One pile, and only one row may count it."""
        self._post(opening_count="120")
        blank = RawProduct.objects.get(name="Superwash Merino Zebra DK")
        undyed = FinishedProduct.objects.get(raw_product=blank, recipe__isnull=True)
        self.assertEqual(blank.number_on_hand, 120)
        self.assertEqual(undyed.number_on_hand, 120)


class ColorwaysFromTheBlankTests(TestCase):
    """A new blank is a finished product per colourway, and there are
    hundreds. Forty typed by hand is a job that gets half done."""

    def setUp(self):
        self.user = User.objects.create_superuser("cw", "c@example.test", "pw")
        self.client.force_login(self.user)
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.sea = make_recipe("Stormy Sea")
        self.rose = make_recipe("Rose")
        self.gone = make_recipe("Discontinued", active=False)

    def _post(self, **overrides):
        data = {
            "name": "Habotai Circle", "category": str(self.silk.pk),
            "is_active": "on", "price": "12.50", "suggested_price": "32.00",
            "number_per_dye_bath": "4", "made_in_a_dye_bath": "True",
            "par_level": "100", "finished_par_default": "8",
            "display_slots_default": "2",
        }
        data.update(overrides)
        return self.client.post(reverse("blank_new"), data)

    def test_picking_colourways_makes_one_product_each(self):
        self._post(colorways=[str(self.sea.pk), str(self.rose.pk)])
        blank = RawProduct.objects.get(name="Habotai Circle")
        made = FinishedProduct.objects.filter(raw_product=blank, recipe__isnull=False)
        self.assertEqual(made.count(), 2)
        self.assertEqual(
            sorted(made.values_list("name", flat=True)),
            ["Habotai Circle - Rose", "Habotai Circle - Stormy Sea"],
        )

    def test_the_blank_defaults_are_what_a_new_colourway_inherits(self):
        self._post(colorways=[str(self.sea.pk)],
                   finished_par_default="12", display_slots_default="3")
        product = FinishedProduct.objects.get(name="Habotai Circle - Stormy Sea")
        self.assertEqual(product.par, 12)
        self.assertEqual(product.display_slots, 3)
        self.assertEqual(product.price, Decimal("32.00"))

    def test_every_new_colourway_gets_a_sku(self):
        """`FinishedProduct.save()` fills it, so a label can be printed and
        Square can scan it without anybody remembering a command."""
        self._post(colorways=[str(self.sea.pk)])
        product = FinishedProduct.objects.get(name="Habotai Circle - Stormy Sea")
        self.assertTrue(product.sku)

    def test_no_suggested_price_lands_at_a_conspicuous_dollar(self):
        self._post(colorways=[str(self.sea.pk)], suggested_price="")
        product = FinishedProduct.objects.get(name="Habotai Circle - Stormy Sea")
        self.assertEqual(product.price, Decimal("1.00"))

    def test_oven_dyed_is_never_inherited_from_another_blank(self):
        """A typed flag about this blank in this colour. The same colour is
        oven in one yarn and not in another."""
        other = RawProduct.objects.create(
            name="Artisan", category=self.silk, price="8.00"
        )
        FinishedProduct.objects.create(
            name="Artisan - Stormy Sea", raw_product=other, recipe=self.sea,
            price=Decimal("32.00"), oven_dyed=True,
        )
        self._post(colorways=[str(self.sea.pk)])
        product = FinishedProduct.objects.get(name="Habotai Circle - Stormy Sea")
        self.assertFalse(product.oven_dyed)

    def test_each_colourway_is_its_own_checkbox(self):
        """A multi-select loses thirty picks to one stray click, with nothing
        said. Every click here is independent and needs no modifier key."""
        body = self.client.get(reverse("blank_new")).content.decode()
        self.assertIn('type="checkbox" name="colorways"', body)
        self.assertNotIn('<select name="colorways"', body)
        self.assertNotIn("⌘-click", body)

    def test_the_dyes_are_shown_as_a_chip_each(self):
        """A colourway is a name like `Babs`, which says nothing about what
        it looks like. The dyes are what somebody recognises."""
        body = self.client.get(reverse("blank_new")).content.decode()
        dye = self.sea.recipe_dyes.first().dye
        self.assertIn(f'background: {dye.hex_color}', body)
        self.assertIn(f'title="{dye.name}"', body)

    def test_dyes_are_never_blended_into_one_swatch(self):
        """A scarf shows each dye distinctly and flows between them, so an
        average would be a colour that is not on the product. Three dyes,
        three chips."""
        make_recipe("Three Colour", hexes=("#ff0000", "#00ff00", "#0000ff"))
        body = self.client.get(reverse("blank_new")).content.decode()
        row = body.split('<span class="cwname">Three Colour</span>')[1].split("</label>")[0]
        self.assertEqual(row.count("dyechip"), 3)
        for hexed in ("#ff0000", "#00ff00", "#0000ff"):
            self.assertIn(f"background: {hexed}", row)

    def test_a_dye_with_no_hex_is_hatched_rather_than_guessed(self):
        """A placeholder swatch is something somebody reads off the screen as
        a fact."""
        from ..models import Dye, DyeBrand, RecipeDye
        brand, _ = DyeBrand.objects.get_or_create(name="TestBrand")
        blankdye = Dye.objects.create(name="Unknown Blue", brand=brand, hex_color="")
        plain = make_recipe("No Hex", hexes=())
        RecipeDye.objects.create(recipe=plain, dye=blankdye, order=1)
        body = self.client.get(reverse("blank_new")).content.decode()
        self.assertIn("dyechip unknown", body)

    def test_a_recipe_with_no_dyes_says_so(self):
        """Most of this catalogue has none on file. Silence would read as a
        colourway with no colour rather than a gap in the dye book."""
        make_recipe("Nothing Entered", hexes=())
        body = self.client.get(reverse("blank_new")).content.decode()
        self.assertIn("no dyes on file", body)

    def test_the_chips_cost_a_fixed_three_queries_not_one_per_row(self):
        """Recipes, their through rows, their dyes — two prefetch levels, and
        the count does not move with the number of colourways."""
        from ..forms import RawProductForm

        for n in range(20):
            make_recipe(f"Colour {n}")
        form = RawProductForm()          # built outside: its own lookups
        with self.assertNumQueries(3):   # are not what this measures
            str(form["colorways"])

        for n in range(20, 60):
            make_recipe(f"Colour {n}")
        form = RawProductForm()
        with self.assertNumQueries(3):
            str(form["colorways"])

    def test_a_retired_colourway_is_not_offered(self):
        """A colour somebody decided to stop making, offered again, is the
        dye-room-sent-after-a-retired-recipe failure with a new door."""
        from ..forms import RawProductForm
        offered = RawProductForm().fields["colorways"].queryset
        self.assertIn(self.sea, offered)
        self.assertNotIn(self.gone, offered)

    def test_adding_more_later_is_additive_and_skips_what_exists(self):
        self._post(colorways=[str(self.sea.pk)])
        blank = RawProduct.objects.get(name="Habotai Circle")

        response = self.client.post(reverse("blank_edit", args=[blank.pk]), {
            "name": blank.name, "category": str(self.silk.pk), "is_active": "on",
            "price": "12.50", "suggested_price": "32.00",
            "number_per_dye_bath": "4", "made_in_a_dye_bath": "True",
            "par_level": "100", "finished_par_default": "8",
            "display_slots_default": "2",
            "colorways": [str(self.sea.pk), str(self.rose.pk)],
        }, follow=True)

        self.assertEqual(
            FinishedProduct.objects.filter(raw_product=blank, recipe__isnull=False).count(), 2
        )
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("1 colourway made" in m and "1 already existed" in m
                            for m in messages))

    def test_picking_none_makes_none(self):
        self._post()
        blank = RawProduct.objects.get(name="Habotai Circle")
        self.assertEqual(blank.finished_products.count(), 0)

    def test_a_fancy_blank_can_have_colourways_too(self):
        """Fancy veils are a real catalogue, and they are dyed — they just
        cannot be produced by a bath."""
        self._post(name="Fancy Veil", made_in_a_dye_bath="False",
                   colorways=[str(self.sea.pk)])
        blank = RawProduct.objects.get(name="Fancy Veil")
        self.assertEqual(blank.finished_products.count(), 1)

    def test_the_page_says_how_many_already_exist(self):
        self._post(colorways=[str(self.sea.pk), str(self.rose.pk)])
        blank = RawProduct.objects.get(name="Habotai Circle")
        body = self.client.get(reverse("blank_edit", args=[blank.pk])).content.decode()
        self.assertIn("2 already exist", body)


class BlankPickerTests(TestCase):
    """The picker exists so the editor is reachable by clicking, per the rule
    in CLAUDE.md — and so a retired blank is findable rather than recreated."""

    def setUp(self):
        self.user = User.objects.create_superuser("pick", "p@example.test", "pw")
        self.client.force_login(self.user)
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.yarn, _ = RawProductCategory.objects.get_or_create(name="Yarn")

    def test_blanks_are_grouped_by_the_table_they_sit_on(self):
        RawProduct.objects.create(name="Infinity", category=self.silk, price="15.37")
        RawProduct.objects.create(name="Heavenly", category=self.yarn, price="12.88")
        response = self.client.get(reverse("blank_index"))
        names = [name for name, _ in response.context["groups"]]
        self.assertEqual(names, ["Silk", "Yarn"])

    def test_a_retired_blank_is_listed_apart_rather_than_hidden(self):
        RawProduct.objects.create(
            name="Old Shawl", category=self.silk, price="9.00", is_active=False
        )
        response = self.client.get(reverse("blank_index"))
        self.assertEqual(
            [p.name for p in response.context["retired"]], ["Old Shawl"]
        )
        self.assertEqual(response.context["groups"], [])
