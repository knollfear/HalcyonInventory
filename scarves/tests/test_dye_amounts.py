"""How much dye goes in a bath.

The dye book writes one figure per dye for a five-skein bath of yarn; the
dye itself is per skein. Everything here is about that division being done
once, in `dyeamounts`, and not being done at all where there is nothing to
divide — silk has no figures in the book.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .. import dyeamounts, production
from ..forms import RecipeDyesForm
from ..models import (
    Dye,
    DyeBrand,
    ProductionRun,
    ProductionRunRow,
    RawProduct,
    RawProductCategory,
    RecipeDye,
)
from .helpers import _pdf_text, make_bathable, make_recipe


def _yarn_bath(recipe, name, bath=5):
    """A bathable product on the yarn table, where the book's figures apply."""
    product = make_bathable(recipe, name, bath=bath)
    category, _ = RawProductCategory.objects.get_or_create(
        name="Yarn", defaults={"dye_book_bath_units": 5}
    )
    if category.dye_book_bath_units != 5:
        category.dye_book_bath_units = 5
        category.save(update_fields=["dye_book_bath_units"])
    RawProduct.objects.filter(pk=product.raw_product_id).update(category=category)
    product.refresh_from_db()
    return product


def _set_amounts(recipe, *amounts):
    for recipe_dye, ounces in zip(recipe.recipe_dyes.all(), amounts):
        recipe_dye.book_ounces = Decimal(ounces)
        recipe_dye.save(update_fields=["book_ounces"])


class BathArithmeticTests(TestCase):
    """A short bath takes less dye, in proportion."""

    def setUp(self):
        self.recipe = make_recipe("Cabernet", hexes=("#800020", "#202020"))
        _set_amounts(self.recipe, "1.0", "0.5")

    def test_a_full_bath_is_the_book_figure(self):
        product = _yarn_bath(self.recipe, "Cabernet Heavenly", bath=5)
        amounts = dyeamounts.bath_amounts(self.recipe, product.raw_product, 5)
        self.assertEqual(
            [oz for _rd, oz in amounts], [Decimal("1.0"), Decimal("0.5")]
        )

    def test_a_four_skein_bath_takes_eighty_percent(self):
        """The whole point: dye is per skein, the book is per five."""
        product = _yarn_bath(self.recipe, "Cabernet Artisan", bath=4)
        amounts = dyeamounts.bath_amounts(self.recipe, product.raw_product, 4)
        self.assertEqual(
            [oz for _rd, oz in amounts], [Decimal("0.8"), Decimal("0.4")]
        )

    def test_silk_gets_no_amount_at_all(self):
        """No basis on the category means the book has no figures for this
        fibre, and a scaled yarn number printed beside a silk bath would be
        read at the sink as a measurement somebody took on silk."""
        product = make_bathable(self.recipe, "Cabernet Rectangle", bath=4)
        self.assertEqual(product.raw_product.category.name, "Silk")
        amounts = dyeamounts.bath_amounts(self.recipe, product.raw_product, 4)
        self.assertEqual([oz for _rd, oz in amounts], [None, None])
        # The dyes themselves still come back — a bath with no figures on
        # file still needs its dyes listed.
        self.assertEqual(len(amounts), 2)

    def test_no_amount_on_file_is_none_and_never_zero(self):
        recipe = make_recipe("Unpriced", hexes=("#123456",))
        product = _yarn_bath(recipe, "Unpriced Noble", bath=5)
        amounts = dyeamounts.bath_amounts(recipe, product.raw_product, 5)
        self.assertEqual([oz for _rd, oz in amounts], [None])

    def test_a_scaled_amount_rounds_to_what_the_scale_reads(self):
        """Four fifths of 0.3 is 0.24 and there is no mark on the scale for
        it. Dyeing is at the by-feel end of precision — printing 0.24 oz asks
        for a weight nobody can hit, which is a sheet asking to be ignored."""
        recipe = make_recipe("Awkward", hexes=("#654321",))
        _set_amounts(recipe, "0.3")
        product = _yarn_bath(recipe, "Awkward Artisan", bath=4)
        amounts = dyeamounts.bath_amounts(recipe, product.raw_product, 4)
        self.assertEqual([oz for _rd, oz in amounts], [Decimal("0.2")])
        self.assertEqual(dyeamounts.format_ounces(amounts[0][1]), "0.2 oz")

    def test_a_tiny_amount_never_rounds_away_to_nothing(self):
        """A fifth of a tenth is 0.02, and `0 oz` on a sheet reads as no dye
        rather than not much — which is a bath somebody runs wrong. The floor
        is the smallest weight the scale has a mark for."""
        recipe = make_recipe("Whisper", hexes=("#eeeeee",))
        _set_amounts(recipe, "0.1")
        product = _yarn_bath(recipe, "Whisper Noble", bath=1)
        amounts = dyeamounts.bath_amounts(recipe, product.raw_product, 1)
        self.assertEqual([oz for _rd, oz in amounts], [Decimal("0.1")])

    def test_the_formatter_drops_the_padding(self):
        self.assertEqual(dyeamounts.format_ounces(Decimal("0.750")), "0.75 oz")
        self.assertEqual(dyeamounts.format_ounces(Decimal("1.000")), "1 oz")
        self.assertEqual(dyeamounts.format_ounces(None), "")


