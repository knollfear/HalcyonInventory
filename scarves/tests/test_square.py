"""Pushing the catalogue and the stock to Square, and the sale webhook.

The reasoning behind these is in `docs/claude/square.md`.
"""
import base64
import csv
import hashlib
import hmac
import json
import os
import re
import tempfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
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
    FakeSquareClient,
    FakeSquareResult,
    make_jpeg,
    make_product,
    make_recipe,
    make_undyed,
)


@override_settings(
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_LOCATION_ID="LOC123",
    SQUARE_ENVIRONMENT="sandbox",
)
class SyncToSquareTests(TestCase):
    """`sync_to_square` had no test coverage at all.

    It can't be exercised against the real Square API from here, so these
    drive the command with a stand-in client: what matters is that the payload
    we build is the payload we meant, and that IDs coming back get written to
    the right rows. A wrong payload is not a crash — it's a catalogue that
    quietly disagrees with the shop.
    """

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_product(self.recipe, "Stormy Silk", with_image=False)
        self.product.number_on_hand = 6
        self.product.price = Decimal("32.00")
        self.product.save()
        self.raw = self.product.raw_product

    def _run(self, client, **kwargs):
        out, err = StringIO(), StringIO()
        with mock.patch("square.client.Client", return_value=client):
            call_command("sync_to_square", stdout=out, stderr=err, **kwargs)
        return out.getvalue() + err.getvalue()

    # --- new catalogue ---------------------------------------------------

    def test_a_new_raw_product_is_sent_as_an_item_with_its_variations(self):
        client = FakeSquareClient()
        self._run(client)

        self.assertEqual(len(client.upserts), 1)
        objects = client.upserts[0]["batches"][0]["objects"]
        self.assertEqual(len(objects), 1)

        item = objects[0]
        self.assertEqual(item["type"], "ITEM")
        self.assertEqual(item["id"], f"#rp_{self.raw.pk}")
        self.assertEqual(item["item_data"]["name"], self.raw.name)

        variation = item["item_data"]["variations"][0]
        self.assertEqual(variation["id"], f"#fp_{self.product.pk}")
        self.assertEqual(variation["item_variation_data"]["name"], self.recipe.name)
        self.assertEqual(
            variation["item_variation_data"]["price_money"],
            {"amount": 3200, "currency": "USD"},
        )

    def test_the_sku_now_always_rides_along(self):
        """Since SKUs are assigned at creation, a variation can no longer
        reach Square with nothing to scan."""
        client = FakeSquareClient()
        self._run(client)
        variation = client.upserts[0]["batches"][0]["objects"][0]["item_data"]["variations"][0]
        self.assertEqual(
            variation["item_variation_data"]["sku"], self.product.sku
        )
        self.assertTrue(self.product.sku)

    def test_returned_ids_are_written_back(self):
        """Without this the next run creates everything a second time."""
        client = FakeSquareClient(upsert_results=[FakeSquareResult({
            "id_mappings": [
                {"client_object_id": f"#rp_{self.raw.pk}", "object_id": "SQ_ITEM"},
                {"client_object_id": f"#fp_{self.product.pk}", "object_id": "SQ_VAR"},
            ],
        })])
        self._run(client)

        self.raw.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.raw.square_item_id, "SQ_ITEM")
        self.assertEqual(self.product.square_variation_id, "SQ_VAR")

    def test_an_already_linked_item_sends_only_the_new_variation(self):
        RawProduct.objects.filter(pk=self.raw.pk).update(square_item_id="SQ_ITEM")
        client = FakeSquareClient()
        self._run(client)

        obj = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(obj["type"], "ITEM_VARIATION")
        self.assertEqual(obj["item_variation_data"]["item_id"], "SQ_ITEM")

    def test_a_fully_linked_catalogue_skips_the_upsert(self):
        RawProduct.objects.filter(pk=self.raw.pk).update(square_item_id="SQ_ITEM")
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        client = FakeSquareClient()
        output = self._run(client)
        self.assertEqual(client.upserts, [])
        self.assertIn("Nothing new to sync", output)
        self.assertEqual(len(client.inventory_changes), 1, "still pushes stock")

    # --- inventory -------------------------------------------------------

    def test_inventory_is_pushed_for_linked_variations(self):
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        client = FakeSquareClient()
        self._run(client, inventory_only=True)

        self.assertEqual(client.upserts, [], "--inventory-only skips catalogue")
        change = client.inventory_changes[0]["changes"][0]
        self.assertEqual(change["type"], "PHYSICAL_COUNT")
        self.assertEqual(change["physical_count"]["catalog_object_id"], "SQ_VAR")
        self.assertEqual(change["physical_count"]["location_id"], "LOC123")
        self.assertEqual(change["physical_count"]["quantity"], "6")

    def test_unlinked_products_are_left_out_of_the_inventory_push(self):
        client = FakeSquareClient()
        output = self._run(client, inventory_only=True)
        self.assertEqual(client.inventory_changes, [])
        self.assertIn("No variations with Square IDs", output)

    # --- failure ---------------------------------------------------------

    def test_a_catalogue_error_stops_before_writing_ids_or_stock(self):
        client = FakeSquareClient(upsert_results=[FakeSquareResult(
            errors=[{"category": "API_ERROR", "detail": "boom"}]
        )])
        with self.assertRaises(CommandError) as caught:
            self._run(client)

        self.raw.refresh_from_db()
        self.assertEqual(self.raw.square_item_id, "")
        self.assertEqual(client.inventory_changes, [],
                         "a failed catalogue must not be followed by a stock push")
        self.assertIn("boom", str(caught.exception))

    def test_an_inventory_error_is_reported(self):
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        client = FakeSquareClient(inventory_result=FakeSquareResult(
            errors=[{"category": "API_ERROR", "detail": "stock boom"}]
        ))
        with self.assertRaises(CommandError) as caught:
            self._run(client, inventory_only=True)
        self.assertIn("stock boom", str(caught.exception))

    # --- --update --------------------------------------------------------

    def test_update_sends_current_price_and_the_version_square_gave_us(self):
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        RawProduct.objects.filter(pk=self.raw.pk).update(square_item_id="SQ_ITEM")
        client = FakeSquareClient(retrieve_result=FakeSquareResult({
            "objects": [{"id": "SQ_VAR", "version": 42}],
        }))
        self._run(client, update=True)

        self.assertEqual(client.retrieves[0]["object_ids"], ["SQ_VAR"])
        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(sent["version"], 42)
        self.assertEqual(sent["item_variation_data"]["price_money"]["amount"], 3200)
        self.assertEqual(sent["item_variation_data"]["sku"], self.product.sku)
