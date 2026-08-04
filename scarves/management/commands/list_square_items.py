from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "List all Square catalog items and their IDs."

    def handle(self, *args, **options):
        from square.client import Client

        client = Client(
            access_token=settings.SQUARE_ACCESS_TOKEN,
            environment=settings.SQUARE_ENVIRONMENT,
        )

        cursor = None
        items = []

        while True:
            result = client.catalog.list_catalog(
                cursor=cursor,
                types="ITEM",
            )
            if result.is_error():
                for error in result.errors:
                    self.stderr.write(self.style.ERROR(f"{error['category']}: {error['detail']}"))
                return

            items.extend(result.body.get("objects", []))
            cursor = result.body.get("cursor")
            if not cursor:
                break

        if not items:
            self.stdout.write("No catalog items found.")
            return

        self.stdout.write(f"\n{'Item Name':<40} {'Square Item ID'}")
        self.stdout.write("-" * 80)

        for item in sorted(items, key=lambda x: x["item_data"]["name"]):
            name = item["item_data"]["name"]
            item_id = item["id"]
            self.stdout.write(f"{name:<40} {item_id}")

            for variation in item["item_data"].get("variations", []):
                var_name = variation["item_variation_data"]["name"]
                var_id = variation["id"]
                self.stdout.write(f"  {'└─ ' + var_name:<38} {var_id}")

        self.stdout.write(f"\n{len(items)} items total.")
