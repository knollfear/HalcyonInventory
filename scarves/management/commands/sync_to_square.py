import io
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from scarves.models import (
    CatalogGroup,
    FinishedProduct,
    FinishedProductImage,
    RawProduct,
)


class Command(BaseCommand):
    """Push the catalogue and stock counts to Square.

    Every failure path here raises `CommandError` rather than printing and
    returning. That matters because this is meant to run on a schedule: a
    bare `return` exits 0, so cron records a successful run, and a catalogue
    that silently stopped syncing looks exactly like one that never needed to.
    The most likely real failure — an expired access token — is precisely the
    kind that would sit unnoticed until someone can't ring up a sale.
    """

    help = "Sync active finished products to Square as catalog items/variations, then push inventory counts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Build everything and print what would be sent, without "
                "sending it. The stand-in for a sandbox account: see exactly "
                "what would land in the live catalogue before it does."
            ),
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help=(
                "Verify the credentials and location, then stop. Changes "
                "nothing — run it before a festival to find out the token "
                "expired while there's still time to do something."
            ),
        )
        parser.add_argument(
            "--inventory-only",
            action="store_true",
            help="Skip catalog upsert and only push inventory counts.",
        )
        parser.add_argument(
            "--images",
            action="store_true",
            help=(
                "Upload product photos and attach them to their Square "
                "variations, then stop. A mode of its own because it is slow "
                "— no batch endpoint, one multipart request per photo — and "
                "has no business running on the schedule that pushes stock."
            ),
        )
        parser.add_argument(
            "--update",
            action="store_true",
            help="Push updated prices and SKUs to existing Square variations, then sync inventory.",
        )
        parser.add_argument(
            "--force-prices",
            action="store_true",
            help=(
                "Let --update overwrite a price Square holds differently. "
                "Without it those variations are skipped and named: a price "
                "edited in the dashboard is somebody's decision, and this "
                "command is the one thing that can erase it without trace."
            ),
        )
        parser.add_argument(
            "--relink",
            action="store_true",
            help=(
                "Match local rows to variations Square already has, by SKU, "
                "and write the IDs back. Run this after anything that could "
                "have lost a square_variation_id — otherwise the next plain "
                "sync creates a second variation for every one of them."
            ),
        )
        parser.add_argument(
            "--reattach",
            action="store_true",
            help=(
                "Re-assert photo attachment for images Square already holds. "
                "For photos that reached the library but never landed on "
                "their variation, which --images cannot repair."
            ),
        )
        parser.add_argument(
            "--reorder",
            action="store_true",
            help=(
                "Put every item's variations back into alphabetical order in "
                "Square, then stop. The ordering pass runs at the end of a "
                "normal sync too; this is how you fix a catalogue that "
                "drifted before it did."
            ),
        )

    def _fail(self, label, result):
        detail = "; ".join(
            f"{e.get('category')}: {e.get('detail')}"
            for e in (result.errors or [])
        ) or "no detail given"
        raise CommandError(f"{label}: {detail}")

    def _preflight(self, client):
        """Confirm the credentials work before changing anything.

        One cheap read. Without it an expired token surfaces partway through
        a batch upsert, where the message is about catalogue objects and the
        actual cause — nobody refreshed the token — is three levels down.
        """
        if not settings.SQUARE_ACCESS_TOKEN:
            raise CommandError("SQUARE_ACCESS_TOKEN is not set.")
        if not settings.SQUARE_LOCATION_ID:
            raise CommandError("SQUARE_LOCATION_ID is not set.")

        result = client.locations.list_locations()
        if result.is_error():
            self._fail(
                "Square rejected the credentials (expired or revoked token?)",
                result,
            )

        locations = result.body.get("locations", []) or []
        known = {loc.get("id") for loc in locations}
        if known and settings.SQUARE_LOCATION_ID not in known:
            raise CommandError(
                f"SQUARE_LOCATION_ID {settings.SQUARE_LOCATION_ID} is not one "
                f"of this account's locations ({', '.join(sorted(known))}). "
                f"Inventory would be pushed nowhere."
            )
        self.stdout.write(self.style.SUCCESS(
            f"Credentials OK — {len(known)} location(s), "
            f"using {settings.SQUARE_LOCATION_ID}."
        ))

    def _describe(self, objects):
        """One readable line per object, so a dry run can be eyeballed.

        Raw JSON is available at -v 2; this is the version you can actually
        scan for something that looks wrong.
        """
        for obj in objects:
            if obj["type"] == "ITEM":
                data = obj["item_data"]
                self.stdout.write(
                    f"  ITEM       {data['name']} "
                    f"({len(data['variations'])} variation(s))"
                )
                for variation in data["variations"]:
                    self._describe_variation(variation, indent=6)
            else:
                self._describe_variation(obj, indent=2)

    def _describe_variation(self, variation, indent):
        data = variation["item_variation_data"]
        price = data["price_money"]["amount"] / 100
        self.stdout.write(
            f"{' ' * indent}VARIATION  {data['name']:<24} "
            f"sku={data.get('sku') or '(none)':<16} ${price:.2f}"
        )

    def _variation(self, fp, item_id):
        """One ITEM_VARIATION payload, however its item was arrived at."""
        return {
            "type": "ITEM_VARIATION",
            "id": fp.square_variation_id or f"#fp_{fp.pk}",
            "item_variation_data": {
                "item_id": item_id,
                "name": fp.variation_name,
                "pricing_type": "FIXED_PRICING",
                "price_money": {
                    "amount": int(fp.price * 100),
                    "currency": "USD",
                },
                "track_inventory": True,
                **({"sku": fp.sku} if fp.sku else {}),
            },
        }

    @staticmethod
    def _item_id_for(fp):
        """The Square item this variation lives under.

        A grouped raw product has no item of its own — the group holds it —
        so reading `raw_product.square_item_id` here would send a blank item
        id and move the variation to nowhere.
        """
        group = fp.raw_product.catalog_group
        return group.square_item_id if group else fp.raw_product.square_item_id

    def _grouped_objects(self, raw_products):
        """Catalog payloads for raw products that share one Square item.

        The group is the item and its members' products are the variations,
        so an unsynced group goes up as a single ITEM carrying all of them,
        and a group Square already knows takes only the variations that are
        new. That mirrors the ungrouped path exactly — the difference is only
        where the item id comes from.
        """
        by_group = {}
        for raw_product in raw_products:
            if raw_product.catalog_group_id:
                by_group.setdefault(raw_product.catalog_group, []).append(raw_product)

        objects = []
        for group, members in by_group.items():
            active_fps = [
                fp
                for raw_product in members
                for fp in raw_product.finished_products.all()
                if fp.is_active
            ]
            if not active_fps:
                continue

            if group.square_item_id:
                objects.extend(
                    self._variation(fp, group.square_item_id)
                    for fp in active_fps
                    if not fp.square_variation_id
                )
                continue

            item_data = {
                "name": group.name,
                "variations": [
                    self._variation(fp, f"#cg_{group.pk}") for fp in active_fps
                ],
            }
            if group.category.square_category_id:
                item_data["category_id"] = group.category.square_category_id
            objects.append({
                "type": "ITEM",
                "id": f"#cg_{group.pk}",
                "item_data": item_data,
            })
        return objects

    def _record_ids(self, raw_products, id_mappings):
        """Write Square's IDs onto our rows.

        Called after *every* successful chunk, not once at the end. A run of
        more than 100 objects is several calls; if a later one fails, the
        earlier ones already created things in Square. Losing those IDs means
        the next run creates them all over again, and the only sign is a
        catalogue with everything in it twice.
        """
        updated_rp = updated_fp = 0

        # Groups first: their variations are recorded by the same loop below,
        # and losing a group's item id is the expensive one — the next run
        # would create a second "Undyed Yarn" item and split the shelf across
        # two of them.
        for group in CatalogGroup.objects.filter(square_item_id=""):
            temp_id = f"#cg_{group.pk}"
            if temp_id in id_mappings:
                group.square_item_id = id_mappings[temp_id]
                group.save(update_fields=["square_item_id"])
                updated_rp += 1

        for raw_product in raw_products:
            temp_id = f"#rp_{raw_product.pk}"
            if temp_id in id_mappings and not raw_product.square_item_id:
                raw_product.square_item_id = id_mappings[temp_id]
                raw_product.save(update_fields=["square_item_id"])
                updated_rp += 1

            for fp in raw_product.finished_products.filter(is_active=True):
                temp_id = f"#fp_{fp.pk}"
                if temp_id in id_mappings and not fp.square_variation_id:
                    fp.square_variation_id = id_mappings[temp_id]
                    fp.save(update_fields=["square_variation_id"])
                    updated_fp += 1
        return updated_rp, updated_fp

    def handle(self, *args, **options):
        from square.client import Client

        self.dry_run = options["dry_run"]

        client = Client(
            access_token=settings.SQUARE_ACCESS_TOKEN,
            environment=settings.SQUARE_ENVIRONMENT,
        )

        self._preflight(client)
        if options["check"]:
            return

        if options["images"]:
            self._push_images(client)
            return

        if options["inventory_only"]:
            self._push_inventory(client)
            return

        if options["update"]:
            self._update_existing(client, force_prices=options["force_prices"])
            # A renamed recipe renames its variation, which is the other way
            # the till's ordering goes wrong.
            self._reorder_variations(client)
            self._push_inventory(client)
            return

        if options["reorder"]:
            self._reorder_variations(client)
            return

        if options["relink"]:
            self._relink(client)
            return

        if options["reattach"]:
            self._reattach_images(client)
            return

        # Relink before deciding what is new. A blank square_variation_id is
        # the *only* thing that marks a variation as needing creating, and it
        # cannot tell "Square never had this" from "Square has it and we lost
        # the id". Creating on the second reads as normal output and appends
        # a duplicate variation under the same SKU — which splits that
        # colourway's stock and its sale history across two rows at the till,
        # silently. Matching on SKU first costs one catalogue read and makes
        # the ambiguity impossible to act on wrongly.
        self._relink(client, announce_only_if_found=True)

        raw_products = (
            RawProduct.objects.filter(is_active=True)
            .select_related("catalog_group")
            .prefetch_related("finished_products__recipe")
            .distinct()
        )

        # Raw products that share a CatalogGroup share one Square item, so
        # they are built together and the rest are built one apiece. See
        # CatalogGroup: for undyed stock the item is the group and the
        # variations are the blanks, which is the blank × colorway axes
        # swapped rather than a special case bolted on.
        grouped = self._grouped_objects(raw_products)

        # Two separate lists:
        # - new_items: RawProducts with no Square ID yet → upsert full ITEM + variations
        # - variation_only: RawProducts with existing Square item → upsert ITEM_VARIATION directly
        new_item_objects = []
        variation_objects = []

        for raw_product in raw_products:
            if raw_product.catalog_group_id:
                continue        # handled in `grouped`

            active_fps = [fp for fp in raw_product.finished_products.all() if fp.is_active]
            if not active_fps:
                continue

            if raw_product.square_item_id:
                # Item exists in Square — add only new variations directly
                variation_objects.extend(
                    self._variation(fp, raw_product.square_item_id)
                    for fp in active_fps
                    if not fp.square_variation_id     # already linked, skip
                )
            else:
                # New item — create full ITEM with all variations
                variations = [
                    self._variation(fp, f"#rp_{raw_product.pk}") for fp in active_fps
                ]
                item_data = {
                    "name": raw_product.name,
                    "variations": variations,
                }
                if raw_product.category.square_category_id:
                    item_data["category_id"] = raw_product.category.square_category_id
                new_item_objects.append({
                    "type": "ITEM",
                    "id": f"#rp_{raw_product.pk}",
                    "item_data": item_data,
                })

        all_objects = new_item_objects + variation_objects + grouped

        if not all_objects:
            self.stdout.write("Nothing new to sync — all items and variations already linked.")
            self._reorder_variations(client)
            self._push_inventory(client)
            return

        self.stdout.write(
            f"Syncing {len(new_item_objects)} new items and {len(variation_objects)} new variations..."
        )

        if self.dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — would create:"))
            self._describe(all_objects)
            if options["verbosity"] >= 2:
                self.stdout.write(json.dumps(all_objects, indent=2, default=str))
            self._reorder_variations(client)
            self._push_inventory(client)
            return

        # Recorded per chunk, and again on the way out of a failure: see
        # _record_ids for why losing a partial result is the expensive case.
        id_mappings = {}
        updated_rp = updated_fp = 0
        try:
            for i in range(0, len(all_objects), 100):
                chunk = all_objects[i:i + 100]
                result = client.catalog.batch_upsert_catalog_objects(body={
                    "idempotency_key": str(uuid.uuid4()),
                    "batches": [{"objects": chunk}],
                })
                if result.is_error():
                    self._fail("Catalog upsert failed", result)
                id_mappings.update({
                    m["client_object_id"]: m["object_id"]
                    for m in result.body.get("id_mappings", [])
                })
                got_rp, got_fp = self._record_ids(raw_products, id_mappings)
                updated_rp += got_rp
                updated_fp += got_fp
        except CommandError:
            if id_mappings:
                self.stderr.write(self.style.WARNING(
                    f"Kept {updated_rp} item and {updated_fp} variation ID(s) "
                    f"from the batches that did succeed — re-run to continue "
                    f"rather than duplicating them."
                ))
            raise

        self.stdout.write(self.style.SUCCESS(
            f"Catalog done: {updated_rp} new items, {updated_fp} new variations created."
        ))

        # A new variation lands at the end of its item's list, so the run that
        # creates one is the run that breaks the order. Fixing it here rather
        # than in a command someone has to remember is the same lesson as SKUs
        # being assigned on save: a step that only runs when recalled doesn't.
        self._reorder_variations(client)

        self._push_inventory(client)

    def _update_existing(self, client, force_prices=False):
        self.stdout.write("Fetching existing variation versions from Square...")

        synced_fps = list(
            FinishedProduct.objects.filter(
                is_active=True,
                square_variation_id__gt="",
            ).select_related("raw_product", "raw_product__catalog_group", "recipe")
        )

        var_ids = [fp.square_variation_id for fp in synced_fps]
        versions = {}
        square_prices = {}
        for i in range(0, len(var_ids), 100):
            result = client.catalog.batch_retrieve_catalog_objects(body={
                "object_ids": var_ids[i:i + 100],
            })
            # Swallowing this used to mean `versions` stayed empty and every
            # variation went back up with "version": None, which Square either
            # rejects or treats as something other than the update intended.
            if result.is_error():
                self._fail("Could not read current variation versions", result)
            for obj in result.body.get("objects", []):
                versions[obj["id"]] = obj["version"]
                # The same read already carries the price, so guarding
                # against an overwrite costs no extra call — which is the
                # whole reason it can be on by default.
                money = (obj.get("item_variation_data") or {}).get("price_money") or {}
                if money.get("amount") is not None:
                    square_prices[obj["id"]] = (
                        Decimal(money["amount"]) / 100
                    ).quantize(Decimal("0.01"))

        objects = []
        stale = []
        diverged = []
        for fp in synced_fps:
            if versions.get(fp.square_variation_id) is None:
                # Square doesn't know this ID any more — deleted or moved
                # accounts. Sending it without a version would create a
                # duplicate rather than update anything.
                stale.append(fp)
                continue

            # A price this app disagrees with was almost certainly typed into
            # the dashboard, because that is the only other place one can be
            # set — and this is the only command that can replace it, with
            # nothing afterwards to say a different number was ever there.
            # So the disagreement stops the row rather than riding through
            # it. `compare_square_prices` is where it gets settled.
            square_price = square_prices.get(fp.square_variation_id)
            if (
                not force_prices
                and square_price is not None
                and square_price != fp.price
            ):
                diverged.append((fp, square_price))
                continue

            variation = self._variation(fp, self._item_id_for(fp))
            variation["id"] = fp.square_variation_id
            variation["version"] = versions.get(fp.square_variation_id)
            objects.append(variation)

        if diverged:
            self.stdout.write(self.style.WARNING(
                f"{len(diverged)} variation(s) have a price in Square that "
                f"disagrees with ours and were left alone:"
            ))
            for fp, square_price in diverged[:10]:
                self.stdout.write(
                    f"  {fp.sku or '(none)':<16} here ${fp.price:.2f} "
                    f"vs Square ${square_price:.2f}"
                )
            if len(diverged) > 10:
                self.stdout.write(f"  ... and {len(diverged) - 10} more")
            self.stdout.write(
                "  Settle them with `compare_square_prices`, or re-run with "
                "--force-prices to overwrite Square."
            )

        if stale:
            self.stdout.write(self.style.WARNING(
                f"{len(stale)} variation(s) have a Square ID that Square "
                f"doesn't recognise and were skipped: "
                f"{', '.join(fp.sku or fp.name for fp in stale[:5])}"
            ))

        self.stdout.write(f"Updating {len(objects)} variations in Square...")

        if self.dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — would update:"))
            self._describe(objects)
            return

        updated = 0
        for i in range(0, len(objects), 100):
            chunk = objects[i:i + 100]
            result = client.catalog.batch_upsert_catalog_objects(body={
                "idempotency_key": str(uuid.uuid4()),
                "batches": [{"objects": chunk}],
            })
            if result.is_error():
                self._fail("Variation update failed", result)
            updated += len(chunk)

        self.stdout.write(self.style.SUCCESS(f"Updated {updated} variations with current prices and SKUs."))

    # Square decides a variation's display order from `ordinal`, and
    # `ordinal` is read-only: on a write it is assigned from each variation's
    # *position* in its parent item's `variations` list. So there is nothing
    # to set — the only way to reorder is to send the item back with the list
    # in the order you want, which is what dragging the handles in the
    # dashboard does. Both paths below therefore upsert whole ITEMs, and that
    # is the most dangerous call in this file: an item upsert replaces its
    # variation list outright, so a variation left out of the list is
    # *deleted*, taking its stock and its Square ID with it.
    #
    # The rule that makes it safe: this pass never builds a variation. It
    # reads the item as Square has it, permutes the list Square returned, and
    # sends that back. Anything it cannot read in full, it leaves alone.

    @staticmethod
    def _variation_sort_key(variation):
        return (variation.get("item_variation_data", {}).get("name") or "").casefold()

    def _items_square_knows(self):
        """Every Square item id this app is responsible for.

        Groups included: for undyed stock the group is the item, and its
        variations are the blanks (see CatalogGroup), which is exactly the
        list a customer scrolls at the till.
        """
        ids = list(
            RawProduct.objects.filter(is_active=True, square_item_id__gt="")
            .values_list("square_item_id", flat=True)
        ) + list(
            CatalogGroup.objects.filter(square_item_id__gt="")
            .values_list("square_item_id", flat=True)
        )
        return list(dict.fromkeys(ids))     # dedupe, keep order

    def _reordered_item(self, obj):
        """`(payload, why)` for an item worth writing, or None to leave it be.

        Returns None both when the order is already right and when the object
        can't be safely rewritten — the caller can't tell those apart and
        doesn't need to, because the action is the same. What it must never
        get is a partial variation list.

        `why` is `resorted` when the names were out of order and `positions`
        when they weren't but Square holds no ordinals to say so. Both need
        the same write; only the second is invisible from the API's own
        answer, so it is worth naming in the output.
        """
        item_data = obj.get("item_data") or {}
        variations = item_data.get("variations") or []

        # An item retrieve inlines the variations, so an empty list here is
        # either an item with none or an answer we didn't understand. Sending
        # it back would delete every variation the item has.
        if len(variations) < 2:
            return None

        # A variation named by an item option carries no `name` of its own and
        # is ordered by the option's values instead. We don't create those,
        # but the dashboard can, and sorting them by an empty string would
        # bunch them at the top and fight whatever set that order.
        if any(not v.get("item_variation_data", {}).get("name") for v in variations):
            return None
        if any(not v.get("id") for v in variations):
            return None

        # Stable, so equal names keep the order Square already has and an
        # already-sorted item is left alone rather than churned every run.
        ordered = sorted(variations, key=self._variation_sort_key)
        same_order = [v["id"] for v in ordered] == [v["id"] for v in variations]

        # ...except when the variations have no ordinal at all, which is the
        # state most of this catalogue was found in. Square only assigns
        # ordinals when a parent item's list is written, and a variation added
        # on its own — the ITEM_VARIATION path, which is how every colourway
        # after the first reached Square — is never part of such a write. The
        # API still hands those back in name order, so the item reads as
        # sorted here while the till, having no positions to read, shows them
        # in the order they were created. That is the reported symptom, and a
        # comparison of names alone would skip exactly the items that have it.
        unpositioned = any(
            v.get("item_variation_data", {}).get("ordinal") is None
            for v in variations
        )
        if same_order and not unpositioned:
            return None

        payload = dict(obj)
        payload.pop("updated_at", None)
        payload["item_data"] = {
            **item_data,
            "variations": [self._strip_ordinal(v) for v in ordered],
        }
        return payload, ("positions" if same_order else "resorted")

    @staticmethod
    def _strip_ordinal(variation):
        """Drop the ordinal Square reported before sending the row back.

        It is read-only and would be ignored, but leaving the *old* number
        beside the new position makes a dry run read as though the payload
        still asks for the order we're trying to change.
        """
        data = {k: v for k, v in variation.items() if k != "updated_at"}
        data["item_variation_data"] = {
            k: v for k, v in (variation.get("item_variation_data") or {}).items()
            if k != "ordinal"
        }
        return data

    def _reorder_variations(self, client):
        """Alphabetise the variations under every item, at the till.

        The POS lists variations in the order the catalogue gives them, and a
        new variation lands at the end — so a colourway added in week three
        sits below the ones added in week one, forever. That is fine in a
        dashboard you scroll at leisure and useless at a stall with a queue:
        the person ringing up is looking for a colourway by name, and an
        unsorted list means reading all of them.
        """
        item_ids = self._items_square_knows()
        if not item_ids:
            return

        objects = {}
        for i in range(0, len(item_ids), 100):
            result = client.catalog.batch_retrieve_catalog_objects(body={
                "object_ids": item_ids[i:i + 100],
            })
            # Same reasoning as the version read in `--update`: a swallowed
            # error here means an empty answer, and an empty answer is
            # indistinguishable from a catalogue that is already in order.
            if result.is_error():
                self._fail("Could not read items to reorder", result)
            for obj in result.body.get("objects", []) or []:
                objects[obj.get("id")] = obj

        missing = [item_id for item_id in item_ids if item_id not in objects]
        if missing:
            self.stdout.write(self.style.WARNING(
                f"{len(missing)} item(s) have a Square ID that Square doesn't "
                f"recognise and were skipped: {', '.join(missing[:5])}"
            ))

        payloads, reasons = [], []
        for item_id in item_ids:
            obj = objects.get(item_id)
            if obj is None:
                continue
            outcome = self._reordered_item(obj)
            if outcome is not None:
                payload, why = outcome
                payloads.append(payload)
                reasons.append(why)

        if not payloads:
            self.stdout.write("Variation order: already alphabetical.")
            return

        if self.dry_run:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN — would reorder {len(payloads)} item(s):"
            ))
            for payload, why in zip(payloads, reasons):
                item_data = payload["item_data"]
                names = ", ".join(
                    v["item_variation_data"]["name"]
                    for v in item_data["variations"]
                )
                note = " (no positions recorded)" if why == "positions" else ""
                self.stdout.write(f"  {item_data.get('name')}{note}: {names}")
            return

        # Chunked by objects rather than by items, because each item carries
        # its variations inline — a hundred items is closer to a thousand
        # objects, and the batch limit counts the children.
        reordered = 0
        for chunk in self._chunk_by_object_count(payloads, 100):
            result = client.catalog.batch_upsert_catalog_objects(body={
                "idempotency_key": str(uuid.uuid4()),
                "batches": [{"objects": chunk}],
            })
            if result.is_error():
                self._fail("Variation reorder failed", result)
            reordered += len(chunk)

        positions = reasons.count("positions")
        detail = (
            f" ({positions} of them already read alphabetically but had no "
            f"positions at the till)"
            if positions else ""
        )
        self.stdout.write(self.style.SUCCESS(
            f"Variation order: {reordered} item(s) alphabetised{detail}."
        ))

    @staticmethod
    def _chunk_by_object_count(payloads, limit):
        chunk, count = [], 0
        for payload in payloads:
            size = 1 + len(payload["item_data"]["variations"])
            if chunk and count + size > limit:
                yield chunk
                chunk, count = [], 0
            chunk.append(payload)
            count += size
        if chunk:
            yield chunk

    def _push_images(self, client):
        """Send product photos to Square and attach them to their variations.

        A photo is of one colorway, so it belongs on the ITEM_VARIATION
        rather than the ITEM — an item here is a style (`Silk Scarf`) and
        every one of them looks completely different depending on the recipe.
        Putting one photo on the item would pick a winner and mislabel every
        other variation under it.

        One request per photo, and slow: there is no batch image endpoint,
        the payload is multipart, and the bucket is private so the bytes go
        Railway -> here -> Square rather than being handed over as a URL that
        Square could fetch itself. That is why this is a mode rather than
        another step in the ordinary sync.
        """
        pending = list(
            FinishedProductImage.objects
            .filter(square_image_id="", finished_product__is_active=True)
            .select_related("finished_product")
            .order_by("finished_product_id", "order", "pk")
        )

        # Two kinds of photo this can't send, counted and named rather than
        # dropped. Both look identical from the Square end — a product with
        # no picture — and neither is a failure worth stopping for.
        unsynced = [i for i in pending if not i.finished_product.square_variation_id]
        external = [i for i in pending if i.finished_product.square_variation_id and not i.image]
        sendable = [
            i for i in pending
            if i.finished_product.square_variation_id and i.image
        ]

        if unsynced:
            self.stdout.write(self.style.WARNING(
                f"{len(unsynced)} photo(s) belong to products Square has "
                f"never seen. Run the plain sync first, then --images: "
                f"{self._name_a_few(unsynced)}"
            ))
        if external:
            self.stdout.write(self.style.WARNING(
                f"{len(external)} photo(s) are external URLs with no file in "
                f"the bucket, and Square's image endpoint only takes bytes: "
                f"{self._name_a_few(external)}"
            ))

        if not sendable:
            self.stdout.write("No new photos to send — everything is already on Square.")
            return

        # Which variations Square already has a picture for. The first photo
        # to land on a variation is its primary, i.e. the one the POS shows;
        # later ones must not quietly displace it on a re-run.
        has_primary = set(
            FinishedProductImage.objects
            .filter(square_image_id__gt="")
            .values_list("finished_product_id", flat=True)
        )

        self.stdout.write(f"Sending {len(sendable)} photo(s) to Square...")

        if self.dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — would upload:"))
            seen = set(has_primary)
            for image in sendable:
                fp = image.finished_product
                primary = fp.pk not in seen
                seen.add(fp.pk)
                self.stdout.write(
                    f"  IMAGE      {image.image.name} -> {fp.sku or fp.name} "
                    f"({fp.square_variation_id})"
                    f"{' [primary]' if primary else ''}"
                )
            return

        sent = 0
        unreadable = []
        try:
            for image in sendable:
                fp = image.finished_product

                # A file missing from the bucket is this photo's problem and
                # nobody else's, so it is collected and the run carries on.
                # Anything broader — bad credentials, the bucket gone — is
                # everybody's problem and bubbles out, which is what the
                # narrow catch buys: S3Storage raises FileNotFoundError only
                # on a 404 and re-raises every other ClientError untouched.
                try:
                    with image.image.open("rb") as fh:
                        payload = fh.read()
                except (OSError, ValueError) as exc:
                    unreadable.append((image, exc))
                    continue

                primary = fp.pk not in has_primary
                square_id = self._upload_image(client, image, payload, primary)

                # Written the moment Square answers, before the next photo is
                # started. The run can die at any point and what got through
                # is already recorded — which is the whole reason the column
                # exists, because Square appends to `image_ids` and cannot
                # tell it is being handed a picture it already holds.
                image.square_image_id = square_id
                image.save(update_fields=["square_image_id"])
                has_primary.add(fp.pk)
                sent += 1
        except CommandError:
            if sent:
                self.stderr.write(self.style.WARNING(
                    f"{sent} photo(s) went up before this and are recorded — "
                    f"re-run to continue rather than uploading them twice."
                ))
            raise

        if unreadable:
            self.stderr.write(self.style.WARNING(
                f"{len(unreadable)} photo(s) could not be read from the "
                f"bucket and were skipped: "
                f"{self._name_a_few([i for i, _ in unreadable])}"
            ))

        self.stdout.write(self.style.SUCCESS(
            f"Images done: {sent} photo(s) uploaded and attached."
        ))

    def _upload_image(self, client, image, payload, primary):
        """One multipart upload. Returns the ID Square assigned."""
        fp = image.finished_product
        request = {
            "idempotency_key": str(uuid.uuid4()),
            # Attaching here rather than in a follow-up variation upsert:
            # `object_id` makes Square do the attach itself, so there is no
            # window where an uploaded image exists and nothing points at it.
            "object_id": fp.square_variation_id,
            "is_primary": primary,
            "image": {
                "type": "IMAGE",
                "id": f"#img_{image.pk}",
                "image_data": {
                    "caption": image.alt_text or fp.name,
                },
            },
        }

        upload = io.BytesIO(payload)
        # The SDK sends this as the multipart filename; without one some
        # servers reject the part outright.
        upload.name = image.image.name.rsplit("/", 1)[-1] or "photo.jpg"

        result = client.catalog.create_catalog_image(
            request=request, image_file=upload
        )
        if result.is_error():
            self._fail(f"Image upload failed for {fp.sku or fp.name}", result)

        square_id = ((result.body or {}).get("image") or {}).get("id")
        if not square_id:
            # Square took the photo and we have no ID to record. Carrying on
            # would upload it again next run and stack a duplicate, so this
            # stops — the one case where a success is worse than an error.
            raise CommandError(
                f"Square accepted the photo for {fp.sku or fp.name} but "
                f"returned no image ID. Nothing was recorded, so re-running "
                f"would attach it twice — check the catalogue before you do."
            )
        return square_id

    @staticmethod
    def _name_a_few(images, limit=5):
        """`sku, sku, sku (and 12 more)` — enough to go and look."""
        names = [i.finished_product.sku or i.finished_product.name for i in images]
        shown = ", ".join(names[:limit])
        rest = len(names) - limit
        return f"{shown} (and {rest} more)" if rest > 0 else shown

    def _square_variations_by_sku(self, client):
        """Every variation under our items, indexed by SKU.

        SKU is what makes this safe. It is write-once on our side —
        `FinishedProduct.save()` only ever fills a blank one, and
        `generate_skus --overwrite` has to be asked twice — and it is what
        Square stores to identify the variation. So an exact match is the
        same product, not a guess about names that a recipe rename would
        break.
        """
        ids = self._items_square_knows()
        by_sku = {}
        for i in range(0, len(ids), 100):
            result = client.catalog.batch_retrieve_catalog_objects(body={
                "object_ids": ids[i:i + 100],
            })
            if result.is_error():
                self._fail("Could not read the catalogue to match SKUs", result)
            for obj in result.body.get("objects", []) or []:
                for variation in (obj.get("item_data") or {}).get("variations", []) or []:
                    sku = (variation.get("item_variation_data") or {}).get("sku")
                    if sku and sku not in by_sku:
                        by_sku[sku] = variation["id"]
        return by_sku

    def _relink(self, client, announce_only_if_found=False):
        """Fill blank variation IDs from the ones Square already holds.

        Nothing is created and nothing is sent — this only ever writes a
        Square ID onto a local row that had none. A row that Square genuinely
        does not know is left blank, which is what the ordinary sync path is
        for.
        """
        blanks = list(
            FinishedProduct.objects
            .filter(is_active=True, square_variation_id="")
            .exclude(sku="")
        )
        if not blanks:
            if not announce_only_if_found:
                self.stdout.write("Nothing to relink — every active product has a Square ID.")
            return 0

        by_sku = self._square_variations_by_sku(client)
        matched = [(fp, by_sku[fp.sku]) for fp in blanks if fp.sku in by_sku]
        missing = [fp for fp in blanks if fp.sku not in by_sku]

        if not matched:
            if not announce_only_if_found:
                self.stdout.write(
                    f"No matches — {len(blanks)} product(s) with no Square ID "
                    f"are genuinely absent from the catalogue."
                )
            return 0

        if self.dry_run:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN — would relink {len(matched)} product(s):"
            ))
            for fp, vid in matched:
                self.stdout.write(f"  {fp.sku:<16} -> {vid}")
            return 0

        for fp, vid in matched:
            fp.square_variation_id = vid
            fp.save(update_fields=["square_variation_id"])

        self.stdout.write(self.style.SUCCESS(
            f"Relinked {len(matched)} product(s) to variations Square already "
            f"had — these would otherwise have been created a second time."
        ))
        for fp, vid in matched:
            self.stdout.write(f"  {fp.sku:<16} -> {vid}")
        if missing:
            self.stdout.write(
                f"{len(missing)} product(s) are genuinely new to Square: "
                f"{', '.join(fp.sku for fp in missing[:5])}"
            )
        return len(matched)

    def _reattach_images(self, client):
        """Re-assert attachment for photos Square already holds.

        `--images` skips anything carrying a `square_image_id`, which is
        right — that column is what stops the same photo being stacked on a
        variation over and over, because Square's image endpoint appends and
        has nothing that says "you already sent me this". But it also means a
        photo that reached the library and never landed on its variation can
        never be repaired by re-running `--images`: the record says sent, and
        sent is the whole of what the column knows.

        Re-posting the bytes is safe, and that is the part worth stating
        plainly because it looks reckless: **Square deduplicates on image
        content.** An upload it already holds comes back as the existing
        image object — same id, same created_at, same caption — and adds
        nothing to the library. What the second post does do is carry
        `object_id` again, and that is what performs the attach.

        Note you cannot check this from the API. At the pinned version
        `image_ids` is not returned on an ITEM or an ITEM_VARIATION at all,
        so a variation with a photo and one without read identically. The
        dashboard is the only oracle.
        """
        images = list(
            FinishedProductImage.objects
            .filter(finished_product__is_active=True)
            .exclude(square_image_id="")
            .select_related("finished_product")
            .order_by("finished_product_id", "order", "pk")
        )
        sendable = [i for i in images if i.finished_product.square_variation_id and i.image]
        skipped = [i for i in images if i not in sendable]

        if not sendable:
            self.stdout.write("Nothing to re-attach.")
            return

        if self.dry_run:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN — would re-attach {len(sendable)} photo(s):"
            ))
            seen = set()
            for image in sendable:
                fp = image.finished_product
                primary = fp.pk not in seen
                seen.add(fp.pk)
                self.stdout.write(
                    f"  {fp.sku or fp.name:<16} {image.square_image_id}"
                    f"{' [primary]' if primary else ''}"
                )
            return

        self.stdout.write(f"Re-attaching {len(sendable)} photo(s)...")

        # First photo per product is its primary, the same rule `--images`
        # applies on the way in — so a repair cannot quietly promote a
        # different picture to the one the POS shows.
        seen, done, failed = set(), 0, []
        for image in sendable:
            fp = image.finished_product
            try:
                with image.image.open("rb") as fh:
                    payload = fh.read()
            except (OSError, ValueError) as exc:
                failed.append((fp.sku or fp.name, str(exc)[:60]))
                continue

            primary = fp.pk not in seen
            square_id = self._upload_image(client, image, payload, primary)
            if square_id != image.square_image_id:
                # Square handed back a different object, so the dedupe did
                # not fire and this really is a new image. Record it or the
                # next run stacks another.
                image.square_image_id = square_id
                image.save(update_fields=["square_image_id"])
            seen.add(fp.pk)
            done += 1

        self.stdout.write(self.style.SUCCESS(f"Re-attached {done} photo(s)."))
        if skipped:
            self.stdout.write(self.style.WARNING(
                f"{len(skipped)} skipped (no variation in Square, or no file "
                f"in the bucket): {self._name_a_few(skipped)}"
            ))
        for sku, why in failed:
            self.stderr.write(self.style.WARNING(f"  could not read {sku}: {why}"))

    def _push_inventory(self, client):
        self.stdout.write("Pushing inventory counts to Square...")

        synced_fps = FinishedProduct.objects.filter(
            is_active=True,
            square_variation_id__gt="",
        )

        occurred_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        changes = [
            {
                "type": "PHYSICAL_COUNT",
                "physical_count": {
                    "catalog_object_id": fp.square_variation_id,
                    "location_id": settings.SQUARE_LOCATION_ID,
                    "state": "IN_STOCK",
                    "quantity": str(fp.number_on_hand),
                    "occurred_at": occurred_at,
                },
            }
            for fp in synced_fps
        ]

        if not changes:
            self.stdout.write("No variations with Square IDs found for inventory push.")
            return

        if self.dry_run:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN — would set {len(changes)} stock count(s) at "
                f"{settings.SQUARE_LOCATION_ID}:"
            ))
            for change in changes:
                count = change["physical_count"]
                self.stdout.write(
                    f"  {count['catalog_object_id']} -> {count['quantity']}"
                )
            return

        total_pushed = 0
        for i in range(0, len(changes), 100):
            chunk = changes[i:i + 100]
            inv_result = client.inventory.batch_change_inventory(body={
                "idempotency_key": str(uuid.uuid4()),
                "changes": chunk,
            })
            if inv_result.is_error():
                self._fail("Inventory push failed", inv_result)
            total_pushed += len(chunk)

        self.stdout.write(self.style.SUCCESS(
            f"Inventory done: {total_pushed} counts pushed to Square."
        ))