@override_settings(
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_LOCATION_ID="LOC123",
    SQUARE_ENVIRONMENT="sandbox",
)
class SquareVariationOrderTests(TestCase):
    """Variations come out of the till in catalogue order, not alphabetical.

    A new colourway is appended, so the list at the stall ends up in the order
    the dye baths happened — which is nobody's mental model of a colour. The
    only lever Square offers is position in the parent item's `variations`
    list, so the pass rewrites whole ITEMs, and an ITEM upsert deletes any
    variation missing from that list. These tests are mostly about the second
    sentence: what the pass refuses to touch matters more than what it sorts.
    """

    def setUp(self):
        recipe = make_recipe("Zinnia")
        self.product = make_product(recipe, "Zinnia Silk", with_image=False)
        self.raw = self.product.raw_product
        RawProduct.objects.filter(pk=self.raw.pk).update(square_item_id="SQ_ITEM")
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR_Z"
        )

    def _run(self, client, **kwargs):
        out, err = StringIO(), StringIO()
        with mock.patch("square.client.Client", return_value=client):
            call_command("sync_to_square", stdout=out, stderr=err, **kwargs)
        return out.getvalue() + err.getvalue()

    def _item(self, *names_and_ids, item_id="SQ_ITEM"):
        """A retrieve response shaped the way Square answers for an ITEM."""
        return FakeSquareResult({"objects": [{
            "type": "ITEM",
            "id": item_id,
            "version": 7,
            "updated_at": "2026-08-01T00:00:00Z",
            "item_data": {
                "name": "Silk Scarf",
                "variations": [
                    {
                        "type": "ITEM_VARIATION",
                        "id": var_id,
                        "version": 11,
                        "item_variation_data": {
                            "item_id": item_id,
                            "name": name,
                            "ordinal": i,
                        },
                    }
                    for i, (name, var_id) in enumerate(names_and_ids)
                ],
            },
        }]})

    def test_variations_go_back_alphabetised(self):
        client = FakeSquareClient(retrieve_results=[
            self._item(("Zinnia", "SQ_VAR_Z"), ("Amber", "SQ_VAR_A")),
        ])
        self._run(client, reorder=True)

        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(sent["type"], "ITEM")
        self.assertEqual(sent["id"], "SQ_ITEM")
        self.assertEqual(sent["version"], 7, "an update needs the version back")
        self.assertEqual(
            [v["item_variation_data"]["name"] for v in sent["item_data"]["variations"]],
            ["Amber", "Zinnia"],
        )
        self.assertEqual(
            [v["id"] for v in sent["item_data"]["variations"]],
            ["SQ_VAR_A", "SQ_VAR_Z"],
            "the rows are Square's own, only permuted",
        )
        self.assertEqual(
            [v["version"] for v in sent["item_data"]["variations"]], [11, 11]
        )

    def test_the_whole_variation_list_is_sent_back(self):
        """The one that costs stock if it's wrong.

        An ITEM upsert replaces the variation list, so a variation left out is
        deleted along with its Square ID and its count. Three go up, three come
        back — including the two this app has never heard of.
        """
        client = FakeSquareClient(retrieve_results=[
            self._item(
                ("Zinnia", "SQ_VAR_Z"),
                ("Amber", "SQ_VAR_A"),
                ("Moss", "SQ_VAR_UNKNOWN_TO_US"),
            ),
        ])
        self._run(client, reorder=True)

        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(
            [v["id"] for v in sent["item_data"]["variations"]],
            ["SQ_VAR_A", "SQ_VAR_UNKNOWN_TO_US", "SQ_VAR_Z"],
        )

    def test_an_item_that_came_back_without_variations_is_left_alone(self):
        """Sending that back would empty the item.

        An answer we didn't understand looks exactly like an item with nothing
        under it, and the difference is the whole catalogue.
        """
        client = FakeSquareClient(retrieve_results=[FakeSquareResult({
            "objects": [{"type": "ITEM", "id": "SQ_ITEM", "version": 7,
                         "item_data": {"name": "Silk Scarf"}}],
        })])
        output = self._run(client, reorder=True)
        self.assertEqual(client.upserts, [])
        self.assertIn("already alphabetical", output)

    def test_a_variation_named_by_an_item_option_is_left_alone(self):
        """It carries no name of its own and is ordered by the option's values.

        Sorting on the empty string would bunch those at the top and fight
        whatever set that order.
        """
        response = self._item(("Zinnia", "SQ_VAR_Z"), ("Amber", "SQ_VAR_A"))
        del response.body["objects"][0]["item_data"]["variations"][1] \
            ["item_variation_data"]["name"]
        client = FakeSquareClient(retrieve_results=[response])
        self._run(client, reorder=True)
        self.assertEqual(client.upserts, [])

    def test_an_item_with_no_positions_is_written_even_when_it_reads_sorted(self):
        """The state most of the live catalogue was found in.

        Square assigns ordinals only when a parent item's list is written, and
        a variation added on its own never is — which is how every colourway
        after the first reached Square. The API hands those back in name
        order, so the item reads as sorted while the till, with no positions
        to go on, shows them as they were created. Comparing names alone would
        skip exactly the items with the reported symptom.
        """
        response = self._item(("Amber", "SQ_VAR_A"), ("Zinnia", "SQ_VAR_Z"))
        for variation in response.body["objects"][0]["item_data"]["variations"]:
            del variation["item_variation_data"]["ordinal"]
        client = FakeSquareClient(retrieve_results=[response])
        output = self._run(client, reorder=True)

        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(
            [v["id"] for v in sent["item_data"]["variations"]],
            ["SQ_VAR_A", "SQ_VAR_Z"],
        )
        self.assertIn("no positions", output)

    def test_an_item_already_in_order_is_not_rewritten(self):
        """Otherwise every run bumps every version for nothing."""
        client = FakeSquareClient(retrieve_results=[
            self._item(("Amber", "SQ_VAR_A"), ("Zinnia", "SQ_VAR_Z")),
        ])
        output = self._run(client, reorder=True)
        self.assertEqual(client.upserts, [])
        self.assertIn("already alphabetical", output)

    def test_sorting_ignores_case(self):
        client = FakeSquareClient(retrieve_results=[
            self._item(("zinnia", "SQ_VAR_Z"), ("Amber", "SQ_VAR_A")),
        ])
        self._run(client, reorder=True)
        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(
            [v["id"] for v in sent["item_data"]["variations"]],
            ["SQ_VAR_A", "SQ_VAR_Z"],
        )

    def test_the_reported_ordinal_is_not_sent_back(self):
        """It is read-only, and the stale number beside the new position makes
        a dry run read as though nothing was being asked for."""
        client = FakeSquareClient(retrieve_results=[
            self._item(("Zinnia", "SQ_VAR_Z"), ("Amber", "SQ_VAR_A")),
        ])
        self._run(client, reorder=True)
        sent = client.upserts[0]["batches"][0]["objects"][0]
        for variation in sent["item_data"]["variations"]:
            self.assertNotIn("ordinal", variation["item_variation_data"])

    def test_a_group_item_is_reordered_too(self):
        """Undyed stock is one item whose variations are the blanks — which is
        exactly the list somebody scrolls at the till."""
        category = RawProductCategory.objects.get_or_create(name="Yarn")[0]
        group = CatalogGroup.objects.create(
            name="Undyed Yarn", category=category, square_item_id="SQ_GROUP"
        )
        client = FakeSquareClient(retrieve_results=[
            self._item(("Wool", "SQ_VAR_W"), ("Alpaca", "SQ_VAR_AL"),
                       item_id="SQ_GROUP"),
        ])
        self._run(client, reorder=True)

        self.assertIn("SQ_GROUP", client.retrieves[0]["object_ids"])
        self.assertEqual(group.square_item_id, "SQ_GROUP")
        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(sent["id"], "SQ_GROUP")
        self.assertEqual(
            [v["id"] for v in sent["item_data"]["variations"]],
            ["SQ_VAR_AL", "SQ_VAR_W"],
        )

    def test_reorder_alone_touches_no_stock(self):
        client = FakeSquareClient(retrieve_results=[
            self._item(("Zinnia", "SQ_VAR_Z"), ("Amber", "SQ_VAR_A")),
        ])
        self._run(client, reorder=True)
        self.assertEqual(client.inventory_changes, [])

    def test_a_read_error_stops_the_command(self):
        """A swallowed read is an empty answer, and an empty answer is
        indistinguishable from a catalogue already in order."""
        client = FakeSquareClient(retrieve_results=[FakeSquareResult(
            errors=[{"category": "API_ERROR", "detail": "read boom"}]
        )])
        with self.assertRaises(CommandError) as caught:
            self._run(client, reorder=True)
        self.assertIn("read boom", str(caught.exception))

    def test_a_normal_sync_ends_by_putting_the_order_right(self):
        """The run that creates a variation is the run that breaks the order,
        so the fix can't live in a command someone has to remember."""
        client = FakeSquareClient(retrieve_results=[
            self._item(("Zinnia", "SQ_VAR_Z"), ("Amber", "SQ_VAR_A")),
        ])
        self._run(client)

        self.assertEqual(client.retrieves[0]["object_ids"], ["SQ_ITEM"])
        sent = client.upserts[-1]["batches"][0]["objects"][0]
        self.assertEqual(
            [v["id"] for v in sent["item_data"]["variations"]],
            ["SQ_VAR_A", "SQ_VAR_Z"],
        )
        self.assertEqual(len(client.inventory_changes), 1,
                         "and stock still goes up afterwards")

    def test_a_dry_run_reorders_nothing(self):
        client = FakeSquareClient(retrieve_results=[
            self._item(("Zinnia", "SQ_VAR_Z"), ("Amber", "SQ_VAR_A")),
        ])
        output = self._run(client, reorder=True, dry_run=True)
        self.assertEqual(client.upserts, [])
        self.assertIn("Amber, Zinnia", output)
