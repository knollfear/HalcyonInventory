import uuid
from datetime import datetime, timezone

from django.conf import settings
from django.core.management.base import BaseCommand

from scarves.models import RawProduct, FinishedProduct


class Command(BaseCommand):
    help = "Sync active finished products to Square as catalog items/variations, then push inventory counts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--inventory-only",
            action="store_true",
            help="Skip catalog upsert and only push inventory counts.",
        )
        parser.add_argument(
            "--update",
            action="store_true",
            help="Push updated prices and SKUs to existing Square variations, then sync inventory.",
        )

    def handle(self, *args, **options):
        from square.client import Client

        client = Client(
            access_token=settings.SQUARE_ACCESS_TOKEN,
            environment=settings.SQUARE_ENVIRONMENT,
        )

        if options["inventory_only"]:
            self._push_inventory(client)
            return

        if options["update"]:
            self._update_existing(client)
            self._push_inventory(client)
            return

        raw_products = (
            RawProduct.objects.filter(is_active=True)
            .prefetch_related("finished_products__recipe")
            .distinct()
        )

        # Two separate lists:
        # - new_items: RawProducts with no Square ID yet → upsert full ITEM + variations
        # - variation_only: RawProducts with existing Square item → upsert ITEM_VARIATION directly
        new_item_objects = []
        variation_objects = []

        for raw_product in raw_products:
            active_fps = [fp for fp in raw_product.finished_products.all() if fp.is_active]
            if not active_fps:
                continue

            if raw_product.square_item_id:
                # Item exists in Square — add only new variations directly
                for fp in active_fps:
                    if fp.square_variation_id:
                        continue  # already linked, skip
                    variation_objects.append({
                        "type": "ITEM_VARIATION",
                        "id": f"#fp_{fp.pk}",
                        "item_variation_data": {
                            "item_id": raw_product.square_item_id,
                            "name": fp.recipe.name,
                            "pricing_type": "FIXED_PRICING",
                            "price_money": {
                                "amount": int(fp.price * 100),
                                "currency": "USD",
                            },
                            "track_inventory": True,
                            **({"sku": fp.sku} if fp.sku else {}),
                        },
                    })
            else:
                # New item — create full ITEM with all variations
                variations = []
                for fp in active_fps:
                    variations.append({
                        "type": "ITEM_VARIATION",
                        "id": fp.square_variation_id or f"#fp_{fp.pk}",
                        "item_variation_data": {
                            "item_id": f"#rp_{raw_product.pk}",
                            "name": fp.recipe.name,
                            "pricing_type": "FIXED_PRICING",
                            "price_money": {
                                "amount": int(fp.price * 100),
                                "currency": "USD",
                            },
                            "track_inventory": True,
                            **({"sku": fp.sku} if fp.sku else {}),
                        },
                    })
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

        all_objects = new_item_objects + variation_objects

        if not all_objects:
            self.stdout.write("Nothing new to sync — all items and variations already linked.")
            self._push_inventory(client)
            return

        self.stdout.write(
            f"Syncing {len(new_item_objects)} new items and {len(variation_objects)} new variations..."
        )

        id_mappings = {}
        for i in range(0, len(all_objects), 100):
            chunk = all_objects[i:i + 100]
            result = client.catalog.batch_upsert_catalog_objects(body={
                "idempotency_key": str(uuid.uuid4()),
                "batches": [{"objects": chunk}],
            })
            if result.is_error():
                for error in result.errors:
                    self.stderr.write(self.style.ERROR(f"{error['category']}: {error['detail']}"))
                return
            id_mappings.update({
                m["client_object_id"]: m["object_id"]
                for m in result.body.get("id_mappings", [])
            })

        updated_rp = updated_fp = 0
        for raw_product in raw_products:
            temp_id = f"#rp_{raw_product.pk}"
            if temp_id in id_mappings:
                raw_product.square_item_id = id_mappings[temp_id]
                raw_product.save(update_fields=["square_item_id"])
                updated_rp += 1

            for fp in raw_product.finished_products.filter(is_active=True):
                temp_id = f"#fp_{fp.pk}"
                if temp_id in id_mappings:
                    fp.square_variation_id = id_mappings[temp_id]
                    fp.save(update_fields=["square_variation_id"])
                    updated_fp += 1

        self.stdout.write(self.style.SUCCESS(
            f"Catalog done: {updated_rp} new items, {updated_fp} new variations created."
        ))

        self._push_inventory(client)

    def _update_existing(self, client):
        self.stdout.write("Fetching existing variation versions from Square...")

        synced_fps = list(
            FinishedProduct.objects.filter(
                is_active=True,
                square_variation_id__gt="",
            ).select_related("raw_product", "recipe")
        )

        var_ids = [fp.square_variation_id for fp in synced_fps]
        versions = {}
        for i in range(0, len(var_ids), 100):
            result = client.catalog.batch_retrieve_catalog_objects(body={
                "object_ids": var_ids[i:i + 100],
            })
            if result.is_success():
                for obj in result.body.get("objects", []):
                    versions[obj["id"]] = obj["version"]

        objects = []
        for fp in synced_fps:
            variation = {
                "type": "ITEM_VARIATION",
                "id": fp.square_variation_id,
                "version": versions.get(fp.square_variation_id),
                "item_variation_data": {
                    "item_id": fp.raw_product.square_item_id,
                    "name": fp.recipe.name,
                    "pricing_type": "FIXED_PRICING",
                    "price_money": {
                        "amount": int(fp.price * 100),
                        "currency": "USD",
                    },
                    "track_inventory": True,
                    **({"sku": fp.sku} if fp.sku else {}),
                },
            }
            objects.append(variation)

        self.stdout.write(f"Updating {len(objects)} variations in Square...")
        updated = 0
        for i in range(0, len(objects), 100):
            chunk = objects[i:i + 100]
            result = client.catalog.batch_upsert_catalog_objects(body={
                "idempotency_key": str(uuid.uuid4()),
                "batches": [{"objects": chunk}],
            })
            if result.is_error():
                for error in result.errors:
                    self.stderr.write(self.style.ERROR(f"{error['category']}: {error['detail']}"))
                return
            updated += len(chunk)

        self.stdout.write(self.style.SUCCESS(f"Updated {updated} variations with current prices and SKUs."))

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

        total_pushed = 0
        for i in range(0, len(changes), 100):
            chunk = changes[i:i + 100]
            inv_result = client.inventory.batch_change_inventory(body={
                "idempotency_key": str(uuid.uuid4()),
                "changes": chunk,
            })
            if inv_result.is_error():
                for error in inv_result.errors:
                    self.stderr.write(self.style.ERROR(f"{error['category']}: {error['detail']}"))
                return
            total_pushed += len(chunk)

        self.stdout.write(self.style.SUCCESS(
            f"Inventory done: {total_pushed} counts pushed to Square."
        ))
