"""Photographing stock: the batch uploader and the display walk.

The reasoning behind these is in `docs/claude/labels.md`.
"""
import re
import tempfile
from datetime import date, datetime, time, timedelta
from io import StringIO
from unittest import mock
from django.contrib.auth.models import User
from django.core.management import call_command
from django.utils import timezone
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
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
from ..views import HOURS_PIN_ATTEMPT_LIMIT, IMAGE_MAX_EDGE
from .helpers import (
    hang,
    image_size,
    make_jpeg,
    make_product,
    make_recipe,
)


class ShrinkImageTests(TestCase):
    """Downscaling on upload — what keeps a round of the game from costing 40MB."""

    def test_long_edge_is_capped_and_aspect_is_kept(self):
        from ..views import IMAGE_MAX_EDGE, _shrink_image

        body, content_type = _shrink_image(make_jpeg((4032, 3024)))
        self.assertEqual(image_size(body), (IMAGE_MAX_EDGE, 900))
        self.assertEqual(content_type, "image/jpeg")

    def test_portrait_is_capped_on_its_own_long_edge(self):
        """The cap is on the longer side, not on width — a portrait photo must
        come back 900x1200, never squashed toward a square."""
        from ..views import _shrink_image

        body, _ = _shrink_image(make_jpeg((3024, 4032)))
        self.assertEqual(image_size(body), (900, 1200))

    def test_result_is_dramatically_smaller(self):
        from ..views import _shrink_image

        original = make_jpeg((4032, 3024))
        body, _ = _shrink_image(original)
        self.assertLess(len(body), len(original) / 4)

    def test_an_already_small_image_is_left_alone(self):
        """Returning None rather than re-encoding: a second pass over an
        in-bounds photo must not cost it any quality."""
        from ..views import _shrink_image

        self.assertIsNone(_shrink_image(make_jpeg((1200, 900))))
        self.assertIsNone(_shrink_image(make_jpeg((800, 600))))

    def test_exif_rotation_is_baked_into_the_pixels(self):
        """Phones store rotation in EXIF instead of rotating the pixels, and
        re-encoding drops the tag. Without transposing first, every portrait
        photo would come out sideways in the games and the PDF — and it would
        look fine right up until it was resized.
        """
        from ..views import _shrink_image

        # Orientation 6 = rotate 90°: a 4000x3000 file that displays as 3000x4000.
        body, _ = _shrink_image(make_jpeg((4000, 3000), exif_orientation=6))
        self.assertEqual(image_size(body), (900, 1200))

    def test_a_small_image_needing_rotation_is_still_rewritten(self):
        from ..views import _shrink_image

        result = _shrink_image(make_jpeg((800, 600), exif_orientation=6))
        self.assertIsNotNone(result)
        self.assertEqual(image_size(result[0]), (600, 800))

    def test_heic_is_transcoded_to_jpeg(self):
        """iPhones shoot HEIC. Chrome and Firefox won't render it, and without
        pillow-heif PIL can't even open it — so the barcode decode and the
        downscale both fail and a 3MB unviewable file lands in the bucket under
        a .jpg name."""
        from io import BytesIO
        from PIL import Image
        from ..views import _shrink_image

        buf = BytesIO()
        Image.new("RGB", (4032, 3024), (200, 80, 40)).save(buf, format="HEIF")

        body, content_type = _shrink_image(buf.getvalue())
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(Image.open(BytesIO(body)).format, "JPEG")
        self.assertEqual(image_size(body), (1200, 900))

    def test_a_small_heic_is_still_transcoded(self):
        """Size isn't the only reason to rewrite. Left alone, a small HEIC sits
        in the bucket under a .jpg key that no browser can open."""
        from io import BytesIO
        from PIL import Image
        from ..views import _shrink_image

        buf = BytesIO()
        Image.new("RGB", (800, 600), (30, 60, 120)).save(buf, format="HEIF")

        result = _shrink_image(buf.getvalue())
        self.assertIsNotNone(result)
        self.assertEqual(result[1], "image/jpeg")
        self.assertEqual(image_size(result[0]), (800, 600))

    def test_heic_content_types_are_named_jpg(self):
        """The key's extension has to describe what ends up in the bucket after
        the transcode, not what the phone sent."""
        from ..views import _CONTENT_TYPE_EXT

        self.assertEqual(_CONTENT_TYPE_EXT["image/heic"], ".jpg")
        self.assertEqual(_CONTENT_TYPE_EXT["image/heif"], ".jpg")

    def test_png_and_webp_keep_their_format(self):
        """The key's extension and the stored Content-Type were set at presign
        time; changing format here would leave both lying."""
        from io import BytesIO
        from PIL import Image
        from ..views import _shrink_image

        for fmt, content_type in (("PNG", "image/png"), ("WEBP", "image/webp")):
            buf = BytesIO()
            Image.new("RGB", (2000, 1500), (20, 120, 90)).save(buf, fmt)
            body, got = _shrink_image(buf.getvalue())
            with self.subTest(fmt=fmt):
                self.assertEqual(got, content_type)
                self.assertEqual(Image.open(BytesIO(body)).format, fmt)
                self.assertEqual(image_size(body), (1200, 900))