@override_settings(
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_LOCATION_ID="LOC123",
    SQUARE_ENVIRONMENT="sandbox",
)
class SquareFailsLoudlyTests(TestCase):
    """The likely real failure is an expired token, and it must not be quiet.

    Every error path used to print to stderr and `return`, which exits 0. On a
    schedule that reads as a successful run, and a catalogue that stopped
    syncing looks identical to one that had nothing to do — until somebody
    can't ring up a sale.
    """

    def setUp(self):
        recipe = make_recipe("Loud Recipe")
        self.product = make_product(recipe, "Loud Product", with_image=False)

    def _run(self, client, **kwargs):
        out = StringIO()
        with mock.patch("square.client.Client", return_value=client):
            call_command("sync_to_square", stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_a_rejected_token_fails_the_command(self):
        client = FakeSquareClient(locations_result=FakeSquareResult(
            errors=[{"category": "AUTHENTICATION_ERROR",
                     "detail": "This request could not be authorized."}]
        ))
        with self.assertRaises(CommandError) as caught:
            self._run(client)
        message = str(caught.exception)
        self.assertIn("expired or revoked", message,
                      "the message has to name the likely cause")
        self.assertEqual(client.upserts, [], "nothing was sent")

    def test_check_verifies_credentials_and_changes_nothing(self):
        client = FakeSquareClient()
        output = self._run(client, check=True)
        self.assertIn("Credentials OK", output)
        self.assertEqual(client.upserts, [])
        self.assertEqual(client.inventory_changes, [])

    @override_settings(SQUARE_ACCESS_TOKEN="")
    def test_a_missing_token_is_caught_before_any_call(self):
        with self.assertRaises(CommandError) as caught:
            self._run(FakeSquareClient())
        self.assertIn("SQUARE_ACCESS_TOKEN", str(caught.exception))

    @override_settings(SQUARE_LOCATION_ID="")
    def test_a_missing_location_is_caught_before_any_call(self):
        with self.assertRaises(CommandError) as caught:
            self._run(FakeSquareClient())
        self.assertIn("SQUARE_LOCATION_ID", str(caught.exception))

    def test_a_location_from_another_account_is_refused(self):
        """Inventory would be pushed nowhere, successfully."""
        client = FakeSquareClient(locations_result=FakeSquareResult(
            {"locations": [{"id": "SOMEONE_ELSE"}]}
        ))
        with self.assertRaises(CommandError) as caught:
            self._run(client)
        self.assertIn("not one of this account's locations", str(caught.exception))

    def test_a_failed_version_read_does_not_send_null_versions(self):
        """Swallowing it meant every variation went back up with
        version: None, which is not the update anyone intended."""
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        client = FakeSquareClient(retrieve_result=FakeSquareResult(
            errors=[{"category": "AUTHENTICATION_ERROR", "detail": "nope"}]
        ))
        with self.assertRaises(CommandError) as caught:
            self._run(client, update=True)
        self.assertIn("current variation versions", str(caught.exception))
        self.assertEqual(client.upserts, [])

    def test_a_variation_square_has_forgotten_is_skipped_not_duplicated(self):
        """No version means Square doesn't know the ID; sending it anyway
        creates a second variation rather than updating the first."""
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="GONE"
        )
        client = FakeSquareClient(retrieve_result=FakeSquareResult({"objects": []}))
        output = self._run(client, update=True)
        self.assertIn("doesn't recognise", output)
        sent = client.upserts[0]["batches"][0]["objects"] if client.upserts else []
        self.assertEqual(sent, [], "nothing with a null version went up")