class SheetPrintsTheBathsOwnAmountTests(TestCase):
    """The paper carries this pot's ounces, not the book's."""

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea", hexes=("#2f4f6f",))
        _set_amounts(self.recipe, "1.0")

    def _sheet_text(self, product, quantity):
        run = ProductionRun.objects.create()
        ProductionRunRow.objects.create(
            run=run, finished_product=product, order=1, quantity=quantity
        )
        return _pdf_text(
            production.render_sheet(run, "https://x.test/r/", "https://x.test/u/")
        )

    def test_a_four_skein_bath_prints_four_fifths(self):
        product = _yarn_bath(self.recipe, "Stormy Homespun", bath=4)
        self.assertIn("0.8 oz", self._sheet_text(product, 4))

    def test_a_silk_bath_prints_the_dye_and_no_ounces(self):
        product = make_bathable(self.recipe, "Stormy Rectangle", bath=4)
        text = self._sheet_text(product, 4)
        self.assertIn("Stormy Sea-dye-1", text)
        self.assertNotIn(" oz", text)


class AmountsSurviveTheEditorTests(TestCase):
    """The row rewrites its dyes on every Save, so the amount has to ride
    back through the form with them — otherwise saving a band clears it."""

    def setUp(self):
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)
        self.recipe = make_recipe("Mooney", hexes=("#445566",))
        _set_amounts(self.recipe, "0.3")
        self.dye = self.recipe.recipe_dyes.get().dye

    def test_the_editor_offers_the_stored_amount(self):
        response = self.client.get(
            reverse("recipe_row", args=[self.recipe.pk]) + "?edit=1"
        )
        self.assertContains(response, 'value="0.3"')

    def test_saving_keeps_an_amount_it_was_given(self):
        self.client.post(
            reverse("recipe_dyes_save", args=[self.recipe.pk]),
            {"dye1": self.dye.pk, f"{RecipeDyesForm.AMOUNT_PREFIX}1": "0.5"},
        )
        self.assertEqual(
            RecipeDye.objects.get(recipe=self.recipe).book_ounces, Decimal("0.5")
        )

    def test_an_amount_with_no_dye_is_dropped_with_its_slot(self):
        """Ounces of nothing."""
        self.client.post(
            reverse("recipe_dyes_save", args=[self.recipe.pk]),
            {"dye1": self.dye.pk, f"{RecipeDyesForm.AMOUNT_PREFIX}2": "9"},
        )
        self.assertEqual(RecipeDye.objects.filter(recipe=self.recipe).count(), 1)

    def test_a_dye_saves_with_no_amount(self):
        """The backlog is dyes with no recipe at all — requiring an amount
        would stop the entry the row exists to keep moving."""
        brand, _ = DyeBrand.objects.get_or_create(name="TestBrand")
        other = Dye.objects.create(name="Black Cherry", brand=brand, hex_color="#300")
        response = self.client.post(
            reverse("recipe_dyes_save", args=[self.recipe.pk]),
            {"dye1": self.dye.pk, "dye2": other.pk,
             f"{RecipeDyesForm.AMOUNT_PREFIX}1": "0.2"},
        )
        self.assertEqual(response.status_code, 200)
        amounts = list(
            RecipeDye.objects.filter(recipe=self.recipe)
            .order_by("order").values_list("book_ounces", flat=True)
        )
        self.assertEqual(amounts, [Decimal("0.2"), None])
