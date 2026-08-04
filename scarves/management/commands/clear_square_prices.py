import uuid

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Remove fixed prices from all Square catalog variations (sets to variable pricing)."

    def handle(self, *args, **options):
        from square.client import Client

        client = Client(
            access_token=settings.SQUARE_ACCESS_TOKEN,
            environment=settings.SQUARE_ENVIRONMENT,
        )

        # Fetch all items
        items = []
        cursor = None
        while True:
            result = client.catalog.list_catalog(cursor=cursor, types="ITEM")
            if result.is_error():
                for e in result.errors:
                    self.stderr.write(self.style.ERROR(f"{e['category']}: {e['detail']}"))
                return
            items.extend(result.body.get("objects", []))
            cursor = result.body.get("cursor")
            if not cursor:
                break

        variations = []
        for item in items:
            for v in item["item_data"].get("variations", []):
                v["item_variation_data"]["pricing_type"] = "VARIABLE_PRICING"
                v["item_variation_data"].pop("price_money", None)
                variations.append(v)

        if not variations:
            self.stdout.write("No variations found.")
            return

        self.stdout.write(f"Clearing prices on {len(variations)} variations...")

        updated = 0
        for i in range(0, len(variations), 100):
            chunk = variations[i:i + 100]
            result = client.catalog.batch_upsert_catalog_objects(body={
                "idempotency_key": str(uuid.uuid4()),
                "batches": [{"objects": chunk}],
            })
            if result.is_error():
                for e in result.errors:
                    self.stderr.write(self.style.ERROR(f"{e['category']}: {e['detail']}"))
                return
            updated += len(chunk)

        self.stdout.write(self.style.SUCCESS(f"Done. {updated} variations set to variable pricing."))