@override_settings(
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_LOCATION_ID="LOC123",
    SQUARE_ENVIRONMENT="sandbox",
)
class SquareDryRunTests(TestCase):
    """`--dry-run` stands in for a sandbox account.

    The sandbox token process has been troublesome, so the way to avoid
    production being the first thing that ever runs this is to build the whole
    payload and print it instead of sending it.
    """

    def setUp(self):
        recipe = make_recipe("Dry Recipe")
        self.product = make_product(recipe, "Dry Product", with_image=False)
        self.product.price = Decimal("18.50")
        self.product.number_on_hand = 4
        self.product.save()

    def _run(self, client, **kwargs):
        out = StringIO()
        with mock.patch("square.client.Client", return_value=client):
            call_command("sync_to_square", stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_it_sends_nothing(self):
        client = FakeSquareClient()
        self._run(client, dry_run=True)
        self.assertEqual(client.upserts, [])
        self.assertEqual(client.inventory_changes, [])

    def test_it_writes_no_ids_back(self):
        client = FakeSquareClient(upsert_results=[FakeSquareResult({
            "id_mappings": [{"client_object_id": f"#rp_{self.product.raw_product.pk}",
                             "object_id": "SHOULD_NOT_BE_SAVED"}],
        })])
        self._run(client, dry_run=True)
        self.product.raw_product.refresh_from_db()
        self.assertEqual(self.product.raw_product.square_item_id, "")

    def test_it_shows_what_would_be_created(self):
        output = self._run(FakeSquareClient(), dry_run=True)
        self.assertIn("DRY RUN", output)
        self.assertIn(self.product.raw_product.name, output)
        self.assertIn(self.product.sku, output)
        self.assertIn("$18.50", output)

    def test_it_shows_the_stock_counts_it_would_set(self):
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        RawProduct.objects.filter(pk=self.product.raw_product.pk).update(
            square_item_id="SQ_ITEM"
        )
        output = self._run(FakeSquareClient(), dry_run=True, inventory_only=True)
        self.assertIn("SQ_VAR -> 4", output)
        self.assertIn("LOC123", output)

    def test_it_still_checks_the_credentials(self):
        """A dry run that passes with a dead token teaches nothing."""
        client = FakeSquareClient(locations_result=FakeSquareResult(
            errors=[{"category": "AUTHENTICATION_ERROR", "detail": "nope"}]
        ))
        with self.assertRaises(CommandError):
            self._run(client, dry_run=True)
@override_settings(
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_LOCATION_ID="LOC123",
    SQUARE_ENVIRONMENT="sandbox",
)
class SquarePartialBatchTests(TestCase):
    """A failure partway through must not orphan what Square already created.

    Over 100 objects is more than one call. If a later chunk fails and the
    IDs from the earlier ones are discarded, those objects exist in Square
    with nothing here pointing at them — and the next run creates them again.
    The only symptom is a catalogue with everything in it twice, which is
    tedious to unpick by hand.
    """

    def setUp(self):
        self.recipe = make_recipe("Batch Recipe")
        category, _ = RawProductCategory.objects.get_or_create(name="Silk")
        self.products = []
        for i in range(150):
            raw = RawProduct.objects.create(
                name=f"Blank {i:03d}", category=category, price="5.00"
            )
            self.products.append(FinishedProduct.objects.create(
                name=f"Product {i:03d}", raw_product=raw,
                recipe=self.recipe, price="20.00",
            ))

    def test_ids_from_a_successful_chunk_survive_a_later_failure(self):
        first = self.products[0]
        client = FakeSquareClient(upsert_results=[
            FakeSquareResult({"id_mappings": [
                {"client_object_id": f"#rp_{first.raw_product.pk}",
                 "object_id": "SAVED_ITEM"},
                {"client_object_id": f"#fp_{first.pk}",
                 "object_id": "SAVED_VAR"},
            ]}),
            FakeSquareResult(errors=[{"category": "API_ERROR", "detail": "later boom"}]),
        ])

        out = StringIO()
        with mock.patch("square.client.Client", return_value=client):
            with self.assertRaises(CommandError):
                call_command("sync_to_square", stdout=out, stderr=out)

        first.refresh_from_db()
        first.raw_product.refresh_from_db()
        self.assertEqual(first.raw_product.square_item_id, "SAVED_ITEM")
        self.assertEqual(first.square_variation_id, "SAVED_VAR")
        self.assertIn("re-run to continue", out.getvalue())

    def test_a_rerun_does_not_recreate_what_is_already_linked(self):
        linked = self.products[0]
        RawProduct.objects.filter(pk=linked.raw_product.pk).update(
            square_item_id="SAVED_ITEM"
        )
        FinishedProduct.objects.filter(pk=linked.pk).update(
            square_variation_id="SAVED_VAR"
        )
        client = FakeSquareClient()
        with mock.patch("square.client.Client", return_value=client):
            call_command("sync_to_square", stdout=StringIO(), stderr=StringIO())

        sent = [o for body in client.upserts
                for o in body["batches"][0]["objects"]]
        self.assertNotIn(f"#rp_{linked.raw_product.pk}", [o["id"] for o in sent])
@override_settings(
    SQUARE_WEBHOOK_SIGNATURE_KEY="test-signature-key",
    SQUARE_WEBHOOK_URL="https://example.test/scarves/webhooks/square",
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_ENVIRONMENT="sandbox",
)
class SquareWebhookTests(TestCase):
    """What the webhook does with a line item, in all four shapes.

    The one that matters is the line it *can't* place. That used to be a bare
    `continue`: a scarf nobody could name was rung up, walked out of the tent,
    and left no trace — Square had the money, this app still had the stock,
    and nothing anywhere said the two disagreed.
    """

    def setUp(self):
        self.silk, _ = RawProductCategory.objects.get_or_create(name="Silk")
        raw = RawProduct.objects.create(name="Infinity", category=self.silk, price="5.00")
        self.product = FinishedProduct.objects.create(
            name="Aegean Infinity", raw_product=raw, recipe=make_recipe("Aegean Sea"),
            price="30.00", number_on_hand=4, square_variation_id="VAR-AEGEAN",
        )
        self.sold_at = "2026-08-15T18:30:00Z"

    def _post(self, line_items, order_id="ORDER-1", closed_at=None):
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

        order = {"line_items": line_items, "closed_at": closed_at or self.sold_at}
        with mock.patch("square.client.Client") as client:
            client.return_value.orders.retrieve_order.return_value = FakeSquareResult(
                {"order": order}
            )
            return self.client.post(
                reverse("square_webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_SQUARE_HMACSHA256_SIGNATURE=signature,
            )

    def test_a_known_variation_still_leaves_inventory(self):
        response = self._post([{
            "uid": "L1", "catalog_object_id": "VAR-AEGEAN", "quantity": "2",
            "name": "Infinity", "variation_name": "Aegean Sea",
        }])

        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 2)
        self.assertEqual(UnmatchedSale.objects.count(), 0)

    def test_an_unknown_variation_is_queued_instead_of_dropped(self):
        self._post([{
            "uid": "L1", "catalog_object_id": "VAR-WHO-KNOWS", "quantity": "1",
            "name": "Scarf", "variation_name": "Regular",
            "total_money": {"amount": 3000},
        }])

        sale = UnmatchedSale.objects.get()
        self.assertEqual(sale.square_variation_id, "VAR-WHO-KNOWS")
        self.assertEqual(sale.quantity, 1)
        self.assertEqual(sale.amount_cents, 3000)
        self.assertTrue(sale.is_open)

    def test_a_line_with_no_catalog_object_is_queued_too(self):
        """A custom amount rung up at the till has no catalog object at all."""
        self._post([{"uid": "L1", "quantity": "1", "name": "Custom Amount",
                     "total_money": {"amount": 2500}}])

        self.assertEqual(UnmatchedSale.objects.get().name, "Custom Amount")

    def test_the_queued_row_carries_squares_time_not_ours(self):
        """The review screen pairs on this timestamp — receipt time would drift
        by however long the webhook took to arrive."""
        self._post([{"uid": "L1", "quantity": "1", "name": "Scarf"}])

        sale = UnmatchedSale.objects.get()
        self.assertEqual(sale.sold_at.isoformat(), "2026-08-15T18:30:00+00:00")

    def test_a_redelivered_order_does_not_queue_it_twice(self):
        """Square sends order.updated more than once for the same order."""
        line = [{"uid": "L1", "quantity": "1", "name": "Scarf"}]
        self._post(line)
        self._post(line)

        self.assertEqual(UnmatchedSale.objects.count(), 1)

    def test_a_redelivered_order_does_not_sell_the_same_scarf_twice(self):
        line = [{"uid": "L1", "catalog_object_id": "VAR-AEGEAN", "quantity": "1"}]
        self._post(line)
        self._post(line)

        self.product.refresh_from_db()
        self.assertEqual(self.product.number_on_hand, 3)
        self.assertEqual(
            InventoryLog.objects.filter(log_type=InventoryLog.SALE).count(), 1
        )
@override_settings(
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_LOCATION_ID="LOC123",
    SQUARE_ENVIRONMENT="sandbox",
    MEDIA_ROOT=tempfile.mkdtemp(),
)
class SquareImageSyncTests(TestCase):
    """`sync_to_square --images`.

    The failure this is built around is a re-run stacking the same photo on
    the same variation: Square appends to `image_ids` and has no way to tell
    it is being handed a picture it already holds, so nothing but our own
    record stops it. Every test here is ultimately about that record being
    written at the right moment.
    """

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.recipe = make_recipe("Stormy Sea")
        self.product = make_product(self.recipe, "Stormy Silk", with_image=False)
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        self.product.refresh_from_db()
        self.image = FinishedProductImage.objects.create(
            finished_product=self.product,
            image=SimpleUploadedFile(
                "stormy.jpg", make_jpeg((60, 40)), content_type="image/jpeg"
            ),
        )

    def _run(self, client, **kwargs):
        out, err = StringIO(), StringIO()
        with mock.patch("square.client.Client", return_value=client):
            call_command("sync_to_square", "--images", stdout=out, stderr=err, **kwargs)
        return out.getvalue() + err.getvalue()

    def _second_photo(self, order=2):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return FinishedProductImage.objects.create(
            finished_product=self.product,
            order=order,
            image=SimpleUploadedFile(
                "stormy-2.jpg", make_jpeg((60, 40)), content_type="image/jpeg"
            ),
        )

    # --- the upload ------------------------------------------------------

    def test_a_photo_is_attached_to_its_variation_not_its_item(self):
        """An item here is a style and every variation under it looks
        completely different — a photo on the item mislabels all but one."""
        client = FakeSquareClient()
        self._run(client)

        self.assertEqual(len(client.images), 1)
        request, payload = client.images[0]
        self.assertEqual(request["object_id"], "SQ_VAR")
        self.assertTrue(request["is_primary"])
        self.assertEqual(request["image"]["type"], "IMAGE")
        self.assertTrue(payload, "the file's bytes have to reach the call")

    def test_the_square_id_is_recorded(self):
        client = FakeSquareClient()
        self._run(client)

        self.image.refresh_from_db()
        self.assertEqual(self.image.square_image_id, "SQ_IMG_1")

    def test_a_second_run_sends_nothing(self):
        """The whole reason the column exists."""
        self._run(FakeSquareClient())

        again = FakeSquareClient()
        output = self._run(again)

        self.assertEqual(again.images, [])
        self.assertIn("already on Square", output)

    def test_only_the_first_photo_on_a_variation_is_primary(self):
        """A later photo must not displace the picture the POS shows."""
        self._second_photo()
        client = FakeSquareClient()
        self._run(client)

        self.assertEqual([r["is_primary"] for r, _ in client.images], [True, False])

    def test_a_photo_added_later_is_not_primary_either(self):
        self._run(FakeSquareClient())
        self._second_photo()

        client = FakeSquareClient()
        self._run(client)

        self.assertEqual(len(client.images), 1)
        self.assertFalse(client.images[0][0]["is_primary"])

    # --- what it can't send ----------------------------------------------

    def test_a_product_square_has_never_seen_is_named_not_dropped(self):
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id=""
        )
        client = FakeSquareClient()
        output = self._run(client)

        self.assertEqual(client.images, [])
        self.assertIn("never seen", output)
        self.assertIn(self.product.sku, output)

    def test_an_external_url_with_no_file_is_named_not_dropped(self):
        """Square's image endpoint takes bytes, and there are none here."""
        FinishedProductImage.objects.filter(pk=self.image.pk).delete()
        FinishedProductImage.objects.create(
            finished_product=self.product,
            image_url="https://example.test/elsewhere.jpg",
        )
        client = FakeSquareClient()
        output = self._run(client)

        self.assertEqual(client.images, [])
        self.assertIn("external URLs", output)

    def test_an_unreadable_file_is_skipped_and_the_run_carries_on(self):
        """One missing object in the bucket is that photo's problem. An API
        error is everybody's problem and stops the run — see below."""
        second = self._second_photo()
        FinishedProductImage.objects.filter(pk=self.image.pk).update(
            image="finished_products/gone.jpg"
        )
        client = FakeSquareClient()
        output = self._run(client)

        self.assertEqual(len(client.images), 1)
        second.refresh_from_db()
        self.assertEqual(second.square_image_id, "SQ_IMG_1")
        self.assertIn("could not be read", output)

    # --- failure ----------------------------------------------------------

    def test_an_api_error_stops_the_run_and_keeps_what_went_up(self):
        self._second_photo()
        client = FakeSquareClient(image_results=[
            FakeSquareResult({"image": {"id": "SQ_IMG_1"}}),
            FakeSquareResult(errors=[{"category": "API_ERROR", "detail": "boom"}]),
        ])
        with self.assertRaises(CommandError) as caught:
            self._run(client)

        self.image.refresh_from_db()
        self.assertEqual(self.image.square_image_id, "SQ_IMG_1",
                         "what succeeded must stay recorded or the re-run duplicates it")
        self.assertIn("boom", str(caught.exception))

    def test_an_accepted_photo_with_no_id_back_stops_the_run(self):
        """The one case where a success is worse than an error: Square has
        the photo, we have nothing to record, and a re-run stacks it."""
        client = FakeSquareClient(image_results=[FakeSquareResult({"image": {}})])
        with self.assertRaises(CommandError) as caught:
            self._run(client)

        self.image.refresh_from_db()
        self.assertEqual(self.image.square_image_id, "")
        self.assertIn("twice", str(caught.exception))

    # --- mode -------------------------------------------------------------

    def test_a_dry_run_uploads_nothing(self):
        client = FakeSquareClient()
        output = self._run(client, dry_run=True)

        self.assertEqual(client.images, [])
        self.assertIn("DRY RUN", output)
        self.image.refresh_from_db()
        self.assertEqual(self.image.square_image_id, "")

    def test_images_is_a_mode_of_its_own(self):
        """Slow and one-at-a-time — it has no business on the schedule that
        pushes stock counts."""
        client = FakeSquareClient()
        self._run(client)

        self.assertEqual(client.upserts, [])
        self.assertEqual(client.inventory_changes, [])