@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ProcessUploadResizeTests(TestCase):
    """The resize where it actually runs: the upload pipeline."""

    def setUp(self):
        self.user = User.objects.create_superuser("up", "u@example.test", "pw")
        self.client.force_login(self.user)

    def _upload(self, data=None):
        key = default_storage.save(
            "finished_products/test.jpg", ContentFile(data or make_jpeg())
        )
        return ProductImageUpload.objects.create(key=key)

    def test_upload_is_downscaled_in_place(self):
        from ..views import IMAGE_MAX_EDGE

        upload = self._upload()
        before = default_storage.size(upload.key)

        response = self.client.post(reverse("process_upload", args=[upload.id]))
        self.assertEqual(response.status_code, 200)

        upload.refresh_from_db()
        with default_storage.open(upload.key, "rb") as fh:
            stored = fh.read()
        self.assertEqual(max(image_size(stored)), IMAGE_MAX_EDGE)
        self.assertLess(len(stored), before / 4)

    def test_the_photo_served_to_the_games_is_the_small_one(self):
        """The whole point: FinishedProductImage must end up pointing at the
        downscaled object, not at a leftover original."""
        from ..views import IMAGE_MAX_EDGE

        product = make_product(make_recipe("Barcoded"), "Barcoded Scarf", with_image=False)
        product.sku = "SKU-RESIZE-1"
        product.save()

        upload = self._upload()
        # Stand in for a successful barcode decode, so the matched path runs.
        with mock.patch("pyzbar.pyzbar.decode") as decode:
            decode.return_value = [mock.Mock(data=b"SKU-RESIZE-1")]
            self.client.post(reverse("process_upload", args=[upload.id]))

        upload.refresh_from_db()
        self.assertEqual(upload.status, ProductImageUpload.STATUS_MATCHED)
        fpi = product.images.get()
        self.assertEqual(fpi.image.name, upload.key)
        with fpi.image.open("rb") as fh:
            self.assertEqual(max(image_size(fh.read())), IMAGE_MAX_EDGE)

    def test_barcode_is_decoded_before_the_downscale(self):
        """A Code128 label is a small part of the frame; decoding a 1200px copy
        is exactly what would stop it resolving. The decoder must see the
        original pixels."""
        from ..views import IMAGE_MAX_EDGE

        upload = self._upload(make_jpeg((4032, 3024)))
        seen = {}
        with mock.patch("pyzbar.pyzbar.decode") as decode:
            decode.side_effect = lambda img: seen.setdefault("size", img.size) and []
            self.client.post(reverse("process_upload", args=[upload.id]))
        self.assertEqual(seen["size"], (4032, 3024))
        self.assertGreater(max(seen["size"]), IMAGE_MAX_EDGE)

    def test_a_photo_that_will_not_resize_is_kept_not_lost(self):
        upload = self._upload(b"this is not an image at all")
        response = self.client.post(reverse("process_upload", args=[upload.id]))
        self.assertEqual(response.status_code, 200)

        upload.refresh_from_db()
        self.assertTrue(default_storage.exists(upload.key))
        self.assertNotEqual(upload.error, "")