class ImportSquareSalesTests(TestCase):
    """The CSV recovery path, which is what runs after a webhook gap.

    It is a desk tool, not a field one — a CSV export off the dashboard,
    matched on SKU. That makes it the only reconciliation route that needs no
    Square API token at all, so an expired token takes out the webhook and the
    inventory push while leaving this intact.
    """

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea")
        self.dyed = make_product(self.recipe, "Stormy Silk", with_image=False)
        FinishedProduct.objects.filter(pk=self.dyed.pk).update(number_on_hand=10)
        self.dyed.refresh_from_db()

        self.undyed = make_undyed("Merino Worsted Natural", on_hand=12)

    def _run(self, rows, **opts):
        """Write `rows` as a Square export and import it."""
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".csv", newline="", delete=False, encoding="utf-8"
        )
        self.addCleanup(os.unlink, handle.name)
        with handle as f:
            writer = csv.DictWriter(
                f, fieldnames=["Date", "Transaction ID", "Item", "SKU", "Qty"]
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

        out = StringIO()
        call_command("import_square_sales", handle.name, stdout=out, **opts)
        return out.getvalue()

    def _row(self, product, qty=2, txn="ORDER-1"):
        return {
            "Date": "2026-08-24",
            "Transaction ID": txn,
            "Item": product.name,
            "SKU": product.sku,
            "Qty": str(qty),
        }

    def test_a_dyed_sale_comes_off_the_finished_row(self):
        self._run([self._row(self.dyed, qty=3)])

        self.dyed.refresh_from_db()
        self.assertEqual(self.dyed.number_on_hand, 7)

    def test_an_undyed_sale_comes_off_the_raw_pile(self):
        """The bug this class was written for.

        Writing `number_on_hand` on a passthrough writes to the mirror:
        `save()` re-derives it from the raw row, the number snaps back, and the
        command reports OK having moved nothing. The reorder signal — the whole
        reason undyed stock is counted — never moves.
        """
        self._run([self._row(self.undyed, qty=2)])

        self.undyed.raw_product.refresh_from_db()
        self.assertEqual(self.undyed.raw_product.number_on_hand, 10)

    def test_the_undyed_mirror_follows(self):
        self._run([self._row(self.undyed, qty=2)])

        self.undyed.refresh_from_db()
        self.assertEqual(self.undyed.number_on_hand, 10)

    def test_it_never_drives_stock_negative(self):
        self._run([self._row(self.undyed, qty=99)])

        self.undyed.raw_product.refresh_from_db()
        self.assertEqual(self.undyed.raw_product.number_on_hand, 0)

    def test_a_sale_the_webhook_already_logged_is_skipped(self):
        """The double-dip guard, and the contract it rests on.

        Square's CSV "Transaction ID" column carries the *order* id — the same
        value `square_webhook` writes to `sale_reference`. That is what makes it
        safe to import a period the webhook partly handled. If either side ever
        keys off something else this fails, which is the point: the symptom
        otherwise is every already-recorded sale decremented a second time.
        """
        InventoryLog.objects.create(
            finished_product=self.dyed,
            raw_product=self.dyed.raw_product,
            log_type=InventoryLog.SALE,
            quantity=-3,
            sale_reference="ORDER-1",
            notes="Square sale via webhook.",
        )

        output = self._run([self._row(self.dyed, qty=3, txn="ORDER-1")])

        self.dyed.refresh_from_db()
        self.assertEqual(self.dyed.number_on_hand, 10)
        self.assertIn("1 duplicate", output)

    def test_running_the_same_export_twice_changes_nothing(self):
        rows = [self._row(self.dyed, qty=3, txn="ORDER-1")]
        self._run(rows)
        self._run(rows)

        self.dyed.refresh_from_db()
        self.assertEqual(self.dyed.number_on_hand, 7)
        self.assertEqual(InventoryLog.objects.filter(log_type=InventoryLog.SALE).count(), 1)

    def test_a_dry_run_moves_nothing(self):
        self._run([self._row(self.undyed, qty=2)], dry_run=True)

        self.undyed.raw_product.refresh_from_db()
        self.assertEqual(self.undyed.raw_product.number_on_hand, 12)
        self.assertFalse(InventoryLog.objects.exists())

    def test_a_line_with_no_sku_is_counted_rather_than_dropped(self):
        """A hand-keyed sale carries no SKU. It can't be recovered here, but
        an unrecoverable line that says nothing is how the last one went
        missing."""
        row = self._row(self.dyed)
        row["SKU"] = ""

        output = self._run([row])

        self.assertIn("1 skipped (no SKU)", output)
@override_settings(
    SQUARE_ACCESS_TOKEN="test-token",
    SQUARE_LOCATION_ID="LOC123",
    SQUARE_ENVIRONMENT="sandbox",
)
class SquarePriceDivergenceTests(TestCase):
    """A price can be set in two places and only one of them writes it down.

    `sync_to_square --update` sends `FinishedProduct.price` at every variation
    Square already has, so a price typed into the dashboard survives until the
    next `--update` and is then replaced with nothing to say a different
    number was ever there. The till starts charging a figure nobody chose, and
    the only symptom is a receipt.
    """

    def setUp(self):
        self.recipe = make_recipe("Stormy Sea")
        self.product = make_product(self.recipe, "Stormy Silk", with_image=False)
        self.product.price = Decimal("32.00")
        self.product.save()
        FinishedProduct.objects.filter(pk=self.product.pk).update(
            square_variation_id="SQ_VAR"
        )
        RawProduct.objects.filter(pk=self.product.raw_product.pk).update(
            square_item_id="SQ_ITEM"
        )
        self.product.refresh_from_db()

    def _square_variation(self, amount, version=42, updated_at="2026-08-28T10:00:00Z"):
        return {
            "id": "SQ_VAR",
            "type": "ITEM_VARIATION",
            "version": version,
            "updated_at": updated_at,
            "item_variation_data": {
                "item_id": "SQ_ITEM",
                "name": self.recipe.name,
                "sku": self.product.sku,
                "pricing_type": "FIXED_PRICING",
                "price_money": {"amount": amount, "currency": "USD"},
            },
        }

    def _run(self, command, client, **kwargs):
        out, err = StringIO(), StringIO()
        with mock.patch("square.client.Client", return_value=client):
            call_command(command, stdout=out, stderr=err, **kwargs)
        return out.getvalue() + err.getvalue()

    # --- the guard on --update ------------------------------------------

    def test_update_leaves_a_price_square_disagrees_with_alone(self):
        """The whole point. Square says $38, we say $32, and the run must not
        quietly make it $32 again."""
        client = FakeSquareClient(retrieve_results=[
            FakeSquareResult({"objects": [self._square_variation(3800)]}),
            FakeSquareResult({"objects": []}),
        ])
        output = self._run("sync_to_square", client, update=True)

        self.assertIn("disagrees with ours", output)
        self.assertIn("$32.00", output)
        self.assertIn("$38.00", output)
        sent = client.upserts[0]["batches"][0]["objects"] if client.upserts else []
        self.assertEqual(sent, [], "nothing went up for the diverged row")

    def test_force_prices_still_overwrites(self):
        """The escape hatch has to exist — sometimes ours really is right."""
        client = FakeSquareClient(retrieve_results=[
            FakeSquareResult({"objects": [self._square_variation(3800)]}),
            FakeSquareResult({"objects": []}),
        ])
        self._run("sync_to_square", client, update=True, force_prices=True)

        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(sent["item_variation_data"]["price_money"]["amount"], 3200)

    def test_an_agreeing_price_updates_as_it_always_did(self):
        """The guard must not turn --update into a no-op for the ordinary
        case, which is what would make somebody pass --force-prices always."""
        client = FakeSquareClient(retrieve_results=[
            FakeSquareResult({"objects": [self._square_variation(3200)]}),
            FakeSquareResult({"objects": []}),
        ])
        self._run("sync_to_square", client, update=True)

        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(sent["version"], 42)
        self.assertEqual(sent["item_variation_data"]["sku"], self.product.sku)

    # --- the diff --------------------------------------------------------

    def test_the_diff_reports_both_prices_and_changes_nothing(self):
        client = FakeSquareClient(retrieve_result=FakeSquareResult({
            "objects": [self._square_variation(3800)],
        }))
        output = self._run("compare_square_prices", client)

        self.assertIn("32.00", output)
        self.assertIn("38.00", output)
        self.assertIn("2026-08-28", output)
        self.assertEqual(client.upserts, [])
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("32.00"))

    def test_pull_takes_squares_price(self):
        client = FakeSquareClient(retrieve_result=FakeSquareResult({
            "objects": [self._square_variation(3800)],
        }))
        self._run("compare_square_prices", client, pull=True)

        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("38.00"))
        self.assertEqual(client.upserts, [], "a pull never writes to Square")

    def test_push_echoes_squares_own_object_back(self):
        """Same rule the ordering pass follows: never *build* a variation.
        A payload assembled here would drop whatever field this app does not
        model — and the drop is silent."""
        obj = self._square_variation(3800)
        obj["item_variation_data"]["location_overrides"] = [{"location_id": "LOC123"}]
        client = FakeSquareClient(retrieve_result=FakeSquareResult({"objects": [obj]}))
        self._run("compare_square_prices", client, push=True)

        sent = client.upserts[0]["batches"][0]["objects"][0]
        self.assertEqual(sent["item_variation_data"]["price_money"]["amount"], 3200)
        self.assertEqual(sent["version"], 42)
        self.assertIn("location_overrides", sent["item_variation_data"])
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("32.00"))

    def test_a_dry_run_resolves_nothing(self):
        client = FakeSquareClient(retrieve_result=FakeSquareResult({
            "objects": [self._square_variation(3800)],
        }))
        output = self._run("compare_square_prices", client, pull=True, dry_run=True)

        self.assertIn("DRY RUN", output)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("32.00"))

    def test_a_mistyped_sku_stops_rather_than_matching_nothing(self):
        """Applying it as 'no rows' reads on screen exactly like a catalogue
        that already agreed."""
        client = FakeSquareClient(retrieve_result=FakeSquareResult({
            "objects": [self._square_variation(3800)],
        }))
        with self.assertRaises(CommandError) as caught:
            self._run("compare_square_prices", client, pull=True, skus=["NOSUCH"])
        self.assertIn("NOSUCH", str(caught.exception))
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("32.00"))

    def test_changed_since_scopes_to_what_square_touched(self):
        client = FakeSquareClient(retrieve_result=FakeSquareResult({
            "objects": [self._square_variation(3800, updated_at="2026-07-01T10:00:00Z")],
        }))
        with self.assertRaises(CommandError) as caught:
            self._run(
                "compare_square_prices", client, pull=True, changed_since="2026-08-01"
            )
        self.assertIn("2026-08-01", str(caught.exception))

    def test_variable_pricing_is_named_not_pulled_as_zero(self):
        """Variable pricing is the till asking the cashier, not a price of
        nothing. Pulling it would set a real product to whatever None became."""
        obj = self._square_variation(3800)
        obj["item_variation_data"]["pricing_type"] = "VARIABLE_PRICING"
        del obj["item_variation_data"]["price_money"]
        client = FakeSquareClient(retrieve_result=FakeSquareResult({"objects": [obj]}))
        output = self._run("compare_square_prices", client, pull=True)

        self.assertIn("no fixed price", output)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("32.00"))

    def test_a_read_failure_is_never_read_as_agreement(self):
        """An empty answer and a catalogue that agrees on every price look
        identical, and only one of them is safe to act on."""
        client = FakeSquareClient(retrieve_result=FakeSquareResult(
            errors=[{"category": "AUTHENTICATION_ERROR", "detail": "nope"}]
        ))
        with self.assertRaises(CommandError) as caught:
            self._run("compare_square_prices", client)
        self.assertIn("catalogue", str(caught.exception))

    def test_two_directions_in_one_run_are_refused(self):
        client = FakeSquareClient()
        with self.assertRaises(CommandError):
            self._run("compare_square_prices", client, pull=True, push=True)