@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class PhotoSessionPrefillTests(TestCase):
    """Saying what is in front of the camera, and what that buys.

    About half the barcodes in a session don't read — a phone, a small
    Code128 on a hang tag, whatever the light is doing — and each miss used
    to cost a product name typed out in full on a phone next to a pile of
    scarves. A session is a pile of *one blank*, so the blank half of
    `BLANK-DYEBATH` is known before the first shot and can be typed once for
    forty photos.

    It fills the box in and decides nothing: the barcode still wins whenever
    it reads. Same bargain `colorbands` and the crew cookie make.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("shoot", "s@example.test", "pw")
        self.client.force_login(self.user)
        self.product = make_product(
            make_recipe("Aegean Sea"), "Half Circle Veil", with_image=False
        )
        self.blank = self.product.raw_product

    def _upload(self):
        key = default_storage.save(
            "finished_products/test.jpg", ContentFile(make_jpeg())
        )
        return ProductImageUpload.objects.create(key=key)

    def _card(self, prefix=None):
        upload = self._upload()
        post = {"prefix": prefix} if prefix is not None else {}
        return self.client.post(
            reverse("process_upload", args=[upload.id]), post
        ).content.decode()

    def test_the_page_offers_the_blanks_by_their_sku_half(self):
        """The menu's values have to be the same six characters the SKU was
        built from, or the prefill narrows to nothing."""
        html = self.client.get(reverse("image_upload")).content.decode()
        self.assertIn(self.blank.name, html)
        self.assertIn(f'value="{skus.slug(self.blank.name)}"', html)

    def test_a_blank_with_nothing_active_under_it_is_not_offered(self):
        """A line in the menu that narrows to nothing is worse than no line:
        it reads as a working answer."""
        retired = make_product(
            make_recipe("Gone"), "Retired Style", with_image=False, active=False
        )
        html = self.client.get(reverse("image_upload")).content.decode()
        self.assertNotIn(retired.raw_product.name, html)

    def test_a_failed_decode_comes_back_with_the_blank_already_typed(self):
        """The whole point: nine characters become two or three."""
        html = self._card(prefix=skus.slug(self.blank.name))
        self.assertIn(f'value="{skus.slug(self.blank.name)}-"', html)
        # And the search runs on arrival, so the first thing on screen is that
        # blank's colorways rather than an empty box.
        self.assertIn("load,", html)

    def test_the_prefilled_search_finds_that_blank_and_not_the_others(self):
        """`HALFCI-` has to be a real narrowing, not decoration."""
        other = make_product(make_recipe("Ember"), "Sash Belt", with_image=False)
        response = self.client.get(
            reverse("product_search"),
            {"q": f"{skus.slug(self.blank.name)}-", "upload_id": self._upload().id},
        )
        html = response.content.decode()
        self.assertIn(self.product.sku, html)
        self.assertNotIn(other.sku, html)

    def test_saying_nothing_leaves_the_box_exactly_as_it_was(self):
        """The menu is optional, and the old flow is what "no answer" means."""
        html = self._card()
        self.assertIn('value=""', html)
        self.assertNotIn("load,", html)

    def test_a_barcode_that_read_still_wins(self):
        """The prefill fills a form in; it never files a photo. A read barcode
        is evidence and the menu is a statement of intent, so a card that
        matched must not be steered by what somebody said they were shooting.
        """
        self.product.sku = "SKU-PREFILL-1"
        self.product.save()
        upload = self._upload()
        with mock.patch("pyzbar.pyzbar.decode") as decode:
            decode.return_value = [mock.Mock(data=b"SKU-PREFILL-1")]
            self.client.post(
                reverse("process_upload", args=[upload.id]), {"prefix": "SASHBE"}
            )
        upload.refresh_from_db()
        self.assertEqual(upload.status, ProductImageUpload.STATUS_MATCHED)
        self.assertEqual(upload.finished_product_id, self.product.pk)

    def test_the_prefix_is_slugged_on_the_way_in(self):
        """It comes off the page and goes into an input's value. `slug` is the
        same function that built the SKU half it is meant to match, so running
        it through is both the safe answer and the correct one."""
        html = self._card(prefix='"><script>alert(1)</script>')
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("SCRIPT-", html)
@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class PhotoWalkTests(TestCase):
    """Photographing a board peg by peg.

    The batch page asks "here are forty photos, work out what they are" and
    leans on a barcode that reads about half the time. A walk inverts that: a
    peg is an identity, so a photo taken at a known stop needs no barcode, no
    typing and no search. The two jobs it has to do are file the photo against
    the peg's colorway, and — on a board being set up — find out what that
    colorway is with as little typing as possible.
    """

    def setUp(self):
        self.user = User.objects.create_superuser("walk", "w@example.test", "pw")
        self.client.force_login(self.user)
        self.product = make_product(
            make_recipe("Crocodile"), "Artisan", with_image=False
        )
        self.blank = self.product.raw_product
        self.fixture = DisplayFixture.objects.create(
            name="Artisan Board", rows=2, columns=3, capacity_per_position=2,
            raw_product=self.blank,
        )

    def _upload(self):
        key = default_storage.save(
            "finished_products/test.jpg", ContentFile(make_jpeg())
        )
        return ProductImageUpload.objects.create(key=key)

    def _stop_vals(self, row, column):
        return {"fixture": self.fixture.pk, "row": row, "column": column}

    # --- where the walk goes ----------------------------------------------

    def test_reserved_spaces_are_not_stops(self):
        """The price tag in the middle of the top row is not a colorway
        nobody got round to photographing, and stopping at it would ask a
        question with no answer."""
        DisplayPosition.objects.filter(
            fixture=self.fixture, row=1, column=2
        ).update(reserved_label="Price tag")
        cells = [(s["row"], s["column"]) for s in photowalk.stops(self.fixture)]
        self.assertNotIn((1, 2), cells)
        self.assertIn((1, 1), cells)

    def test_an_empty_hook_is_a_stop_whether_or_not_it_has_a_row(self):
        """On the wall an unassigned peg and one nobody has created yet are
        the same empty hook — the reading `DisplayFixture.grid` already
        takes — and a photo taken at either is how the map finds out."""
        # `ensure_positions` gives a new board a row per cell, so make a real
        # gap the way a grown board does: the fixture got wider afterwards.
        DisplayPosition.objects.filter(
            fixture=self.fixture, row=2, column=3
        ).delete()
        cells = [(s["row"], s["column"]) for s in photowalk.stops(self.fixture)]
        self.assertIn((1, 1), cells)   # exists, unassigned
        self.assertIn((2, 3), cells)   # no row at all
        self.assertEqual(len(cells), 6)

    def test_an_address_that_is_not_a_stop_resumes_at_the_next_one(self):
        """A bookmark taken before the board was rearranged has to land
        somewhere. Refusing it would end the walk at exactly the moment
        somebody was trying to resume it."""
        DisplayPosition.objects.filter(
            fixture=self.fixture, row=1, column=2
        ).update(reserved_label="Price tag")
        stop, _ = photowalk.stop_at(self.fixture, 1, 2)
        self.assertEqual((stop["row"], stop["column"]), (1, 3))

    def test_walking_past_the_last_peg_is_finished_not_an_error(self):
        stop, _ = photowalk.stop_at(self.fixture, 9, 9)
        self.assertIsNone(stop)
        response = self.client.get(
            reverse("photo_walk", args=[self.fixture.pk]), {"row": 9, "column": 9}
        )
        self.assertContains(response, "That's the whole board")

    def test_the_page_says_what_to_photograph_and_stays_addressable(self):
        hang(self.fixture, self.product, 1, 2)
        response = self.client.get(
            reverse("photo_walk", args=[self.fixture.pk]), {"row": 1, "column": 2}
        )
        self.assertContains(response, "Artisan — Crocodile")
        # And the next peg is a plain link, so skipping needs no scripting.
        self.assertContains(response, "row=1&amp;column=3")

    def test_a_peg_whose_colorway_already_has_a_photo_says_so(self):
        """Said, never acted on. A colorway that already has a photo is often
        exactly the one worth retaking."""
        hang(self.fixture, self.product, 1, 1)
        FinishedProductImage.objects.create(
            finished_product=self.product,
            image_url="https://example.test/croc.jpg",
        )
        response = self.client.get(reverse("photo_walk", args=[self.fixture.pk]))
        self.assertContains(response, "Already has 1 photo")

    # --- filing a photo at a peg ------------------------------------------

    def test_a_photo_taken_at_a_peg_files_itself(self):
        """The whole point of the map-first route: no barcode, no typing."""
        hang(self.fixture, self.product, 1, 1)
        upload = self._upload()

        self.client.post(
            reverse("process_upload", args=[upload.id]), self._stop_vals(1, 1)
        )

        upload.refresh_from_db()
        self.assertEqual(upload.status, ProductImageUpload.STATUS_MATCHED)
        self.assertEqual(upload.finished_product_id, self.product.pk)

    def test_the_peg_beats_a_barcode_that_disagrees_and_says_so(self):
        """A symbol that resolves in shot may belong to the colorway hanging
        two inches to the left. The stop is a per-photo claim made at the peg,
        so it wins — and the disagreement is reported, because silence in
        either direction files a photo on the wrong colorway with nothing to
        say so."""
        hang(self.fixture, self.product, 1, 1)
        neighbour = make_product(make_recipe("Neighbour"), "Artisan Two", with_image=False)
        neighbour.sku = "SKU-NEIGHBOUR"
        neighbour.save()
        upload = self._upload()

        with mock.patch("pyzbar.pyzbar.decode") as decode:
            decode.return_value = [mock.Mock(data=b"SKU-NEIGHBOUR")]
            response = self.client.post(
                reverse("process_upload", args=[upload.id]), self._stop_vals(1, 1)
            )

        upload.refresh_from_db()
        self.assertEqual(upload.finished_product_id, self.product.pk)
        self.assertContains(response, "A barcode in the photo read")
        self.assertContains(response, neighbour.name)

    def test_off_a_walk_the_barcode_still_decides(self):
        """The batch page is not standing anywhere, and its blank picker is a
        coarse statement covering forty photos. Nothing about the walk changes
        what happens there."""
        self.product.sku = "SKU-BATCH-1"
        self.product.save()
        upload = self._upload()

        with mock.patch("pyzbar.pyzbar.decode") as decode:
            decode.return_value = [mock.Mock(data=b"SKU-BATCH-1")]
            self.client.post(reverse("process_upload", args=[upload.id]))

        upload.refresh_from_db()
        self.assertEqual(upload.finished_product_id, self.product.pk)

    # --- filling the map from the photos ----------------------------------

    def test_picking_a_colorway_at_an_empty_peg_fills_the_map(self):
        """The photos-first route: walk a bare board once and come away with
        both the pictures and the map."""
        upload = self._upload()
        self.client.post(
            reverse("process_upload", args=[upload.id]), self._stop_vals(2, 1)
        )

        response = self.client.post(
            reverse("assign_upload", args=[upload.id])
            + f"?fixture={self.fixture.pk}&row=2&column=1",
            {"product_id": self.product.pk},
        )

        position = DisplayPosition.objects.get(fixture=self.fixture, row=2, column=1)
        self.assertEqual(position.finished_product_id, self.product.pk)
        upload.refresh_from_db()
        self.assertEqual(upload.finished_product_id, self.product.pk)
        # The map is the source of display capacity, so the peg starts
        # counting the moment it is assigned.
        self.product.refresh_from_db()
        self.assertEqual(self.product.display_slots, 2)
        # And the walk moves on by itself.
        self.assertEqual(
            response["HX-Redirect"],
            reverse("photo_walk", args=[self.fixture.pk]) + "?row=2&column=2",
        )

    def test_an_occupied_peg_is_never_overwritten_from_here(self):
        """The refusal `copy_board_layout` makes: a peg that already names a
        colorway is somebody's decision, and disagreeing with it is a map
        question rather than a photo one."""
        hang(self.fixture, self.product, 1, 1)
        other = make_product(make_recipe("Other"), "Artisan Other", with_image=False)
        upload = self._upload()

        self.client.post(
            reverse("assign_upload", args=[upload.id])
            + f"?fixture={self.fixture.pk}&row=1&column=1",
            {"product_id": other.pk},
        )

        position = DisplayPosition.objects.get(fixture=self.fixture, row=1, column=1)
        self.assertEqual(position.finished_product_id, self.product.pk)
        # The photo is still filed against what was picked — that half was
        # never in doubt.
        upload.refresh_from_db()
        self.assertEqual(upload.finished_product_id, other.pk)

    def test_the_last_peg_ends_the_walk_rather_than_wrapping(self):
        upload = self._upload()
        response = self.client.post(
            reverse("assign_upload", args=[upload.id])
            + f"?fixture={self.fixture.pk}&row=2&column=3",
            {"product_id": self.product.pk},
        )
        self.assertEqual(
            response["HX-Redirect"],
            reverse("photo_walk", args=[self.fixture.pk]) + "?done=1",
        )

    # --- the ordering ------------------------------------------------------

    def _colorway(self, name, bands, confirmed=True):
        product = make_product(make_recipe(name), f"Artisan {name}", with_image=False)
        FinishedProduct.objects.filter(pk=product.pk).update(raw_product=self.blank)
        recipe = product.recipe
        recipe.color_bands = bands
        recipe.bands_confirmed_at = timezone.now() if confirmed else None
        recipe.save()
        return FinishedProduct.objects.get(pk=product.pk)

    def test_the_ranking_tiers_are_exact_then_any_superset_then_overlap(self):
        """**A superset of any size is one tier.** Blue+green+five sits beside
        blue+green+one: a scarf with a lot going on is not a worse match for
        the blue and green in the photo, it is a scarf with a lot going on.
        """
        exact = self._colorway("Exact", ["blue", "green"])
        plus_one = self._colorway("Plus One", ["blue", "green", "red"])
        plus_five = self._colorway("Plus Five", [
            "blue", "green", "red", "orange", "yellow", "purple", "pink",
        ])
        overlap = self._colorway("Overlap", ["blue", "brown"])
        miss = self._colorway("Miss", ["brown"])

        ranked = photowalk.rank(
            [miss, overlap, plus_five, plus_one, exact], ["blue", "green"]
        )
        tiers = {row["product"].pk: row["tier"] for row in ranked}

        self.assertEqual(tiers[exact.pk], photowalk.EXACT)
        self.assertEqual(tiers[plus_one.pk], photowalk.SUPERSET)
        self.assertEqual(tiers[plus_five.pk], photowalk.SUPERSET)
        self.assertEqual(tiers[overlap.pk], photowalk.OVERLAP)
        self.assertEqual(tiers[miss.pk], photowalk.REST)
        self.assertEqual(ranked[0]["product"].pk, exact.pk)

    def test_a_photo_of_a_rainbow_matches_the_rainbow_colorway(self):
        """The walk gets this free from the classifier folding. Without it a
        rainbow photo produces most of the sheet's bands and matches nothing,
        which is the one photo this classifier reads *well* — there is no
        dominant colour to get wrong."""
        from .. import colorbands

        rainbow = self._colorway("Rainbow", ["rainbow"])
        plain = self._colorway("Just Red", ["red"])
        seen = colorbands.fold_rainbow(
            ["red", "orange", "yellow", "green", "blue", "purple"]
        )

        ranked = photowalk.rank([plain, rainbow], seen)
        self.assertEqual(ranked[0]["product"].pk, rainbow.pk)
        self.assertEqual(ranked[0]["tier"], photowalk.EXACT)

    def test_an_unconfirmed_colorway_is_never_ranked_on_its_guess(self):
        """`bands_confirmed_at` is what says a person agreed. An unreviewed
        guess ordering the list would look exactly like a reviewed one — the
        reason the rainbow sheet skips them too."""
        guessed = self._colorway("Guessed", ["blue", "green"], confirmed=False)
        ranked = photowalk.rank([guessed], ["blue", "green"])
        self.assertEqual(ranked[0]["tier"], photowalk.REST)

    def test_with_no_colour_read_the_list_is_alphabetical(self):
        """What it was before any of this, which is the right failure."""
        b = self._colorway("Bravo", ["blue"])
        a = self._colorway("Alpha", ["green"])
        ranked = photowalk.rank([b, a], [])
        self.assertEqual(
            [row["product"].name for row in ranked],
            ["Artisan Alpha", "Artisan Bravo"],
        )

    def test_candidates_are_scoped_to_the_board_s_blank(self):
        """A yarn board is one base in forty colours, so the answer is one of
        forty rather than one of a few hundred."""
        self._colorway("Mine", ["blue"])
        elsewhere = make_product(
            make_recipe("Someone Else"), "Different Blank", with_image=False
        )
        rows, _ = photowalk.candidates(self.fixture, ["blue"])
        self.assertNotIn(elsewhere.pk, [row["product"].pk for row in rows])

    def test_the_card_says_how_much_of_the_list_could_be_ordered(self):
        """A list that fell back to alphabetical for want of confirmed bands
        looks identical to one where the photo matched nothing, and only the
        first has a fix."""
        self._colorway("Confirmed", ["blue"])
        self._colorway("Unconfirmed", ["blue"], confirmed=False)

        upload = self._upload()
        response = self.client.post(
            reverse("process_upload", args=[upload.id]), self._stop_vals(2, 2)
        )
        self.assertContains(response, "have confirmed colours to sort by")
        self.assertContains(response, "until somebody confirms them")
@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class StoredFileCleanupTests(TestCase):
    """Deleting a photo must take its stored file with it.

    Django stopped deleting files on model delete in 1.3, so without the
    post_delete receivers a removal in the admin drops the row and leaves the
    object in the bucket — paying storage forever with nothing left that knows
    its key. Invisible from the admin, which reports success either way.
    """

    def _image(self):
        product = make_product(make_recipe("Cleanup"), "Cleanup Scarf", with_image=False)
        fpi = FinishedProductImage(finished_product=product)
        fpi.image.save("cleanup.jpg", ContentFile(make_jpeg((40, 30))), save=True)
        return product, fpi

    def test_deleting_an_image_row_deletes_its_file(self):
        _, fpi = self._image()
        name = fpi.image.name
        self.assertTrue(default_storage.exists(name))

        fpi.delete()
        self.assertFalse(default_storage.exists(name))

    def test_bulk_delete_also_deletes_files(self):
        """The admin's checkbox action deletes through the queryset, which never
        calls Model.delete() — only the signal reaches it."""
        _, fpi = self._image()
        name = fpi.image.name

        FinishedProductImage.objects.all().delete()
        self.assertFalse(default_storage.exists(name))

    def test_deleting_the_product_cascades_to_its_files(self):
        product, fpi = self._image()
        name = fpi.image.name

        product.delete()
        self.assertFalse(default_storage.exists(name))

    def test_deleting_an_unfiled_upload_deletes_its_object(self):
        """An upload whose barcode never matched has no FinishedProductImage at
        all, so its tracking row is the only thing pointing at the object."""
        key = default_storage.save("finished_products/orphan.jpg",
                                   ContentFile(make_jpeg((40, 30))))
        upload = ProductImageUpload.objects.create(key=key)

        upload.delete()
        self.assertFalse(default_storage.exists(key))

    def test_a_row_with_no_file_deletes_cleanly(self):
        product = make_product(make_recipe("External"), "External Scarf", with_image=False)
        fpi = FinishedProductImage.objects.create(
            finished_product=product, image_url="https://example.test/x.jpg"
        )
        fpi.delete()  # must not raise
        self.assertEqual(FinishedProductImage.objects.count(), 0)
@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class PurgeCommandTests(TestCase):
    def setUp(self):
        product = make_product(make_recipe("Purge"), "Purge Scarf", with_image=False)
        self.fpi = FinishedProductImage(finished_product=product)
        self.fpi.image.save("purge.jpg", ContentFile(make_jpeg((40, 30))), save=True)
        self.upload = ProductImageUpload.objects.create(key=self.fpi.image.name)

    def test_dry_run_deletes_nothing(self):
        from django.core.management import call_command
        from io import StringIO

        out = StringIO()
        call_command("purge_product_images", stdout=out)
        self.assertIn("Dry run", out.getvalue())
        self.assertEqual(FinishedProductImage.objects.count(), 1)
        self.assertTrue(default_storage.exists(self.fpi.image.name))

    def test_yes_deletes_rows_and_files(self):
        from django.core.management import call_command
        from io import StringIO

        name = self.fpi.image.name
        call_command("purge_product_images", "--yes", stdout=StringIO())

        self.assertEqual(FinishedProductImage.objects.count(), 0)
        self.assertEqual(ProductImageUpload.objects.count(), 0)
        self.assertFalse(default_storage.exists(name))

    def test_keep_external_leaves_url_only_rows(self):
        from django.core.management import call_command
        from io import StringIO

        product = FinishedProduct.objects.first()
        FinishedProductImage.objects.create(
            finished_product=product, image_url="https://example.test/keep.jpg"
        )
        call_command("purge_product_images", "--yes", "--keep-external", stdout=StringIO())

        remaining = FinishedProductImage.objects.all()
        self.assertEqual([i.image_url for i in remaining], ["https://example.test/keep.jpg"])
